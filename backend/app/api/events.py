"""LangGraph 流事件到 WebSocket 事件的转换（P2）。

该模块不依赖 FastAPI 应用全局对象，便于单独导入和测试。
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable, Dict, Iterable, Optional, Tuple

from ..models import WSEvent, WSEventType


def next_update(iterator: Iterable[Dict[str, Any]]) -> Tuple[bool, Optional[Dict[str, Any]]]:
    try:
        return True, next(iterator)  # type: ignore[arg-type]
    except StopIteration:
        return False, None


def event(event_type: WSEventType, thread_id: str, **payload: Any) -> WSEvent:
    return WSEvent(type=event_type, thread_id=thread_id, payload=payload)


async def emit_update(
    send: Callable[[str, WSEvent], Awaitable[None]],
    state: Callable[[str], Dict[str, Any]],
    client_id: Optional[str],
    thread_id: str,
    update: Dict[str, Any],
) -> None:
    if not client_id:
        return
    for node, delta in update.items():
        if node == "__interrupt__":
            pending = state(thread_id).get("pending_approval") or {}
            if pending:
                await send(client_id, event(WSEventType.approval_required, thread_id, **pending))
            continue
        safe_delta = delta if isinstance(delta, dict) else {"value": str(delta)}
        await send(client_id, event(WSEventType.node, thread_id, node=node, status=safe_delta.get("status")))
        current = safe_delta.get("current_step")
        if node == "act" and current:
            await send(client_id, event(WSEventType.tool_call, thread_id, tool=current.get("tool", ""), name=current.get("tool", ""), task_id=current.get("id", ""), detail=current.get("description", ""), args=current.get("args", {})))
        observations = safe_delta.get("observations")
        if observations:
            latest = observations[-1]
            await send(client_id, event(WSEventType.tool_result, thread_id, tool=latest.get("tool", ""), name=latest.get("tool", ""), task_id=latest.get("step_id", ""), detail=latest.get("error") or str(latest.get("result", "")), success=latest.get("status") == "ok", status=latest.get("status", ""), observation=latest))
            task_status = {"ok": "success", "failed": "failed", "rejected": "failed"}.get(latest.get("status"), "running")
            await send(client_id, event(WSEventType.task, thread_id, task_id=latest.get("step_id"), title=latest.get("tool") or "文件任务", description=latest.get("description"), detail=latest.get("error") or latest.get("description", ""), status=task_status, tool=latest.get("tool"), error=latest.get("error", "")))
        pending = safe_delta.get("pending_approval")
        if pending:
            await send(client_id, event(WSEventType.approval_required, thread_id, **pending))


async def emit_token(send: Callable[[str, WSEvent], Awaitable[None]], client_id: Optional[str], thread_id: str, data: Any) -> None:
    if not client_id:
        return
    chunk = data[0] if isinstance(data, tuple) else data
    content = getattr(chunk, "content", None)
    if isinstance(content, str) and content:
        await send(client_id, event(WSEventType.token, thread_id, token=content))


async def dispatch(send: Callable[[str, WSEvent], Awaitable[None]], state: Callable[[str], Dict[str, Any]], client_id: Optional[str], thread_id: str, item: Any) -> None:
    if isinstance(item, tuple) and len(item) == 2 and item[0] in ("updates", "messages"):
        mode, data = item
        if mode == "messages":
            await emit_token(send, client_id, thread_id, data)
        elif isinstance(data, dict):
            await emit_update(send, state, client_id, thread_id, data)
    elif isinstance(item, dict):
        await emit_update(send, state, client_id, thread_id, item)


# ---------------- 任务完成结构化摘要（体验优化） ----------------

_TOOL_LABELS = {
    "scan_directory": "扫描目录",
    "extract_text": "提取文本",
    "classify_file": "分类文件",
    "make_dir": "新建目录",
    "move_file": "移动文件",
    "rename_file": "重命名",
    "delete_file": "删除文件",
    "write_summary": "生成摘要",
    "set_preference": "保存偏好",
    "create_schedule": "创建定时任务",
}

_STATUS_LABELS = {
    "ok": "成功",
    "failed": "失败",
    "rejected": "跳过",
    "skipped": "跳过",
    "cancelled": "跳过",
}


def _observation_status_label(status: object) -> str:
    return _STATUS_LABELS.get(str(status), str(status) or "未知")


def _observation_paths(obs: Dict[str, Any]) -> tuple[str, Optional[str]]:
    """从一条观测里提取主路径与目标路径，供摘要展示与复制。"""
    tool = str(obs.get("tool", ""))
    args = obs.get("args") if isinstance(obs.get("args"), dict) else {}
    src = str(args.get("src") or args.get("path") or args.get("file_path") or args.get("directory") or args.get("output_dir") or "")
    dest = ""
    if tool == "move_file":
        dest = str(args.get("dest_dir") or "")
        if args.get("new_name"):
            dest = str(args.get("new_name"))
            if dest and not dest.startswith(("\\", "/", ":")):
                parent = str(args.get("dest_dir") or "")
                dest = str(parent) + "\\" + str(dest) if parent else str(dest)
                dest = dest.replace("/", "\\")
    elif tool == "rename_file":
        dest = str(args.get("new_name") or "")
    elif tool == "write_summary":
        output_dir = str(args.get("output_dir") or "")
        output_name = str(args.get("output_name") or "")
        if output_dir and output_name:
            dest = str(output_dir) + "\\" + str(output_name)
        elif output_dir:
            dest = output_dir
    result = obs.get("result")
    if isinstance(result, dict) and isinstance(result.get("items"), list) and not src:
        first = result["items"][0] if result["items"] else None
        if isinstance(first, dict):
            src = str(first.get("path") or "")
    return src, dest


def build_task_summary(observations: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """从观测列表生成结构化任务摘要（体验优化）。

    只回传安全、可审计的字段；操作明细不做全文转储，文件路径按唯一性截断展示。
    """
    items = list(observations)
    ok = 0
    failed = 0
    skipped = 0
    files: list[str] = []
    operations: list[Dict[str, Any]] = []

    def add_file(value: str) -> None:
        value = (value or "").strip()
        if value and value not in files:
            files.append(value)

    for obs in items:
        raw_status = str(obs.get("status") or "")
        status = raw_status
        if raw_status == "ok":
            ok += 1
        elif raw_status == "failed":
            failed += 1
        elif raw_status in {"rejected", "skipped", "cancelled"}:
            skipped += 1
        path, dest = _observation_paths(obs)
        add_file(path)
        add_file(dest)
        operations.append({
            "tool": str(obs.get("tool") or ""),
            "tool_label": _TOOL_LABELS.get(str(obs.get("tool") or ""), str(obs.get("tool") or "文件操作")),
            "description": str(obs.get("description") or ""),
            "status": status,
            "status_label": _observation_status_label(status),
            "path": path,
            "dest": dest or None,
        })

    return {
        "ok": ok,
        "failed": failed,
        "skipped": skipped,
        "files": files[:12],
        "operations": operations,
    }
