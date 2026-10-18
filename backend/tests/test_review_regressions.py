"""针对代码评审 4 条意见的回归测试（每条先复现，再修复）。

1. P1 空计划会被错误标记为目标完成（graph._plan）
2. P2 连续文件操作会被误判为部分完成（outcome 只在任务结束时校验路径）
3. P2 澄清回答丢失原始操作授权（graph.start_stream 重建 trusted_user_intent）
4. P2 模型预算漏算摘要工具内部调用（run_trace.model_calls 只统计规划/反思）
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, List

import pytest

from app import db
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


class _Planner:
    def __init__(self, plan: List[PlanStep], final: str = "完成") -> None:
        self.plan = plan
        self.final = final

    def invoke(self, messages: List[Any]) -> PlanOutput:
        context = json.loads(messages[-1].content)
        if context.get("tool_observations"):
            return PlanOutput(goal="done", steps=[], user_message=self.final)
        return PlanOutput(goal="plan", steps=self.plan, user_message="")


class _DoneReflector:
    def invoke(self, messages: List[Any]) -> ReflectionOutput:
        if json.loads(messages[-1].content).get("是否还有步骤"):
            return ReflectionOutput(decision="continue", reasoning="继续")
        return ReflectionOutput(decision="done", reasoning="完成", final_response="完成")


class _ReplanUntilMoveReflector:
    """计划里还没有 batch_move 就继续重规划；出现后结束。"""

    def invoke(self, messages: List[Any]) -> ReflectionOutput:
        context = json.loads(messages[-1].content)
        if context.get("是否还有步骤"):
            return ReflectionOutput(decision="continue", reasoning="继续")
        tools = {str(step.get("tool")) for step in context.get("当前计划") or []}
        if "batch_move" in tools:
            return ReflectionOutput(decision="done", reasoning="完成")
        return ReflectionOutput(decision="replan", reasoning="继续移动")


def _install(monkeypatch: pytest.MonkeyPatch, planner: _Planner, llm_invoke=None) -> None:
    class _LLM:
        def with_structured_output(self, schema: Any) -> Any:
            return planner if schema is PlanOutput else _DoneReflector()

        def invoke(self, messages: Any) -> Any:
            if llm_invoke is None:  # pragma: no cover - 本文件只有摘要用例需要
                raise AssertionError("不应直接调用 llm.invoke")
            return llm_invoke(messages)

    monkeypatch.setattr(graph_module, "build_llm", lambda temperature=0.1: _LLM())


# ---------------- 1. 空计划 ----------------

def test_empty_plan_is_not_reported_as_success(
    sandbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """规划器返回空计划时没有执行任何工具，不能宣称“目标已验证完成”。"""
    _install(
        monkeypatch,
        _Planner([], final="没有需要执行的文件操作。"),
    )
    runtime = graph_module.AgentRuntime()
    for _ in runtime.start_stream("把 download 里的 PDF 移到 PDF 文件夹", "empty-plan"):
        pass

    state = runtime.state("empty-plan")
    assert state.get("status") == "completed"
    assert state.get("outcome") != "success", state.get("completion")
    assert state.get("stop_reason") == "no_operations"
    assert state["completion"]["verified"] is False


# ---------------- 2. 连续文件操作 ----------------

def test_chained_moves_are_not_partial(
    sandbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A→B 再 B→C：任务结束时 B 已不存在，但两步都成功，不应判为部分完成。"""
    source = sandbox / "a.txt"
    source.write_text("x", encoding="utf-8")
    stage = sandbox / "stage"
    final = sandbox / "final"
    _install(
        monkeypatch,
        _Planner([
            PlanStep(id="m1", description="先归档到 stage", tool="move_file",
                     args={"src": str(source), "dest_dir": str(stage)}),
            PlanStep(id="m2", description="再移动到 final", tool="move_file",
                     args={"src": str(stage / "a.txt"), "dest_dir": str(final)}),
        ]),
    )
    runtime = graph_module.AgentRuntime()
    for _ in runtime.start_stream("把 a.txt 移动两次做归档", "chain"):
        pass

    state = runtime.state("chain")
    assert [obs["status"] for obs in state["observations"]] == ["ok", "ok"]
    assert (final / "a.txt").exists()
    assert state.get("outcome") == "success", state.get("completion", {}).get("checks")


# ---------------- 3. 澄清后的授权 ----------------

