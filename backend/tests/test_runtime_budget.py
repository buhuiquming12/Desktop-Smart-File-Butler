"""P1-5 回归：最小运行追踪与预算。

- 运行编号、节点耗时、模型调用次数、工具调用次数、停止原因。
- 供应商提供 usage 时记录 Token；不提供时标记 unknown，而不是伪造 0。
- 模型调用上限可配置；等待审批/用户输入不计入执行预算。
- 取消在步骤边界生效；不盲目重试可能重复写入的操作。
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any, List

import pytest

from app import db, main
from app.agent import graph as graph_module
from app.agent import tracing
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


class _OneMovePlanner:
    def __init__(self, source: Path, dest: Path) -> None:
        self.source, self.dest = source, dest

    def invoke(self, messages: List[Any]) -> PlanOutput:
        context = json.loads(messages[-1].content)
        if context.get("tool_observations"):
            return PlanOutput(goal="完成", steps=[], user_message="完成")
        return PlanOutput(goal="移动", steps=[PlanStep(
            id="mv", description="移动", tool="move_file",
            args={"src": str(self.source), "dest_dir": str(self.dest)},
        )], user_message="")


class _ContinueReflector:
    def invoke(self, messages: List[Any]) -> ReflectionOutput:
        if json.loads(messages[-1].content).get("是否还有步骤"):
            return ReflectionOutput(decision="continue", reasoning="继续")
        return ReflectionOutput(decision="done", reasoning="完成", final_response="完成")


def _runtime(sandbox: Path, monkeypatch: pytest.MonkeyPatch):
    source = sandbox / "a.txt"
    source.write_text("x", encoding="utf-8")
    dest = sandbox / "out"
    dest.mkdir()

    class _LLM:
        def with_structured_output(self, schema: Any) -> Any:
            return _OneMovePlanner(source, dest) if schema is PlanOutput else _ContinueReflector()

        def invoke(self, _: Any) -> Any:  # pragma: no cover
            raise AssertionError

    monkeypatch.setattr(graph_module, "build_llm", lambda temperature=0.1: _LLM())
    return graph_module.AgentRuntime(), source, dest


def test_run_trace_records_timings_and_calls(
    sandbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, source, dest = _runtime(sandbox, monkeypatch)
    for _ in runtime.start_stream("把 a.txt 移动到 out", "trace-1"):
        pass
    state = runtime.state("trace-1")
    trace = state.get("run_trace") or {}
    assert state.get("run_id")
    assert trace.get("run_id") == state["run_id"]
    assert trace.get("model_calls") == 2  # planning + reflecting
    assert trace.get("tool_calls") == 1
    assert trace.get("execution_ms", 0) > 0
    timed_nodes = {item["node"] for item in trace.get("node_timings", [])}
    assert {"perceive", "planning", "act", "reflecting"}.issubset(timed_nodes)
    assert trace["token_usage"]["source"] == "unknown"
    assert trace["token_usage"]["input"] is None
    assert (dest / "a.txt").exists()


def test_token_usage_recorded_when_provider_reports_it() -> None:
    class _Chunk:
        usage_metadata = {"input_tokens": 12, "output_tokens": 7}

    tracing.clear("usage-1")
    tracing.record("usage-1", _Chunk())
    usage = tracing.usage_for("usage-1")
    assert usage["input"] == 12 and usage["output"] == 7
    assert usage["source"] == "provider"
    # 没有 usage 的普通 chunk 不应把状态改成 0/known。
    tracing.record("usage-1", object())
    assert tracing.usage_for("usage-1")["input"] == 12
    assert tracing.usage_for("no-such-thread")["source"] == "unknown"

    # 运行追踪只在供应商真的提供 usage 时覆盖 unknown。
    state = {"thread_id": "usage-1"}
    trace = graph_module.AgentRuntime._trace_update(object.__new__(graph_module.AgentRuntime), state)
    assert trace["token_usage"]["source"] == "provider"
    assert trace["token_usage"]["input"] == 12
    unknown_trace = graph_module.AgentRuntime._trace_update(
        object.__new__(graph_module.AgentRuntime), {"thread_id": "no-usage"}
    )
    assert unknown_trace["token_usage"]["source"] == "unknown"
    assert unknown_trace["token_usage"]["input"] is None


def test_model_call_limit_stops_with_budget_outcome(
    sandbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, source, dest = _runtime(sandbox, monkeypatch)
    db.set_preference("agent_max_model_calls", "1")
    for _ in runtime.start_stream("把 a.txt 移动到 out", "budget-1"):
        pass
    state = runtime.state("budget-1")
    assert state.get("outcome") == "budget_exhausted"
    assert state.get("stop_reason") == "budget_exhausted"
    assert "模型调用达到上限" in state.get("final_response", "")
    # 已执行的步骤保留、未执行的步骤要说明。
    assert state.get("completion", {}).get("unexecuted_steps") is not None


def test_waiting_for_approval_does_not_consume_budget(
    sandbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, source, dest = _runtime(sandbox, monkeypatch)
    db.set_preference("agent_execution_budget_seconds", "1")
    # 删除需要审批；等待 1.2 秒后再批准，预算按节点耗时累计，不应因等待而耗尽。
    victim = sandbox / "victim.txt"
    victim.write_text("x", encoding="utf-8")

    class _DeletePlanner:
        def invoke(self, messages: List[Any]) -> PlanOutput:
            context = json.loads(messages[-1].content)
            if context.get("tool_observations"):
                return PlanOutput(goal="完成", steps=[], user_message="完成")
            return PlanOutput(goal="删除", steps=[PlanStep(
                id="del", description="删除", tool="delete_file",
                args={"path": str(victim)},
            )], user_message="")

    class _LLM:
        def with_structured_output(self, schema: Any) -> Any:
            return _DeletePlanner() if schema is PlanOutput else _ContinueReflector()

        def invoke(self, _: Any) -> Any:  # pragma: no cover
            raise AssertionError

    monkeypatch.setattr(graph_module, "build_llm", lambda temperature=0.1: _LLM())
    runtime = graph_module.AgentRuntime()
    for _ in runtime.start_stream("删掉 victim.txt", "wait-budget"):
        pass
    assert runtime.state("wait-budget").get("status") == "waiting_approval"
    time.sleep(1.2)
    for _ in runtime.resume_stream("wait-budget", "approve"):
        pass
    state = runtime.state("wait-budget")
    assert state.get("outcome") != "budget_exhausted", state.get("final_response")
    assert not victim.exists()


def test_cancel_marks_outcome_and_stops_further_steps(
    sandbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, source, dest = _runtime(sandbox, monkeypatch)
    for _ in runtime.start_stream("把 a.txt 移动到 out", "cancel-1"):
        pass
    runtime.cancel("cancel-1")
    state = runtime.state("cancel-1")
    assert state.get("status") == "cancelled"
    assert state.get("outcome") == "cancelled"
    assert state.get("stop_reason") == "cancelled"
    assert "撤销" in state.get("final_response", "")


def test_failed_write_is_not_retried_automatically(
    sandbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """一次失败的工具调用只执行一次：自动重试可能造成重复写入。"""
    calls: List[str] = []

    def _boom(src: str, dest_dir: str, new_name: Any = None) -> str:
        calls.append(src)
        raise OSError("模拟写入失败")

    monkeypatch.setattr("app.tools.filesystem.move_file", _boom)
    from app.agent.tool_executor import ToolExecutor

    result = ToolExecutor().execute({
        "id": "mv", "description": "移动", "tool": "move_file",
        "args": {"src": str(sandbox / "a.txt"), "dest_dir": str(sandbox / "out")},
    })
    assert result["status"] == "failed"
    assert len(calls) == 1, f"失败操作被重试了 {len(calls)} 次"


def test_done_event_carries_outcome_and_separate_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """终态事件必须带 outcome / 停止原因 / 文件级计数，前端才能区别呈现。"""
    state = {
        "thread_id": "done-1",
        "status": "completed",
        "outcome": "partial",
        "stop_reason": "tool_failed",
        "run_id": "run-1",
        "final_response": "任务部分完成：成功 8、失败 2",
        "observations": [{
            "step_id": "m", "tool": "batch_move", "description": "批量移动",
            "status": "ok",
            "result": {"matched": 10, "success": 8, "failed": 2, "skipped": 0,
                       "truncated": False, "reason": "none", "preview": []},
        }],
        "mutations": {"move": 8, "rename": 0, "delete": 0},
        "plan": [{"id": "m", "tool": "batch_move", "description": "批量移动"}],
        "step_index": 1,
        "run_trace": {"model_calls": 2, "tool_calls": 1, "execution_ms": 12},
    }

    class _Runtime:
        def state(self, _thread_id: str) -> dict:
            return state

        def retain_stream(self) -> None:
            pass

        def release_stream(self) -> None:
            pass

    sent: List[Any] = []

    class _Connections:
        async def send(self, _client_id: str, event: Any) -> None:
            sent.append(event)

        async def broadcast(self, event: Any) -> None:
            sent.append(event)

    monkeypatch.setattr(main, "connections", _Connections())
    tracing.clear("done-1")
    asyncio.run(main._run_stream(iter([]), "done-1", "c1", runtime=_Runtime()))

    done = next(event for event in sent if event.type == "done")
    payload = done.payload
    assert payload["outcome"] == "partial"
    assert payload["stop_reason"] == "tool_failed"
    assert payload["run_id"] == "run-1"
    assert payload["summary"]["files_ok"] == 8
    assert payload["summary"]["files_failed"] == 2
    assert payload["summary"]["tool_calls"] == 1
    assert payload["usage"]["source"] == "unknown"
