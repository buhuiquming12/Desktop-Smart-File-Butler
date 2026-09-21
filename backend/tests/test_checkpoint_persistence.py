"""P1-6 回归：checkpoint 持久化到 SQLite，重启（新建 runtime）后待审批不丢。"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, List

import pytest

from app import db
from app.agent import graph as graph_module
from app.agent.state import PlanOutput, PlanStep, ReflectionOutput
from app.config import get_settings

BATCH_N = 25


class _MovePlanner:
    def __init__(self, root: Path) -> None:
        self.root = root

    def invoke(self, messages: List[Any]) -> PlanOutput:
        if json.loads(messages[-1].content).get("已获得观察"):
            return PlanOutput(goal="done", steps=[], user_message="完成")
        return PlanOutput(goal="批量", steps=[
            PlanStep(id=f"m{i}", description="移动", tool="move_file",
                     args={"src": str(self.root / f"f{i}.txt"), "dest_dir": str(self.root / "arch")})
            for i in range(BATCH_N)
        ], user_message="")


class _Reflector:
    def invoke(self, messages: List[Any]) -> ReflectionOutput:
        if json.loads(messages[-1].content).get("是否还有步骤"):
            return ReflectionOutput(decision="continue", reasoning="继续")
        return ReflectionOutput(decision="done", reasoning="done", final_response="done")


def _llm(root: Path):
    class _LLM:
        def with_structured_output(self, schema: Any) -> Any:
            return _MovePlanner(root) if schema is PlanOutput else _Reflector()
        def invoke(self, _: Any) -> Any:  # pragma: no cover
            raise AssertionError
    return _LLM()


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SANDBOX_ROOTS", str(tmp_path))
    monkeypatch.setenv("DB_PATH", str(tmp_path / "butler.db"))
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    get_settings.cache_clear()
    db._initialized = False
    db.init_db()
    for i in range(BATCH_N):
        (tmp_path / f"f{i}.txt").write_text(str(i), encoding="utf-8")
    monkeypatch.setattr(graph_module, "build_llm", lambda temperature=0.1: _llm(tmp_path))
    yield tmp_path
    get_settings.cache_clear()
    db._initialized = False


def test_pending_approval_survives_new_runtime(env: Path) -> None:
    sandbox = env
    thread_id = "persist-1"

    # runtime1：跑到批量审批处暂停
    runtime1 = graph_module.AgentRuntime()
    for _ in runtime1.start_stream("归档", thread_id):
        pass
    assert runtime1.state(thread_id).get("status") == "waiting_approval"

    # 模拟重启：全新 runtime 读同一个 checkpoints.sqlite
    runtime2 = graph_module.AgentRuntime()
    state = runtime2.state(thread_id)
    assert state.get("status") == "waiting_approval"
    assert (state.get("pending_approval") or {}).get("action") == "batch_move"

    # 新 runtime 能恢复并拒绝，文件一个都没动
    for _ in runtime2.resume_stream(thread_id, "reject"):
        pass
    for i in range(BATCH_N):
        assert (sandbox / f"f{i}.txt").exists()


def test_checkpoint_file_created(env: Path) -> None:
    graph_module.AgentRuntime()
    assert (env / "checkpoints.sqlite").exists()


def test_cancel_is_persisted_and_clears_pending_approval(env: Path) -> None:
    thread_id = "persist-cancel"
    runtime1 = graph_module.AgentRuntime()
    for _ in runtime1.start_stream("归档", thread_id):
        pass
    assert runtime1.state(thread_id).get("status") == "waiting_approval"

    runtime1.cancel(thread_id)
    cancelled = runtime1.state(thread_id)
    assert cancelled.get("status") == "cancelled"
    assert cancelled.get("pending_approval") is None

    runtime2 = graph_module.AgentRuntime()
    persisted = runtime2.state(thread_id)
    assert persisted.get("status") == "cancelled"
    assert persisted.get("pending_approval") is None
