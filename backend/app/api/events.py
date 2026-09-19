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
