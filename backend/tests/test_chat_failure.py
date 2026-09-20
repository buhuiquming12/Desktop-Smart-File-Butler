"""B1 回归：任何失败路径都必须广播终态事件，前端不得永久停在 busy。

历史缺陷：``get_runtime()`` / ``start_stream()`` 位于 ``_run_stream`` 的异常捕获
之外，模型未配置时 ``AgentRuntime`` 构造即抛错，异常被 ``_track`` 静默吞掉，
前端永远等不到 done/error 事件，界面卡死在“处理中”。

同时锁定事件语义：终态统一为 ``done``（status=completed|failed|cancelled），
``error`` 类型只用于“审批过期”这类非终态诊断。
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Tuple

import pytest

from app import main
from app.models import ChatRequest, WSEvent


class _RecordingConnections:
    """替代真实 ConnectionManager，记录下发的事件而不触碰网络。"""

    def __init__(self) -> None:
        self.sent: List[Tuple[str, WSEvent]] = []

    async def send(self, client_id: str, event: WSEvent) -> None:
        self.sent.append((client_id, event))

    def events_for(self, client_id: str) -> List[WSEvent]:
        return [event for cid, event in self.sent if cid == client_id]


@pytest.fixture()
def recorder(monkeypatch: pytest.MonkeyPatch) -> _RecordingConnections:
    rec = _RecordingConnections()
    monkeypatch.setattr(main, "connections", rec)
    main._cancel_requested.clear()
    return rec


def _terminal(recorder: _RecordingConnections, client_id: str = "client-1") -> WSEvent:
    """取最后一条事件并断言它是终态 done，避免断言前的中间事件干扰。"""
    events = recorder.events_for(client_id)
    assert events, "没有任何事件下发，前端会永久 busy"
    last = events[-1]
    assert last.type == "done", f"终态事件应为 done，实际为 {last.type}"
    return last


def test_start_chat_broadcasts_terminal_failure_when_runtime_unavailable(
    recorder: _RecordingConnections, monkeypatch: pytest.MonkeyPatch
) -> None:
    """模型未配置 → AgentRuntime 构造抛错 → 必须发出 done(status=failed)。"""

    class _UnconfiguredRuntime:
        def __init__(self) -> None:
            raise RuntimeError("provider=openai 但未配置 API Key（请在设置界面或 .env 中填写）")

    monkeypatch.setattr(main, "get_runtime", lambda: _UnconfiguredRuntime())

    request = ChatRequest(message="整理一下下载目录", thread_id="b1-no-llm", client_id="client-1")
    asyncio.run(main._start_chat(request))

    event = _terminal(recorder)
    assert event.thread_id == "b1-no-llm"
    assert event.payload["status"] == "failed"
    # 错误文案须对用户可读且可审计：保留原始异常中面向用户的说明。
    assert "API Key" in event.payload["message"]
    assert "API Key" in event.payload["error"]


def test_start_chat_broadcasts_terminal_failure_when_stream_construction_fails(
    recorder: _RecordingConnections, monkeypatch: pytest.MonkeyPatch
) -> None:
    """运行时能构造但 start_stream 本身抛错时同样必须收尾。"""

    class _BrokenStreamRuntime:
        def start_stream(self, _message: str, _thread_id: str) -> Any:
            raise ValueError("checkpointer 不可用")

        def state(self, _thread_id: str) -> Dict[str, Any]:
            return {}

    monkeypatch.setattr(main, "get_runtime", lambda: _BrokenStreamRuntime())

    request = ChatRequest(message="整理一下", thread_id="b1-broken", client_id="client-1")
    asyncio.run(main._start_chat(request))

    event = _terminal(recorder)
    assert event.payload["status"] == "failed"
    assert "checkpointer 不可用" in event.payload["error"]


def test_run_stream_maps_failed_state_to_done(
    recorder: _RecordingConnections, monkeypatch: pytest.MonkeyPatch
) -> None:
    """图内失败（state.status=failed）也走 done 终态，不再发 error 诊断事件。"""

    class _FailedRuntime:
        def state(self, _thread_id: str) -> Dict[str, Any]:
            return {
                "status": "failed",
                "error": "模型超时",
                "final_response": "规划失败：模型超时",
            }

    monkeypatch.setattr(main, "get_runtime", lambda: _FailedRuntime())

    state = asyncio.run(main._run_stream(iter(()), "b1-in-graph", "client-1"))

    assert state["status"] == "failed"
    events = recorder.events_for("client-1")
    assert len(events) == 1, "图内失败应只发一条终态事件"
    assert events[0].type == "done"
    assert events[0].payload["status"] == "failed"
    assert events[0].payload["message"] == "规划失败：模型超时"


def test_run_stream_exception_broadcasts_done(
    recorder: _RecordingConnections, monkeypatch: pytest.MonkeyPatch
) -> None:
    """流式迭代中途抛错也必须收尾（此前只发 error，现统一为 done）。"""

    class _Runtime:
        def state(self, _thread_id: str) -> Dict[str, Any]:
            return {"status": "running"}

    monkeypatch.setattr(main, "get_runtime", lambda: _Runtime())

    def _exploding_stream():
        yield ("updates", {})
        raise RuntimeError("流式执行中断")

    state = asyncio.run(main._run_stream(_exploding_stream(), "b1-explode", "client-1"))

    assert state["status"] == "failed"
    event = _terminal(recorder)
    assert event.payload["status"] == "failed"
    assert "流式执行中断" in event.payload["message"]


def test_start_chat_without_client_emits_nothing(
    recorder: _RecordingConnections, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REST 直调没有 WS 客户端：不得因广播失败而抛出（调用方改为轮询会话状态）。"""

    class _UnconfiguredRuntime:
        def __init__(self) -> None:
            raise RuntimeError("provider=openai 但未配置 API Key")

    monkeypatch.setattr(main, "get_runtime", lambda: _UnconfiguredRuntime())

    request = ChatRequest(message="整理一下", thread_id="b1-rest-only")
    assert asyncio.run(main._start_chat(request)) == "b1-rest-only"
    assert recorder.sent == []
