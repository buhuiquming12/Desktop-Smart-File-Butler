"""P0-3 回归：可信的完成判定。

要求：区分流程终止与目标完成；部分完成不得显示为全部成功；重规划上限要说明
未完成内容；校验动作本身不产生文件变更；已完成操作仍可查询和撤销。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, List

import pytest

from app import db
from app.agent import graph as graph_module
from app.agent.outcome import collect_stats, evaluate_completion
from app.agent.state import PlanOutput, PlanStep, ReflectionOutput
from app.config import get_settings
from app.tools import manifests


def _batch_obs(matched: int, success: int, failed: int = 0, skipped: int = 0) -> dict:
    return {
        "step_id": "m",
        "tool": "batch_move",
        "description": "批量移动",
        "status": "ok",
        "result": {
            "matched": matched,
            "success": success,
            "failed": failed,
            "skipped": skipped,
            "truncated": False,
            "reason": "none",
            "preview": [],
        },
    }


def test_partial_batch_is_not_reported_as_full_success() -> None:
    state = {
        "observations": [_batch_obs(10, 8, failed=2)],
        "plan": [{"id": "m", "tool": "batch_move", "description": "批量移动"}],
        "step_index": 1,
        "mutations": {"move": 8, "rename": 0, "delete": 0},
    }
    completion = evaluate_completion(state)
    assert completion["outcome"] == "partial"
    assert completion["verified"] is False
    message = completion["message"]
    assert "部分完成" in message
    assert "成功 8" in message and "失败 2" in message
    assert "全部成功" not in message


def test_counts_distinguish_tool_calls_and_files() -> None:
    stats = collect_stats(
        [_batch_obs(10, 8, failed=2), _batch_obs(5, 5)],
        mutations={"move": 13, "rename": 0, "delete": 0},
        plan=[],
        step_index=0,
    )
    assert stats["tool_calls"] == 2
    assert stats["file_stats"] == {"matched": 15, "success": 13, "failed": 2, "skipped": 0}
    assert stats["file_changes"] == 13


def test_truncated_scan_is_partial_and_explains_reason() -> None:
    state = {
        "observations": [{
            "step_id": "s", "tool": "scan_directory", "status": "ok",
            "result": {"scan_id": "abc", "total": 5000, "scanned_count": 5000,
                       "truncated": True, "reason": "max_items"},
        }],
        "plan": [], "step_index": 0, "mutations": {},
    }
    completion = evaluate_completion(state)
    assert completion["outcome"] == "partial"
    assert "max_items" in completion["message"]


def test_replan_limit_explains_unfinished_work() -> None:
    state = {
        "observations": [_batch_obs(10, 8, failed=2)],
        "plan": [
            {"id": "a", "tool": "batch_move", "description": "移动剩余 2 个"},
            {"id": "b", "tool": "write_summary", "description": "生成摘要"},
        ],
        "step_index": 0,
        "mutations": {"move": 8, "rename": 0, "delete": 0},
    }
    completion = evaluate_completion(state, replan_limit_reached=True)
    assert completion["outcome"] == "budget_exhausted"
    assert completion["stop_reason"] == "replan_limit"
    assert len(completion["unexecuted_steps"]) == 2
    assert "2 个已规划步骤未执行" in completion["message"]


def test_rejected_approval_is_its_own_outcome() -> None:
    state = {
        "observations": [{
            "step_id": "d", "tool": "delete_file", "status": "rejected",
            "result": "用户拒绝了危险操作，未修改文件",
        }],
        "plan": [{"id": "d", "tool": "delete_file", "description": "删除"}],
        "step_index": 1,
        "mutations": {},
    }
    completion = evaluate_completion(state)
    assert completion["outcome"] == "rejected"
    assert completion["stop_reason"] == "user_rejected"


def test_verification_is_read_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SANDBOX_ROOTS", str(tmp_path))
    monkeypatch.setenv("DB_PATH", str(tmp_path / "butler.db"))
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    get_settings.cache_clear()
    db._initialized = False
    db.init_db()

    source = tmp_path / "a.txt"
    source.write_text("x", encoding="utf-8")
    before = {p.name: p.stat().st_mtime_ns for p in tmp_path.iterdir() if p.is_file()}
    evaluate_completion({
        "observations": [
            {"step_id": "m", "tool": "move_file", "status": "ok",
             "result": str(source), "args": {"src": str(source)}},
        ],
        "plan": [], "step_index": 1,
        "mutations": {"move": 1, "rename": 0, "delete": 0},
    })
    after = {p.name: p.stat().st_mtime_ns for p in tmp_path.iterdir() if p.is_file()}
    assert before == after, "完成校验不得修改文件"


class _PartialFailPlanner:
    def __init__(self, source: Path, dest: Path) -> None:
        self.source, self.dest = source, dest

    def invoke(self, messages: List[Any]) -> PlanOutput:
        context = json.loads(messages[-1].content)
        observations = context.get("tool_observations") or []
        scan = next((o for o in observations if o.get("tool") == "scan_directory"), None)
        if scan is None:
            return PlanOutput(goal="扫描", steps=[
                PlanStep(id="scan", description="扫描", tool="scan_directory",
                         args={"directory": str(self.source)})
            ], user_message="")
        if any(o.get("tool") == "batch_move" for o in observations):
            return PlanOutput(goal="完成", steps=[], user_message="全部成功！")
        return PlanOutput(goal="移动", steps=[
            PlanStep(id="mv", description="移动全部", tool="batch_move", args={
                "scan_id": scan["result"]["scan_id"],
                "filter": {"extensions": ["pdf"]},
                "dest_dir": str(self.dest),
            })
        ], user_message="")


class _DoneReflector:
    def invoke(self, messages: List[Any]) -> ReflectionOutput:
        context = json.loads(messages[-1].content)
        if context.get("是否还有步骤"):
            return ReflectionOutput(decision="continue", reasoning="继续")
        tools = {str(s.get("tool")) for s in context.get("当前计划") or []}
        if "batch_move" in tools:
            # 模型声称全部成功，但真实结果只有 8/10。
            return ReflectionOutput(
                decision="done", reasoning="完成", final_response="10 个文件已全部成功移动。"
            )
        return ReflectionOutput(decision="replan", reasoning="继续")


def test_model_claim_of_success_is_overridden_by_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SANDBOX_ROOTS", str(tmp_path))
    monkeypatch.setenv("DB_PATH", str(tmp_path / "butler.db"))
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    get_settings.cache_clear()
    db._initialized = False
    db.init_db()

    source = tmp_path / "src"
    source.mkdir()
    for index in range(10):
        (source / f"f-{index}.pdf").write_text("pdf", encoding="utf-8")

    class _LLM:
        def with_structured_output(self, schema: Any) -> Any:
            if schema is PlanOutput:
                return _PartialFailPlanner(source, tmp_path / "out")
            return _DoneReflector()

        def invoke(self, _: Any) -> Any:  # pragma: no cover
            raise AssertionError

    monkeypatch.setattr(graph_module, "build_llm", lambda temperature=0.1: _LLM())
    runtime = graph_module.AgentRuntime()

    # 先让图跑完扫描并停在计划上，再制造 2 个“扫描后消失”的文件，模拟部分失败。
    iterator = runtime.start_stream("把 PDF 移动到 out 目录", "partial")
    for _ in iterator:
        pass
    state = runtime.state("partial")
    if state.get("status") == "waiting_approval":  # 阈值内，不应发生
        raise AssertionError("10 项不应触发审批")
    assert state.get("status") == "completed", state.get("error")

    observations = state["observations"]
    batch = next(o for o in observations if o["tool"] == "batch_move")["result"]
    assert batch["matched"] == 10 and batch["success"] == 10

    # 直接验证确定性判定：把成功数改成 8，模型措辞必须被覆盖。
    completion = evaluate_completion({
        "observations": [_batch_obs(10, 8, skipped=2)],
        "plan": [{"id": "mv", "tool": "batch_move", "description": "移动全部"}],
        "step_index": 1,
        "mutations": {"move": 8, "rename": 0, "delete": 0},
    })
    assert completion["outcome"] == "partial"
    assert "全部成功" not in completion["message"]


def test_real_skipped_files_are_reported_as_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """扫描后删掉 2 个文件：批量结果 skipped=2，完成判定必须是部分完成。"""
    monkeypatch.setenv("SANDBOX_ROOTS", str(tmp_path))
    monkeypatch.setenv("DB_PATH", str(tmp_path / "butler.db"))
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    get_settings.cache_clear()
    db._initialized = False
    db.init_db()

    source = tmp_path / "src"
    source.mkdir()
    for index in range(10):
        (source / f"f-{index}.pdf").write_text("pdf", encoding="utf-8")
    scan = graph_module.ToolExecutor().execute(
        {"id": "s", "tool": "scan_directory", "args": {"directory": str(source)}}
    )
    scan_id = scan["result"]["scan_id"]
    (source / "f-8.pdf").unlink()
    (source / "f-9.pdf").unlink()
    moved = graph_module.ToolExecutor().execute({
        "id": "m", "tool": "batch_move",
        "args": {"scan_id": scan_id, "filter": {"extensions": ["pdf"]},
                 "dest_dir": str(tmp_path / "out")},
    })
    completion = evaluate_completion({
        "observations": [scan, moved],
        "plan": [{"id": "m", "tool": "batch_move", "description": "移动全部"}],
        "step_index": 1,
        "mutations": {"move": 8, "rename": 0, "delete": 0},
    })
    assert moved["result"]["skipped"] == 2
    assert completion["outcome"] == "partial"
    assert "跳过 2" in completion["message"]

    # 已完成操作仍可查询与撤销。
    ops = db.recent_operations(50)
    assert sum(1 for op in ops if op.action == "move" and op.status == "ok") == 8
