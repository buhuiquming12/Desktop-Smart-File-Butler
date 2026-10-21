"""复审意见回归（第六轮）：三处 P2。

1. 英文只读新任务仍继承旧授权：``show / check / find / search / what's in`` 这类
   常见说法不在只读词表里，澄清后的只读新任务被当成“没有动词”的补充信息，
   沿用上一轮的移动授权。
2. 混合请求仍漏查独立的新建目录目标：“新建目录 + 移动到该目录”里 create 只被当作
   从属授权，``make_dir`` 从未成功也判定成功。
3. 源路径截断导致大批量操作链误判：批量结果只保留前 200 条 ``sources``，
   超过 200 项的链式操作后半段会被判成“结果消失”，任务变成 partial。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, List

import pytest

from app import db
from app.agent import graph as graph_module
from app.agent.outcome import evaluate_completion
from app.agent.state import PlanOutput, PlanStep, ReflectionOutput
from app.agent.tool_executor import ToolExecutor
from app.config import get_settings
from app.policy.engine import explicit_intent_effects, intent_effects


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


# ---------------- 1. 英文只读新任务 ----------------

@pytest.mark.parametrize(
    "text",
    [
        "show the files",
        "show me what's in the folder",
        "check the folder",
        "display the files",
        "find all pdfs",
        "search for pdf",
        "count the files",
        "inspect the folder",
        "peek at the folder",
        "print the list",
        "tell me what's inside",
        "what is in D:\\scan",
    ],
)
def test_english_read_only_task_is_recognised(text: str) -> None:
    assert "read" in intent_effects(text), intent_effects(text)


@pytest.mark.parametrize(
    "text",
    [
        "show the files",
        "check the folder",
        "find all pdfs",
        "what is in D:\\scan",
        "看看这个目录",
        "看看 D:\\scan 里有什么",
    ],
)
def test_english_read_only_task_is_not_a_clarification_answer(text: str) -> None:
    """只读新任务必须撤销上一轮授权，不能当作纯补充信息继承。"""
    assert graph_module._is_clarification_answer(text) is False, text


@pytest.mark.parametrize(
    "answer",
    [r"D:\scan", r"D:\My scan", r"D:\list", r"D:\清单", r"D:\Program Files\scan"],
)
def test_path_answers_still_inherit(answer: str) -> None:
    """上一轮的路径判定不能被词表扩展破坏。"""
    assert graph_module._is_clarification_answer(answer) is True, answer


class _MovePlanner:
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


@pytest.mark.parametrize(
    "answer",
    [
        "show me the files", "check the folder", "find all pdfs", "看看这个目录",
        r"D:\scan then list files",
        r"D:\My Documents\scan please show the files",
        "/home/me/scan then check the folder",
    ],
)
def test_english_read_only_answer_does_not_inherit_move_authorization(
    sandbox: Path, monkeypatch: pytest.MonkeyPatch, answer: str
) -> None:
    target = sandbox / "a.txt"
    target.write_text("x", encoding="utf-8")
    dest = sandbox / "out"

    class _LLM:
        def with_structured_output(self, schema: Any) -> Any:
            if schema is PlanOutput:
                return _MovePlanner(target, dest)
            return _ReplanTillMove()

        def invoke(self, _: Any) -> Any:  # pragma: no cover
            raise AssertionError

    monkeypatch.setattr(graph_module, "build_llm", lambda temperature=0.1: _LLM())
    runtime = graph_module.AgentRuntime()
    for _ in runtime.start_stream("帮我整理一下文件", "read-en"):
        pass
    assert runtime.state("read-en").get("status") == "needs_clarification"

    for _ in runtime.start_stream(answer, "read-en"):
        pass
    state = runtime.state("read-en")
    assert "整理" not in str(state.get("trusted_user_intent") or ""), state.get(
        "trusted_user_intent"
    )
    moves = [o for o in state["observations"] if o["tool"] == "move_file"]
    assert moves and moves[0]["status"] == "failed", moves
    assert target.exists(), "只要求查看时不应移动文件"


# ---------------- 2. 混合请求里的独立新建目录 ----------------

def test_mixed_request_keeps_explicit_create_target() -> None:
    text = "新建一个备份文件夹，然后把 PDF 移动进去"
    assert explicit_intent_effects(text) == {"create"}
    assert "move" in intent_effects(text)


def test_attributive_create_is_not_a_target() -> None:
    """“移动到新建的 out 文件夹”里建目录只是从属动作。"""
    assert explicit_intent_effects("把 PDF 移动到新建的 out 文件夹") == set()


def test_missing_independent_directory_in_mixed_request_is_partial(sandbox: Path) -> None:
    moved = {"id": "m", "tool": "move_file", "status": "ok",
             "args": {"src": str(sandbox / "a.txt"), "dest_dir": str(sandbox / "out")},
             "result": str(sandbox / "out" / "a.txt")}
    (sandbox / "out").mkdir()
    (sandbox / "out" / "a.txt").write_text("x", encoding="utf-8")
    completion = evaluate_completion({
        "observations": [moved],
        "plan": [{"id": "m", "tool": "move_file", "description": "移动"}],
        "step_index": 1,
        "trusted_user_intent": "把 PDF 移动到 out，另外新建一个空的备份文件夹",
        "mutations": {"move": 1, "rename": 0, "delete": 0},
    })
    assert completion["unmet_effects"] == ["create"]
    assert completion["outcome"] == "partial"
    assert "新建目录" in completion["message"]


def test_mixed_request_succeeds_when_make_dir_runs(sandbox: Path) -> None:
    executor = ToolExecutor()
    target = sandbox / "backup"
    made = executor.execute({"id": "mk", "tool": "make_dir", "args": {"path": str(target)}})
    (sandbox / "a.txt").write_text("x", encoding="utf-8")
    moved = executor.execute({
        "id": "mv", "tool": "move_file",
        "args": {"src": str(sandbox / "a.txt"), "dest_dir": str(target)},
    })
    completion = evaluate_completion({
        "observations": [made, moved],
        "plan": [
            {"id": "mk", "tool": "make_dir", "description": "新建"},
            {"id": "mv", "tool": "move_file", "description": "移动"},
        ],
        "step_index": 2,
        "trusted_user_intent": "新建一个备份文件夹，然后把 PDF 移动进去",
        "mutations": {"move": 1, "rename": 0, "delete": 0},
    })
    assert completion["unmet_effects"] == []
    assert completion["outcome"] == "success", completion["message"]


def test_create_only_request_still_requires_make_dir(sandbox: Path) -> None:
    completion = evaluate_completion({
        "observations": [],
        "plan": [],
        "step_index": 0,
        "trusted_user_intent": "新建一个备份文件夹",
        "mutations": {},
    })
    assert completion["unmet_effects"] == ["create"]
    assert completion["outcome"] == "partial"


# ---------------- 3. 大批量链不因 sources 截断误判 ----------------

def test_large_batch_chain_is_not_truncated(sandbox: Path) -> None:
    """220 项 > 旧的 200 条上限：链式操作的源路径必须完整保留。"""
    count = 220
    source = sandbox / "src"
    source.mkdir()
    for index in range(count):
        (source / f"f{index}.pdf").write_text("pdf", encoding="utf-8")
    stage = sandbox / "stage"
    final = sandbox / "final"

    executor = ToolExecutor()
    scan_src = executor.execute(
        {"id": "s1", "tool": "scan_directory", "args": {"directory": str(source)}}
    )
    moved = executor.execute({
        "id": "m", "tool": "batch_move",
        "args": {"scan_id": scan_src["result"]["scan_id"],
                 "filter": {"extensions": ["pdf"]}, "dest_dir": str(stage)},
    })
    scan_stage = executor.execute(
        {"id": "s2", "tool": "scan_directory", "args": {"directory": str(stage)}}
    )
    classified = executor.execute({
        "id": "c", "tool": "batch_classify",
        "args": {"scan_id": scan_stage["result"]["scan_id"], "dest_root": str(final)},
    })

    assert moved["result"]["success"] == count
    assert classified["result"]["success"] == count
    assert len(moved["result"]["sources"]) == count
    assert len(classified["result"]["sources"]) == count

    completion = evaluate_completion({
        "observations": [scan_src, moved, scan_stage, classified],
        "plan": [{"id": "c", "tool": "batch_classify", "description": "分类"}],
        "step_index": 1,
        "mutations": {"move": count * 2, "rename": 0, "delete": 0},
        "trusted_user_intent": "把 PDF 按类别整理到 final",
    })
    statuses = [check["status"] for check in completion["checks"]]
    assert statuses == ["verified"] * 4, completion["checks"]
    assert completion["outcome"] == "success", completion["message"]
