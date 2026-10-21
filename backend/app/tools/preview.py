"""P0：文件操作预览（逐文件、确定性、默认只读）。

设计要点：
- 计划完全由 manifest + 确定性规则算出，LLM 只提供规则，不枚举文件。
- 生成预览不触碰文件系统（只读 stat / 扩展名 / 可选的内容提取），因此“仅预览”
  不可能产生副作用。
- 预览记录 digest；执行前会重新核验源文件与目标冲突，任何一个选中项与预览不一致
  就整体拒绝并要求刷新，绝不静默执行“看起来差不多”的操作。
- 确认（confirm）把“这一次选中的条目集合 + 每个条目最终落点”一起绑定成 approve
  token；执行时条目集合与目标都必须与 token 完全一致，因此改选/改类别不会被旧确认
  自动授权。
- 确认是一次性的：执行开始前会把预览状态原子地从 ``preview`` 抢到 ``executing``，
  同一个 token 无法再次执行（文件恢复原位后也不行）。
- 目标位置在执行前必须为空：确认时占用即拒绝，执行瞬间被占用则跳过该项，
  **不会**自动改名成 ``name (1).ext`` 静默偏离已确认的计划。
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from .. import db
from ..logging_conf import get_logger
from ..security import SandboxViolation, is_within_sandbox, resolve_in_sandbox
from . import classify, extract, filesystem, manifests, naming
from .path_locks import path_locks

logger = get_logger(__name__)

SUPPORTED_OPS = ("move", "rename", "classify", "delete")

# 预览里内容分类的默认模型调用预算；超出后明确回退到扩展名规则，不假装“按内容”。
DEFAULT_CONTENT_BUDGET = 30

# 单个预览最多包含的条目数：一次预览几千行没有意义，也会拖慢界面。
MAX_PREVIEW_ITEMS = 2000


class PreviewError(Exception):
    """预览相关的可预期错误（参数、失效、确认不匹配）。"""

    def __init__(self, message: str, *, code: str = "invalid", detail: Optional[dict] = None) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.detail = detail or {}


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def item_id_for(path: str) -> str:
    return hashlib.sha1(path.casefold().encode("utf-8")).hexdigest()[:16]


def _digest(items: Iterable[dict]) -> str:
    payload = [
        [item["item_id"], item["source"], item["target"], item["status"]]
        for item in sorted(items, key=lambda entry: entry["item_id"])
    ]
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def _selection_digest(preview_digest: str, item_ids: Iterable[str]) -> str:
    payload = json.dumps(
        [preview_digest, sorted(str(value) for value in item_ids)], ensure_ascii=False
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _stat_signature(path: Path) -> tuple[int, int]:
    stat = path.stat()
    return int(stat.st_size), int(stat.st_mtime_ns)


def _source_dir(spec: dict) -> tuple[str, str]:
    """返回 ``(directory, scan_id)``；用户可直接给目录，也可复用已有 scan_id。"""
    scan_id = str(spec.get("scan_id") or "").strip()
    if scan_id:
        manifest = manifests.load(scan_id)
        return str(manifest.get("directory") or ""), scan_id
    directory = str(spec.get("directory") or "").strip()
    if not directory:
        raise PreviewError("需要 directory 或 scan_id 之一")
    return str(resolve_in_sandbox(directory, must_exist=True)), ""


def _ensure_manifest(spec: dict) -> tuple[str, dict[str, Any]]:
    """拿到 (directory, manifest 摘要)；必要时只读扫描一次并写入 manifest。"""
    directory, scan_id = _source_dir(spec)
    if scan_id:
        return directory, manifests.load(scan_id)
    scan = filesystem.scan_directory(directory, bool(spec.get("recursive", True)))
    all_items = [item.model_dump(mode="json") for item in scan.items]
    created = manifests.create(directory, all_items, {
        "scanned_count": scan.scanned_count,
        "truncated": scan.truncated,
        "reason": scan.reason,
    })
    return directory, manifests.load(created["scan_id"])


_DATE_PATTERNS = (
    re.compile(r"(?<!\d)(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})(?!\d)"),
    re.compile(r"(?<!\d)(\d{4})年(\d{1,2})月(\d{1,2})日"),
)


def extract_date_from_text(text: str) -> Optional[datetime]:
    """从正文里找**明确**的日期；找不到返回 None（绝不猜）。"""
    for pattern in _DATE_PATTERNS:
        match = pattern.search(text or "")
        if not match:
            continue
        year, month, day = (int(part) for part in match.groups())
        try:
            return datetime(year, month, day)
        except ValueError:
            continue
    return None


def _content_date(path: Path) -> tuple[Optional[datetime], str]:
    """按需读取正文取日期；只有真的提取成功才返回日期。"""
    try:
        text = extract.extract_text(str(path), max_chars=20_000)
    except Exception as exc:  # noqa: BLE001 - 提取失败只影响“内容日期”这一种规则
        logger.info("预览内容日期提取失败 %s: %s", path, exc)
        return None, "内容提取失败"
    if not text or text.startswith("["):
        return None, "内容不可用"
    value = extract_date_from_text(text)
    return value, "" if value else "正文中未找到明确日期"


def resolve_date(meta: dict, source: str) -> tuple[Optional[datetime], str]:
    """按声明的日期来源取日期，并返回不可用原因。"""
    if source == "now":
        return datetime.now(), ""
    if source == "content":
        return _content_date(Path(str(meta["path"])))
    raw = meta.get("modified") or ""
    try:
        modified = datetime.fromisoformat(str(raw)) if raw else None
    except ValueError:
        modified = None
    if modified is None:
        return None, "文件时间不可用"
    if source == "created":
        try:
            return datetime.fromtimestamp(Path(str(meta["path"])).stat().st_ctime), ""
        except OSError:
            return None, "无法读取创建时间"
    return modified, ""


def _classify_category(
    meta: dict,
    *,
    mode: str,
    llm: Any,
    budget: Any,
) -> dict[str, Any]:
    """给出分类结果与可展示的依据；内容模式失败时明确回退。"""
    return classify.classify_path(Path(str(meta["path"])), llm=llm, mode=mode, budget=budget)


def _as_int(value: Any, default: int, *, minimum: Optional[int] = None) -> int:
    """把可为空/可缺省的参数转成整数。

    不能写 ``value or default``：``sequence_start=0`` 是合法值（序号从 000 开始），
    ``0 or 1`` 会把它悄悄改成 1，让用户看到的计划与实际不符。
    """
    if value is None or value == "":
        return default
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    if minimum is not None and number < minimum:
        return default if default >= minimum else minimum
    return number


def _rename_rule(spec: dict) -> naming.RenameRule:
    return naming.RenameRule(
        prefix=str(spec.get("prefix") or ""),
        suffix=str(spec.get("suffix") or ""),
        find=str(spec.get("find") or ""),
        replace=str(spec.get("replace") or ""),
        case_sensitive=bool(spec.get("case_sensitive", False)),
        template=str(spec.get("template") or ""),
        sequence_start=_as_int(spec.get("sequence_start"), 1, minimum=0),
        sequence_step=_as_int(spec.get("sequence_step"), 1, minimum=1),
        sequence_padding=_as_int(spec.get("sequence_padding"), 3, minimum=1),
        sequence_position=str(spec.get("sequence_position") or "suffix"),
        date_format=str(spec.get("date_format") or "YYYYMMDD"),
    )


def build_items(
    spec: dict,
    *,
    llm: Any = None,
    content_budget: int = DEFAULT_CONTENT_BUDGET,
) -> dict[str, Any]:
    """根据 manifest + 规则生成逐文件计划（只读）。"""
    op = str(spec.get("op") or "")
    if op not in SUPPORTED_OPS:
        raise PreviewError(f"不支持的操作类型: {op or '(空)'}")
    directory, manifest = _ensure_manifest(spec)
    matches = manifests.match(str(manifest["scan_id"]), dict(spec.get("filter") or {}))
    if len(matches) > MAX_PREVIEW_ITEMS:
        raise PreviewError(
            f"匹配到 {len(matches)} 个文件，超过单次预览上限 {MAX_PREVIEW_ITEMS}；请先缩小过滤范围"
        )

    dest_dir = str(spec.get("dest_dir") or "").strip()
    dest_root = str(spec.get("dest_root") or "").strip()
    if op == "move" and not dest_dir:
        raise PreviewError("移动预览需要 dest_dir")
    if op == "classify" and not dest_root:
        raise PreviewError("分类预览需要 dest_root")
    if dest_dir:
        dest_dir = str(resolve_in_sandbox(dest_dir))
    if dest_root:
        dest_root = str(resolve_in_sandbox(dest_root))

    rule = _rename_rule(spec)
    date_source = str(spec.get("date_source") or "modified")
    use_date = bool(spec.get("use_date", False)) or "{date}" in rule.template
    use_sequence = bool(spec.get("use_sequence", False)) or "{seq}" in rule.template
    mode = str(spec.get("mode") or "extension")
    budget = classify.Budget(remaining=max(0, int(content_budget)))

    # 重命名需要一次性拿到全批次的日期与序号，因此先构造记录再统一定名。
    rename_records: list[dict] = []
    if op == "rename":
        for meta in matches:
            date_value, date_reason = resolve_date(meta, date_source)
            rename_records.append({
                "meta": meta,
                "stem": Path(str(meta["path"])).stem,
                "ext": Path(str(meta["path"])).suffix,
                "date": date_value if use_date else None,
                "date_reason": date_reason if use_date else "",
            })
        planned = naming.plan_names(
            rename_records,
            rule,
            use_sequence=use_sequence,
            use_date=use_date,
            source_date=date_source,
        )
    else:
        planned = []

    items: list[dict] = []
    for index, meta in enumerate(matches):
        source = str(meta["path"])
        name = str(meta.get("name") or Path(source).name)
        entry: dict[str, Any] = {
            "item_id": item_id_for(source),
            "source": source,
            "name": name,
            "op": op,
            "size": int(meta.get("size") or 0),
            "modified": str(meta.get("modified") or ""),
            "target": "",
            "target_name": "",
            "status": "ready",
            "reason": "",
            "category": "",
            "basis": "",
            "fallback": False,
            "editable_category": op == "classify",
        }
        try:
            size, mtime_ns = _stat_signature(Path(source))
            entry["size"], entry["mtime_ns"] = size, mtime_ns
        except OSError:
            entry["status"] = "skipped"
            entry["reason"] = "源文件已不存在"
            items.append(entry)
            continue

        if op == "move":
            entry["target"] = str(Path(dest_dir) / name)
        elif op == "rename":
            record = planned[index]
            date_reason = ""
            if use_date and not isinstance(record.get("date"), datetime):
                date_reason = str(record.get("date_reason") or "日期来源不可用")
            entry["target_name"] = str(record.get("target_name") or "")
            entry["target"] = str(Path(source).with_name(entry["target_name"])) if entry["target_name"] else ""
            entry["status"] = str(record.get("status") or "ready")
            entry["reason"] = date_reason or str(record.get("reason") or "")
        elif op == "classify":
            result = _classify_category(meta, mode=mode, llm=llm, budget=budget)
            entry["category"] = result["category"]
            entry["basis"] = result["basis"]
            entry["fallback"] = bool(result["fallback"])
            entry["fallback_reason"] = result["fallback_reason"]
            entry["target"] = str(Path(dest_root) / result["category"] / name)
        elif op == "delete":
            # 目标展示为“回收站”，实际路径由执行时的沙箱回收站决定。
            entry["target"] = "（项目回收站）"
            entry["target_name"] = name

        if entry["status"] == "ready" and op in {"move", "rename", "classify"}:
            target = Path(entry["target"])
            if target.exists() and target.resolve() != Path(source).resolve():
                entry["status"] = "conflict"
                entry["reason"] = "目标位置已存在同名文件（不会覆盖）"
            elif not is_within_sandbox(target):
                entry["status"] = "skipped"
                entry["reason"] = "目标路径越界，已被拒绝"
        items.append(entry)

    ready = sum(1 for item in items if item["status"] == "ready")
    return {
        "op": op,
        "directory": directory,
        "scan_id": str(manifest["scan_id"]),
        "spec": spec,
        "items": items,
        "digest": _digest(items),
        "counts": {
            "total": len(items),
            "ready": ready,
            "conflict": sum(1 for item in items if item["status"] == "conflict"),
            "skipped": sum(1 for item in items if item["status"] == "skipped"),
            "fallback": sum(1 for item in items if item.get("fallback")),
            "model_calls": budget.used,
        },
    }


def create(spec: dict, *, llm: Any = None, content_budget: int = DEFAULT_CONTENT_BUDGET) -> dict:
    """生成并持久化预览。全过程只读（scan_directory 只读，内容提取只读）。"""
    built = build_items(spec, llm=llm, content_budget=content_budget)
    preview_id = uuid.uuid4().hex
    record = {
        "preview_id": preview_id,
        "created_at": _iso_now(),
        "op": built["op"],
        "directory": built["directory"],
        "spec_json": json.dumps(built["spec"], ensure_ascii=False),
        "items_json": json.dumps(built["items"], ensure_ascii=False),
        "digest": built["digest"],
        "approval_json": "",
        "thread_id": f"preview-{preview_id}",
        "status": "preview",
        # 真实的模型调用次数：内容分类达到预算后回退，这里记录的是“真的调用了几次”，
        # 供运行预算对账（此前界面统计恒为 0，等于预算形同虚设）。
        "model_calls": int(built["counts"]["model_calls"]),
    }
    db.save_preview(record)
    logger.info(
        "生成操作预览 %s op=%s total=%s ready=%s",
        preview_id, built["op"], built["counts"]["total"], built["counts"]["ready"],
    )
    return page(preview_id, offset=0, limit=100)


def load(preview_id: str) -> dict:
    preview = db.get_preview(preview_id)
    if preview is None:
        raise PreviewError(f"预览不存在或已过期: {preview_id}", code="not_found")
    return preview


def page(preview_id: str, *, offset: int = 0, limit: int = 100) -> dict:
    preview = load(preview_id)
    items = preview["items"]
    offset = max(0, int(offset))
    limit = max(1, min(500, int(limit)))
    counts = {
        "total": len(items),
        "ready": sum(1 for item in items if item["status"] == "ready"),
        "conflict": sum(1 for item in items if item["status"] == "conflict"),
        "skipped": sum(1 for item in items if item["status"] == "skipped"),
        "fallback": sum(1 for item in items if item.get("fallback")),
        "model_calls": int(preview.get("model_calls") or 0),
    }
    return {
        "preview_id": preview["preview_id"],
        "created_at": preview["created_at"],
        "op": preview["op"],
        "directory": preview["directory"],
        "digest": preview["digest"],
        "status": preview["status"],
        "spec": preview["spec"],
        "counts": counts,
        "items": items[offset:offset + limit],
        "offset": offset,
        "limit": limit,
        "has_more": offset + limit < len(items),
        "approval": preview.get("approval"),
    }


def _selected(preview: dict, item_ids: Iterable[str]) -> list[dict]:
    wanted = [str(value) for value in item_ids]
    if not wanted:
        raise PreviewError("请至少选择一个文件", code="empty_selection")
    index = {item["item_id"]: item for item in preview["items"]}
    unknown = [value for value in wanted if value not in index]
    if unknown:
        raise PreviewError(
            f"有 {len(unknown)} 个条目不属于该预览，请刷新后重试", code="unknown_items"
        )
    if len(set(wanted)) != len(wanted):
        raise PreviewError("选择的条目有重复", code="duplicate_items")
    return [index[value] for value in wanted]


def threshold() -> int:
    raw = db.get_preference("batch_approval_threshold")
    try:
        value = int(raw) if raw is not None else 20
    except (TypeError, ValueError):
        return 20
    return value if value > 0 else 20


def confirm(
    preview_id: str,
    item_ids: Iterable[str],
    *,
    approve: bool = False,
    category_overrides: Optional[dict[str, str]] = None,
) -> dict:
    """把“这一次选中的条目 + 每个条目的最终落点”绑定为一次性确认。

    删除与超过阈值的批量操作必须 ``approve=true``；后端不会因为“用户打开过预览”
    就认为已经授权，因此旧确认无法覆盖后来扩大的选择（选择变了就要重新确认）。
    类别覆盖必须在这里提交并写进确认记录，执行时只认这一组落点。
    """
    preview = load(preview_id)
    if preview.get("status") not in {"preview", ""}:
        raise PreviewError(
            "该预览已经执行过或正在执行，请重新生成预览后再确认", code="confirm_required"
        )
    selected = _selected(preview, item_ids)
    not_ready = [item for item in selected if item["status"] != "ready"]
    if not_ready:
        raise PreviewError(
            f"有 {len(not_ready)} 个条目存在冲突或已跳过，请取消勾选后再执行",
            code="not_ready",
        )
    overrides = _clean_overrides(preview, selected, category_overrides)
    conflicts = _conflicts(preview, selected, overrides)
    if conflicts:
        lines = [f"{Path(item['source']).name}：{item['reason']}" for item in conflicts[:5]]
        raise PreviewError(
            "目标位置不可用，已拒绝确认（不会覆盖，也不会自动改名）。具体原因："
            + "；".join(lines)
            + (f"（另有 {len(conflicts) - 5} 项）" if len(conflicts) > 5 else ""),
            code="not_ready",
            detail={"conflicts": conflicts[:50]},
        )
    limit = threshold()
    needs_approval = preview["op"] == "delete" or len(selected) > limit
    if needs_approval and not approve:
        raise PreviewError(
            f"该操作需要确认：{preview['op']} 共 {len(selected)} 项"
            + ("（删除始终需要确认）" if preview["op"] == "delete" else f"（阈值 {limit}）"),
            code="approval_required",
            detail={"requires_approval": True, "count": len(selected), "threshold": limit},
        )
    targets = {
        item["item_id"]: _target_for(item, overrides.get(item["item_id"]))
        for item in selected
    }
    approval = {
        "approval_id": uuid.uuid4().hex,
        "item_ids": sorted(item["item_id"] for item in selected),
        "count": len(selected),
        "preview_digest": preview["digest"],
        "selection_digest": _selection_digest(preview["digest"], [item["item_id"] for item in selected]),
        "approved": bool(needs_approval and approve),
        # 落点绑定：执行时不再接受“临时改类别”，只承认这里记录的目标。
        "targets": targets,
        "overrides": overrides,
        "used_at": "",
        "created_at": _iso_now(),
    }
    db.update_preview(preview_id, approval_json=json.dumps(approval, ensure_ascii=False))
    return {
        "preview_id": preview_id,
        "approval_id": approval["approval_id"],
        "count": approval["count"],
        "requires_approval": needs_approval,
        "threshold": limit,
        "selection_digest": approval["selection_digest"],
        "targets": targets,
        "overrides": overrides,
    }


def _verify_item(item: dict, target: str = "") -> str:
    """执行前核验：源文件、大小与修改时间、目标冲突。返回空串表示有效。

    ``target`` 是本次确认绑定的落点（可能因用户改类别而与预览里的不同）；
    核验必须针对**真正要写入**的路径，否则改过类别后就能绕过冲突检查。
    """
    source = Path(item["source"])
    if not is_within_sandbox(source):
        return "源路径已不在授权目录内"
    try:
        size, mtime_ns = _stat_signature(source)
    except OSError:
        return "源文件已不存在"
    if int(item.get("size", -1)) != size or int(item.get("mtime_ns", -1)) != mtime_ns:
        return "源文件在预览后发生变化"
    if item["op"] in {"move", "rename", "classify"}:
        target_path = Path(target or str(item["target"]))
        if not is_within_sandbox(target_path):
            return "目标路径越界"
        if target_path.exists() and target_path.resolve() != source.resolve():
            return "目标位置已被占用"
    return ""


def _apply_category(item: dict, override: Optional[str]) -> str:
    """应用用户改过的类别；只接受单层安全目录名。"""
    target = Path(item["target"])
    if not override:
        return item["target"]
    category = naming.safe_category(override)
    if category == item.get("category"):
        return item["target"]
    return str(target.parent.parent / category / target.name)


def _target_for(item: dict, override: Optional[str]) -> str:
    """某一项在执行时真正会写入的目标路径（含用户改过的类别）。"""
    if item["op"] == "classify" and override:
        return _apply_category(item, override)
    return str(item["target"])


def _clean_overrides(
    preview: dict, selected: list[dict], category_overrides: Optional[dict[str, str]]
) -> dict[str, str]:
    """规范化类别覆盖：只接受选中项、只接受安全单层目录名。

    覆盖必须**在确认时**提交；执行时只能沿用已确认的那一组，否则会出现
    “确认的是 A 目录、执行写入 B 目录”。因此这里同时用于 confirm 与 execute
    的比对，两侧的规范化规则完全一致。
    """
    raw = {str(key): str(value) for key, value in (category_overrides or {}).items() if str(value).strip()}
    if not raw:
        return {}
    if preview["op"] != "classify":
        raise PreviewError("只有分类预览可以修改类别", code="invalid")
    known = {item["item_id"]: item for item in selected}
    cleaned: dict[str, str] = {}
    for item_id, value in raw.items():
        item = known.get(item_id)
        if item is None:
            raise PreviewError("有类别修改不属于本次选择的条目，请刷新后重试", code="unknown_items")
        try:
            category = naming.safe_category(value)
        except ValueError as exc:
            raise PreviewError(f"类别名不合法：{exc}", code="invalid") from exc
        if category != str(item.get("category") or ""):
            cleaned[item_id] = category
    return cleaned


def _conflicts(preview: dict, selected: list[dict], overrides: dict[str, str]) -> list[dict]:
    """确认前的落点核验：目标越界或已被占用都返回，由调用方整体拒绝。"""
    problems: list[dict] = []
    for item in selected:
        if item["op"] == "delete":
            continue
        target = _target_for(item, overrides.get(item["item_id"]))
        path = Path(target)
        if not is_within_sandbox(path):
            problems.append({
                "item_id": item["item_id"], "source": item["source"],
                "target": target, "reason": "目标路径越界",
            })
            continue
        if path.exists() and path.resolve() != Path(item["source"]).resolve():
            problems.append({
                "item_id": item["item_id"], "source": item["source"], "target": target,
                "reason": "目标位置已存在同名文件（不会覆盖，也不会自动改名）",
            })
    return problems


def execute(
    preview_id: str,
    item_ids: Iterable[str],
    *,
    confirm_token: str = "",
    category_overrides: Optional[dict[str, str]] = None,
) -> dict:
    """执行用户选中的条目。

    - 需要重新核验每个选中项；任何一项与预览不一致就整体拒绝（不猜测、不静默跳过）。
    - confirm_token 必须与本次选择的条目集合、以及确认时绑定的落点完全一致，
      避免旧确认授权更大的操作，或在确认后偷偷改分类目标。
    - 确认只能用一次：先把预览状态原子地抢到 ``executing``，抢不到就拒绝。
    - 所有写操作仍经 ``resolve_in_sandbox``、目录锁与审计，与其它工具一致。
    """
    preview = load(preview_id)
    selected = _selected(preview, item_ids)
    approval = preview.get("approval") or {}
    if not confirm_token or approval.get("approval_id") != confirm_token:
        raise PreviewError("执行前需要确认本次选择（确认已过期）", code="confirm_required")
    if approval.get("used_at"):
        raise PreviewError(
            "这次确认已经用过了；再次执行需要重新确认", code="confirm_required"
        )
    wanted = sorted(item["item_id"] for item in selected)
    if approval.get("item_ids") != wanted:
        raise PreviewError(
            "选择在确认后发生变化，原确认不再有效，请重新确认", code="selection_changed"
        )
    if approval.get("preview_digest") != preview["digest"]:
        raise PreviewError("预览内容已变化，请刷新后重新确认", code="stale_preview")

    # 落点绑定：请求里的类别覆盖必须与确认时提交的完全一致。
    approved_overrides = {str(k): str(v) for k, v in (approval.get("overrides") or {}).items()}
    requested_overrides = _clean_overrides(preview, selected, category_overrides)
    if requested_overrides != approved_overrides:
        raise PreviewError(
            "分类目标在确认后被修改，原确认不再有效，请重新确认", code="targets_changed"
        )
    targets = {
        item["item_id"]: str(
            approval.get("targets", {}).get(item["item_id"])
            or _target_for(item, approved_overrides.get(item["item_id"]))
        )
        for item in selected
    }

    # 一次性：状态只能从 preview 抢到 executing，抢不到说明已被执行/正在执行。
    if not db.claim_preview_execution(preview_id):
        raise PreviewError(
            "该预览已执行或正在执行，请重新生成预览并确认", code="confirm_required"
        )
    stale: list[tuple[dict, str]] = []
    for item in selected:
        reason = (
            _verify_item(item)
            if item["op"] == "delete"
            else _verify_item(item, targets[item["item_id"]])
        )
        if reason:
            stale.append((item, reason))
    if stale:
        # 没有任何写入发生，释放抢占后要求刷新重试。
        db.release_preview_execution(preview_id)
        lines = [f"{Path(item['source']).name}：{reason}" for item, reason in stale[:5]]
        more = f"（另有 {len(stale) - 5} 项）" if len(stale) > 5 else ""
        raise PreviewError(
            "预览已失效，请刷新后重试。具体原因：" + "；".join(lines) + more,
            code="stale_preview",
            detail={"stale": [
                {"source": item["source"], "reason": reason} for item, reason in stale[:50]
            ]},
        )
    approval["used_at"] = _iso_now()
    db.update_preview(preview_id, approval_json=json.dumps(approval, ensure_ascii=False))

    results: list[dict] = []
    thread_id = preview.get("thread_id") or f"preview-{preview_id}"
    with db.operation_thread(thread_id):
        for item in selected:
            outcome = _execute_item(item, targets[item["item_id"]])
            results.append(outcome)

    ok = sum(1 for result in results if result["status"] == "ok")
    failed = sum(1 for result in results if result["status"] == "failed")
    skipped = sum(1 for result in results if result["status"] == "skipped")
    db.update_preview(preview_id, status="executed")
    db.log_operation(
        "preview_execute", preview_id, "ok" if not failed else "failed",
        detail=f"{preview['op']} 选中 {len(selected)} 项：成功 {ok}，失败 {failed}，跳过 {skipped}",
        thread_id=thread_id,
    )
    try:  # 预览执行后顺手更新内容索引（索引缺失时是空操作）
        from . import indexer

        for result in results:
            if result["status"] == "ok" and result.get("target") and preview["op"] != "delete":
                indexer.note_path_moved(result["source"], result["target"])
            elif result["status"] == "ok" and preview["op"] == "delete":
                indexer.note_path_removed(result["source"])
    except Exception:  # noqa: BLE001 - 索引更新失败不能影响已完成的文件操作
        logger.exception("预览执行后更新内容索引失败")
    return {
        "preview_id": preview_id,
        "op": preview["op"],
        "selected": len(selected),
        "ok": ok,
        "failed": failed,
        "skipped": skipped,
        "results": results,
        "rollback_thread_id": thread_id,
    }


def _execute_item(item: dict, target: str) -> dict:
    source = item["source"]
    op = item["op"]
    # delete 的 target 只是展示用的“回收站”占位，真正路径由执行时的沙箱回收站决定。
    base = {
        "item_id": item["item_id"], "source": source, "op": op,
        "target": item["target"] if op == "delete" else target,
    }
    try:
        if op == "move":
            target_path = Path(target)
            with path_locks.acquire(Path(source).parent, target_path.parent):
                reason = _verify_item(item, target)
                if reason:
                    return {**base, "status": "skipped", "detail": reason}
                # ensure_unique=False：目标已被占用就失败，绝不自动改名为 “name (1).ext”，
                # 否则实际落点会悄悄偏离用户确认过的目标。
                actual = filesystem.move_file(
                    source, str(target_path.parent), ensure_unique=False
                )
            return {**base, "status": "ok", "detail": "已移动", "target": actual}
        if op == "rename":
            target_path = Path(target)
            with path_locks.acquire(Path(source).parent):
                reason = _verify_item(item, target)
                if reason:
                    return {**base, "status": "skipped", "detail": reason}
                actual = filesystem.rename_file(source, target_path.name, ensure_unique=False)
            return {**base, "status": "ok", "detail": "已重命名", "target": actual}
        if op == "classify":
            target_path = Path(target)
            with path_locks.acquire(Path(source).parent, target_path.parent):
                reason = _verify_item(item, target)
                if reason:
                    return {**base, "status": "skipped", "detail": reason}
                actual = filesystem.move_file(
                    source, str(target_path.parent), ensure_unique=False
                )
            return {**base, "status": "ok", "detail": "已按类别移动", "target": actual}
        if op == "delete":
            with path_locks.acquire(Path(source).parent):
                reason = _verify_item(item)
                if reason:
                    return {**base, "status": "skipped", "detail": reason}
                actual = filesystem.delete_file(source)
            return {**base, "status": "ok", "detail": "已移入回收站", "target": actual}
        return {**base, "status": "failed", "detail": f"不支持的操作: {op}"}
    except (SandboxViolation, FileNotFoundError) as exc:
        return {**base, "status": "skipped", "detail": str(exc)}
    except FileExistsError as exc:
        # 执行瞬间目标被占用：不覆盖、不改名，明确报告跳过。
        return {**base, "status": "skipped", "detail": f"目标位置在执行时被占用：{exc}"}
    except Exception as exc:  # noqa: BLE001 - 单个文件失败不中断整批
        logger.exception("预览执行单项失败 %s", source)
        return {**base, "status": "failed", "detail": str(exc)}

