"""第二轮评审 4 条意见的回归测试（先失败，后修复）。

1. P1 澄清期间切换任务仍继承旧授权（graph.start_stream）
2. P1 只扫描就结束仍被标记为目标完成（收尾判定缺少“目标要求”）
3. P2 工具内部模型调用可超过预算（只在节点边界检查）
4. P2 缓存校验替代了最终结果检查（结果消失仍报成功）
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, List

import pytest

from app import db
from app.agent import graph as graph_module
from app.agent.outcome import evaluate_completion, record_verification
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


class _MovePlanner:
    """第一轮要求澄清；之后无论用户说什么都规划一次移动。"""

    def __init__(self, target: Path, dest: Path) -> None:
        self.target, self.dest = target, dest

    def invoke(self, messages: List[Any]) -> PlanOutput:
        context = json.loads(messages[-1].content)
        if not context.get("tool_observations") and "?" not in str(
            context.get("original_user_request") or ""
        ):
            first = not context.get("conversation") or len(context["conversation"]) == 1
            if first:
                return PlanOutput(
                    goal="澄清",
                    steps=[PlanStep(id="ask", description="询问", tool="request_clarification",
                                    args={"question": "要整理哪个目录？"})],
                    user_message="",
                )
        if context.get("tool_observations"):
            return PlanOutput(goal="done", steps=[], user_message="完成")
        return PlanOutput(
            goal="移动",
            steps=[PlanStep(id="mv", description="移动", tool="move_file",
                            args={"src": str(self.target), "dest_dir": str(self.dest)})],
            user_message="",
        )


class _DoneReflector:
    def invoke(self, messages: List[Any]) -> ReflectionOutput:
        if json.loads(messages[-1].content).get("是否还有步骤"):
            return ReflectionOutput(decision="continue", reasoning="继续")
        return ReflectionOutput(decision="done", reasoning="完成", final_response="完成")


class _ReplanTillMove:
    def invoke(self, messages: List[Any]) -> ReflectionOutput:
        context = json.loads(messages[-1].content)
        if context.get("是否还有步骤"):
            return ReflectionOutput(decision="continue", reasoning="继续")
        tools = {str(step.get("tool")) for step in context.get("当前计划") or []}
        return ReflectionOutput(
            decision="done" if "move_file" in tools else "replan",
            reasoning="继续",
        )


def _install(monkeypatch: pytest.MonkeyPatch, planner: Any, reflector: Any = None, llm_invoke=None) -> None:
    class _LLM:
        def with_structured_output(self, schema: Any) -> Any:
            if schema is PlanOutput:
                return planner
            return reflector or _DoneReflector()

        def invoke(self, messages: Any) -> Any:
            if llm_invoke is None:  # pragma: no cover
                raise AssertionError("不应直接调用 llm.invoke")
            return llm_invoke(messages)

    monkeypatch.setattr(graph_module, "build_llm", lambda temperature=0.1: _LLM())


# ---------------- 1. 澄清期间切换任务 ----------------

def test_task_switch_during_clarification_drops_old_authorization(
    sandbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """澄清中用户改口说“只扫描”，不能再用上一轮的“整理”授权去移动文件。"""
    target = sandbox / "a.txt"
    target.write_text("x", encoding="utf-8")
    dest = sandbox / "out"
    _install(monkeypatch, _MovePlanner(target, dest), _ReplanTillMove())

    runtime = graph_module.AgentRuntime()
    for _ in runtime.start_stream("帮我整理一下文件", "switch"):
        pass
    assert runtime.state("switch").get("status") == "needs_clarification"

    for _ in runtime.start_stream("算了，只帮我扫描一下目录", "switch"):
        pass
    state = runtime.state("switch")
    assert "整理" not in str(state.get("trusted_user_intent") or ""), state.get("trusted_user_intent")
    moves = [o for o in state["observations"] if o["tool"] == "move_file"]
    assert moves and moves[0]["status"] == "failed", moves
    assert target.exists(), "改口后文件不应被移动"


def test_pure_path_answer_still_inherits(sandbox: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """只回答一个路径（没有任何操作动词）时仍沿用上一轮授权。"""
    target = sandbox / "a.txt"
    target.write_text("x", encoding="utf-8")
    dest = sandbox / "out"
    _install(monkeypatch, _MovePlanner(target, dest), _ReplanTillMove())

    runtime = graph_module.AgentRuntime()
    for _ in runtime.start_stream("帮我整理一下文件", "inherit"):
        pass
    for _ in runtime.start_stream(str(sandbox), "inherit"):
        pass
    state = runtime.state("inherit")
    assert "整理" in str(state.get("trusted_user_intent") or "")
    assert (dest / "a.txt").exists()


# ---------------- 2. 只扫描就结束 ----------------

def test_scan_only_stop_is_not_goal_success(
    sandbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """用户要求移动，模型只扫描就收尾：不能算目标完成。"""
    source = sandbox / "downloads"
    source.mkdir()
    (source / "a.pdf").write_text("pdf", encoding="utf-8")

    class _ScanOnly:
        def invoke(self, messages: List[Any]) -> PlanOutput:
            context = json.loads(messages[-1].content)
            if context.get("tool_observations"):
                return PlanOutput(goal="done", steps=[], user_message="扫描完成")
            return PlanOutput(
                goal="扫描",
                steps=[PlanStep(id="scan", description="扫描", tool="scan_directory",
                                args={"directory": str(source)})],
                user_message="",
            )

    _install(monkeypatch, _ScanOnly())
    runtime = graph_module.AgentRuntime()
    for _ in runtime.start_stream("把下载目录里的 PDF 移动到 PDF 文件夹", "scan-only"):
        pass

    state = runtime.state("scan-only")
    assert state.get("outcome") != "success", state.get("completion")
    assert state.get("stop_reason") == "goal_unmet"
    assert "移动" in state.get("final_response", "")
    assert state["completion"].get("unmet_effects") == ["move"]


# ---------------- 3. 工具内部模型调用不得超预算 ----------------

def test_tool_internal_calls_stop_at_budget(
    sandbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """一次 write_summary 内的多次模型调用必须受剩余预算约束。"""
    doc = sandbox / "doc.txt"
    doc.write_text("x" * (graph_module._SUMMARY_CHUNK_CHARS * 5), encoding="utf-8")
    out = sandbox / "summaries"
    calls: List[int] = []

    def _invoke(_messages: Any) -> Any:
        calls.append(1)

        class _R:
            content = "小结"

        return _R()

    _install(
        monkeypatch,
        _MovePlanner(doc, out),
        llm_invoke=_invoke,
    )
    # 直接构造只跑摘要的计划，避免 _MovePlanner 的澄清分支。
    class _SummaryPlanner:
        def invoke(self, messages: List[Any]) -> PlanOutput:
            if json.loads(messages[-1].content).get("tool_observations"):
                return PlanOutput(goal="done", steps=[], user_message="完成")
            return PlanOutput(
                goal="摘要",
                steps=[PlanStep(id="s", description="摘要", tool="write_summary",
                                args={"file_path": str(doc), "output_dir": str(out)})],
                user_message="",
            )

    _install(monkeypatch, _SummaryPlanner(), llm_invoke=_invoke)
    db.set_preference("agent_max_model_calls", "4")
    runtime = graph_module.AgentRuntime()
    for _ in runtime.start_stream("给 doc.txt 生成摘要", "budget-inner"):
        pass

    state = runtime.state("budget-inner")
    # planning 已用 1 次，剩余 3 次是硬上限：分段摘要最多再成功调用 3 次。
    assert len(calls) <= 3, f"工具内部模型调用超过剩余预算: {len(calls)}"
    assert state.get("outcome") != "success"
    joined = " ".join(
        str(o.get("error") or "") for o in state["observations"]
    ) + str(state.get("final_response") or "")
    assert "预算" in joined or "上限" in joined, joined


# ---------------- 4. 缓存校验不能掩盖结果消失 ----------------

def _move_obs(path: Path, tool: str = "move_file", src: str = "") -> dict:
    return record_verification({
        "step_id": "m", "tool": tool, "description": "移动",
        "status": "ok", "result": str(path),
        "args": {"src": src or str(path), "dest_dir": str(path.parent)},
    })


def test_cached_verification_does_not_mask_vanished_result(tmp_path: Path) -> None:
    """记录时文件存在、收尾时已消失且没有任何后续操作解释它：不能报成功。"""
    moved = tmp_path / "out" / "a.txt"
    moved.parent.mkdir()
    moved.write_text("x", encoding="utf-8")
    obs = _move_obs(moved)
    assert obs["verification"]["status"] == "verified"

    moved.unlink()  # 被外部删掉，没有任何后续观察
    completion = evaluate_completion({
        "observations": [obs],
        "plan": [{"id": "m", "tool": "move_file", "description": "移动"}],
        "step_index": 1,
        "mutations": {"move": 1, "rename": 0, "delete": 0},
        "trusted_user_intent": "把 a.txt 移动到 out",
    })
    assert completion["outcome"] != "success", completion
    assert completion["checks"][0]["status"] == "failed"


def test_superseded_result_is_not_treated_as_vanished(tmp_path: Path) -> None:
    """A→B→C：B 处结果被后续移动消费，属于正常链路，不能判失败。"""
    stage = tmp_path / "stage" / "a.txt"
    stage.parent.mkdir()
    stage.write_text("x", encoding="utf-8")
    first = _move_obs(stage)

    final = tmp_path / "final" / "a.txt"
    final.parent.mkdir()
    final.write_text("x", encoding="utf-8")
    second = record_verification({
        "step_id": "m2", "tool": "move_file", "description": "再移动",
        "status": "ok", "result": str(final),
        "args": {"src": str(stage), "dest_dir": str(final.parent)},
    })
    stage.unlink()

    # 真实工具执行后，first 的 verification 是执行当下记录的；这里模拟链路后复查。
    completion = evaluate_completion({
        "observations": [first, second],
        "plan": [{"id": "m", "tool": "move_file", "description": "移动"}],
        "step_index": 2,
        "mutations": {"move": 2, "rename": 0, "delete": 0},
        "trusted_user_intent": "把 a.txt 移动两次做归档",
    })
    assert completion["outcome"] == "success", completion["checks"]
