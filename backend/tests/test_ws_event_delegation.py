"""B5 回归：``_emit_update`` / ``_emit_token`` / ``_dispatch`` 的行为契约。

历史缺陷：三者在委派给 ``app/api/events.py`` 之后仍留着整段旧实现，只靠一个
``return`` 隔着（约 105 行不可达代码）。删除死代码本身不改变行为，因此这里锁定
的是**对外可观测语义**：委派后的路由、过滤与无客户端时的静默，都必须与
``api/events.py`` 保持一致，避免两处实现再次分叉。
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest

from app import db, main
from app.config import get_settings
from app.models import WSEvent


class _Recorder:
    """替代真实 ConnectionManager：只记录下发的事件，不触碰网络。"""

    def __init__(self) -> None:
        self.sent: List[Tuple[str, WSEvent]] = []

    async def send(self, client_id: str, event: WSEvent) -> None:
        self.sent.append((client_id, event))

    def types(self) -> List[str]:
        return [event.type for _, event in self.sent]


class _Runtime:
    def __init__(self, state: Dict[str, Any] | None = None) -> None:
        self._state = state or {}

    def state(self, _thread_id: str) -> Dict[str, Any]:
        return self._state


class _Chunk:
    """模拟 LLM 流式 chunk；结构化输出时 content 为空串。"""

    def __init__(self, content: Any) -> None:
        self.content = content


@pytest.fixture()
def recorder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Recorder:
    monkeypatch.setenv("SANDBOX_ROOTS", str(tmp_path))
    monkeypatch.setenv("DB_PATH", str(tmp_path / "butler.db"))
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    get_settings.cache_clear()
    db._initialized = False
    db.init_db()
    rec = _Recorder()
    monkeypatch.setattr(main, "connections", rec)
    yield rec
    get_settings.cache_clear()
    db._initialized = False


def test_dispatch_routes_updates_items(recorder: _Recorder, monkeypatch: pytest.MonkeyPatch) -> None:
    """updates 模式 → node 事件，且带上节点名。"""
    monkeypatch.setattr(main, "get_runtime", lambda: _Runtime())
    asyncio.run(main._dispatch("c1", "t1", ("updates", {"planning": {"status": "running"}})))
    assert recorder.types() == ["node"]
    assert recorder.sent[0][1].payload["node"] == "planning"


def test_dispatch_routes_messages_items(recorder: _Recorder) -> None:
    """messages 模式 → token 事件，载荷为 chunk 文本。"""
    asyncio.run(main._dispatch("c1", "t1", ("messages", _Chunk("片段"))))
    assert recorder.types() == ["token"]
    assert recorder.sent[0][1].payload["token"] == "片段"


def test_dispatch_filters_empty_token_content(recorder: _Recorder) -> None:
    """结构化输出的 chunk.content 为空，不得下发空 token（P1-4）。"""
    asyncio.run(main._dispatch("c1", "t1", ("messages", _Chunk(""))))
    assert recorder.sent == []


def test_dispatch_emits_approval_on_interrupt(recorder: _Recorder, monkeypatch: pytest.MonkeyPatch) -> None:
    """__interrupt__ 更新 → approval_required，载荷取自 state.pending_approval。"""
    monkeypatch.setattr(
        main, "get_runtime", lambda: _Runtime({"pending_approval": {"approval_id": "ap-9", "action": "delete"}})
    )
    asyncio.run(main._dispatch("c1", "t1", ("updates", {"__interrupt__": []})))
    assert recorder.types() == ["approval_required"]
    assert recorder.sent[0][1].payload["approval_id"] == "ap-9"


def test_dispatch_emits_tool_and_task_events(recorder: _Recorder, monkeypatch: pytest.MonkeyPatch) -> None:
    """act 节点的观测结果 → tool_call 与 task 两个事件（前端任务面板依赖）。"""
    monkeypatch.setattr(main, "get_runtime", lambda: _Runtime())
    update = {
        "act": {
            "current_step": {"id": "s1", "tool": "move_file", "description": "移动文件", "args": {}},
            "observations": [{"step_id": "s1", "tool": "move_file", "status": "ok", "result": "ok"}],
        }
    }
    asyncio.run(main._dispatch("c1", "t1", ("updates", update)))
    assert recorder.types() == ["node", "tool_call", "tool_result", "task"]
    assert recorder.sent[-1][1].payload["status"] == "success"


def test_emit_update_without_client_is_noop(recorder: _Recorder, monkeypatch: pytest.MonkeyPatch) -> None:
    """REST 直调没有 WS 客户端：委派后仍不得广播。"""
    monkeypatch.setattr(main, "get_runtime", lambda: _Runtime())
    asyncio.run(main._emit_update(None, "t1", {"planning": {"status": "running"}}))
    asyncio.run(main._emit_token(None, "t1", _Chunk("x")))
    assert recorder.sent == []
