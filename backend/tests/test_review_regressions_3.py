"""复审意见回归：只说“扫描”的澄清回答仍会继承上一轮的移动授权。

根因：``_is_clarification_answer`` 依赖 ``intent_effects`` 判断“这轮是否有新操作”，
但意图词表里没有 scan/查看这类只读动作，于是“只扫描一下这个目录”被当成单纯补充，
``trusted_user_intent`` 继续带着上一轮的“整理”，移动因此被放行。
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
from app.policy.engine import intent_effects


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


# ---------------- 词表层 ----------------

@pytest.mark.parametrize(
    "text",
    [
        "只扫描一下这个目录",
        "改为扫描这个目录",
        "扫描 C:/Downloads",
        "查看一下这个目录",
        "浏览一下",
        "列一下文件清单",
        "scan this directory",
        "list the files",
    ],
)
def test_read_only_phrases_are_recognised(text: str) -> None:
    """只读动作必须被识别出来，否则会被误当作“没有新操作”。"""
    assert "read" in intent_effects(text), text


@pytest.mark.parametrize(
    "text,expected",
    [
        ("只扫描一下这个目录", {"read"}),
        ("帮我整理一下文件", {"move", "create"}),
        # create 的词表刻意包含整理类动词：建目录通常是整理的从属动作。
        ("把 PDF 移动到 out", {"move", "create"}),
        ("不要移动任何文件", set()),
        ("扫描并删除临时文件", {"read", "delete"}),
    ],
)
def test_intent_effects_does_not_gain_false_mutations(
    text: str, expected: set[str]
) -> None:
    """只读识别不能顺带把「移动/删除」也算出来。"""
    effects = intent_effects(text)
    assert effects == expected, effects


def test_read_effect_is_not_a_required_outcome() -> None:
    """只读不是“用户要求产生的副作用”，不应进 unmet_effects。"""
    from app.agent.outcome import _unmet_effects

    assert _unmet_effects("扫描一下目录", []) == []
    assert _unmet_effects("扫描后把 PDF 移动到 out", []) == ["move"]


@pytest.mark.parametrize(
    "text,expected",
    [
        # 词边界：remove 里的 move、already 里的 read 都不是操作意图。
        ("remove the files", {"delete"}),
        ("already done", set()),
        ("spread the word", set()),
        # 词组仍按整词匹配。
        ("look at the folder", {"read"}),
        ("list the files", {"read"}),
    ],
)
def test_ascii_terms_require_word_boundaries(text: str, expected: set[str]) -> None:
    assert intent_effects(text) == expected


def test_clarification_answer_detection() -> None:
    """纯补充信息才返回 True；只读动作与改口都算新请求。"""
    assert graph_module._is_clarification_answer(r"D:\Downloads") is True
    assert graph_module._is_clarification_answer(r"放到 D:\out") is False
    for message in ("只扫描一下这个目录", "改为扫描这个目录", "算了不用了"):
        assert graph_module._is_clarification_answer(message) is False, message


# ---------------- 端到端 ----------------

class _PlannerAlwaysMoves:
    """第一轮要求澄清；之后无论用户说什么都规划一次移动（模拟模型误判）。"""

    def __init__(self, target: Path, dest: Path) -> None:
        self.target, self.dest = target, dest

    def invoke(self, messages: List[Any]) -> PlanOutput:
        context = json.loads(messages[-1].content)
        if not context.get("tool_observations") and len(
            context.get("conversation") or []
        ) == 1:
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


def _install(monkeypatch: pytest.MonkeyPatch, planner: Any) -> None:
    class _LLM:
        def with_structured_output(self, schema: Any) -> Any:
            return planner if schema is PlanOutput else _ReplanTillMove()

        def invoke(self, _: Any) -> Any:  # pragma: no cover
            raise AssertionError

    monkeypatch.setattr(graph_module, "build_llm", lambda temperature=0.1: _LLM())


@pytest.mark.parametrize(
    "answer",
    ["只扫描一下这个目录", "改为扫描这个目录", "查看一下 {dir}"],
)
def test_scan_only_answer_does_not_inherit_move_authorization(
    sandbox: Path, monkeypatch: pytest.MonkeyPatch, answer: str
) -> None:
    """澄清后用户改口只说扫描：不得沿用上一轮的移动授权。"""
    target = sandbox / "a.txt"
    target.write_text("x", encoding="utf-8")
    dest = sandbox / "out"
    _install(monkeypatch, _PlannerAlwaysMoves(target, dest))

    runtime = graph_module.AgentRuntime()
    for _ in runtime.start_stream("帮我整理一下文件", "scan-answer"):
        pass
    assert runtime.state("scan-answer").get("status") == "needs_clarification"

    for _ in runtime.start_stream(answer.format(dir=sandbox), "scan-answer"):
        pass
    state = runtime.state("scan-answer")
    assert "整理" not in str(state.get("trusted_user_intent") or ""), state.get(
        "trusted_user_intent"
    )
    moves = [o for o in state["observations"] if o["tool"] == "move_file"]
    assert moves and moves[0]["status"] == "failed", moves
    assert target.exists(), "只要求扫描时不应移动文件"


def test_plain_path_answer_still_inherits(
    sandbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """仍然保留原有用例：纯路径补充继续沿用上一轮授权。"""
    target = sandbox / "a.txt"
    target.write_text("x", encoding="utf-8")
    dest = sandbox / "out"
    _install(monkeypatch, _PlannerAlwaysMoves(target, dest))

    runtime = graph_module.AgentRuntime()
    for _ in runtime.start_stream("帮我整理一下文件", "path-answer"):
        pass
    for _ in runtime.start_stream(str(sandbox), "path-answer"):
        pass
    state = runtime.state("path-answer")
    assert "整理" in str(state.get("trusted_user_intent") or "")
    assert (dest / "a.txt").exists()
