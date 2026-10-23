"""审批绑定（approval binding）：把“这一次确认”钉死在具体执行内容上。

每个待审批操作都会生成一份不可复用的审批记录，包含：

- ``approval_id`` / 一次性 ``token`` / ``thread_id`` / ``request_id``
- ``request_digest``（原始用户请求）与 ``authorization_scope_digest``（授权范围）
- ``selected_item_digest``（选中的文件集合）与 ``mutation_count``（累计变更数）
- ``target_paths``（最终落点）与 ``expires_at``（过期时间）

恢复执行前会重新校验源文件（存在性、大小、mtime）、目标冲突、选择集合与目标路径。
审批后扩大选择、扩大目标范围、改变操作类型、换请求或换授权范围都会得到
``code=selection_changed / targets_changed / operation_changed / request_changed /
scope_changed / stale_source``，调用方必须返回 409 并拒绝执行，而不是“尽力而为”。
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional

from ..logging_conf import get_logger
from .authorization import AuthorizationScope, request_digest as _request_digest

logger = get_logger(__name__)

DEFAULT_APPROVAL_TTL_SECONDS = 900
MAX_SNAPSHOT_ITEMS = 2000

# 会改变文件位置/名字的工具：它们的累计审批必须绑定整条计划的执行集合。
_CUMULATIVE_TOOL_NAMES = (
    "move_file", "batch_move", "rename_file", "batch_rename", "batch_classify",
)


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def item_digest(paths: Iterable[str]) -> str:
    """选中条目指纹：对规范化后的路径集合排序再哈希（与顺序无关）。"""
    return digest(sorted(str(item) for item in paths))


def arg_digest(tool: str, args: Mapping[str, Any]) -> str:
    return digest({"tool": tool, "args": dict(args or {})})


def scope_digest(scope: Optional[AuthorizationScope]) -> str:
    return scope.digest() if scope is not None else ""


def approval_signature(
    tool: str,
    args: Mapping[str, Any],
    *,
    scope: Optional[AuthorizationScope],
    request_digest: str,
) -> str:
    """用于“拒绝后不再重复弹同一审批”的稳定指纹。"""
    return digest(
        {
            "tool": tool,
            "args_digest": arg_digest(tool, args),
            "scope": scope_digest(scope),
            "request": request_digest,
        }
    )


@dataclass
class ApprovalCheck:
    ok: bool
    code: str = ""
    message: str = ""
    detail: Dict[str, Any] = field(default_factory=dict)

    @property
    def status_code(self) -> int:
        return 200 if self.ok else 409


def _stat(path: str) -> Dict[str, Any]:
    try:
        stat = Path(path).stat()
        return {
            "path": str(path),
            "exists": True,
            "size": int(stat.st_size),
            "mtime_ns": int(stat.st_mtime_ns),
        }
    except OSError:
        return {"path": str(path), "exists": False, "size": -1, "mtime_ns": -1}


def _batch_items(args: Mapping[str, Any]) -> List[Dict[str, Any]]:
    from ..tools import manifests

    scan_id = str(args.get("scan_id") or "")
    if not scan_id:
        return []
    try:
        matched = manifests.match(scan_id, dict(args.get("filter") or {}))
    except Exception:  # noqa: BLE001 - 清单失效由上游单独拒绝
        return []
    return matched


def snapshot_items(
    tool: str, args: Mapping[str, Any], extra_paths: Iterable[str] = ()
) -> List[Dict[str, Any]]:
    """记录审批时刻的源文件状态（只读 stat），执行前据此重检。

    ``extra_paths`` 用于累计审批：审批弹窗只描述“当前这一步”，但获批的是整条
    计划里的全部文件变更，因此必须把这些文件也快照进来，否则审批期间修改第 2 个
    文件不会被发现。快照只读，不产生任何副作用。
    """
    paths: List[str] = []
    if tool in {"move_file", "rename_file"}:
        paths = [str(args.get("src") or "")]
    elif tool == "delete_file":
        paths = [str(args.get("path") or "")]
    elif tool == "write_summary":
        paths = [str(args.get("file_path") or "")]
    elif tool in {"batch_move", "batch_rename", "batch_classify"}:
        paths = [str(item.get("path") or "") for item in _batch_items(args)]
    for item in extra_paths or ():
        paths.append(str(item or ""))
    # 去重后按首次出现顺序保留：同一文件既可能是当前步骤，也可能来自覆盖集合。
    unique: List[str] = []
    seen: set[str] = set()
    for path in paths:
        if not path:
            continue
        key = str(Path(path))
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)
    return [_stat(path) for path in unique][:MAX_SNAPSHOT_ITEMS]


def _step_sources(step: Mapping[str, Any]) -> List[str]:
    tool = str(step.get("tool") or "")
    args = dict(step.get("args") or {})
    if tool in {"move_file", "rename_file"}:
        return [str(args.get("src") or "")]
    if tool in {"batch_move", "batch_rename", "batch_classify"}:
        return [str(item.get("path") or "") for item in _batch_items(args)]
    return []


def execution_sources(steps: Iterable[Mapping[str, Any]]) -> List[str]:
    """整条剩余计划里会被移动/重命名的源文件集合（去重、保序）。"""
    paths: List[str] = []
    for step in steps or []:
        for path in _step_sources(step):
            if path and path not in paths:
                paths.append(path)
    return paths


def execution_targets(steps: Iterable[Mapping[str, Any]]) -> List[str]:
    """整条剩余计划里的目标落点集合（去重、保序）。"""
    targets: List[str] = []
    for step in steps or []:
        tool = str(step.get("tool") or "")
        if tool not in _CUMULATIVE_TOOL_NAMES:
            continue
        for target in target_paths_for(tool, dict(step.get("args") or {})):
            if target and target not in targets:
                targets.append(target)
    return targets


def execution_tools(steps: Iterable[Mapping[str, Any]]) -> List[str]:
    """整条剩余计划里会被执行的变更工具清单。"""
    names: List[str] = []
    for step in steps or []:
        tool = str(step.get("tool") or "")
        if tool in _CUMULATIVE_TOOL_NAMES and tool not in names:
            names.append(tool)
    return names


def target_paths_for(tool: str, args: Mapping[str, Any]) -> List[str]:
    """记录审批时刻的“最终落点”集合（防止审批后扩大目标范围）。"""
    if tool == "move_file":
        dest = str(args.get("dest_dir") or "")
        new_name = str(args.get("new_name") or "")
        if new_name:
            return [str(Path(dest) / new_name)]
        return [dest] if dest else []
    if tool == "rename_file":
        src = Path(str(args.get("src") or ""))
        name = str(args.get("new_name") or "")
        return [str(src.with_name(name))] if name else []
    if tool == "batch_move":
        return [str(args.get("dest_dir") or "")]
    if tool == "batch_classify":
        return [str(args.get("dest_root") or "")]
    if tool == "write_summary":
        return [str(args.get("output_dir") or "")]
    if tool == "make_dir":
        return [str(args.get("path") or "")]
    return []


def build_pending_approval(
    *,
    approval_id: str,
    tool: str,
    args: Mapping[str, Any],
    scope: Optional[AuthorizationScope],
    request: str,
    state: Mapping[str, Any],
    action: str,
    target: str,
    detail: str,
    count: int,
    cumulative_before: int,
    batch: bool = False,
    coverage: Optional[Mapping[str, Any]] = None,
    ttl_seconds: int = DEFAULT_APPROVAL_TTL_SECONDS,
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """构造待审批记录。``approval_id`` 同时充当一次性确认 token。

    ``coverage`` 是“这次审批实际覆盖的整条执行集合”（源文件 / 目标落点 / 操作类型）。
    累计审批弹窗只展示当前这一步，但获批的是整条计划，因此执行集合必须一起绑定：
    快照覆盖全部源文件，恢复执行前再逐项比对，审批期间任何一个文件被改动都会让
    审批失效，而不是“只看第 1 个文件”。
    """
    moment = time.time() if now is None else now
    coverage_payload: Dict[str, Any] = {
        "sources": [str(item) for item in (coverage or {}).get("sources") or []],
        "targets": [str(item) for item in (coverage or {}).get("targets") or []],
        "tools": [str(item) for item in (coverage or {}).get("tools") or []],
    }
    items = snapshot_items(tool, args, extra_paths=coverage_payload["sources"])
    selected_paths = [str(item["path"]) for item in items]
    return {
        "approval_id": approval_id,
        "token": approval_id,
        "action": action,
        "target": target,
        "detail": str(detail or "")[:2000],
        "tool": tool,
        "args": dict(args),
        "args_digest": arg_digest(tool, args),
        "count": int(count),
        "batch": bool(batch),
        "thread_id": str(state.get("thread_id") or ""),
        "request_id": str(state.get("request_id") or ""),
        "request_digest": str(
            state.get("request_digest")
            or _request_digest(
                str(state.get("thread_id") or ""),
                str(state.get("request_id") or ""),
                request,
            )
        ),
        "scope_digest": scope_digest(scope),
        "selected_items": items,
        "selected_paths": selected_paths,
        "selected_item_digest": item_digest(selected_paths),
        "target_paths": target_paths_for(tool, args),
        "coverage": coverage_payload,
        "mutation_count": int(cumulative_before) + int(count),
        "mutation_before": int(cumulative_before),
        "approved_limit": int(cumulative_before) + int(count),
        "destructive": bool(scope.destructive) if scope is not None else False,
        "expires_at": float(moment + max(1, int(ttl_seconds))),
        "created_at": moment,
    }


def _expired(pending: Mapping[str, Any], now: float) -> bool:
    try:
        return now > float(pending.get("expires_at") or 0)
    except (TypeError, ValueError):
        return True


def _changed_items(items: Iterable[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """源文件/大小/mtime 在执行前发生了变化。"""
    changed: List[Dict[str, Any]] = []
    for item in items or []:
        path = str(item.get("path") or "")
        current = _stat(path)
        if not current["exists"]:
            changed.append({"path": path, "reason": "源文件已不存在"})
            continue
        if int(current["size"]) != int(item.get("size", -1)) or int(
            current["mtime_ns"]
        ) != int(item.get("mtime_ns", -1)):
            changed.append({"path": path, "reason": "源文件在审批后发生变化"})
    return changed


def _target_conflicts(pending: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """目标位置在审批后被占用（不会覆盖、不会自动改名）。"""
    tool = str(pending.get("tool") or "")
    args = dict(pending.get("args") or {})
    conflicts: List[Dict[str, Any]] = []
    if tool == "move_file":
        dest = Path(str(args.get("dest_dir") or ""))
        source = Path(str(args.get("src") or ""))
        new_name = str(args.get("new_name") or "")
        target = dest / (new_name or source.name)
        if target.exists() and target.resolve() != source.resolve():
            conflicts.append({"target": str(target), "reason": "目标位置已被占用"})
    elif tool == "rename_file":
        source = Path(str(args.get("src") or ""))
        target = source.with_name(str(args.get("new_name") or ""))
        if target.exists() and target.resolve() != source.resolve():
            conflicts.append({"target": str(target), "reason": "目标位置已被占用"})
    elif tool == "write_summary":
        output = Path(str(args.get("output_dir") or ""))
        if not output.is_dir():
            conflicts.append({"target": str(output), "reason": "输出目录不存在"})
    elif tool == "batch_move":
        dest = Path(str(args.get("dest_dir") or ""))
        for item in pending.get("selected_items") or []:
            target = dest / Path(str(item.get("path") or "")).name
            if target.exists() and target.resolve() != Path(str(item.get("path"))).resolve():
                conflicts.append({"target": str(target), "reason": "目标位置已被占用"})
    return conflicts


def _selection(current_paths: List[str], approved_paths: List[str]) -> tuple[bool, str, Dict[str, Any]]:
    approved = set(approved_paths)
    current = set(current_paths)
    if current == approved:
        return True, "", {}
    added = sorted(current - approved)
    removed = sorted(approved - current)
    detail = {"added": added[:50], "removed": removed[:50]}
    if added:
        return False, "审批后选择集合扩大，原审批不再有效，请重新确认", detail
    return False, "审批后选择集合发生变化，原审批不再有效，请重新确认", detail


def verify_pending_approval(
    pending: Optional[Mapping[str, Any]],
    *,
    state: Mapping[str, Any],
    confirm_token: str = "",
    now: Optional[float] = None,
) -> ApprovalCheck:
    """恢复执行前的完整校验；任何不一致都必须拒绝执行。"""
    if not pending:
        return ApprovalCheck(False, "no_pending", "该会话没有待审批操作")
    moment = time.time() if now is None else now
    approved_id = str(pending.get("approval_id") or "")
    token = str(pending.get("token") or approved_id)
    provided = str(confirm_token or "").strip()
    if not approved_id:
        return ApprovalCheck(False, "invalid_approval", "审批记录缺少 approval_id")
    if provided and provided not in {approved_id, token}:
        return ApprovalCheck(False, "token_mismatch", "审批 token 不匹配，请重新确认")
    if _expired(pending, moment):
        return ApprovalCheck(False, "approval_expired", "审批已过期，请重新发起操作")
    consumed = {str(item) for item in (state.get("consumed_approvals") or [])}
    if approved_id in consumed:
        return ApprovalCheck(False, "approval_used", "该审批已使用过，不能重复执行")

    if str(pending.get("thread_id") or "") and str(pending.get("thread_id")) != str(
        state.get("thread_id") or ""
    ):
        return ApprovalCheck(False, "thread_changed", "审批属于其他会话，拒绝执行")
    if str(pending.get("request_digest") or "") != str(
        state.get("request_digest") or ""
    ):
        return ApprovalCheck(False, "request_changed", "审批属于其他请求，拒绝执行")
    scope = AuthorizationScope.from_dict(state.get("authorization_scope"))
    if scope is None:
        return ApprovalCheck(False, "missing_scope", "缺少授权范围，拒绝执行")
    if str(pending.get("scope_digest") or "") != scope.digest():
        return ApprovalCheck(False, "scope_changed", "授权范围已变化，请重新确认")
    if scope.expired(now=moment):
        return ApprovalCheck(False, "scope_expired", "授权范围已过期，请重新发起请求")

    pending_args = pending.get("args") or {}
    if str(pending.get("args_digest") or "") != arg_digest(
        str(pending.get("tool") or ""), pending_args
    ):
        return ApprovalCheck(False, "approval_tampered", "审批记录与执行参数不一致")
    current_step = state.get("current_step") or {}
    if current_step:
        if str(current_step.get("tool") or "") != str(pending.get("tool") or ""):
            return ApprovalCheck(
                False, "operation_changed", "操作类型在审批后发生变化，拒绝执行"
            )
        if arg_digest(
            str(current_step.get("tool") or ""), dict(current_step.get("args") or {})
        ) != str(pending.get("args_digest") or ""):
            return ApprovalCheck(
                False, "operation_changed", "执行参数在审批后发生变化，拒绝执行"
            )

    tool = str(pending.get("tool") or "")
    approved_paths = [str(item) for item in (pending.get("selected_paths") or [])]
    if str(pending.get("selected_item_digest") or "") != item_digest(approved_paths):
        return ApprovalCheck(
            False, "selection_changed", "审批记录的选中条目指纹不一致，拒绝执行"
        )
    # 累计审批绑定的是“整条计划的执行集合”。恢复执行前必须重新算出同一个集合：
    # 源文件、目标落点、操作类型任何一项对不上，都说明获批的内容已经不是要执行的内容。
    coverage = dict(pending.get("coverage") or {})
    if coverage.get("sources") or coverage.get("tools"):
        covered_sources = [str(item) for item in coverage.get("sources") or []]
        plan = list(state.get("plan") or [])
        index = int(state.get("step_index") or 0)
        remaining = plan[index:] if plan else []
        current_sources = execution_sources(remaining)
        if sorted(current_sources) != sorted(covered_sources):
            return ApprovalCheck(
                False,
                "selection_changed",
                "审批覆盖的文件集合在审批后发生变化，原审批不再有效，请重新确认",
                {
                    "added": sorted(set(current_sources) - set(covered_sources))[:50],
                    "removed": sorted(set(covered_sources) - set(current_sources))[:50],
                },
            )
        current_targets = execution_targets(remaining)
        if sorted(current_targets) != sorted(
            str(item) for item in coverage.get("targets") or []
        ):
            return ApprovalCheck(
                False, "targets_changed", "审批覆盖的目标范围在审批后发生变化，拒绝执行"
            )
        current_tools = execution_tools(remaining)
        if sorted(current_tools) != sorted(
            str(item) for item in coverage.get("tools") or []
        ):
            return ApprovalCheck(
                False, "operation_changed", "审批覆盖的操作类型在审批后发生变化，拒绝执行"
            )
    if tool in {"batch_move", "batch_rename", "batch_classify"}:
        current_paths = [
            str(item.get("path") or "") for item in _batch_items(pending_args)
        ]
        ok, message, detail = _selection(
            current_paths, [str(item) for item in (pending.get("selected_paths") or [])]
        )
        if not ok:
            return ApprovalCheck(False, "selection_changed", message, detail)
        recomputed_targets = target_paths_for(tool, pending_args)
        if [str(item) for item in recomputed_targets] != [
            str(item) for item in (pending.get("target_paths") or [])
        ]:
            return ApprovalCheck(
                False, "targets_changed", "目标范围在审批后发生变化，拒绝执行"
            )

    cumulative = sum(
        int((state.get("mutations") or {}).get(name, 0)) for name in ("move", "rename")
    )
    if cumulative != int(pending.get("mutation_before") or 0):
        return ApprovalCheck(
            False,
            "mutation_changed",
            "审批后累计文件变更数量发生变化，拒绝执行",
            {"expected": int(pending.get("mutation_before") or 0), "actual": cumulative},
        )

    changed = _changed_items(pending.get("selected_items") or [])
    if changed:
        return ApprovalCheck(
            False, "stale_source", "源文件在审批后发生变化，请重新预览/确认", {"items": changed[:50]}
        )
    conflicts = _target_conflicts(pending)
    if conflicts:
        return ApprovalCheck(
            False, "target_conflict", "目标位置在审批后不可用，拒绝执行", {"items": conflicts[:50]}
        )
    return ApprovalCheck(True)
