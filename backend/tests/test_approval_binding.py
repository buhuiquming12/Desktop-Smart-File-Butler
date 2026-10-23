"""审批绑定（一次性确认）回归测试。

覆盖：
- 审批 token / request_digest / selected_item_digest / 授权范围不匹配时拒绝；
- 审批后源文件变化、目标被占用、累计变更数量变化时拒绝；
- 审批后扩大选择集合、改变操作类型或目标范围时返回 409；
- 审批过期、重复使用同一审批时拒绝；
- 审批被拒绝后，同一请求的后续同类 mutation 不再重复弹窗。
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Iterator, List

import pytest
from fastapi import HTTPException

from app import db, main as main_module
from app.agent import graph as graph_module
from app.agent.state import PlanOutput, PlanStep, ReflectionOutput
from app.agent.tool_executor import ToolExecutor
from app.config import get_settings
from app.models import ApprovalResponse
from app.policy.approval import (
    build_pending_approval,
    verify_pending_approval,
)
from app.policy.authorization import build_scope
from app.tools import manifests, preview


@pytest.fixture()
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.setenv("SANDBOX_ROOTS", str(root))
    monkeypatch.setenv("DB_PATH", str(tmp_path / "butler.db"))
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    get_settings.cache_clear()
    db._initialized = False
    db.init_db()
    yield root
    get_settings.cache_clear()
    db._initialized = False


def _pending(root: Path, tool: str, args: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    scope = build_scope(
        f"删除 {root / 'a.txt'}" if tool == "delete_file" else f"整理 {root} 的文件",
        request_id="rid",
        thread_id="tid",
    )
    state = {
        "thread_id": "tid",
        "request_id": "rid",
        "request_digest": scope.request_digest,
        "authorization_scope": scope.to_dict(),
        "mutations": {"move": 0, "rename": 0, "delete": 0},
    }
    pending = build_pending_approval(
        approval_id="ap-1",
        tool=tool,
        args=args,
        scope=scope,
        request=str(state.get("trusted_user_intent") or ""),
        state=state,
        action=tool,
        target=str(root),
        detail="test",
        count=1,
        cumulative_before=0,
        batch=False,
    )
    return pending, state


# ---------------- 单元：绑定字段 ----------------

def test_token_mismatch_is_rejected(sandbox: Path) -> None:
    target = sandbox / "a.txt"
    target.write_text("x", encoding="utf-8")
    pending, state = _pending(sandbox, "delete_file", {"path": str(target)})
    check = verify_pending_approval(pending, state=state, confirm_token="forged")
    assert check.ok is False and check.code == "token_mismatch"
    assert check.status_code == 409


def test_request_digest_mismatch_is_rejected(sandbox: Path) -> None:
    target = sandbox / "a.txt"
    target.write_text("x", encoding="utf-8")
    pending, state = _pending(sandbox, "delete_file", {"path": str(target)})
    state["request_digest"] = "other-request"
    check = verify_pending_approval(pending, state=state, confirm_token="ap-1")
    assert check.ok is False and check.code == "request_changed"


def test_scope_digest_mismatch_is_rejected(sandbox: Path) -> None:
    target = sandbox / "a.txt"
    target.write_text("x", encoding="utf-8")
    pending, state = _pending(sandbox, "delete_file", {"path": str(target)})
    wider = dict(state["authorization_scope"])
    wider["max_items"] = 10_000
    state["authorization_scope"] = wider
    check = verify_pending_approval(pending, state=state, confirm_token="ap-1")
    assert check.ok is False and check.code == "scope_changed"


def test_selected_item_digest_mismatch_is_rejected(sandbox: Path) -> None:
    target = sandbox / "a.txt"
    target.write_text("x", encoding="utf-8")
    pending, state = _pending(sandbox, "delete_file", {"path": str(target)})
    pending["selected_item_digest"] = "deadbeef"
    check = verify_pending_approval(pending, state=state, confirm_token="ap-1")
    assert check.ok is False and check.code == "selection_changed"


def test_source_change_after_approval_is_rejected(sandbox: Path) -> None:
    target = sandbox / "a.txt"
    target.write_text("v1", encoding="utf-8")
    pending, state = _pending(sandbox, "delete_file", {"path": str(target)})
    assert verify_pending_approval(pending, state=state, confirm_token="ap-1").ok

    target.write_text("v2-changed", encoding="utf-8")
    check = verify_pending_approval(pending, state=state, confirm_token="ap-1")
    assert check.ok is False and check.code == "stale_source"


def test_target_conflict_after_approval_is_rejected(sandbox: Path) -> None:
    source = sandbox / "a.txt"
    source.write_text("x", encoding="utf-8")
    dest = sandbox / "out"
    dest.mkdir()
    pending, state = _pending(
        sandbox, "move_file", {"src": str(source), "dest_dir": str(dest)}
    )
    assert verify_pending_approval(pending, state=state, confirm_token="ap-1").ok

    (dest / "a.txt").write_text("occupied", encoding="utf-8")
    check = verify_pending_approval(pending, state=state, confirm_token="ap-1")
    assert check.ok is False and check.code == "target_conflict"


def test_mutation_count_change_after_approval_is_rejected(sandbox: Path) -> None:
    target = sandbox / "a.txt"
    target.write_text("x", encoding="utf-8")
    pending, state = _pending(sandbox, "delete_file", {"path": str(target)})
    state["mutations"] = {"move": 5, "rename": 0, "delete": 0}
    check = verify_pending_approval(pending, state=state, confirm_token="ap-1")
    assert check.ok is False and check.code == "mutation_changed"


def test_expired_approval_is_rejected(sandbox: Path) -> None:
    target = sandbox / "a.txt"
    target.write_text("x", encoding="utf-8")
    pending, state = _pending(sandbox, "delete_file", {"path": str(target)})
    pending["expires_at"] = 1.0
    check = verify_pending_approval(pending, state=state, confirm_token="ap-1")
    assert check.ok is False and check.code == "approval_expired"


def test_consumed_approval_cannot_be_reused(sandbox: Path) -> None:
    target = sandbox / "a.txt"
    target.write_text("x", encoding="utf-8")
    pending, state = _pending(sandbox, "delete_file", {"path": str(target)})
    state["consumed_approvals"] = ["ap-1"]
    check = verify_pending_approval(pending, state=state, confirm_token="ap-1")
    assert check.ok is False and check.code == "approval_used"


def test_expanded_selection_after_approval_is_rejected(sandbox: Path) -> None:
    source = sandbox / "in"
    source.mkdir()
    for name in ("a.txt", "b.txt", "c.txt"):
        (source / name).write_text("x", encoding="utf-8")
    executor = ToolExecutor()
    scan_id = executor.execute(
        {"id": "s", "tool": "scan_directory", "args": {"directory": str(source)}}
    )["result"]["scan_id"]
    args = {"scan_id": scan_id, "dest_dir": str(sandbox / "out"), "filter": {}}
    pending, state = _pending(sandbox, "batch_move", args)
    assert verify_pending_approval(pending, state=state, confirm_token="ap-1").ok

    # 审批后新增一个文件：清单匹配集合扩大。
    (source / "d.txt").write_text("x", encoding="utf-8")
    manifests.create(
        str(source),
        [item.model_dump(mode="json") for item in __import__(
            "app.tools.filesystem", fromlist=["scan_directory"]
        ).scan_directory(str(source), False).items],
        {"scanned_count": 4},
    )
    # 重新扫描生成新 manifest 会换 scan_id；这里直接改旧清单，模拟“选择集合扩大”。
    with db.connection() as conn:
        row = conn.execute(
            "SELECT items_json FROM scan_manifests WHERE scan_id=?", (scan_id,)
        ).fetchone()
        items = json.loads(row["items_json"])
        items.append({"name": "d.txt", "path": str(source / "d.txt"), "ext": "txt",
                      "size": 1, "is_dir": False, "modified": ""})
        conn.execute(
            "UPDATE scan_manifests SET items_json=? WHERE scan_id=?",
            (json.dumps(items, ensure_ascii=False), scan_id),
        )
    check = verify_pending_approval(pending, state=state, confirm_token="ap-1")
    assert check.ok is False
    assert check.code == "selection_changed"
    assert check.detail["added"]


# ---------------- 预览：确认绑定与 409 ----------------

class _Env:
    def __init__(self, root: Path, source: Path) -> None:
        self.root = root
        self.source = source

    def make(self, name: str) -> Path:
        path = self.source / name
        path.write_text("x", encoding="utf-8")
        return path


@pytest.fixture()
def preview_env(sandbox: Path) -> _Env:
    source = sandbox / "inbox"
    source.mkdir()
    return _Env(sandbox, source)


def test_preview_token_mismatch_is_rejected(preview_env: _Env) -> None:
    preview_env.make("a.txt")
    page = preview.create({"op": "move", "directory": str(preview_env.source),
                           "dest_dir": str(preview_env.root / "out")})
    ids = [item["item_id"] for item in page["items"]]
    confirm = preview.confirm(page["preview_id"], ids)
    with pytest.raises(preview.PreviewError) as exc:
        preview.execute(page["preview_id"], ids, confirm_token="forged")
    assert exc.value.code == "confirm_required"
    assert confirm["confirm_token"] != "forged"


def test_preview_confirm_binds_request_and_selection_digests(preview_env: _Env) -> None:
    preview_env.make("a.txt")
    preview_env.make("b.txt")
    page = preview.create({"op": "move", "directory": str(preview_env.source),
                           "dest_dir": str(preview_env.root / "out")})
    ids = [item["item_id"] for item in page["items"]]
    confirm = preview.confirm(page["preview_id"], ids[:1])
    assert confirm["request_digest"]
    assert confirm["authorization_scope_digest"]
    assert confirm["selected_item_digest"]

    # 用另一个条目集合执行：绑定指纹不匹配。
    with pytest.raises(preview.PreviewError) as exc:
        preview.execute(
            page["preview_id"], ids, confirm_token=confirm["confirm_token"]
        )
    assert exc.value.code == "selection_changed"


def test_preview_target_change_after_confirm_is_rejected(preview_env: _Env) -> None:
    preview_env.make("a.txt")
    page = preview.create({"op": "move", "directory": str(preview_env.source),
                           "dest_dir": str(preview_env.root / "out")})
    item = page["items"][0]
    confirm = preview.confirm(page["preview_id"], [item["item_id"]])
    # 模拟审批后把授权范围改大（新的目标根目录）。
    with db.connection() as conn:
        row = conn.execute(
            "SELECT spec_json FROM operation_previews WHERE preview_id=?",
            (page["preview_id"],),
        ).fetchone()
        spec = json.loads(row["spec_json"])
        spec["authorization_scope"]["target_roots"] = [str(preview_env.root)]
        conn.execute(
            "UPDATE operation_previews SET spec_json=? WHERE preview_id=?",
            (json.dumps(spec, ensure_ascii=False), page["preview_id"]),
        )
    with pytest.raises(preview.PreviewError) as exc:
        preview.execute(
            page["preview_id"], [item["item_id"]],
            confirm_token=confirm["confirm_token"],
        )
    assert exc.value.code == "scope_changed"
    assert not (preview_env.root / "out" / "a.txt").exists()


def test_preview_source_change_after_confirm_is_rejected(preview_env: _Env) -> None:
    path = preview_env.make("a.txt")
    page = preview.create({"op": "move", "directory": str(preview_env.source),
                           "dest_dir": str(preview_env.root / "out")})
    item = page["items"][0]
    confirm = preview.confirm(page["preview_id"], [item["item_id"]])
    path.write_text("changed content", encoding="utf-8")
    with pytest.raises(preview.PreviewError) as exc:
        preview.execute(
            page["preview_id"], [item["item_id"]],
            confirm_token=confirm["confirm_token"],
        )
    assert exc.value.code == "stale_preview"


# ---------------- 图 / REST：409 与拒绝后不重复弹窗 ----------------

class _ScriptedPlanner:
    def __init__(self, steps: List[dict[str, Any]], *, once: bool = True) -> None:
        self.steps = steps
        self.once = once

    def invoke(self, messages: List[Any]) -> PlanOutput:
        context = json.loads(messages[-1].content)
        if self.once and context.get("tool_observations"):
            return PlanOutput(goal="done", steps=[], user_message="")
        return PlanOutput(
            goal="执行",
            steps=[PlanStep(**step) for step in self.steps],
            user_message="",
        )


class _DoneReflector:
    def invoke(self, messages: List[Any]) -> ReflectionOutput:
        if json.loads(messages[-1].content).get("是否还有步骤"):
            return ReflectionOutput(decision="continue", reasoning="继续")
        return ReflectionOutput(decision="done", reasoning="结束")


def _runtime(monkeypatch: pytest.MonkeyPatch, steps: List[dict[str, Any]], *,
             once: bool = True) -> graph_module.AgentRuntime:
    class _LLM:
        def with_structured_output(self, schema: Any) -> Any:
            return _ScriptedPlanner(steps, once=once) if schema is PlanOutput else _DoneReflector()

        def invoke(self, _: Any) -> Any:  # pragma: no cover
            raise AssertionError

    monkeypatch.setattr(graph_module, "build_llm", lambda temperature=0.1: _LLM())
    return graph_module.AgentRuntime()


def test_delete_approval_rechecks_source_and_returns_409(
    sandbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = sandbox / "victim.txt"
    target.write_text("v1", encoding="utf-8")
    runtime = _runtime(monkeypatch, [
        {"id": "d", "description": "删除", "tool": "delete_file",
         "args": {"path": str(target)}},
    ])
    for _ in runtime.start_stream("删掉这个文件", "bind-1"):
        pass
    state = runtime.state("bind-1")
    assert state["status"] == "waiting_approval"
    pending = state["pending_approval"]
    assert pending["request_digest"] == state["request_digest"]
    from app.policy.authorization import AuthorizationScope

    assert pending["scope_digest"] == AuthorizationScope.from_dict(
        state["authorization_scope"]
    ).digest()
    assert pending["selected_item_digest"]
    assert pending["mutation_count"] == 1

    # 审批后源文件变化 → REST 层必须 409，且文件仍在。
    target.write_text("v2-changed-content", encoding="utf-8")
    monkeypatch.setattr(main_module, "get_runtime", lambda: runtime)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(main_module._resume_approval(ApprovalResponse(
            thread_id="bind-1",
            approval_id=pending["approval_id"],
            decision="approve",
        )))
    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "stale_source"
    assert target.exists()


def test_approval_token_mismatch_returns_409(
    sandbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = sandbox / "victim.txt"
    target.write_text("x", encoding="utf-8")
    runtime = _runtime(monkeypatch, [
        {"id": "d", "description": "删除", "tool": "delete_file",
         "args": {"path": str(target)}},
    ])
    for _ in runtime.start_stream("删掉这个文件", "bind-2"):
        pass
    pending = runtime.state("bind-2")["pending_approval"]
    monkeypatch.setattr(main_module, "get_runtime", lambda: runtime)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(main_module._resume_approval(ApprovalResponse(
            thread_id="bind-2",
            approval_id=pending["approval_id"],
            decision="approve",
            confirm_token="forged-token",
        )))
    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "token_mismatch"
    assert target.exists()


def test_rejected_approval_is_not_prompted_again(
    sandbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = sandbox / "victim.txt"
    target.write_text("x", encoding="utf-8")
    step = {"id": "d", "description": "删除", "tool": "delete_file",
            "args": {"path": str(target)}}
    runtime = _runtime(monkeypatch, [step, {**step, "id": "d2"}])
    for _ in runtime.start_stream("删掉这个文件", "bind-3"):
        pass
    assert runtime.state("bind-3")["pending_approval"]

    for _ in runtime.resume_stream("bind-3", "reject"):
        pass

    state = runtime.state("bind-3")
    rejected = [o for o in state["observations"] if o["status"] == "rejected"]
    # 第一步被拒绝；第二步是同一份审批内容，必须直接跳过而不是再次弹窗。
    assert len(rejected) >= 2, state["observations"]
    assert state.get("pending_approval") is None
    assert target.exists()


def test_rest_approval_endpoint_returns_409_after_source_change(
    sandbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REST 审批接口必须同步返回 409：它是 202 + 后台执行，后台异常传不回调用方。"""
    from fastapi.testclient import TestClient

    from app import main as main_module

    target = sandbox / "victim.txt"
    target.write_text("v1", encoding="utf-8")
    runtime = _runtime(monkeypatch, [
        {"id": "d", "description": "删除", "tool": "delete_file",
         "args": {"path": str(target)}},
    ])
    for _ in runtime.start_stream("删掉这个文件", "bind-rest"):
        pass
    pending = runtime.state("bind-rest")["pending_approval"]
    monkeypatch.setattr(main_module, "get_runtime", lambda: runtime)

    target.write_text("v2-changed-after-approval", encoding="utf-8")
    client = TestClient(main_module.app)
    client.headers.update({"X-Butler-Token": main_module.get_session_token()})
    response = client.post("/api/approvals/respond", json={
        "thread_id": "bind-rest",
        "approval_id": pending["approval_id"],
        "decision": "approve",
    })
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "stale_source"
    assert target.exists()


def test_delete_always_prompts_even_for_short_intent(
    sandbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = sandbox / "victim.txt"
    target.write_text("x", encoding="utf-8")
    runtime = _runtime(monkeypatch, [
        {"id": "d", "description": "删除", "tool": "delete_file",
         "args": {"path": str(target)}},
    ])
    for _ in runtime.start_stream("删掉这个文件", "bind-4"):
        pass
    assert runtime.state("bind-4")["status"] == "waiting_approval"
    assert target.exists()
