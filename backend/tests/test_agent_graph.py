"""Agent 图结构与递归预算测试（使用假模型，不调用外部 API）。"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, List

import pytest

from app import db
from app.agent import graph as graph_module
from app.agent.state import PlanOutput, PlanStep, ReflectionOutput
from app.config import get_settings


def _parse_context(messages: List[Any]) -> dict:
    return json.loads(messages[-1].content)


class _ScriptedPlanner:
    """初次规划返回 30 步，重规划（已有观察）返回 10 步。"""

    def invoke(self, messages: List[Any]) -> PlanOutput:
        context = _parse_context(messages)
        observations = context.get("已获得观察") or []
        if not observations:
            steps = [
                PlanStep(id=f"s{i}", description=f"步骤{i}", tool="set_preference",
                         args={"key": f"k{i}", "value": "v"})
                for i in range(30)
            ]
        else:
            steps = [
                PlanStep(id=f"r{i}", description=f"重规划{i}", tool="set_preference",
                         args={"key": f"rk{i}", "value": "v"})
                for i in range(10)
            ]
        return PlanOutput(goal="test", steps=steps, user_message="")


class _ScriptedReflector:
    """当前计划还有步骤则 continue；执行完后第一次 replan，重规划后 done。"""

    def invoke(self, messages: List[Any]) -> ReflectionOutput:
        context = _parse_context(messages)
        if context.get("是否还有步骤"):
            return ReflectionOutput(decision="continue", reasoning="继续")
        if context.get("已重规划次数", 0) == 0:
            return ReflectionOutput(decision="replan", reasoning="需要据观察重规划")
        return ReflectionOutput(decision="done", reasoning="完成", final_response="全部完成")


class _ScriptedLLM:
    def with_structured_output(self, schema: Any) -> Any:
        if schema is PlanOutput:
            return _ScriptedPlanner()
        return _ScriptedReflector()

    def invoke(self, _: Any) -> Any:  # pragma: no cover - 本测试不触发 write_summary
        raise AssertionError("不应直接调用 llm.invoke")


@pytest.fixture()
def configured_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> graph_module.AgentRuntime:
    monkeypatch.setenv("SANDBOX_ROOTS", str(tmp_path))
    monkeypatch.setenv("DB_PATH", str(tmp_path / "butler.db"))
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    get_settings.cache_clear()
    db._initialized = False
    db.init_db()
    monkeypatch.setattr(graph_module, "build_llm", lambda temperature=0.1: _ScriptedLLM())
    runtime = graph_module.AgentRuntime()
    yield runtime
    get_settings.cache_clear()
    db._initialized = False


def test_agent_graph_compiles(configured_runtime: graph_module.AgentRuntime) -> None:
    assert configured_runtime.graph is not None
    graph = configured_runtime.graph.get_graph()
    nodes = set(graph.nodes)
    assert {"perceive", "planning", "act", "approval", "reflecting"}.issubset(nodes)
    # P0-3：空的 observe 节点已移除，act 直连 reflecting。
    assert "observe" not in nodes


def test_recursion_limit_covers_max_plan_and_replans() -> None:
    # 旧的固定 120 在 30 步 + 一次重规划时会踩线；派生上限须留足余量。
    assert graph_module._recursion_limit() > 120


def test_thirty_step_plan_with_replan_completes(
    configured_runtime: graph_module.AgentRuntime,
) -> None:
    """P0-3 验收：30 步初计划 + 1 次 10 步重规划能跑完，不抛 GraphRecursionError。"""
    thread_id = "recursion-test"
    for _ in configured_runtime.start_stream("整理测试目录", thread_id):
        pass
    state = configured_runtime.state(thread_id)
    assert state.get("status") == "completed", state.get("error")
    # 30 步 + 10 步重规划，观察持续累积，共 40 条。
    assert len(state.get("observations", [])) == 40
