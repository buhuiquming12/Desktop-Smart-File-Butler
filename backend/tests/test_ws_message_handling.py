"""B6 回归：WebSocket 消息循环的健壮性。

历史缺陷：``except HTTPException`` 里读的是 ``message`` 变量，而 ``message``
在 ``json.loads`` 之后才被赋值。实测该分支当前不可触发（进入 HTTPException 分支
必须先通过 ``json.loads``，故 ``message`` 必然已绑定），属于潜在脆弱点；改用
每轮显式重置的 ``thread_id`` 后，该分支不再依赖解析结果。

同一次排查发现的真实可达缺陷：合法 JSON 但不是对象（如 ``123``、``[1,2]``）时
``message.get`` 抛 ``AttributeError``，两个内层 except 都接不住，一路冒到外层
``except Exception`` 直接断开连接 —— 前端收不到任何错误事件，只看到断线重连。

设计约束：断言必须**快速失败**，不得依赖阻塞式 ``receive_json`` —— 连接一旦被判
死，读取会永久挂起测试。因此这里只记录 ``send``，用「后续消息是否仍被处理」来
证明循环存活。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import db, main
from app.config import get_settings
from app.models import WSEvent

GOOD = main.get_session_token()


class _RecordingManager:
    """只记录 send，其余委托真实 ConnectionManager。

    connect 必须委托：真正 accept 连接的是它；send 不委托，避免把事件写进没人读的
    套接字缓冲区。liveness 改由业务信号证明（见 _send_noise）。
    """

    def __init__(self, real: Any) -> None:
        self.real = real
        self.sent: List[Tuple[str, WSEvent]] = []

    async def connect(self, client_id: str, websocket: Any) -> None:
        await self.real.connect(client_id, websocket)

    def disconnect(self, client_id: str, websocket: Any) -> None:
        self.real.disconnect(client_id, websocket)

    async def send(self, client_id: str, event: WSEvent) -> None:
        self.sent.append((client_id, event))

    def payloads(self, event_type: str) -> List[Dict[str, Any]]:
        return [event.payload for _, event in self.sent if event.type == event_type]

    def events(self, event_type: str) -> List[WSEvent]:
        return [event for _, event in self.sent if event.type == event_type]

    def types(self) -> List[str]:
        return [event.type for _, event in self.sent]


class _FakeRuntime:
    """state() 可切换为抛 HTTPException，用于验证 WS 循环的异常分支。"""

    def __init__(self, *, raises: bool = False) -> None:
        self.raises = raises

    def state(self, _thread_id: str) -> Dict[str, Any]:
        if self.raises:
            raise HTTPException(status_code=400, detail="会话状态不可用")
        return {}


def _send_noise(ws: Any) -> None:
    """再发一条已知消息，作为「循环仍在运行」的证据。

    若连接已被异常断开，这条消息不会得到任何处理，后续断言随即失败 —— 不需要
    阻塞式读取，测试因此永远不会挂起。
    """
    ws.send_text('{"type": "__liveness__"}')


@pytest.fixture()
def ws_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("SANDBOX_ROOTS", str(tmp_path))
    monkeypatch.setenv("DB_PATH", str(tmp_path / "butler.db"))
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    get_settings.cache_clear()
    db._initialized = False
    db.init_db()
    recorder = _RecordingManager(main.connections)
    monkeypatch.setattr(main, "connections", recorder)
    yield TestClient(main.app), recorder
    get_settings.cache_clear()
    db._initialized = False


def _connect(client: TestClient, client_id: str) -> Any:
    return client.websocket_connect(f"/ws/{client_id}?token={GOOD}")


def _errors(recorder: _RecordingManager) -> List[WSEvent]:
    """错误事件列表；取事件本身而非 payload，因为 thread_id 在事件顶层。"""
    return recorder.events("error")


# ---------------- B6：非法输入不得打断连接 ----------------


def test_invalid_json_reports_error_and_keeps_running(ws_env: Any) -> None:
    """非法 JSON 只报错，循环必须继续（旧代码此处读未绑定的 message → NameError）。"""
    client, recorder = ws_env
    with _connect(client, "b6-bad-json") as ws:
        ws.send_text("这不是 JSON")
        _send_noise(ws)

    errors = _errors(recorder)
    assert len(errors) == 2, "首条错误后循环已退出（连接被断开）"
    assert errors[0].thread_id == ""
    assert "消息格式错误" in errors[0].payload["message"]


def test_non_object_json_reports_error_and_keeps_running(ws_env: Any) -> None:
    """合法 JSON 但不是对象（123 / [1,2]）此前会 AttributeError 断开连接。"""
    client, recorder = ws_env
    with _connect(client, "b6-non-object") as ws:
        for raw in ("123", "[1, 2]", '"str"', "null"):
            ws.send_text(raw)
        _send_noise(ws)

    errors = _errors(recorder)
    assert len(errors) == 5, f"应有 4 条格式错误 + 1 条存活探针，实际 {len(errors)}"
    for event in errors[:4]:
        assert event.thread_id == ""
        assert "JSON 对象" in event.payload["message"]


def test_http_exception_reports_its_own_thread_id(ws_env: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """处理消息时抛 HTTPException：错误事件必须落在该消息的 thread_id 上。"""
    client, recorder = ws_env
    monkeypatch.setattr(main, "get_runtime", lambda: _FakeRuntime(raises=True))

    with _connect(client, "b6-http") as ws:
        ws.send_text('{"type": "approval", "thread_id": "t-http", "approval_id": "a1", "decision": "approve"}')
        _send_noise(ws)

    errors = _errors(recorder)
    assert len(errors) == 2, "HTTPException 之后循环已退出"
    assert errors[0].thread_id == "t-http"
    assert errors[0].payload["message"] == "会话状态不可用"


def test_thread_id_does_not_leak_across_messages(ws_env: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """上一轮的 thread_id 不得残留到下一轮的错误事件里。

    这是旧代码最隐蔽的一处：``message`` 是上一轮循环留下的变量，JSON 解析失败时
    它仍指向旧消息，于是报错被投递到早已结束的会话，前端在错误的会话里看到提示。
    """
    client, recorder = ws_env
    monkeypatch.setattr(main, "get_runtime", lambda: _FakeRuntime(raises=True))

    with _connect(client, "b6-leak") as ws:
        ws.send_text('{"type": "approval", "thread_id": "t-old", "approval_id": "a1", "decision": "approve"}')
        ws.send_text("{ 坏掉的 JSON")
        _send_noise(ws)

    errors = _errors(recorder)
    assert len(errors) == 3, "循环在解析失败后退出了"
    assert errors[0].thread_id == "t-old"
    assert errors[1].thread_id == "", "解析失败时的 thread_id 沿用了上一轮的值"


def test_unknown_message_type_reports_error(ws_env: Any) -> None:
    client, recorder = ws_env
    with _connect(client, "b6-unknown") as ws:
        ws.send_text('{"type": "nope", "thread_id": "t-1"}')

    errors = _errors(recorder)
    assert len(errors) == 1
    assert errors[0].payload["message"] == "未知消息类型"
    assert errors[0].thread_id == ""


def test_handshake_event_still_emitted(ws_env: Any) -> None:
    """连接建立时必须收到 connected 事件，否则前端无法判定链路可用。"""
    client, recorder = ws_env
    with _connect(client, "b6-handshake"):
        pass
    assert recorder.payloads("node") == [{"node": "connected", "status": "ready"}]
