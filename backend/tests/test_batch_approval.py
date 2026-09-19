"""P1-2 回归：批量 move/rename 超阈值进入审批；拒绝后文件一个都不动。"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, List

import pytest

from app import db
from app.agent import graph as graph_module
from app.agent.state import PlanOutput, PlanStep, ReflectionOutput
from app.config import get_settings

BATCH_N = 25  # > 默认阈值 20


class _MovePlanner:
    """初次规划返回 count 个绝对路径 move 步骤；重规划返回空（完成）。"""

    def __init__(self, root: Path, count: int, dest: str) -> None:
        self.root, self.count, self.dest = root, count, dest

    def invoke(self, messages: List[Any]) -> PlanOutput:
        if json.loads(messages[-1].content).get("已获得观察"):
            return PlanOutput(goal="done", steps=[], user_message="完成")
        steps = [
            PlanStep(id=f"m{i}", description=f"移动 f{i}", tool="move_file",
                     args={"src": str(self.root / f"f{i}.txt"),
                           "dest_dir": str(self.root / self.dest)})
            for i in range(self.count)
        ]
        return PlanOutput(goal="批量移动", steps=steps, user_message="")


class _ContinueReflector:
    def invoke(self, messages: List[Any]) -> ReflectionOutput:
        if json.loads(messages[-1].content).get("是否还有步骤"):
            return ReflectionOutput(decision="continue", reasoning="继续")
        return ReflectionOutput(decision="done", reasoning="结束", final_response="done")


def _make_llm(root: Path, count: int, dest: str = "archive"):
    class _LLM:
        def with_structured_output(self, schema: Any) -> Any:
            return _MovePlanner(root, count, dest) if schema is PlanOutput else _ContinueReflector()

        def invoke(self, _: Any) -> Any:  # pragma: no cover
            raise AssertionError("不应直接调用 llm.invoke")

    return _LLM()


def _setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, count: int, dest: str = "archive"):
    monkeypatch.setenv("SANDBOX_ROOTS", str(tmp_path))
    monkeypatch.setenv("DB_PATH", str(tmp_path / "butler.db"))
    monkeypatch.setenv("CHROMA_DIR", str(tmp_path / "chroma"))
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    get_settings.cache_clear()
    db._initialized = False
    db.init_db()
    for i in range(count):
        (tmp_path / f"f{i}.txt").write_text(str(i), encoding="utf-8")
    monkeypatch.setattr(graph_module, "build_llm",
                        lambda temperature=0.1: _make_llm(tmp_path, count, dest))
    return graph_module.AgentRuntime()


@pytest.fixture()
def runtime_and_sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    runtime = _setup(tmp_path, monkeypatch, BATCH_N)
    yield runtime, tmp_path
    get_settings.cache_clear()
    db._initialized = False


def _run(runtime, thread_id: str) -> None:
    for _ in runtime.start_stream("把所有文件归档", thread_id):
        pass


def test_batch_move_triggers_approval(runtime_and_sandbox) -> None:
    runtime, _ = runtime_and_sandbox
    _run(runtime, "batch-1")
    state = runtime.state("batch-1")
    assert state.get("status") == "waiting_approval"
    pending = state.get("pending_approval") or {}
    assert pending.get("action") == "batch_move"
    assert pending.get("count") == BATCH_N
    assert pending.get("batch") is True


def test_batch_reject_moves_nothing(runtime_and_sandbox) -> None:
    runtime, sandbox = runtime_and_sandbox
    _run(runtime, "batch-2")
    for _ in runtime.resume_stream("batch-2", "reject"):
        pass
    for i in range(BATCH_N):
        assert (sandbox / f"f{i}.txt").exists()
    archive = sandbox / "archive"
    assert (list(archive.iterdir()) if archive.exists() else []) == []
    assert not any(op.action == "move" and op.status == "ok" for op in db.recent_operations(200))


def test_batch_approve_moves_all(runtime_and_sandbox) -> None:
    runtime, sandbox = runtime_and_sandbox
    _run(runtime, "batch-3")
    for _ in runtime.resume_stream("batch-3", "approve"):
        pass
    state = runtime.state("batch-3")
    assert state.get("status") == "completed", state.get("error")
    for i in range(BATCH_N):
        assert not (sandbox / f"f{i}.txt").exists()
    assert len(list((sandbox / "archive").iterdir())) == BATCH_N


def test_small_batch_no_approval(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = _setup(tmp_path, monkeypatch, 3, dest="out")
    for _ in runtime.start_stream("移动三个文件", "small-1"):
        pass
    state = runtime.state("small-1")
    assert state.get("status") == "completed", state.get("error")
    assert len(list((tmp_path / "out").iterdir())) == 3
    get_settings.cache_clear()
    db._initialized = False
