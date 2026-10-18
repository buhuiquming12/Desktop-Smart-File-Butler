"""确定性完成判定：流程终止不等于目标完成。

模型可以提出 ``done``，但不能决定“是否成功”。本模块只读取已确认计划与真实工具
结果，检查可验证的后置条件（文件是否存在、批量计数是否自洽、扫描是否截断），
再给出独立于模型措辞的 outcome。校验过程只读元数据，不创建、不移动、不删除文件。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Iterable, Optional

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


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _result_paths_exist(result: Any) -> tuple[bool, str]:
    """只读校验结果路径；任何异常都视为“无法确认”，不向外抛。"""
    candidates: list[str] = []
    if isinstance(result, str):
        candidates = [result]
    elif isinstance(result, dict):
        preview = result.get("preview")
        if isinstance(preview, list):
            candidates = [str(item) for item in preview if isinstance(item, str)]
    for value in candidates:
        if not value:
            continue
        try:
            if not Path(value).exists():
                return False, f"结果路径不存在: {value}"
        except OSError as exc:
            return False, f"无法校验结果路径 {value}: {exc}"
    return True, ""


def _verify_observation(obs: Dict[str, Any]) -> Dict[str, Any]:
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
        paths_ok, path_error = _result_paths_exist(result)
        if not paths_ok:
            check.update(status="failed", detail=path_error)
            return check
        check.update(status="verified", detail=f"批量操作 {success} 项结果可验证")
        return check

    if tool in _PATH_RESULT_TOOLS and isinstance(result, str):
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

    if outcome == "success":
        return f"任务完成：{files}；{calls}。所有可验证的文件后置条件均已通过。"
    if outcome == "partial":
        return (
            f"任务部分完成：{files}；{calls}{reason_text}{pending}。"
            "已完成的操作仍可在操作日志中查询或撤销。"
        )
    if outcome == "rejected":
        return (
            f"任务已停止：用户拒绝了需要审批的操作，未执行后续变更。"
            f"{files}；{calls}{reason_text}{pending}。"
        )
    if outcome == "cancelled":
        return (
            "任务已取消：已完成的文件操作已保留，可在操作日志中查询或撤销。"
            f"{files}；{calls}。"
        )
    if outcome == "budget_exhausted":
        return (
            f"任务因重规划/执行预算达到上限而停止：{files}；{calls}"
            f"{reason_text}{pending}。"
            "已完成的操作已保留，可检查后继续或撤销。"
        )
    if outcome == "needs_clarification":
        return "需要补充信息后才能继续，尚未执行任何文件变更。"
    if outcome == "failed":
        return (
            f"任务失败：{files}；{calls}{reason_text}{pending}。"
            "如已有文件被修改，可在操作日志中查询或撤销。"
        )
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
    checks = [_verify_observation(obs) for obs in observations]
    unexecuted = _unexecuted(plan, step_index)
    check_statuses = [str(check.get("status")) for check in checks]

    file_stats = stats["file_stats"]
    file_success = _int(file_stats.get("success"))
    file_failed = _int(file_stats.get("failed"))
    tool_failed = _int(stats.get("tool_failed"))
    rejected = _int(stats.get("tool_rejected"))
    incomplete = (
        any(status in {"failed", "incomplete", "unverified"} for status in check_statuses)
        or bool(stats.get("truncated_reasons"))
        or bool(unexecuted)
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
        if stop_reason in {"model_done", "goal_verified"}:
            stop_reason = (
                "user_rejected"
                if rejected
                else "tool_failed"
                if tool_failed or file_failed
                else "incomplete_goal"
            )
    else:
        outcome = "success"
        stop_reason = "goal_verified"

    message = completion_message(
        outcome, stats, checks, unexecuted, stop_reason=stop_reason
    )
    return {
        "outcome": outcome,
        "stop_reason": stop_reason,
        "verified": outcome == "success",
        "message": message,
        "checks": checks,
        "unexecuted_steps": unexecuted,
        "stats": stats,
        "replan_limit": {
            "reached": bool(replan_limit_reached),
        },
    }
