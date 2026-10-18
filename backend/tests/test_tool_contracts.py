"""P0-1 回归：工具契约、权限覆盖与累计审批。

覆盖此前确认的缺陷：
- batch_classify 实际移动文件，却未登记为 move 侧副作用，可绕过移动授权与累计审批。
- 未知工具 / 缺参数 / 参数类型错误曾被当成“可执行”。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, List

import pytest

from app import db
from app.agent import graph as graph_module
from app.agent.state import PlanOutput, PlanStep, ReflectionOutput
from app.agent.tool_executor import ToolExecutor
from app.config import get_settings
from app.policy import PolicyEngine
from app.tools import manifests
from app.tools.contracts import MUTATION_KIND, TOOL_CONTRACTS, validate_tool_args


def _state(intent: str, **extra: Any) -> dict:
    return {
        "trusted_user_intent": intent,
        "mutations": {"move": 0, "rename": 0, "delete": 0},
        "approved_mutation_limit": 0,
        **extra,
    }


def test_every_dispatchable_tool_has_a_contract() -> None:
    """工具执行器的每个分支都必须有契约，防止新增工具漏登记副作用。"""
    source = Path("app/agent/tool_executor.py").read_text(encoding="utf-8")
    dispatched = {
        line.split('== "')[1].split('"')[0]
        for line in source.splitlines()
        if 'tool == "' in line
    }
    assert dispatched <= set(TOOL_CONTRACTS), dispatched - set(TOOL_CONTRACTS)


def test_batch_classify_is_registered_as_move() -> None:
    assert MUTATION_KIND["batch_classify"] == "move"
    assert TOOL_CONTRACTS["batch_classify"].side_effect == "move"
    assert TOOL_CONTRACTS["batch_classify"].is_cumulative


def test_unknown_tool_is_rejected() -> None:
    decision = PolicyEngine(20).evaluate("rm_rf_everything", state=_state("整理文件"))
    assert decision.allowed is False
    assert "未知工具" in decision.reason


def test_invalid_args_are_rejected_before_execution(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SANDBOX_ROOTS", str(tmp_path))
    monkeypatch.setenv("DB_PATH", str(tmp_path / "butler.db"))
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    get_settings.cache_clear()
    db._initialized = False
    db.init_db()

    decision = PolicyEngine(20).evaluate(
        "move_file",
        state=_state("移动文件"),
        args={"src": str(tmp_path / "a.txt")},  # 缺 dest_dir
    )
    assert decision.allowed is False
    assert "参数无效" in decision.reason

    # 执行器同样必须失败，而不是带着残缺参数动文件。
    result = ToolExecutor().execute(
        {"id": "s", "description": "坏参数", "tool": "move_file", "args": {"src": "x"}}
    )
    assert result["status"] == "failed"
    assert "参数无效" in result["error"]

    unknown = ToolExecutor().execute(
        {"id": "s", "description": "未知工具", "tool": "not_a_tool", "args": {}}
    )
    assert unknown["status"] == "failed"
    assert "未知工具" in unknown["error"]


def test_extra_fields_are_rejected() -> None:
    args, error = validate_tool_args(
        "scan_directory", {"directory": "C:/tmp", "recursive": False, "rm": True}
    )
    assert args is None
    assert "rm" in error


def test_summary_only_request_cannot_authorize_classify_move() -> None:
    decision = PolicyEngine(20).evaluate(
        "batch_classify", state=_state("请总结这个目录里的文档"), proposed_count=5
    )
    assert decision.allowed is False
    assert "未授权 move" in decision.reason


def test_classify_counts_towards_cumulative_approval() -> None:
    policy = PolicyEngine(20)
    first = policy.evaluate(
        "batch_classify", state=_state("把文件按类型分类整理"), proposed_count=15
    )
    assert first.allowed and not first.requires_approval
    second_state = _state(
        "把文件按类型分类整理", mutations={"move": 15, "rename": 0, "delete": 0}
    )
    second = policy.evaluate("batch_classify", state=second_state, proposed_count=10)
    assert second.requires_approval, "分类移动必须与 move 共用累计阈值"
    assert "25" in second.reason


class _ClassifyPlanner:
    def __init__(self, root: Path, dest: Path) -> None:
        self.root, self.dest = root, dest

    def invoke(self, messages: List[Any]) -> PlanOutput:
        context = json.loads(messages[-1].content)
        observations = context.get("tool_observations") or []
        scan = next(
            (obs for obs in observations if obs.get("tool") == "scan_directory"),
            None,
        )
        if scan is None:
            return PlanOutput(
                goal="扫描",
                steps=[
                    PlanStep(id="scan", description="扫描", tool="scan_directory",
                             args={"directory": str(self.root)})
                ],
                user_message="",
            )
        if any(obs.get("tool") == "batch_classify" for obs in observations):
            return PlanOutput(goal="分类", steps=[], user_message="完成")
        scan_id = scan["result"]["scan_id"]
        return PlanOutput(
            goal="分类",
            steps=[
                PlanStep(id="classify", description="按扩展名分类", tool="batch_classify",
                         args={"scan_id": scan_id, "dest_root": str(self.dest)})
            ],
            user_message="",
        )


class _ReplanningReflector:
    """计划执行完后，只要目标工具还没出现就重规划；出现后结束。"""

    def __init__(self, target_tool: str) -> None:
        self.target_tool = target_tool

    def invoke(self, messages: List[Any]) -> ReflectionOutput:
        context = json.loads(messages[-1].content)
        if context.get("是否还有步骤"):
            return ReflectionOutput(decision="continue", reasoning="继续")
        tools = {
            str(step.get("tool"))
            for step in context.get("当前计划") or []
        }
        if self.target_tool in tools:
            return ReflectionOutput(decision="done", reasoning="结束")
        return ReflectionOutput(decision="replan", reasoning="需要下一步")


def _runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, n: int, dest: str = "sorted"):
    monkeypatch.setenv("SANDBOX_ROOTS", str(tmp_path))
    monkeypatch.setenv("DB_PATH", str(tmp_path / "butler.db"))
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    get_settings.cache_clear()
    db._initialized = False
    db.init_db()
    batch = tmp_path / "batch"
    batch.mkdir()
    for index in range(n):
        (batch / f"file-{index:02d}.txt").write_text("x", encoding="utf-8")

    class _LLM:
        def with_structured_output(self, schema: Any) -> Any:
            if schema is PlanOutput:
                return _ClassifyPlanner(batch, tmp_path / dest)
            return _ReplanningReflector("batch_classify")

        def invoke(self, _: Any) -> Any:  # pragma: no cover
            raise AssertionError

    monkeypatch.setattr(graph_module, "build_llm", lambda temperature=0.1: _LLM())
    return graph_module.AgentRuntime(), batch


def test_classify_over_threshold_waits_before_any_move(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, batch = _runtime(tmp_path, monkeypatch, 25)
    for _ in runtime.start_stream("把这些文件按类型分类整理", "classify-approval"):
        pass
    state = runtime.state("classify-approval")
    assert state.get("status") == "waiting_approval"
    pending = state.get("pending_approval") or {}
    assert pending.get("action") == "batch_classify"
    assert pending.get("count") == 25
    # 审批前一个文件都不能动。
    assert len(list(batch.iterdir())) == 25


def test_classify_reject_keeps_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, batch = _runtime(tmp_path, monkeypatch, 25)
    for _ in runtime.start_stream("把这些文件按类型分类整理", "classify-reject"):
        pass
    for _ in runtime.resume_stream("classify-reject", "reject"):
        pass
    state = runtime.state("classify-reject")
    assert len(list(batch.iterdir())) == 25
    assert state.get("outcome") in {"rejected", "partial"}
    assert not any(op.action == "move" and op.status == "ok" for op in db.recent_operations(100))


def test_classify_approve_moves_all(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, batch = _runtime(tmp_path, monkeypatch, 25)
    for _ in runtime.start_stream("把这些文件按类型分类整理", "classify-approve"):
        pass
    for _ in runtime.resume_stream("classify-approve", "approve"):
        pass
    assert list(batch.iterdir()) == []
    moved = list((tmp_path / "sorted").rglob("*.txt"))
    assert len(moved) == 25


def test_summary_only_request_blocks_classify_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """用户只说“总结”，即使模型计划了分类移动，图也必须拒绝且不动文件。"""
    monkeypatch.setenv("SANDBOX_ROOTS", str(tmp_path))
    monkeypatch.setenv("DB_PATH", str(tmp_path / "butler.db"))
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    get_settings.cache_clear()
    db._initialized = False
    db.init_db()
    source = tmp_path / "docs"
    source.mkdir()
    for index in range(3):
        (source / f"a-{index}.pdf").write_text("x", encoding="utf-8")

    class _Planner:
        def invoke(self, _: List[Any]) -> PlanOutput:
            # 这里故意跳过扫描，直接分类移动；策略必须在执行前拒绝。
            return PlanOutput(goal="分类", steps=[PlanStep(
                id="classify", description="分类", tool="batch_classify",
                args={"scan_id": "not-used", "dest_root": str(tmp_path / "sorted")},
            )], user_message="")

    class _Reflector:
        def invoke(self, _: List[Any]) -> ReflectionOutput:
            return ReflectionOutput(decision="done", reasoning="结束")

    class _LLM:
        def with_structured_output(self, schema: Any) -> Any:
            return _Planner() if schema is PlanOutput else _Reflector()

        def invoke(self, _: Any) -> Any:  # pragma: no cover
            raise AssertionError

    monkeypatch.setattr(graph_module, "build_llm", lambda temperature=0.1: _LLM())
    runtime = graph_module.AgentRuntime()
    for _ in runtime.start_stream("请总结这个目录里的文档", "summary-only"):
        pass

    state = runtime.state("summary-only")
    observation = state["observations"][0]
    assert observation["status"] == "failed"
    assert "未授权 move" in observation["error"]
    assert len(list(source.iterdir())) == 3
    assert not (tmp_path / "sorted").exists()


def test_replan_does_not_reset_cumulative_approval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """同一请求内先移动 15 项，重规划后再分类 10 项，必须累计到 25 触发审批。"""
    monkeypatch.setenv("SANDBOX_ROOTS", str(tmp_path))
    monkeypatch.setenv("DB_PATH", str(tmp_path / "butler.db"))
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    get_settings.cache_clear()
    db._initialized = False
    db.init_db()
    source = tmp_path / "src"
    source.mkdir()
    for index in range(15):
        (source / f"a-{index}.txt").write_text("x", encoding="utf-8")
    (source / "b.pdf").write_text("pdf", encoding="utf-8")

    class _Planner:
        def invoke(self, messages: List[Any]) -> PlanOutput:
            context = json.loads(messages[-1].content)
            observations = context.get("tool_observations") or []
            if not observations:
                return PlanOutput(
                    goal="移动", steps=[
                        PlanStep(id="scan", description="扫描", tool="scan_directory",
                                 args={"directory": str(source)})
                    ], user_message="")
            scan = next(o for o in observations if o["tool"] == "scan_directory")
            scan_id = scan["result"]["scan_id"]
            if len(observations) == 1:
                return PlanOutput(
                    goal="先移动", steps=[
                        PlanStep(id="mv", description="移动 txt", tool="batch_move",
                                 args={"scan_id": scan_id,
                                       "filter": {"extensions": ["txt"]},
                                       "dest_dir": str(tmp_path / "txt")})
                    ], user_message="")
            return PlanOutput(
                goal="再分类", steps=[
                    PlanStep(id="cls", description="分类剩余", tool="batch_classify",
                             args={"scan_id": scan_id, "dest_root": str(tmp_path / "sorted")})
                ], user_message="")

    class _Reflector:
        def invoke(self, messages: List[Any]) -> ReflectionOutput:
            context = json.loads(messages[-1].content)
            if context.get("是否还有步骤"):
                return ReflectionOutput(decision="continue", reasoning="继续")
            tools = {str(step.get("tool")) for step in context.get("当前计划") or []}
            # 先移动，再分类；两者都规划过才结束。
            if "batch_classify" in tools:
                return ReflectionOutput(decision="done", reasoning="完成")
            return ReflectionOutput(decision="replan", reasoning="继续分类")

    class _LLM:
        def with_structured_output(self, schema: Any) -> Any:
            return _Planner() if schema is PlanOutput else _Reflector()

        def invoke(self, _: Any) -> Any:  # pragma: no cover
            raise AssertionError

    monkeypatch.setattr(graph_module, "build_llm", lambda temperature=0.1: _LLM())
    runtime = graph_module.AgentRuntime()
    for _ in runtime.start_stream("把 txt 移动后，其余文件按类型分类整理", "cumulative"):
        pass
    state = runtime.state("cumulative")
    assert state.get("mutations", {}).get("move") == 15
    assert state.get("status") == "waiting_approval", state.get("final_response")
    pending = state.get("pending_approval") or {}
    assert pending.get("action") == "batch_classify"
    # manifest 仍包含已移动的 15 个 txt（按清单确定性匹配），因此本次匹配 16 项、
    # 累计 31 项；审批数量取“将尝试的匹配数”，只会更保守，不会少报。
    assert pending.get("count") == 16
    assert pending.get("approved_limit") == 31
