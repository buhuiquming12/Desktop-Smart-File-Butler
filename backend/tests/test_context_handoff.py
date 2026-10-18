"""P0-2 回归：工具结果 → 规划器的交接。

历史缺陷：扫描摘要丢失 scan_id、reason、scanned_count、完整 summary，导致后续
批处理无法引用清单；同时把“前 20 条样本统计”当成完整统计。
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
from app.tools import filesystem, manifests


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


def _real_scan_observation(directory: Path) -> dict:
    """用真实工具产生观察，确保结构不是测试自造的。"""
    scan = filesystem.scan_directory(str(directory), False)
    items = [item.model_dump(mode="json") for item in scan.items]
    result = manifests.create(str(directory), items, {
        "scanned_count": scan.scanned_count,
        "truncated": scan.truncated,
        "reason": scan.reason,
    })
    result["items"] = result["preview"]
    return graph_module.ToolExecutor.observation(
        {"id": "scan", "description": "扫描", "tool": "scan_directory",
         "args": {"directory": str(directory)}},
        "ok",
        result=result,
    )


def test_scan_summary_keeps_execution_identifiers(sandbox: Path) -> None:
    source = sandbox / "downloads"
    source.mkdir()
    for index in range(230):
        (source / f"doc-{index:03d}.pdf").write_text("pdf", encoding="utf-8")
    for index in range(5):
        (source / f"img-{index}.jpg").write_text("jpg", encoding="utf-8")

    slim = graph_module._summarize_observation(_real_scan_observation(source))
    result = slim["result"]
    assert result["scan_id"], "规划器必须拿到 scan_id 才能生成批处理步骤"
    assert result["total"] == 235
    assert result["scanned_count"] == 235
    assert result["truncated"] is False
    assert result["reason"] == "none"
    # 完整扩展名统计来自 manifest，不是 20 条样本。
    assert result["extension_distribution"] == {"pdf": 230, "jpg": 5}
    assert result["statistics_source"] == "manifest"
    assert len(result["sample"]) == graph_module._PLANNER_SAMPLE
    assert slim["args"]["directory"] == str(source)


def test_truncated_scan_reason_reaches_planner_and_user(
    sandbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(filesystem, "_MAX_SCAN_ITEMS", 10)
    source = sandbox / "many"
    source.mkdir()
    for index in range(50):
        (source / f"f-{index:02d}.txt").write_text("x", encoding="utf-8")

    obs = _real_scan_observation(source)
    assert obs["result"]["truncated"] is True
    slim = graph_module._summarize_observation(obs)["result"]
    assert slim["truncated"] is True
    assert slim["reason"] == "max_items"
    assert slim["scanned_count"] == 10

    # 用户侧的完成判定也必须说明“扫描不完整”及原因。
    from app.agent.outcome import evaluate_completion

    completion = evaluate_completion({
        "observations": [obs],
        "plan": [],
        "step_index": 0,
        "mutations": {"move": 0, "rename": 0, "delete": 0},
    })
    assert completion["outcome"] in {"partial", "failed"}
    assert "max_items" in completion["message"]


def _graph_llm(captured: List[dict], source: Path, dest: Path, n_expected: int):
    class _Planner:
        def invoke(self, messages: List[Any]) -> PlanOutput:
            context = json.loads(messages[-1].content)
            captured.append(context)
            observations = context.get("tool_observations") or []
            scan = next(
                (o for o in observations if o.get("tool") == "scan_directory"), None
            )
            if scan is None:
                return PlanOutput(
                    goal="扫描",
                    steps=[PlanStep(id="scan", description="扫描", tool="scan_directory",
                                    args={"directory": str(source)})],
                    user_message="",
                )
            if any(o.get("tool") == "batch_move" for o in observations):
                return PlanOutput(goal="完成", steps=[], user_message="完成")
            # 关键：只使用 scan_id，不枚举 sample。
            return PlanOutput(
                goal="移动 PDF",
                steps=[PlanStep(
                    id="move", description="移动所有 PDF", tool="batch_move",
                    args={
                        "scan_id": scan["result"]["scan_id"],
                        "filter": {"extensions": ["pdf"]},
                        "dest_dir": str(dest),
                    },
                )],
                user_message="",
            )

    class _Reflector:
        def invoke(self, messages: List[Any]) -> ReflectionOutput:
            context = json.loads(messages[-1].content)
            if context.get("是否还有步骤"):
                return ReflectionOutput(decision="continue", reasoning="继续")
            tools = {str(s.get("tool")) for s in context.get("当前计划") or []}
            if "batch_move" in tools:
                return ReflectionOutput(decision="done", reasoning="完成")
            return ReflectionOutput(decision="replan", reasoning="继续")

    class _LLM:
        def with_structured_output(self, schema: Any) -> Any:
            return _Planner() if schema is PlanOutput else _Reflector()

        def invoke(self, _: Any) -> Any:  # pragma: no cover
            raise AssertionError

    return _LLM()


def test_scan_context_plan_batch_moves_all_matches_beyond_preview(
    sandbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = sandbox / "downloads"
    source.mkdir()
    total = 230
    for index in range(total):
        (source / f"doc-{index:03d}.pdf").write_text("pdf", encoding="utf-8")
    for index in range(5):
        (source / f"img-{index}.jpg").write_text("jpg", encoding="utf-8")

    captured: List[dict] = []
    monkeypatch.setattr(
        graph_module, "build_llm",
        lambda temperature=0.1: _graph_llm(captured, source, sandbox / "PDF", total),
    )
    runtime = graph_module.AgentRuntime()
    for _ in runtime.start_stream("把下载目录里的 PDF 移动到 PDF 文件夹", "handoff"):
        pass

    # 230 项超过默认阈值 20，必须先审批——这本身也是 P0-1 期望的行为。
    assert runtime.state("handoff").get("status") == "waiting_approval"
    for _ in runtime.resume_stream("handoff", "approve"):
        pass

    state = runtime.state("handoff")
    assert state.get("status") == "completed", state.get("error")
    # 规划器看到的是有界摘要，但仍能拿到 scan_id 与完整统计。
    scan_context = captured[-1]
    last_scan = scan_context.get("last_scan") or {}
    assert last_scan.get("scan_id")
    assert last_scan.get("total") == 235
    # 全部 230 个 PDF 都真正移动了，而不是只有预览里的 20 个。
    moved = list((sandbox / "PDF").glob("*.pdf"))
    assert len(moved) == total
    assert len(list(source.glob("*.jpg"))) == 5

    # 批处理结果保留 matched/success/failed/skipped。
    batch_obs = next(
        o for o in state["observations"] if o["tool"] == "batch_move"
    )
    assert batch_obs["result"]["matched"] == total
    assert batch_obs["result"]["success"] == total
    assert batch_obs["result"]["failed"] == 0
    assert batch_obs["result"]["skipped"] == 0
    assert state.get("outcome") == "success"


def test_batch_result_summary_keeps_counts(sandbox: Path) -> None:
    source = sandbox / "src"
    source.mkdir()
    for index in range(10):
        (source / f"f-{index}.pdf").write_text("pdf", encoding="utf-8")
    obs = _real_scan_observation(source)
    scan_id = obs["result"]["scan_id"]
    executor = graph_module.ToolExecutor()
    result = executor.batch_move({
        "scan_id": scan_id,
        "filter": {"extensions": ["pdf"]},
        "dest_dir": str(sandbox / "out"),
    })
    observation = graph_module.ToolExecutor.observation(
        {"id": "m", "tool": "batch_move", "args": {"scan_id": scan_id}}, "ok", result=result
    )
    slim = graph_module._summarize_observation(observation)["result"]
    for key in ("matched", "success", "failed", "skipped", "truncated", "reason"):
        assert key in slim, key
    assert slim["matched"] == 10 and slim["success"] == 10
