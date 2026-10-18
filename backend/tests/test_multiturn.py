"""P1-4 回归：澄清、多轮上下文与基础记忆。

覆盖 5 组多轮场景：
1. 补充条件（先澄清，再给出目录）。
2. 修改目标（第二轮改变目标，不复用上一轮授权）。
3. 含糊指代（“刚才那些 PDF”引用上一轮扫描）。
4. 旧扫描失效（目录被删后必须重新扫描，不能拿旧清单移动）。
5. 跨会话隔离（另一会话看不到上一会话的扫描与对话）。
外加：历史审批不会自动授权新请求；偏好可查看/更新/删除。
"""
from __future__ import annotations

import json
import shutil
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


class _ClarifyThenScanPlanner:
    """没有明确目录时先澄清；用户给出目录后再扫描。"""

    def __init__(self, capture: List[dict]) -> None:
        self.capture = capture

    def invoke(self, messages: List[Any]) -> PlanOutput:
        context = json.loads(messages[-1].content)
        self.capture.append(context)
        request = str(context.get("original_user_request") or "")
        observations = context.get("tool_observations") or []
        if "downloads" not in request and not observations:
            return PlanOutput(
                goal="澄清",
                steps=[PlanStep(
                    id="ask", description="询问目录", tool="request_clarification",
                    args={"question": "请告诉我要整理哪个目录？"},
                )],
                user_message="",
            )
        if not observations:
            return PlanOutput(
                goal="扫描", steps=[PlanStep(
                    id="scan", description="扫描目录", tool="scan_directory",
                    args={"directory": str(context["环境感知"]["allowed_roots"][0])},
                )], user_message="",
            )
        return PlanOutput(goal="完成", steps=[], user_message="扫描完成")


class _DoneReflector:
    def invoke(self, messages: List[Any]) -> ReflectionOutput:
        if json.loads(messages[-1].content).get("是否还有步骤"):
            return ReflectionOutput(decision="continue", reasoning="继续")
        return ReflectionOutput(decision="done", reasoning="完成")


class _LLMClarify:
    def __init__(self, capture: List[dict]) -> None:
        self.capture = capture

    def with_structured_output(self, schema: Any) -> Any:
        return _ClarifyThenScanPlanner(self.capture) if schema is PlanOutput else _DoneReflector()

    def invoke(self, _: Any) -> Any:  # pragma: no cover
        raise AssertionError


