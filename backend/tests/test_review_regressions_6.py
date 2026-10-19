"""复审意见回归（第五轮）：七处 P2。

1. 紧贴路径的中文动作被当成路径名（``D:\\scan删除``）。
2. 英文指令被当成纯路径屏蔽（``D:\\scan delete``）。
3. 空计划结束时仍可能显示模型编造的“完成”回复。
4. 用户明确要求“新建目录”却没建成，仍被判为成功。
5. 批量操作链（batch_move → batch_classify）的结果被误判为“消失”而部分完成。
6. 拒绝批量审批后，后续同类步骤反复弹审批。
7. 断线重连遗漏 needs_clarification（前端用例在 frontend/src/api.test.ts）。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, List

import pytest

from app import db
from app.agent import graph as graph_module
from app.agent.outcome import _unmet_effects, evaluate_completion
from app.agent.state import PlanOutput, PlanStep, ReflectionOutput
from app.agent.tool_executor import ToolExecutor
from app.config import get_settings
from app.policy.engine import explicit_intent_effects, intent_effects

BATCH_N = 25  # > 默认阈值 20


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


# ---------------- 1. 紧贴路径的中文动作 ----------------

@pytest.mark.parametrize(
    "text,expected",
    [
        (r"D:\scan删除", "delete"),
        (r"D:\scan整理一下", "move"),
        (r"D:\scan删除旧文件", "delete"),
        ("把D:\\scan里的PDF移动到out", "move"),
        ("给我D:\\scan里的PDF移动到out", "move"),
        (r"D:\scan移到 out", "move"),
    ],
)
def test_glued_chinese_command_is_kept(text: str, expected: str) -> None:
    assert expected in intent_effects(text), intent_effects(text)


@pytest.mark.parametrize(
    "text",
    [
        # 中文目录名不能被误判成指令（否则纯路径回答会清空上一轮授权）。
        r"D:\扫描件2024",
        r"D:\资料的备份",
        r"D:\资料\扫描",
        r"D:\我的 扫描件",
        r"D:\资料 整理箱",
        r"D:\scan",
    ],
)
def test_chinese_directory_names_stay_paths(text: str) -> None:
    assert intent_effects(text) == set(), intent_effects(text)


# ---------------- 2. 英文指令 ----------------

@pytest.mark.parametrize(
    "text,expected",
    [
        (r"D:\scan delete", "delete"),
        (r"D:\scan move", "move"),
        (r"D:\scan rename", "rename"),
        (r"D:\My Documents\scan remove", "delete"),
    ],
)
def test_ascii_command_after_path_is_kept(text: str, expected: str) -> None:
    assert expected in intent_effects(text), intent_effects(text)


def test_ascii_read_words_stay_path_names() -> None:
    """scan/list 仍是常见目录名，纯路径回答不能因此变成新操作。"""
    assert intent_effects(r"D:\My scan") == set()
    assert intent_effects(r"D:\scan list") == set()


# ---------------- 3. 空计划不能谎报完成 ----------------

class _EmptyPlanPlanner:
    def invoke(self, _: List[Any]) -> PlanOutput:
        return PlanOutput(goal="完成", steps=[], user_message="全部整理完成，共 123 个文件。")


class _UnusedReflector:
    def invoke(self, _: List[Any]) -> ReflectionOutput:  # pragma: no cover
        raise AssertionError("空计划不应进入反思")


def test_empty_plan_does_not_echo_model_success_claim(
    sandbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _LLM:
        def with_structured_output(self, schema: Any) -> Any:
            return _EmptyPlanPlanner() if schema is PlanOutput else _UnusedReflector()

        def invoke(self, _: Any) -> Any:  # pragma: no cover
            raise AssertionError

    monkeypatch.setattr(graph_module, "build_llm", lambda temperature=0.1: _LLM())
    runtime = graph_module.AgentRuntime()
    for _ in runtime.start_stream("整理一下文件", "empty-claim"):
        pass

    state = runtime.state("empty-claim")
    assert state.get("outcome") != "success"
    assert "123" not in str(state.get("final_response")), state.get("final_response")
    assert "未执行任何文件操作" in str(state.get("final_response"))


# ---------------- 4. 明确要求的新建目录 ----------------

def test_explicit_create_is_an_explicit_target() -> None:
    assert explicit_intent_effects("新建一个备份文件夹") == {"create"}
    assert explicit_intent_effects("mkdir D:\\backup") == {"create"}
    # “整理/移动”派生的 create 只是从属授权，不是目标。
    assert explicit_intent_effects("整理一下文件") == set()
    # “创建摘要文件”是写入，不是建目录目标。
    assert explicit_intent_effects("创建摘要文件") == set()


def test_missing_make_dir_is_reported_as_unmet(sandbox: Path) -> None:
    completion = evaluate_completion({
        "observations": [],
        "plan": [],
        "step_index": 0,
        "trusted_user_intent": "新建一个备份文件夹",
        "mutations": {},
    })
    assert completion["unmet_effects"] == ["create"]
    assert completion["outcome"] == "partial"
    assert "新建目录" in completion["message"]


def test_create_is_subordinate_when_a_move_is_requested(sandbox: Path) -> None:
    """“移动到 out”里即使出现“新建”，建目录也不是目标（move_file 自己会建）。"""
    moved = {"id": "m", "tool": "move_file", "status": "ok",
             "args": {"src": str(sandbox / "a.txt"), "dest_dir": str(sandbox / "out")},
             "result": str(sandbox / "out" / "a.txt")}
    (sandbox / "out").mkdir()
    (sandbox / "out" / "a.txt").write_text("x", encoding="utf-8")
    completion = evaluate_completion({
        "observations": [moved],
        "plan": [{"id": "m", "tool": "move_file", "description": "移动"}],
        "step_index": 1,
        "trusted_user_intent": "把 PDF 移动到新建的 out 文件夹",
        "mutations": {"move": 1, "rename": 0, "delete": 0},
    })
    assert completion["unmet_effects"] == []
    assert completion["outcome"] == "success", completion["message"]


def test_created_directory_satisfies_the_target(sandbox: Path) -> None:
    target = sandbox / "backup"
    step = {"id": "mk", "tool": "make_dir", "args": {"path": str(target)}}
    observation = ToolExecutor().execute(step)
    completion = evaluate_completion({
        "observations": [observation],
        "plan": [step],
        "step_index": 1,
        "trusted_user_intent": "新建一个备份文件夹",
        "mutations": {},
    })
    assert completion["checks"][0]["status"] == "verified"
    assert completion["unmet_effects"] == []
    assert completion["outcome"] == "success", completion["message"]


def test_move_intent_does_not_require_create() -> None:
    """只移动时，create 是从属授权，不能因为没建目录就判部分完成。"""
    scan = {"id": "s", "tool": "scan_directory", "status": "ok",
            "result": {"scan_id": "s1", "total": 1, "scanned_count": 1}}
    assert "create" not in _unmet_effects("把 a.txt 移动到 out", [scan])


# ---------------- 5. 批量操作链 ----------------

def test_successful_batch_chain_is_not_partial(sandbox: Path) -> None:
    source = sandbox / "src"
    source.mkdir()
    for name in ("a.pdf", "b.pdf"):
        (source / name).write_text("pdf", encoding="utf-8")
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
    assert moved["result"]["success"] == 2
    assert classified["result"]["success"] == 2
    assert classified["result"]["sources"], "批量结果必须记录被消费的源路径"

    completion = evaluate_completion({
        "observations": [scan_src, moved, scan_stage, classified],
        "plan": [{"id": "c", "tool": "batch_classify", "description": "分类"}],
        "step_index": 1,
        "mutations": {"move": 4, "rename": 0, "delete": 0},
        "trusted_user_intent": "把 PDF 按类别整理到 final",
    })
    statuses = [check["status"] for check in completion["checks"]]
    assert statuses == ["verified"] * 4, completion["checks"]
    assert completion["outcome"] == "success", completion["message"]
    assert (final / "文档" / "a.pdf").exists()


# ---------------- 6. 拒绝批量审批后不再反复弹窗 ----------------

class _MovePlanner:
    """初次规划 25 个 move 步骤（触发请求级审批）；重规划返回空。"""

    def __init__(self, root: Path, count: int, dest: str) -> None:
        self.root, self.count, self.dest = root, count, dest

    def invoke(self, messages: List[Any]) -> PlanOutput:
        if json.loads(messages[-1].content).get("已获得观察"):
            return PlanOutput(goal="done", steps=[], user_message="完成")
        return PlanOutput(
            goal="批量移动",
            steps=[
                PlanStep(id=f"m{i}", description=f"移动 f{i}", tool="move_file",
                         args={"src": str(self.root / f"f{i}.txt"),
                               "dest_dir": str(self.root / self.dest)})
                for i in range(self.count)
            ],
            user_message="",
        )


class _ContinueReflector:
    def invoke(self, messages: List[Any]) -> ReflectionOutput:
        if json.loads(messages[-1].content).get("是否还有步骤"):
            return ReflectionOutput(decision="continue", reasoning="继续")
        return ReflectionOutput(decision="done", reasoning="结束", final_response="done")


def test_rejecting_batch_approval_does_not_ask_again(
    sandbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for i in range(BATCH_N):
        (sandbox / f"f{i}.txt").write_text(str(i), encoding="utf-8")

    class _LLM:
        def with_structured_output(self, schema: Any) -> Any:
            if schema is PlanOutput:
                return _MovePlanner(sandbox, BATCH_N, "archive")
            return _ContinueReflector()

        def invoke(self, _: Any) -> Any:  # pragma: no cover
            raise AssertionError

    monkeypatch.setattr(graph_module, "build_llm", lambda temperature=0.1: _LLM())
    runtime = graph_module.AgentRuntime()
    for _ in runtime.start_stream("把所有文件归档", "reject-once"):
        pass
    assert runtime.state("reject-once").get("status") == "waiting_approval"

    for _ in runtime.resume_stream("reject-once", "reject"):
        pass

    state = runtime.state("reject-once")
    # 拒绝后一次性收敛：不再停在审批，也不再请求第二次。
    assert state.get("status") == "completed", state.get("error")
    assert state.get("pending_approval") is None
    assert state.get("outcome") == "rejected", state.get("completion")
    observations = state["observations"]
    assert len(observations) == BATCH_N
    assert all(obs["status"] == "rejected" for obs in observations)
    assert not any(obs["tool"] == "move_file" and obs["status"] == "ok" for obs in observations)
    for i in range(BATCH_N):
        assert (sandbox / f"f{i}.txt").exists()