class _ClarifyThenMovePlanner:
    """第一次没给目录时澄清；给出路径后再扫描并移动。"""

    def __init__(self, pdf_dir: Path, dest: Path) -> None:
        self.pdf_dir, self.dest = pdf_dir, dest

    def invoke(self, messages: List[Any]) -> PlanOutput:
        context = json.loads(messages[-1].content)
        request = str(context.get("original_user_request") or "")
        observations = context.get("tool_observations") or []
        if "\\" not in request and "/" not in request:
            return PlanOutput(
                goal="澄清",
                steps=[PlanStep(id="ask", description="询问目录", tool="request_clarification",
                                args={"question": "请告诉我要整理哪个目录？"})],
                user_message="",
            )
        if not observations:
            return PlanOutput(
                goal="扫描",
                steps=[PlanStep(id="scan", description="扫描", tool="scan_directory",
                                args={"directory": str(self.pdf_dir)})],
                user_message="",
            )
        if any(o["tool"] == "batch_move" for o in observations):
            return PlanOutput(goal="完成", steps=[], user_message="完成")
        scan_id = next(o for o in observations if o["tool"] == "scan_directory")["result"]["scan_id"]
        return PlanOutput(
            goal="移动",
            steps=[PlanStep(id="mv", description="移动 PDF", tool="batch_move",
                            args={"scan_id": scan_id, "filter": {"extensions": ["pdf"]},
                                  "dest_dir": str(self.dest)})],
            user_message="",
        )


def test_clarification_answer_keeps_original_authorization(
    sandbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """第二轮只回答目录（不含“整理/移动”字样）时，仍沿用第一轮的整理授权。"""
    source = sandbox / "downloads"
    source.mkdir()
    for index in range(3):
        (source / f"f-{index}.pdf").write_text("pdf", encoding="utf-8")
    dest = sandbox / "out"

    class _LLM:
        def with_structured_output(self, schema: Any) -> Any:
            if schema is PlanOutput:
                return _ClarifyThenMovePlanner(source, dest)
            return _ReplanUntilMoveReflector()

        def invoke(self, _: Any) -> Any:  # pragma: no cover
            raise AssertionError

    monkeypatch.setattr(graph_module, "build_llm", lambda temperature=0.1: _LLM())
    runtime = graph_module.AgentRuntime()

    for _ in runtime.start_stream("帮我整理一下文件", "clarify-auth"):
        pass
    assert runtime.state("clarify-auth").get("status") == "needs_clarification"

    # 只给路径，没有操作动词。
    for _ in runtime.start_stream(str(source), "clarify-auth"):
        pass
    state = runtime.state("clarify-auth")
    failed = [o for o in state["observations"] if o["status"] == "failed"]
    assert not failed, failed
    assert len(list(dest.glob("*.pdf"))) == 3


def test_completed_run_does_not_lend_authorization_to_next_message(
    sandbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """授权只在“上一轮要求澄清”时延续；已完成/失败的请求不得为下一条消息背书。"""
    source = sandbox / "a.txt"
    source.write_text("x", encoding="utf-8")
    _install(
        monkeypatch,
        _Planner([PlanStep(id="m1", description="移动", tool="move_file",
                           args={"src": str(source), "dest_dir": str(sandbox / "out")})]),
    )
    runtime = graph_module.AgentRuntime()
    for _ in runtime.start_stream("把 a.txt 移动到 out", "no-lend"):
        pass
    assert runtime.state("no-lend").get("outcome") == "success"

    # 下一轮只说一个路径：旧授权不应被沿用，任何移动都必须被拒绝。
    for _ in runtime.start_stream(str(sandbox / "whatever"), "no-lend"):
        pass
    state = runtime.state("no-lend")
    assert state.get("trusted_user_intent") == str(sandbox / "whatever")
    assert not any(
        o["tool"] == "move_file" and o["status"] == "ok" for o in state["observations"]
    )


# ---------------- 4. 摘要工具的模型调用 ----------------

def test_summary_tool_model_calls_are_counted(
    sandbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """write_summary 的分段摘要也是模型调用，必须计入 run_trace 与预算。"""
    long_text = "x" * (graph_module._SUMMARY_CHUNK_CHARS * 2)  # 2 段 → map+reduce
    doc = sandbox / "doc.txt"
    doc.write_text(long_text, encoding="utf-8")
    out = sandbox / "summaries"
    calls: List[Any] = []

    def _invoke(messages: Any) -> Any:
        calls.append(messages)

        class _R:
            content = "小结"

        return _R()

    _install(
        monkeypatch,
        _Planner([PlanStep(id="s1", description="生成摘要", tool="write_summary",
                           args={"file_path": str(doc), "output_dir": str(out)})]),
        llm_invoke=_invoke,
    )
    runtime = graph_module.AgentRuntime()
    for _ in runtime.start_stream("给 doc.txt 生成摘要", "summary-calls"):
        pass

    state = runtime.state("summary-calls")
    # 2 段 map + 1 次 reduce = 3 次摘要模型调用。
    assert len(calls) == 3
    trace = state.get("run_trace") or {}
    # planning(1) + reflecting(1) + 摘要 3 次
    assert trace.get("model_calls", 0) >= 5, trace
