"""复审意见回归：路径里的英文单词被当成操作指令。

根因：``intent_effects`` 直接在整句话上做词表匹配，未区分“参数”和“指令”。
用户只回答目录 ``D:\\scan`` 时，路径中的 ``scan`` 命中只读词表，
``_is_clarification_answer`` 因此判定“这轮自带新操作”，把上一轮的授权清空，
随后模型规划的移动被拒绝（未授权 move）。``list`` / ``清单`` 目录名同理。
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


# ---------------- 词表层：路径是参数，不是指令 ----------------

@pytest.mark.parametrize(
    "answer",
    [
        r"D:\scan",
        r"D:\list",
        r"D:\scan\list",
        r"C:/Users/me/list",
        r"\\server\share\scan",
        r"/home/me/list",
        r"downloads\scan",
        r"D:\清单",
    ],
)
def test_path_only_answer_has_no_intent(answer: str) -> None:
    assert intent_effects(answer) == set(), answer


@pytest.mark.parametrize(
    "text,expected",
    [
        # 路径被当作参数剥掉，动词仍然来自自然语言部分。
        (r"把 D:\scan 里的 PDF 移动到 out", "move"),
        (r"扫描 D:\list", "read"),
        (r"删除 D:\list 里的临时文件", "delete"),
        # 路径外的“清单”仍是只读指令。
        ("列一下文件清单", "read"),
    ],
)
def test_verbs_outside_paths_still_detected(text: str, expected: str) -> None:
    assert expected in intent_effects(text), intent_effects(text)


def test_path_words_do_not_create_false_effects() -> None:
    """路径里的 scan/list 不能反过来制造只读意图。"""
    assert "read" not in intent_effects(r"移动 D:\scan 到 D:\list")


# ---------------- 澄清判定 ----------------

@pytest.mark.parametrize(
    "answer",
    [r"D:\scan", r"D:\list", r"/home/me/list", r"downloads\scan"],
)
def test_path_answer_is_a_clarification_answer(answer: str) -> None:
    assert graph_module._is_clarification_answer(answer) is True, answer


def test_path_with_verb_is_not_a_clarification_answer() -> None:
    assert graph_module._is_clarification_answer(r"放到 D:\out") is False
    # 只读动作仍然算“新请求”，撤销旧授权（上一轮修复的行为保持不变）。
    assert graph_module._is_clarification_answer("只扫描一下这个目录") is False


# ---------------- 端到端 ----------------

class _ClarifyThenScanThenMove:
    """澄清 → 用用户给的路径扫描 → 移动 a.txt。"""

    def __init__(self, dest: Path) -> None:
        self.dest = dest

    def invoke(self, messages: List[Any]) -> PlanOutput:
        context = json.loads(messages[-1].content)
        observations = context.get("tool_observations") or []
        conversation = context.get("conversation") or []
        if not observations and len(conversation) == 1:
            return PlanOutput(
                goal="澄清",
                steps=[PlanStep(id="ask", description="询问", tool="request_clarification",
                                args={"question": "要整理哪个目录？"})],
                user_message="",
            )
        directory = str((conversation[-1] or {}).get("content") or "")
        if not any(o["tool"] == "scan_directory" for o in observations):
            return PlanOutput(goal="扫描", steps=[PlanStep(
                id="scan", description="扫描", tool="scan_directory",
                args={"directory": directory},
            )], user_message="")
        if any(o["tool"] == "move_file" for o in observations):
            return PlanOutput(goal="done", steps=[], user_message="完成")
        return PlanOutput(goal="移动", steps=[PlanStep(
            id="mv", description="移动", tool="move_file",
            args={"src": str(Path(directory) / "a.txt"), "dest_dir": str(self.dest)},
        )], user_message="")


class _ContinueReflector:
    """计划走完后：还没规划过移动就重规划，规划过就结束。"""

    def invoke(self, messages: List[Any]) -> ReflectionOutput:
        context = json.loads(messages[-1].content)
        if context.get("是否还有步骤"):
            return ReflectionOutput(decision="continue", reasoning="继续")
        tools = {str(step.get("tool")) for step in context.get("当前计划") or []}
        return ReflectionOutput(
            decision="done" if "move_file" in tools else "replan",
            reasoning="继续",
        )


def _run_clarify_flow(sandbox: Path, monkeypatch: pytest.MonkeyPatch, name: str) -> dict:
    directory = sandbox / name
    directory.mkdir()
    (directory / "a.txt").write_text("x", encoding="utf-8")
    dest = sandbox / "out"

    class _LLM:
        def with_structured_output(self, schema: Any) -> Any:
            if schema is PlanOutput:
                return _ClarifyThenScanThenMove(dest)
            return _ContinueReflector()

        def invoke(self, _: Any) -> Any:  # pragma: no cover
            raise AssertionError

    monkeypatch.setattr(graph_module, "build_llm", lambda temperature=0.1: _LLM())
    runtime = graph_module.AgentRuntime()
    thread_id = f"dir-{name}"
    for _ in runtime.start_stream("帮我整理一下文件", thread_id):
        pass
    assert runtime.state(thread_id).get("status") == "needs_clarification"

    for _ in runtime.start_stream(str(directory), thread_id):
        pass
    state = runtime.state(thread_id)
    assert (dest / "a.txt").exists(), state["observations"]
    return state


def test_directory_named_scan_keeps_authorization(
    sandbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """目录真的叫 ...\\scan：用户只回这个路径时，上一轮的整理授权必须保留。"""
    state = _run_clarify_flow(sandbox, monkeypatch, "scan")
    assert "整理" in str(state.get("trusted_user_intent") or "")


def test_directory_named_list_via_clarification_keeps_authorization(
    sandbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """完整链路：先澄清，再只回 ...\\list，移动仍然被授权。"""
    state = _run_clarify_flow(sandbox, monkeypatch, "list")
    assert "整理" in str(state.get("trusted_user_intent") or "")
    failed = [o for o in state["observations"] if o["status"] == "failed"]
    assert not failed, failed
