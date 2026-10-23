"""复审意见回归（第九轮）：密钥落盘、审批执行集合、过滤/白名单与预览授权。

覆盖：
1. 模型异常里带密钥时，state / final_response / checkpoints.sqlite 都不得出现明文；
2. 累计审批必须绑定整条计划的执行集合（21 个单文件移动），审批期间修改第 2 个
   文件必须让审批失效，而不是只核验第 1 个；
3. 授权范围的过滤条件（“只移动 PDF”）必须同样约束 move_file / rename_file 等
   单文件工具；
4. 用户点名的文件不能被扩大成“整个父目录”：同目录其它文件的删除仍被拒绝；
5. 预览的确认与执行都要重新校验具体操作副作用、源文件范围、过滤条件与数量上限；
6. 带引号的路径（``organize "C:\\目录"``）不能再抛 AttributeError；
7. 执行器对变更类工具强制要求有效 scope，缺省/空 scope 一律 fail closed；
8. FastAPI 422 校验错误不得回显 ``input``（超长 api_key 会随响应泄漏）。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterator, List

import pytest
from fastapi.testclient import TestClient

from app import db
from app.agent import graph as graph_module
from app.agent.state import PlanOutput, PlanStep, ReflectionOutput
from app.agent.tool_executor import ToolExecutor
from app.config import get_settings
from app.main import app, get_session_token
from app.policy.approval import verify_pending_approval
from app.policy.authorization import (
    AuthorizationScope,
    authorize_preview_selection,
    authorize_tool_call,
    build_scope,
    extract_paths,
)
from app.security import secrets as secret_mod
from app.tools import preview

SECRET = "LEAKED-PLANNER-SECRET-9f3a2b"


@pytest.fixture()
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    monkeypatch.setenv("SANDBOX_ROOTS", str(tmp_path))
    monkeypatch.setenv("DB_PATH", str(tmp_path / "butler.db"))
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    get_settings.cache_clear()
    db._initialized = False
    db.init_db()
    secret_mod.clear_registered_secrets()
    yield tmp_path
    secret_mod.clear_registered_secrets()
    get_settings.cache_clear()
    db._initialized = False


def _scope(intent: str) -> AuthorizationScope:
    return build_scope(intent, request_id="rid", thread_id="tid")


# ---------------- 1. 密钥不落 checkpoint ----------------


def test_planner_error_never_reaches_checkpoint(
    sandbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """模型异常带着已知密钥时，error / final_response / checkpoint 都不能有明文。"""
    secret_mod.register_secret(SECRET)

    class _Planner:
        def invoke(self, _messages: List[Any]) -> PlanOutput:
            raise RuntimeError(f"upstream 401: invalid api key {SECRET}")

    class _LLM:
        def with_structured_output(self, schema: Any) -> Any:
            return _Planner()

        def invoke(self, _: Any) -> Any:  # pragma: no cover
            raise AssertionError("不应直接调用 llm.invoke")

    monkeypatch.setattr(graph_module, "build_llm", lambda temperature=0.1: _LLM())
    runtime = graph_module.AgentRuntime()
    for _ in runtime.start_stream("扫描一下这个目录", "secret-plan-error"):
        pass

    state = runtime.state("secret-plan-error")
    assert state.get("status") == "failed"
    blob = json.dumps(state, ensure_ascii=False, default=str)
    assert SECRET not in blob
    assert SECRET not in str(state.get("error") or "")
    assert SECRET not in str(state.get("final_response") or "")
    assert "REDACTED" in blob

    db_dir = Path(get_settings().db_path).parent
    raw = b"".join(path.read_bytes() for path in db_dir.glob("*.sqlite*"))
    assert SECRET.encode() not in raw


# ---------------- 2. 累计审批绑定整条执行集合 ----------------

BATCH_N = 21  # > 默认阈值 20


class _MovePlanner:
    def __init__(self, root: Path, count: int) -> None:
        self.root, self.count = root, count

    def invoke(self, messages: List[Any]) -> PlanOutput:
        if json.loads(messages[-1].content).get("已获得观察"):
            return PlanOutput(goal="done", steps=[], user_message="完成")
        return PlanOutput(
            goal="批量移动",
            steps=[
                PlanStep(
                    id=f"m{i}",
                    description=f"移动 f{i}",
                    tool="move_file",
                    args={"src": str(self.root / f"f{i}.txt"),
                          "dest_dir": str(self.root / "archive")},
                )
                for i in range(self.count)
            ],
            user_message="",
        )


class _ContinueReflector:
    def invoke(self, messages: List[Any]) -> ReflectionOutput:
        if json.loads(messages[-1].content).get("是否还有步骤"):
            return ReflectionOutput(decision="continue", reasoning="继续")
        return ReflectionOutput(decision="done", reasoning="结束", final_response="done")


def _batch_runtime(root: Path, monkeypatch: pytest.MonkeyPatch, count: int = BATCH_N):
    monkeypatch.setenv("SANDBOX_ROOTS", str(root))
    monkeypatch.setenv("DB_PATH", str(root / "butler.db"))
    monkeypatch.setenv("LOG_DIR", str(root / "logs"))
    get_settings.cache_clear()
    db._initialized = False
    db.init_db()
    for index in range(count):
        (root / f"f{index}.txt").write_text(str(index), encoding="utf-8")

    class _LLM:
        def with_structured_output(self, schema: Any) -> Any:
            return _MovePlanner(root, count) if schema is PlanOutput else _ContinueReflector()

        def invoke(self, _: Any) -> Any:  # pragma: no cover
            raise AssertionError("不应直接调用 llm.invoke")

    monkeypatch.setattr(graph_module, "build_llm", lambda temperature=0.1: _LLM())
    return graph_module.AgentRuntime()


def test_cumulative_approval_binds_every_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = _batch_runtime(tmp_path, monkeypatch)
    for _ in runtime.start_stream("把所有文件归档", "cum-bind"):
        pass
    pending = runtime.state("cum-bind").get("pending_approval") or {}
    assert pending.get("batch") is True
    assert len(pending.get("coverage", {}).get("sources") or []) == BATCH_N
    # 快照必须覆盖全部 21 个文件，而不是只覆盖当前这一步。
    assert len(pending.get("selected_paths") or []) == BATCH_N
    assert {Path(item).name for item in pending["selected_paths"]} == {
        f"f{index}.txt" for index in range(BATCH_N)
    }


def test_change_during_approval_voids_the_whole_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """审批期间修改第 2 个文件：整份审批失效，一个文件都不能移动。"""
    runtime = _batch_runtime(tmp_path, monkeypatch)
    for _ in runtime.start_stream("把所有文件归档", "cum-stale"):
        pass
    assert (runtime.state("cum-stale").get("status")) == "waiting_approval"

    (tmp_path / "f1.txt").write_text("changed while waiting for approval", encoding="utf-8")
    for _ in runtime.resume_stream("cum-stale", "approve"):
        pass

    state = runtime.state("cum-stale")
    assert state.get("status") != "waiting_approval"
    rejected = [
        item for item in (state.get("observations") or [])
        if item.get("status") == "rejected"
    ]
    assert rejected, state.get("observations")
    assert "审批后校验失败" in str(rejected[0].get("result") or "")
    for index in range(BATCH_N):
        assert (tmp_path / f"f{index}.txt").exists()
    archive = tmp_path / "archive"
    assert list(archive.iterdir()) if archive.exists() else [] == []


def test_step_outside_approved_set_is_rejected(sandbox: Path) -> None:
    approved = {"sources": [str(sandbox / "a.txt")], "done": []}
    violation = graph_module.AgentRuntime._approved_batch_violation(
        {"approved_batch": approved},
        "move_file",
        {"src": str(sandbox / "b.txt"), "dest_dir": str(sandbox / "out")},
        requires_approval=False,
    )
    assert "不在已审批的执行集合内" in violation


def test_already_executed_file_is_not_repeated(sandbox: Path) -> None:
    approved = {"sources": [str(sandbox / "a.txt")], "done": [str(sandbox / "a.txt")]}
    violation = graph_module.AgentRuntime._approved_batch_violation(
        {"approved_batch": approved},
        "move_file",
        {"src": str(sandbox / "a.txt"), "dest_dir": str(sandbox / "out")},
        requires_approval=False,
    )
    assert "已经" in violation or "重复" in violation


def test_missing_batch_binding_is_rejected(sandbox: Path) -> None:
    violation = graph_module.AgentRuntime._approved_batch_violation(
        {"batch_approved": True},
        "move_file",
        {"src": str(sandbox / "a.txt"), "dest_dir": str(sandbox / "out")},
        requires_approval=False,
    )
    assert "缺少绑定" in violation


def test_coverage_change_between_approval_and_execution_is_rejected(sandbox: Path) -> None:
    """计划里的执行集合在审批后被改动 → 审批失效。"""
    from app.policy.approval import build_pending_approval, execution_sources
    from scopes import scope_for

    first = sandbox / "a.txt"
    second = sandbox / "b.txt"
    first.write_text("a", encoding="utf-8")
    second.write_text("b", encoding="utf-8")
    scope = scope_for(sandbox)
    state = {
        "thread_id": "tid",
        "request_id": "rid",
        "request_digest": scope.request_digest,
        "authorization_scope": scope.to_dict(),
        "mutations": {"move": 0, "rename": 0, "delete": 0},
        "step_index": 0,
        "plan": [
            {"id": "m0", "tool": "move_file",
             "args": {"src": str(first), "dest_dir": str(sandbox / "out")}},
        ],
    }
    pending = build_pending_approval(
        approval_id="ap",
        tool="move_file",
        args=state["plan"][0]["args"],
        scope=scope,
        request="",
        state=state,
        action="batch_move",
        target="累计 2 项文件变更",
        detail="",
        count=2,
        cumulative_before=0,
        batch=True,
        coverage={
            "sources": execution_sources(state["plan"]),
            "targets": [str(sandbox / "out")],
            "tools": ["move_file"],
        },
    )
    ok = verify_pending_approval(pending, state=state, confirm_token="ap")
    assert ok.ok is True, ok.code

    # 审批后计划被扩成两个文件：获批的执行集合不再等于将执行的集合。
    state["plan"] = [
        *state["plan"],
        {"id": "m1", "tool": "move_file",
         "args": {"src": str(second), "dest_dir": str(sandbox / "out")}},
    ]
    check = verify_pending_approval(pending, state=state, confirm_token="ap")
    assert check.ok is False
    assert check.code == "selection_changed"


# ---------------- 3. 单文件工具也受过滤条件约束 ----------------


def test_single_file_move_respects_extension_filter(sandbox: Path) -> None:
    pdf = sandbox / "a.pdf"
    jpg = sandbox / "b.jpg"
    pdf.write_text("pdf", encoding="utf-8")
    jpg.write_text("jpg", encoding="utf-8")
    scope = _scope(f"只把 {sandbox} 里的 pdf 移动到 {sandbox / 'out'}")
    assert scope.filter.get("extensions") == ["pdf"]

    allowed = authorize_tool_call(
        scope, "move_file", {"src": str(pdf), "dest_dir": str(sandbox / "out")}
    )
    assert allowed.allowed is True

    blocked = authorize_tool_call(
        scope, "move_file", {"src": str(jpg), "dest_dir": str(sandbox / "out")}
    )
    assert blocked.allowed is False
    assert blocked.code == "filter_mismatch"


def test_single_file_rename_respects_extension_filter(sandbox: Path) -> None:
    jpg = sandbox / "b.jpg"
    jpg.write_text("jpg", encoding="utf-8")
    scope = _scope(f"只重命名 {sandbox} 里的 pdf")
    assert scope.filter.get("extensions") == ["pdf"]
    blocked = authorize_tool_call(scope, "rename_file", {"src": str(jpg), "new_name": "x.jpg"})
    assert blocked.allowed is False
    assert blocked.code == "filter_mismatch"


# ---------------- 4. 点名文件不扩大为父目录 ----------------


def test_named_file_does_not_authorize_siblings(sandbox: Path) -> None:
    keep = sandbox / "a.pdf"
    victim = sandbox / "b.pdf"
    keep.write_text("a", encoding="utf-8")
    victim.write_text("b", encoding="utf-8")
    scope = _scope(f"删除 {keep}")
    assert scope.file_whitelist == (str(keep.resolve()),)

    allowed = authorize_tool_call(scope, "delete_file", {"path": str(keep)})
    assert allowed.allowed is True

    blocked = authorize_tool_call(scope, "delete_file", {"path": str(victim)})
    assert blocked.allowed is False
    assert blocked.code == "file_not_authorized"
    assert victim.exists()


def test_named_file_does_not_authorize_batch_over_directory(sandbox: Path) -> None:
    from scopes import scope_for

    one = sandbox / "a.pdf"
    other = sandbox / "b.pdf"
    one.write_text("a", encoding="utf-8")
    other.write_text("b", encoding="utf-8")
    executor = ToolExecutor()
    scan = executor.execute({"tool": "scan_directory", "args": {"directory": str(sandbox)}})
    scan_id = scan["result"]["scan_id"]

    scope = _scope(f"把 {one} 移动到 {sandbox / 'out'}")
    assert scope.file_whitelist
    blocked = authorize_tool_call(
        scope,
        "batch_move",
        {"scan_id": scan_id, "dest_dir": str(sandbox / "out"), "filter": {}},
        proposed_count=2,
    )
    assert blocked.allowed is False
    assert blocked.code == "file_not_authorized"


# ---------------- 5. 预览确认/执行必须校验具体操作 ----------------


def _rename_preview(sandbox: Path, scope: AuthorizationScope) -> dict:
    spec = {
        "op": "rename",
        "directory": str(sandbox),
        "recursive": True,
        "filter": {},
        "prefix": "x_",
    }
    return preview.create(spec, authorization_scope=scope.to_dict())


def test_readonly_scope_cannot_confirm_a_rename_preview(sandbox: Path) -> None:
    target = sandbox / "a.pdf"
    target.write_text("pdf", encoding="utf-8")
    scope = _scope(f"看一下 {sandbox} 里有哪些文件")
    assert "rename" not in scope.allowed_effects
    page = _rename_preview(sandbox, scope)
    ids = [item["item_id"] for item in preview.page(page["preview_id"])["items"]]

    with pytest.raises(preview.PreviewError) as excinfo:
        preview.confirm(page["preview_id"], ids, approve=True)
    assert excinfo.value.code == "effect_not_allowed"
    assert target.exists()


def test_execute_rechecks_scope_after_confirmation(sandbox: Path) -> None:
    """确认后授权范围被替换成只读：执行必须被拒绝，文件不能动。"""
    target = sandbox / "a.pdf"
    target.write_text("pdf", encoding="utf-8")
    scope = _scope(f"把 {sandbox} 里的文件重命名")
    page = _rename_preview(sandbox, scope)
    items = preview.page(page["preview_id"])["items"]
    ids = [item["item_id"] for item in items]
    confirmed = preview.confirm(page["preview_id"], ids, approve=True)

    stored = db.get_preview(page["preview_id"])
    read_only = _scope(f"看一下 {sandbox} 里的文件")
    spec = dict(stored["spec"])
    spec["authorization_scope"] = read_only.to_dict()
    approval = dict(stored["approval"])
    approval["authorization_scope_digest"] = read_only.digest()
    approval["request_digest"] = read_only.request_digest
    # 模拟“确认之后授权范围快照被替换成只读”：连指纹一起改掉，才能验证执行阶段的
    # 副作用校验本身（而不是指纹不一致这条更早的防线）。
    with db._conn() as conn:
        conn.execute(
            "UPDATE operation_previews SET spec_json=?, approval_json=? WHERE preview_id=?",
            (
                json.dumps(spec, ensure_ascii=False),
                json.dumps(approval, ensure_ascii=False),
                page["preview_id"],
            ),
        )

    with pytest.raises(preview.PreviewError) as excinfo:
        preview.execute(
            page["preview_id"], ids, confirm_token=confirmed["confirm_token"]
        )
    assert excinfo.value.code == "effect_not_allowed"
    assert target.exists()


def test_preview_selection_respects_max_items(sandbox: Path) -> None:
    scope = _scope(f"把 {sandbox} 里的文件移动到 {sandbox / 'out'}")
    tight = AuthorizationScope(**{**scope.__dict__, "max_items": 1})
    decision = authorize_preview_selection(
        tight,
        op="move",
        sources=[str(sandbox / "a.txt"), str(sandbox / "b.txt")],
        targets=[str(sandbox / "out" / "a.txt"), str(sandbox / "out" / "b.txt")],
    )
    assert decision.allowed is False
    assert decision.code == "max_items_exceeded"


# ---------------- 6. 带引号路径不再抛异常 ----------------


def test_quoted_path_is_parsed_without_error(sandbox: Path) -> None:
    quoted = f'organize "{sandbox}"'
    spans = extract_paths(quoted)
    assert spans, quoted
    assert any(Path(raw) == sandbox for raw, _start, _end in spans)

    scope = build_scope(quoted, request_id="rid", thread_id="tid")
    assert str(sandbox) in scope.source_roots


def test_quoted_windows_path_with_unicode_does_not_raise() -> None:
    # 原实现把 raw 当 span 用（raw.start()），这里只要求不抛 AttributeError。
    assert extract_paths('整理 "D:\\资料\\合同" 里的文件') is not None


# ---------------- 7. 执行器对变更工具强制要求 scope ----------------


def test_executor_fails_closed_without_scope(sandbox: Path) -> None:
    victim = sandbox / "victim.txt"
    victim.write_text("x", encoding="utf-8")
    executor = ToolExecutor()

    missing = executor.execute({"tool": "delete_file", "args": {"path": str(victim)}})
    assert missing["status"] == "failed"
    assert "授权范围" in missing["error"]

    empty = executor.execute(
        {"tool": "delete_file", "args": {"path": str(victim)}}, authorization_scope={}
    )
    assert empty["status"] == "failed"
    assert victim.exists()


def test_executor_still_allows_read_tools_without_scope(sandbox: Path) -> None:
    (sandbox / "a.txt").write_text("x", encoding="utf-8")
    result = ToolExecutor().execute(
        {"tool": "scan_directory", "args": {"directory": str(sandbox)}}
    )
    assert result["status"] == "ok"


# ---------------- 8. 422 不回显敏感输入 ----------------


def test_validation_error_does_not_echo_api_key(sandbox: Path) -> None:
    client = TestClient(app)
    client.headers.update({"X-Butler-Token": get_session_token()})
    oversized = "sk-" + "a" * 600
    response = client.put("/api/settings/llm", json={"openai_api_key": oversized})
    assert response.status_code == 422
    assert oversized not in response.text
    body = response.json()
    assert body["detail"], body
    assert all("input" not in item for item in body["detail"])
    assert all("ctx" not in item for item in body["detail"])
