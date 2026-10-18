"""固定场景总表（与 docs/AGENT_SCENARIOS.md 一一对应）。

本文件只补充此前分散测试没有端到端覆盖的三类场景：正常任务的确定性验证、
重规划上限的真实图行为、以及取消在步骤边界生效且不产生后台写入。
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, List

import pytest

from app import db, main
from app.agent import graph as graph_module
from app.agent.state import PlanOutput, PlanStep, ReflectionOutput
from app.config import get_settings


@pytest.fixture()
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("SANDBOX_ROOTS", str(tmp_path))
    monkeypatch.setenv("DB_PATH", str(tmp_path / "butler.db"))
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    get_settings.cache_clear()
    db._initialized = False
    db.init_db()
    yield tmp_path
    get_settings.cache_clear()
    db._initialized = False
    main._cancel_requested.clear()


def test_scenario_normal_move_is_verified(
    sandbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """场景 1：正常任务。移动一个文件，完成后必须能验证目标路径存在。"""
    source = sandbox / "report.pdf"
    source.write_text("pdf", encoding="utf-8")
    dest = sandbox / "archive"

    class _Planner:
        def invoke(self, messages: List[Any]) -> PlanOutput:
            if json.loads(messages[-1].content).get("tool_observations"):
                return PlanOutput(goal="完成", steps=[], user_message="完成")
            return PlanOutput(goal="归档", steps=[PlanStep(
                id="mv", description="归档 report.pdf", tool="move_file",
                args={"src": str(source), "dest_dir": str(dest)},
            )], user_message="")

    class _Reflector:
        def invoke(self, messages: List[Any]) -> ReflectionOutput:
            if json.loads(messages[-1].content).get("是否还有步骤"):
                return ReflectionOutput(decision="continue", reasoning="继续")
            return ReflectionOutput(decision="done", reasoning="完成", final_response="已归档")

    class _LLM:
        def with_structured_output(self, schema: Any) -> Any:
            return _Planner() if schema is PlanOutput else _Reflector()

        def invoke(self, _: Any) -> Any:  # pragma: no cover
            raise AssertionError

    monkeypatch.setattr(graph_module, "build_llm", lambda temperature=0.1: _LLM())
    runtime = graph_module.AgentRuntime()
    for _ in runtime.start_stream("把 report.pdf 归档到 archive", "scenario-normal"):
        pass

    state = runtime.state("scenario-normal")
    assert state.get("status") == "completed"
    assert state.get("outcome") == "success"
    assert state.get("stop_reason") == "goal_verified"
    assert state["completion"]["verified"] is True
    assert (dest / "report.pdf").exists()
    assert not source.exists()
    # 校验只读：操作日志里不应出现额外的 write/summary 记录。
    actions = [op.action for op in db.recent_operations(20)]
    assert actions.count("move") == 1


def test_scenario_replan_limit_is_reported_end_to_end(
    sandbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """场景 17：反思器反复要求重规划，必须在 3 次后停止并说明未完成内容。"""
    source = sandbox / "a.txt"
    source.write_text("x", encoding="utf-8")

    class _Planner:
        def invoke(self, messages: List[Any]) -> PlanOutput:
            context = json.loads(messages[-1].content)
            if context.get("tool_observations"):
                return PlanOutput(goal="继续", steps=[PlanStep(
                    id="again", description="再来一次", tool="scan_directory",
                    args={"directory": str(sandbox)},
                )], user_message="")
            return PlanOutput(goal="扫描", steps=[PlanStep(
                id="scan", description="扫描", tool="scan_directory",
                args={"directory": str(sandbox)},
            )], user_message="")

    class _AlwaysReplan:
        def invoke(self, _: List[Any]) -> ReflectionOutput:
            return ReflectionOutput(decision="replan", reasoning="还想继续")

    class _LLM:
        def with_structured_output(self, schema: Any) -> Any:
            return _Planner() if schema is PlanOutput else _AlwaysReplan()

        def invoke(self, _: Any) -> Any:  # pragma: no cover
            raise AssertionError

    monkeypatch.setattr(graph_module, "build_llm", lambda temperature=0.1: _LLM())
    runtime = graph_module.AgentRuntime()
    for _ in runtime.start_stream("整理目录", "scenario-replan"):
        pass

    state = runtime.state("scenario-replan")
    assert state.get("outcome") == "budget_exhausted"
    assert state.get("stop_reason") == "replan_limit"
    assert state.get("replan_count", 0) >= 3
    message = state.get("final_response", "")
    assert "预算" in message or "重规划" in message
    assert state["completion"]["replan_limit"]["reached"] is True


def test_scenario_cancel_takes_effect_at_step_boundary(
    sandbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """场景 13：取消后不得再产生文件变更。"""
    source = sandbox / "a.txt"
    source.write_text("x", encoding="utf-8")
    dest = sandbox / "out"

    class _Planner:
        def invoke(self, _: List[Any]) -> PlanOutput:
            return PlanOutput(goal="移动", steps=[PlanStep(
                id="mv", description="移动", tool="move_file",
                args={"src": str(source), "dest_dir": str(dest)},
            )], user_message="")

    class _Reflector:
        def invoke(self, _: List[Any]) -> ReflectionOutput:
            return ReflectionOutput(decision="done", reasoning="完成")

    class _LLM:
        def with_structured_output(self, schema: Any) -> Any:
            return _Planner() if schema is PlanOutput else _Reflector()

        def invoke(self, _: Any) -> Any:  # pragma: no cover
            raise AssertionError

    monkeypatch.setattr(graph_module, "build_llm", lambda temperature=0.1: _LLM())
    runtime = graph_module.AgentRuntime()
    thread_id = "scenario-cancel"
    main._cancel_requested.add(thread_id)
    iterator = runtime.start_stream("把 a.txt 移动到 out", thread_id)

    monkeypatch.setattr(main, "get_runtime", lambda: runtime)
    asyncio.run(main._run_stream(iterator, thread_id, None, runtime=runtime))

    assert source.exists(), "取消后文件仍应留在原位"
    assert not dest.exists() or not any(dest.iterdir())
    state = runtime.state(thread_id)
    assert state.get("status") == "cancelled"
    assert state.get("outcome") == "cancelled"
