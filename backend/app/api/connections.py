"""WebSocket 客户端连接注册表。"""
from __future__ import annotations

import asyncio
from typing import Dict

from fastapi import WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

from ..logging_conf import get_logger
from ..models import WSEvent

logger = get_logger(__name__)


class ConnectionManager:
    """维护 Renderer WebSocket；发送失败不得中断 Agent 工作流。"""

    def __init__(self) -> None:
        self._connections: Dict[str, WebSocket] = {}

    async def connect(self, client_id: str, websocket: WebSocket) -> None:
        await websocket.accept()
        old = self._connections.get(client_id)
        self._connections[client_id] = websocket
        if old and old is not websocket:
            try:
                await old.close(code=1000, reason="由新连接替换")
            except RuntimeError:
                pass

    def disconnect(self, client_id: str, websocket: WebSocket) -> None:
        if self._connections.get(client_id) is websocket:
            self._connections.pop(client_id, None)

    async def send(self, client_id: str, event: WSEvent) -> None:
        websocket = self._connections.get(client_id)
        if websocket is None:
            return
        if websocket.application_state is not WebSocketState.CONNECTED:
            self.disconnect(client_id, websocket)
            return
        try:
            await websocket.send_json(event.model_dump(mode="json"))
        except (RuntimeError, WebSocketDisconnect):
            logger.debug("推送时发现连接已断开，已清理 client=%s", client_id)
            self.disconnect(client_id, websocket)
        except Exception:  # noqa: BLE001
            logger.exception("推送事件失败 client=%s type=%s", client_id, event.type)
            self.disconnect(client_id, websocket)

    async def broadcast(self, event: WSEvent) -> None:
        client_ids = list(self._connections)
        if client_ids:
            await asyncio.gather(*(self.send(client_id, event) for client_id in client_ids))