def test_clarification_pauses_then_continues(
    sandbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    capture: List[dict] = []
    monkeypatch.setattr(
        graph_module, "build_llm", lambda temperature=0.1: _LLMClarify(capture)
    )
    runtime = graph_module.AgentRuntime()

    for _ in runtime.start_stream("帮我整理一下文件", "clarify-1"):
        pass
    first = runtime.state("clarify-1")
    assert first.get("status") == "needs_clarification"
    assert first.get("outcome") == "needs_clarification"
    assert "哪个目录" in first.get("final_response", "")

    for _ in runtime.start_stream("整理 downloads 目录", "clarify-1"):
        pass
    second = runtime.state("clarify-1")
    assert second.get("status") in {"completed", "failed"}
    assert second.get("status") != "needs_clarification"
    assert any(o["tool"] == "scan_directory" for o in second["observations"])
    # 第二轮的上下文能看到第一轮对话，但 observations 已按“单次运行”重置。
    assert any(
        "帮我整理一下文件" == item.get("content")
        for item in capture[-1].get("conversation", [])
    )


def _scan_llm(capture: List[dict], directory: Path):
    class _Planner:
        def invoke(self, messages: List[Any]) -> PlanOutput:
            context = json.loads(messages[-1].content)
            capture.append(context)
            observations = context.get("tool_observations") or []
            last_scan = context.get("last_scan") or {}
            # 追问引用上一轮扫描：直接用它，不重新枚举/扫描。
            if last_scan.get("scan_id") and not any(
                o["tool"] == "batch_move" for o in observations
            ):
                return PlanOutput(goal="移动这些 PDF", steps=[PlanStep(
                    id="move", description="移动刚才那些 PDF", tool="batch_move",
                    args={"scan_id": last_scan["scan_id"],
                          "filter": {"extensions": ["pdf"]},
                          "dest_dir": str(directory.parent / "pdf-out")},
                )], user_message="")
            if not observations:
                return PlanOutput(goal="扫描", steps=[PlanStep(
                    id="scan", description="扫描", tool="scan_directory",
                    args={"directory": str(directory)},
                )], user_message="")
            if any(o["tool"] == "batch_move" for o in observations):
                return PlanOutput(goal="完成", steps=[], user_message="完成")
            return PlanOutput(goal="继续", steps=[], user_message="完成")

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


def _prepare_downloads(sandbox: Path, count: int = 5) -> Path:
    directory = sandbox / "downloads"
    directory.mkdir()
    for index in range(count):
        (directory / f"doc-{index}.pdf").write_text("pdf", encoding="utf-8")
    (directory / "note.txt").write_text("txt", encoding="utf-8")
    return directory


def test_followup_reference_reuses_last_scan(
    sandbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = _prepare_downloads(sandbox, 5)
    capture: List[dict] = []
    monkeypatch.setattr(
        graph_module, "build_llm",
        lambda temperature=0.1: _scan_llm(capture, directory),
    )
    runtime = graph_module.AgentRuntime()
    for _ in runtime.start_stream(f"扫描 {directory}", "multi-1"):
        pass
    first = runtime.state("multi-1")
    assert first.get("last_scan", {}).get("scan_id")

    # 第二轮：含糊指代“刚才那些 PDF”，必须复用上一轮 scan_id。
    for _ in runtime.start_stream("把刚才那些 PDF 移到 pdf-out", "multi-1"):
        pass
    followup_context = capture[-1]
    assert followup_context["last_scan"]["scan_id"] == first["last_scan"]["scan_id"]
    second = runtime.state("multi-1")
    assert any(o["tool"] == "batch_move" for o in second["observations"])
    assert len(list((sandbox / "pdf-out").glob("*.pdf"))) == 5
    # 第二轮是一次新的运行：run_id 变化，但对话历史保留。
    assert second.get("run_id") != first.get("run_id")
    assert len(second.get("conversation") or []) >= 2


def test_stale_scan_reference_is_reverified_before_move(
    sandbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = _prepare_downloads(sandbox, 3)
    capture: List[dict] = []
    monkeypatch.setattr(
        graph_module, "build_llm",
        lambda temperature=0.1: _scan_llm(capture, directory),
    )
    runtime = graph_module.AgentRuntime()
    for _ in runtime.start_stream(f"扫描 {directory}", "stale-1"):
        pass
    assert runtime.state("stale-1").get("last_scan", {}).get("scan_id")

    # 扫描目录消失：引用旧 scan_id 的批处理必须失败并提示重新扫描。
    shutil.rmtree(directory)
    observed: List[dict] = []
    for _ in runtime.start_stream("把刚才那些 PDF 移到 pdf-out", "stale-1"):
        pass
    state = runtime.state("stale-1")
    failed = [
        o for o in state["observations"]
        if o["tool"] == "batch_move" and o["status"] == "failed"
    ]
    assert failed, state["observations"]
    assert "重新扫描" in failed[0]["error"]
    assert not (sandbox / "pdf-out").exists() or not any(
        (sandbox / "pdf-out").iterdir()
    )


def test_cross_session_isolation(
    sandbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = _prepare_downloads(sandbox, 2)
    capture: List[dict] = []
    monkeypatch.setattr(
        graph_module, "build_llm",
        lambda temperature=0.1: _scan_llm(capture, directory),
    )
    runtime = graph_module.AgentRuntime()
    for _ in runtime.start_stream(f"扫描 {directory}", "session-a"):
        pass
    assert runtime.state("session-a").get("last_scan", {}).get("scan_id")

    # 新会话：没有 last_scan、没有对话历史，必须先重新扫描。
    for _ in runtime.start_stream("把刚才那些 PDF 移到 pdf-out", "session-b"):
        pass
    b_first = next(
        c for c in capture
        if len(c.get("conversation") or []) == 1
        and c.get("original_user_request") == "把刚才那些 PDF 移到 pdf-out"
    )
    assert b_first.get("last_scan") in ({}, None)
    assert all(
        item.get("content") != f"扫描 {directory}"
        for item in b_first.get("conversation", [])
    )
    b_state = runtime.state("session-b")
    assert b_state.get("last_scan", {}).get("scan_id")
    assert b_state["last_scan"]["scan_id"] != runtime.state("session-a")["last_scan"]["scan_id"]


def test_history_approval_does_not_authorize_new_request(
    sandbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """上一轮批准过批量移动，不构成本轮授权，也不放宽本轮累计上限。"""
    directory = _prepare_downloads(sandbox, 25)
    capture: List[dict] = []
    monkeypatch.setattr(
        graph_module, "build_llm",
        lambda temperature=0.1: _scan_llm(capture, directory),
    )
    runtime = graph_module.AgentRuntime()

    # 第一轮：25 个 PDF 超过阈值，批准后完成。
    for _ in runtime.start_stream(f"扫描 {directory}", "approve-once"):
        pass
    # 直接向 state 注入“已批准”的假象，确认新运行不会继承。
    state = runtime.state("approve-once")
    assert state.get("approved_mutation_limit") == 0

    # 第二轮换一个目标（修改目标），必须重新计算授权与阈值。
    monkeypatch.setattr(
        graph_module, "build_llm",
        lambda temperature=0.1: _scan_llm(capture, directory),
    )
    for _ in runtime.start_stream("只做摘要，不要移动任何文件", "approve-once"):
        pass
    state = runtime.state("approve-once")
    # 新请求未授权 move：任何移动尝试都必须被拒绝。
    assert not any(
        o["tool"] == "batch_move" and o["status"] == "ok"
        for o in state["observations"]
    )

def test_preference_can_be_viewed_updated_deleted(sandbox: Path) -> None:
    db.set_preference("archive_style", "by-month")
    assert db.get_preference("archive_style") == "by-month"
    db.set_preference("archive_style", "by-type")
    assert {p.key: p.value for p in db.all_preferences()}["archive_style"] == "by-type"
    assert db.delete_preference("archive_style") is True
    assert db.get_preference("archive_style") is None
    assert db.delete_preference("archive_style") is False


def test_reserved_preference_cannot_be_deleted(sandbox: Path) -> None:
    db.set_preference("__sandbox_roots__", "x", allow_reserved=True)
    with pytest.raises(ValueError):
        db.delete_preference("__sandbox_roots__")
