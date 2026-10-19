"""确定性完成判定：流程终止不等于目标完成。

模型可以提出 ``done``，但不能决定“是否成功”。本模块只读取已确认计划与真实工具
结果，检查可验证的后置条件（文件是否存在、批量计数是否自洽、扫描是否截断），
再给出独立于模型措辞的 outcome。校验过程只读元数据，不创建、不移动、不删除文件。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from ..policy.engine import explicit_intent_effects, intent_effects
from ..tools.contracts import TOOL_CONTRACTS

OUTCOMES = (
    "success",
    "partial",
    "failed",
    "rejected",
    "cancelled",
    "budget_exhausted",
    "needs_clarification",
    "unknown",
)

_BATCH_TOOLS = {"batch_move", "batch_rename", "batch_classify"}
_SINGLE_FILE_TOOLS = {"move_file", "rename_file", "delete_file", "write_summary", "make_dir"}
_PATH_RESULT_TOOLS = {"move_file", "rename_file", "delete_file", "write_summary"}
_MUTATION_TOOLS = _BATCH_TOOLS | _SINGLE_FILE_TOOLS

_EFFECT_LABELS = {
    "move": "移动",
    "rename": "重命名",
    "delete": "删除",
    "create": "新建目录",
    "write": "生成摘要",
    "preference": "保存偏好",
    "schedule": "创建定时任务",
}


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _result_paths_exist(
    result: Any, skipped: Optional[set[str]] = None
) -> tuple[bool, str]:
    """只读校验结果路径；任何异常都视为“无法确认”，不向外抛。

    ``skipped`` 是被后续成功变更消费掉的路径（``A→B`` 再 ``B→C`` 的 B 处）：
    它们本就应该消失，不能因此判失败。
    """
    candidates: list[str] = []
    if isinstance(result, str):
        candidates = [result]
    elif isinstance(result, dict):
        preview = result.get("preview")
        if isinstance(preview, list):
            candidates = [str(item) for item in preview if isinstance(item, str)]
    superseded = skipped or set()
    for value in candidates:
        if not value or _norm_path(value) in superseded:
            continue
        try:
            if not Path(value).exists():
                return False, f"结果路径不存在: {value}"
        except OSError as exc:
            return False, f"无法校验结果路径 {value}: {exc}"
    return True, ""


def _norm_path(value: Any) -> str:
    try:
        return os.path.normcase(os.path.abspath(str(value)))
    except (OSError, ValueError):
        return str(value)


def _result_paths(obs: Dict[str, Any]) -> list[str]:
    result = obs.get("result")
    if isinstance(result, str):
        return [result]
    if isinstance(result, dict):
        preview = result.get("preview")
        if isinstance(preview, list):
            return [str(item) for item in preview if isinstance(item, str) and item]
    return []


def _consumed_sources(observations: list, index: int) -> set[str]:
    """收集“后续成功变更”消费掉的源路径。

    ``A→B`` 再 ``B→C`` 时，B 处的旧结果会在收尾时消失，但这属于正常链路：
    后续操作把该路径作为源消费掉了，不能算“结果凭空消失”。

    批量工具的参数里没有逐项源路径（只有 scan_id），因此还要读结果里的
    ``sources``：``batch_move`` 之后再 ``batch_classify`` 也是同一条正常链路。
    """
    sources: set[str] = set()
    for later in observations[index + 1:]:
        if str(later.get("status") or "") != "ok":
            continue
        if str(later.get("tool") or "") not in _MUTATION_TOOLS:
            continue
        args = later.get("args") if isinstance(later.get("args"), dict) else {}
        for key in ("src", "path", "file_path", "directory"):
            value = args.get(key)
            if isinstance(value, str) and value:
                sources.add(_norm_path(value))
        result = later.get("result")
        if isinstance(result, dict):
            for value in result.get("sources") or []:
                if isinstance(value, str) and value:
                    sources.add(_norm_path(value))
    return sources


def _verify_observation(obs: Dict[str, Any], *, superseded: Optional[set[str]] = None) -> Dict[str, Any]:
    """返回该观察的校验结论。

    执行当下的记录（``verification``）解决“中间状态被后续操作改变”的误判；但收尾时
    仍要复核最终状态，否则文件在任务结束前消失也会被报成成功。只有被后续成功变更
    消费掉的路径才允许跳过复核。
    """
    recorded = obs.get("verification")
    if not (isinstance(recorded, dict) and recorded.get("status")):
        return _verify_live(obs, superseded=superseded)

    check = {
        "step_id": obs.get("step_id"),
        "tool": str(obs.get("tool") or ""),
        "status": str(recorded.get("status")),
        "detail": str(recorded.get("detail") or ""),
        "recorded": True,
    }
    if check["status"] != "verified":
        # 失败与不完整不会因为时间推移变好，直接采信执行当下的结论。
        return check

    skipped = superseded or set()
    missing = [
        path
        for path in _result_paths(obs)
        if _norm_path(path) not in skipped and not Path(path).exists()
    ]
    if missing:
        check.update(
            status="failed",
            detail=f"结果路径在任务结束前已消失: {missing[0]}",
        )
    return check


def _unmet_effects(intent: str, observations: list) -> list[str]:
    """用户要求、但本次运行从未成功产生的副作用。

    例如用户说“把 PDF 移动到 PDF 文件夹”，模型只扫描就结束：扫描不构成“移动”。

    ``create`` 平时是移动/整理的从属授权（移动前可能要建目标目录，``move_file``
    自己也会建），因此默认不当作目标；但用户在请求里明确要求“新建目录/文件夹”时
    它就是独立目标——混合请求（先建目录再移动）同样必须真的建成。
    “移动到新建的 out 文件夹”这类定语用法由 ``explicit_intent_effects`` 排除。
    """
    required = {kind for kind in intent_effects(intent) if kind not in {"read", "create"}}
    required |= explicit_intent_effects(intent)
    if not required:
        return []
    performed: set[str] = set()
    for obs in observations:
        if str(obs.get("status") or "") != "ok":
            continue
        contract = TOOL_CONTRACTS.get(str(obs.get("tool") or ""))
        if contract is not None:
            performed.add(contract.side_effect)
    return sorted(required - performed)


def record_verification(obs: Dict[str, Any]) -> Dict[str, Any]:
    """工具刚执行完时立即记录后置条件，随观察一起持久化。

    校验仍然只读；它只回答“在操作完成的那一刻，结果是否成立”。调用方负责在
    执行后立刻调用一次，避免把中间状态当成最终状态。
    """
    if str(obs.get("status") or "") != "ok":
        return obs
    if isinstance(obs.get("verification"), dict):
        return obs
    return {**obs, "verification": _verify_live(obs)}


def _verify_live(
    obs: Dict[str, Any], *, superseded: Optional[set[str]] = None
) -> Dict[str, Any]:
    tool = str(obs.get("tool") or "")
    status = str(obs.get("status") or "")
    check: Dict[str, Any] = {
        "step_id": obs.get("step_id"),
        "tool": tool,
        "status": "unverified",
        "detail": "",
    }
    if status == "failed":
        check.update(status="failed", detail=str(obs.get("error") or "工具执行失败"))
        return check
    if status == "rejected":
        check.update(status="skipped", detail="用户拒绝，未执行该步骤")
        return check
    if status != "ok":
        check.update(status="unverified", detail=f"未知工具状态: {status or '<empty>'}")
        return check

    result = obs.get("result")

    if tool == "scan_directory":
        if not isinstance(result, dict):
            check.update(status="failed", detail="扫描结果不是对象")
            return check
        if not result.get("scan_id"):
            check.update(status="failed", detail="扫描结果缺少 scan_id")
            return check
        if bool(result.get("truncated")):
            reason = str(result.get("reason") or "unknown")
            check.update(status="incomplete", detail=f"扫描不完整：{reason}")
            return check
        check.update(status="verified", detail=f"扫描清单 {result.get('scan_id')} 可用")
        return check

    if tool in _BATCH_TOOLS:
        if not isinstance(result, dict):
            check.update(status="failed", detail="批量结果不是对象")
            return check
        matched = _int(result.get("matched"))
        success = _int(result.get("success"))
        failed = _int(result.get("failed"))
        skipped = _int(result.get("skipped"))
        if success + failed + skipped != matched:
            check.update(
                status="failed",
                detail=f"批量计数不自洽：matched={matched}, success={success}, failed={failed}, skipped={skipped}",
            )
            return check
        if success < matched:
            reasons = []
            if failed:
                reasons.append(f"失败 {failed}")
            if skipped:
                reasons.append(f"跳过 {skipped}")
            if result.get("truncated"):
                reasons.append(f"扫描截断 {result.get('reason') or 'unknown'}")
            check.update(
                status="incomplete",
                detail=f"仅完成 {success}/{matched} 项" + (f"（{'，'.join(reasons)}）" if reasons else ""),
            )
            return check
        if bool(result.get("truncated")):
            check.update(
                status="incomplete",
                detail=f"扫描不完整：{result.get('reason') or 'unknown'}",
            )
            return check
        paths_ok, path_error = _result_paths_exist(result, superseded)
        if not paths_ok:
            check.update(status="failed", detail=path_error)
            return check
        check.update(status="verified", detail=f"批量操作 {success} 项结果可验证")
        return check

    if tool in _PATH_RESULT_TOOLS and isinstance(result, str):
        if superseded and _norm_path(result) in superseded:
            check.update(status="verified", detail="结果已被后续操作消费")
            return check
        try:
            exists = Path(result).exists()
        except OSError as exc:
            check.update(status="failed", detail=f"无法校验结果路径: {exc}")
            return check
        if not exists:
            check.update(status="failed", detail=f"结果路径不存在: {result}")
        else:
            check.update(status="verified", detail="结果路径存在")
        return check

    if tool == "make_dir":
        if isinstance(result, str):
            try:
                is_dir = Path(result).is_dir()
            except OSError as exc:
                check.update(status="failed", detail=f"无法校验目录: {exc}")
                return check
            check.update(
                status="verified" if is_dir else "failed",
                detail="目录存在" if is_dir else f"目录不存在: {result}",
            )
            return check
        check.update(status="failed", detail="目录结果不是路径")
        return check

    if tool == "set_preference":
        check.update(
            status="verified" if isinstance(result, dict) and result.get("saved") else "failed",
            detail="偏好已保存" if isinstance(result, dict) and result.get("saved") else "偏好保存结果不可验证",
        )
        return check

    if tool == "delete_preference":
        deleted = isinstance(result, dict) and result.get("deleted")
        check.update(
            status="verified" if deleted else "failed",
            detail="偏好已删除" if deleted else "偏好不存在或未删除",
        )
        return check

    if tool == "request_clarification":
        check.update(
            status="verified" if isinstance(result, dict) and result.get("question") else "failed",
            detail="已生成澄清问题",
        )
        return check

    if tool == "create_schedule":
        has_job = isinstance(result, dict) and bool(result.get("job_id"))
        check.update(
            status="verified" if has_job else "failed",
            detail="定时任务已登记" if has_job else "定时任务结果缺少 job_id",
        )
        return check

    if tool in {"extract_text", "classify_file"}:
        check.update(
            status="verified" if result not in (None, "") else "failed",
            detail="" if result not in (None, "") else "工具没有返回可用结果",
        )
        return check

    check.update(status="unverified", detail=f"没有为该工具登记后置条件: {tool}")
    return check


def collect_stats(
    observations: Iterable[Dict[str, Any]],
    *,
    mutations: Optional[Dict[str, int]] = None,
    plan: Optional[list] = None,
    step_index: int = 0,
) -> Dict[str, Any]:
    """分别统计工具调用与文件级结果，避免用“调用次数”冒充“文件数”。"""
    items = list(observations)
    stats: Dict[str, Any] = {
        "tool_calls": len(items),
        "tool_ok": 0,
        "tool_failed": 0,
        "tool_rejected": 0,
        "tool_skipped": 0,
        "file_changes": 0,
        "file_stats": {
            "matched": 0,
            "success": 0,
            "failed": 0,
            "skipped": 0,
        },
        "truncated_reasons": [],
        "unexecuted_steps": 0,
    }
    reasons: list[str] = []
    for obs in items:
        status = str(obs.get("status") or "")
        if status == "ok":
            stats["tool_ok"] += 1
        elif status == "failed":
            stats["tool_failed"] += 1
        elif status == "rejected":
            stats["tool_rejected"] += 1
        else:
            stats["tool_skipped"] += 1

        tool = str(obs.get("tool") or "")
        result = obs.get("result")
        if tool in _BATCH_TOOLS and isinstance(result, dict):
            stats["file_stats"]["matched"] += _int(result.get("matched"))
            stats["file_stats"]["success"] += _int(result.get("success"))
            stats["file_stats"]["failed"] += _int(result.get("failed"))
            stats["file_stats"]["skipped"] += _int(result.get("skipped"))
            if result.get("truncated"):
                reason = str(result.get("reason") or "unknown")
                if reason not in reasons:
                    reasons.append(reason)
        elif tool in _SINGLE_FILE_TOOLS:
            if status == "ok":
                stats["file_stats"]["success"] += 1
            elif status == "failed":
                stats["file_stats"]["failed"] += 1
            elif status == "rejected":
                stats["file_stats"]["skipped"] += 1
        if tool == "scan_directory" and isinstance(result, dict) and result.get("truncated"):
            reason = str(result.get("reason") or "unknown")
            if reason not in reasons:
                reasons.append(reason)

    mutation_counts = mutations or {}
    # 实际成功变更以 mutation 计数为准；批量/单文件结果用于文件级明细。
    stats["file_changes"] = sum(
        _int(mutation_counts.get(name)) for name in ("move", "rename", "delete")
    )
    stats["truncated_reasons"] = reasons
    plan_items = plan or []
    stats["unexecuted_steps"] = max(0, len(plan_items) - max(0, _int(step_index)))
    return stats


def _unexecuted(plan: Optional[list], step_index: int) -> list[Dict[str, Any]]:
    items = plan or []
    return [
        {
            "id": str(step.get("id") or index),
            "tool": str(step.get("tool") or ""),
            "description": str(step.get("description") or ""),
        }
        for index, step in enumerate(items[max(0, _int(step_index)):], start=max(0, _int(step_index)))
    ]


def completion_message(
    outcome: str,
    stats: Dict[str, Any],
    checks: list[Dict[str, Any]],
    unexecuted: list[Dict[str, Any]],
    *,
    stop_reason: str,
    unmet_effects: Optional[list[str]] = None,
) -> str:
    file_stats = stats.get("file_stats") or {}
    files = (
        f"文件成功 {file_stats.get('success', 0)}、失败 {file_stats.get('failed', 0)}、"
        f"跳过 {file_stats.get('skipped', 0)}"
    )
    calls = (
        f"工具调用 {stats.get('tool_calls', 0)} 次"
        f"（成功 {stats.get('tool_ok', 0)}、失败 {stats.get('tool_failed', 0)}、"
        f"拒绝 {stats.get('tool_rejected', 0)}）"
    )
    reasons = stats.get("truncated_reasons") or []
    reason_text = f"；扫描不完整原因：{'、'.join(map(str, reasons))}" if reasons else ""
    pending = (
        f"；仍有 {len(unexecuted)} 个已规划步骤未执行"
        if unexecuted
        else ""
    )
    unmet = [name for name in (unmet_effects or []) if name]
    unmet_text = (
        "；用户要求的「" + "、".join(_EFFECT_LABELS.get(name, name) for name in unmet) + "」尚未执行"
        if unmet
        else ""
    )

    if outcome == "success":
        return f"任务完成：{files}；{calls}。所有可验证的文件后置条件均已通过。"
    if outcome == "partial":
        return (
            f"任务部分完成：{files}；{calls}{reason_text}{pending}{unmet_text}。"
            "已完成的操作仍可在操作日志中查询或撤销。"
        )
    if outcome == "rejected":
        return (
            f"任务已停止：用户拒绝了需要审批的操作，未执行后续变更。"
            f"{files}；{calls}{reason_text}{pending}{unmet_text}。"
        )
    if outcome == "cancelled":
        return (
            "任务已取消：已完成的文件操作已保留，可在操作日志中查询或撤销。"
            f"{files}；{calls}。"
        )
    if outcome == "budget_exhausted":
        return (
            f"任务因重规划/执行预算达到上限而停止：{files}；{calls}"
            f"{reason_text}{pending}{unmet_text}。"
            "已完成的操作已保留，可检查后继续或撤销。"
        )
    if outcome == "needs_clarification":
        return "需要补充信息后才能继续，尚未执行任何文件变更。"
    if outcome == "failed":
        return (
            f"任务失败：{files}；{calls}{reason_text}{pending}{unmet_text}。"
            "如已有文件被修改，可在操作日志中查询或撤销。"
        )
    if not stats.get("tool_calls"):
        # 空计划 / 纯问答：没有任何可验证的后置条件，不能声称完成。
        return "未执行任何文件操作，无法验证目标是否完成。"
    return f"任务结束，但完成状态无法确认：{files}；{calls}{reason_text}{pending}。"


def evaluate_completion(
    state: Dict[str, Any],
    *,
    stop_reason: str = "model_done",
    forced_outcome: Optional[str] = None,
    replan_limit_reached: bool = False,
) -> Dict[str, Any]:
    """根据已确认计划与实际工具结果给出 outcome 与可审计校验记录。"""
    observations = list(state.get("observations") or [])
    plan = list(state.get("plan") or [])
    step_index = _int(state.get("step_index"), 0)
    stats = collect_stats(
        observations,
        mutations=state.get("mutations") or {},
        plan=plan,
        step_index=step_index,
    )
    checks: list[Dict[str, Any]] = []
    for index, obs in enumerate(observations):
        superseded = _consumed_sources(observations, index)
        checks.append(_verify_observation(obs, superseded=superseded))
    unexecuted = _unexecuted(plan, step_index)
    check_statuses = [str(check.get("status")) for check in checks]
    unmet_effects = _unmet_effects(
        str(state.get("trusted_user_intent") or state.get("user_request") or ""),
        observations,
    )

    file_stats = stats["file_stats"]
    file_success = _int(file_stats.get("success"))
    file_failed = _int(file_stats.get("failed"))
    tool_failed = _int(stats.get("tool_failed"))
    rejected = _int(stats.get("tool_rejected"))
    incomplete = (
        any(status in {"failed", "incomplete", "unverified"} for status in check_statuses)
        or bool(stats.get("truncated_reasons"))
        or bool(unexecuted)
        or bool(unmet_effects)
        or file_failed > 0
        or file_stats.get("skipped", 0) > 0
        or tool_failed > 0
        or rejected > 0
    )

    if forced_outcome:
        outcome = forced_outcome
    elif replan_limit_reached:
        outcome = "budget_exhausted"
        stop_reason = "replan_limit"
    elif stop_reason == "cancelled":
        outcome = "cancelled"
    elif rejected and file_success == 0 and tool_failed == 0:
        outcome = "rejected"
        stop_reason = "user_rejected"
    elif tool_failed and file_success == 0 and not any(
        status == "ok" for status in (str(obs.get("status") or "") for obs in observations)
    ):
        outcome = "failed"
        stop_reason = "tool_failed"
    elif incomplete:
        outcome = "partial"
        if stop_reason in {"model_done", "goal_verified", "no_operations"}:
            stop_reason = (
                "user_rejected"
                if rejected
                else "tool_failed"
                if tool_failed or file_failed
                else "goal_unmet"
                if unmet_effects
                else "incomplete_goal"
            )
    else:
        outcome = "success"
        stop_reason = "goal_verified"

    message = completion_message(
        outcome,
        stats,
        checks,
        unexecuted,
        stop_reason=stop_reason,
        unmet_effects=unmet_effects,
    )
    return {
        "outcome": outcome,
        "stop_reason": stop_reason,
        "verified": outcome == "success",
        "message": message,
        "checks": checks,
        "unexecuted_steps": unexecuted,
        "unmet_effects": unmet_effects,
        "stats": stats,
        "replan_limit": {
            "reached": bool(replan_limit_reached),
        },
    }
