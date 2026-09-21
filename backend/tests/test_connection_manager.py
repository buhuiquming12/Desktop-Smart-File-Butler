"""B2 回归：向已断开的客户端推送事件不得抛错，也不得留下僵尸连接。

历史缺陷：单个客户端的连接异常会沿着 ``connections.send`` 冒泡回会话执行循环，
既可能中断 Agent 执行，也会让映射表里残留已死的 WebSocket。
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, List

import pytest
from starlette.websockets import WebSocketDisconnect, WebSocketState

from app.main import ConnectionManager
from app.models import WSEvent, WSEventType


class _FakeWebSocket:
    """最小 WebSocket 替身：只覆盖 send_json 与 application_state。"""

    def __init__(
        self,
        state: WebSocketState = WebSocketState.CONNECTED,
        error: Exception | None = None,
    ) -> None:
        self.application_state = state
        self._error = error
        self.sent: List[Dict[str, Any]] = []

    async def send_json(self, payload: Dict[str, Any]) -> None:
        if self._error is not None:
            raise self._error
        self.sent.append(payload)


def _event() -> WSEvent:
    return WSEvent(type=WSEventType.done, thread_id="t-1", payload={"status": "completed"})


def _send(manager: ConnectionManager, client_id: str) -> None:
    asyncio.run(manager.send(client_id, _event()))


def test_send_to_unknown_client_is_noop() -> None:
    manager = ConnectionManager()
    _send(manager, "nobody")  # 不应抛错
    assert manager._connections == {}


def test_send_delivers_to_connected_client() -> None:
    manager = ConnectionManager()
    socket = _FakeWebSocket()
    manager._connections["c1"] = socket  # type: ignore[assignment]

    _send(manager, "c1")

    assert len(socket.sent) == 1
    assert socket.sent[0]["type"] == "done"
    assert "c1" in manager._connections  # 正常连接不应被清理


def test_send_to_closed_client_does_not_raise_and_cleans_up() -> None:
    manager = ConnectionManager()
    socket = _FakeWebSocket(state=WebSocketState.DISCONNECTED)
    manager._connections["c1"] = socket  # type: ignore[assignment]

    _send(manager, "c1")

    assert socket.sent == []
    assert "c1" not in manager._connections


@pytest.mark.parametrize(
    "error",
    [
        RuntimeError('Cannot call "send" once a close message has been sent.'),
        WebSocketDisconnect(code=1006),
    ],
)
def test_send_survives_disconnect_errors(error: Exception) -> None:
    """发送瞬间对端断开：吞掉异常并清理映射，不得冒泡。"""
    manager = ConnectionManager()
    socket = _FakeWebSocket(error=error)
    manager._connections["c1"] = socket  # type: ignore[assignment]

    _send(manager, "c1")

    assert "c1" not in manager._connections


def test_send_survives_unexpected_error() -> None:
    """非预期的推送异常也不得中断会话执行（B2 的核心不变量）。"""
    manager = ConnectionManager()
    socket = _FakeWebSocket(error=ValueError("底层传输炸了"))
    manager._connections["c1"] = socket  # type: ignore[assignment]

    _send(manager, "c1")

    assert "c1" not in manager._connections


def test_disconnect_ignores_stale_socket() -> None:
    """旧连接迟到的断开事件不得摘掉已被替换的新连接。"""
    manager = ConnectionManager()
    old, new = _FakeWebSocket(), _FakeWebSocket()
    manager._connections["c1"] = new  # type: ignore[assignment]

    manager.disconnect("c1", old)  # type: ignore[arg-type]

    assert manager._connections["c1"] is new


def test_broadcast_delivers_to_all_connected_clients() -> None:
    manager = ConnectionManager()
    first, second = _FakeWebSocket(), _FakeWebSocket()
    manager._connections.update({"c1": first, "c2": second})  # type: ignore[arg-type]

    asyncio.run(manager.broadcast(_event()))

    assert len(first.sent) == 1 and len(second.sent) == 1
