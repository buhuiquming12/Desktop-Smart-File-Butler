"""P2 补测试：删除审批的通过 / 拒绝两条路径。"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, List

import pytest

from app import db
from app.agent import graph as graph_module
from app.agent.state import PlanOutput, PlanStep, ReflectionOutput
from app.config import get_settings
from app.tools import filesystem


class _DeletePlanner:
    def __init__(self, target: Path) -> None:
        self.target = target

    def invoke(self, messages: List[Any]) -> PlanOutput:
        if json.loads(messages[-1].content).get("已获得观察"):
            return PlanOutput(goal="done", steps=[], user_message="完成")
        return PlanOutput(goal="删除", steps=[
            PlanStep(id="d1", description="删除文件", tool="delete_file",
                     args={"path": str(self.target)})
        ], user_message="")


class _DoneReflector:
    def invoke(self, messages: List[Any]) -> ReflectionOutput:
        if json.loads(messages[-1].content).get("是否还有步骤"):
            return ReflectionOutput(decision="continue", reasoning="继续")
        return ReflectionOutput(decision="done", reasoning="done", final_response="完成")


def _llm(target: Path):
    class _LLM:
        def with_structured_output(self, schema: Any) -> Any:
            return _DeletePlanner(target) if schema is PlanOutput else _DoneReflector()
        def invoke(self, _: Any) -> Any:  # pragma: no cover
            raise AssertionError
    return _LLM()


@pytest.fixture()
def runtime_target(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SANDBOX_ROOTS", str(tmp_path))
    monkeypatch.setenv("DB_PATH", str(tmp_path / "butler.db"))
    monkeypatch.setenv("CHROMA_DIR", str(tmp_path / "chroma"))
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    get_settings.cache_clear()
    db._initialized = False
    db.init_db()
    target = tmp_path / "victim.txt"
    target.write_text("data", encoding="utf-8")
    monkeypatch.setattr(graph_module, "build_llm", lambda temperature=0.1: _llm(target))
    yield graph_module.AgentRuntime(), target
    get_settings.cache_clear()
    db._initialized = False


def test_delete_requires_approval(runtime_target) -> None:
    runtime, target = runtime_target
    for _ in runtime.start_stream("删掉它", "del-1"):
        pass
    state = runtime.state("del-1")
    assert state.get("status") == "waiting_approval"
    assert (state.get("pending_approval") or {}).get("action") == "delete"
    assert target.exists()  # 审批前不动


def test_delete_approved_moves_to_trash(runtime_target) -> None:
    runtime, target = runtime_target
    for _ in runtime.start_stream("删掉它", "del-2"):
        pass
    for _ in runtime.resume_stream("del-2", "approve"):
        pass
    assert not target.exists()  # 原位已移除
    # 进了回收站（可撤销），并记为成功删除
    trash = target.parent / filesystem.TRASH_DIRNAME / target.name
    assert trash.exists()
    assert any(op.action == "delete" and op.status == "ok" for op in db.recent_operations(50))


def test_delete_rejected_keeps_file(runtime_target) -> None:
    runtime, target = runtime_target
    for _ in runtime.start_stream("删掉它", "del-3"):
        pass
    for _ in runtime.resume_stream("del-3", "reject"):
        pass
    assert target.exists()  # 文件仍在
    assert any(op.action == "delete" and op.status == "rejected" for op in db.recent_operations(50))
