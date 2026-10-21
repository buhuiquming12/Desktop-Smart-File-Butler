"""第八轮复核回归：预览确认绑定、索引取消、重复清理批次、模型预算与搜索准确性。

覆盖的复现问题：
1. 确认没绑定最终目标、令牌可重复使用、目标被占用时自动改名；
2. 取消索引后仍进入清理阶段，把未遍历到的文件当已删除；
3. 重复清理返回的 dedupe-* 编号下没有任何操作记录（无法整批撤销）；
4. 内容分类预览绕过模型调用预算，统计却记 0；
5. 移出索引目录但仍在授权范围内的文件搜不到；分页时总数被截断。
"""
from __future__ import annotations

import shutil
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import db
from app.agent.tool_executor import ToolExecutor
from app.api import events as ws_events
from app.config import get_settings
from app.main import app, get_session_token
from app.tools import filesystem, indexer


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "root"
    docs = root / "docs"
    docs.mkdir(parents=True)
    monkeypatch.setenv("SANDBOX_ROOTS", str(root))
    monkeypatch.setenv("DB_PATH", str(tmp_path / "butler.db"))
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    get_settings.cache_clear()
    db._initialized = False
    db.init_db()
    indexer.reset_jobs()
    client = TestClient(app)
    client.headers.update({"X-Butler-Token": get_session_token()})
    yield SimpleEnv(root, docs, client, monkeypatch)
    indexer.reset_jobs()
    get_settings.cache_clear()
    db._initialized = False


class SimpleEnv:
    def __init__(self, root: Path, docs: Path, client: TestClient,
                 monkeypatch: pytest.MonkeyPatch) -> None:
        self.root = root
        self.docs = docs
        self.client = client
        self.monkeypatch = monkeypatch

    def write(self, name: str, content: str, *, directory: Path | None = None) -> Path:
        path = (directory or self.docs) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def preview(self, **body) -> dict:
        response = self.client.post("/api/previews", json=body)
        assert response.status_code == 201, response.text
        return response.json()

    def confirm(self, preview_id: str, item_ids: list[str], **body) -> object:
        return self.client.post(
            f"/api/previews/{preview_id}/confirm", json={"item_ids": item_ids, **body}
        )

    def execute(self, preview_id: str, item_ids: list[str], token: str, **body) -> object:
        return self.client.post(
            f"/api/previews/{preview_id}/execute",
            json={"item_ids": item_ids, "confirm_token": token, **body},
        )

    def index(self, **body) -> dict:
        response = self.client.post(
            "/api/index/scan", json={"directory": str(self.docs), **body}
        )
        assert response.status_code == 202, response.text
        job_id = response.json()["job_id"]
        deadline = time.time() + 20
        while time.time() < deadline:
            state = self.client.get(f"/api/index/jobs/{job_id}").json()
            if state["status"] != "running":
                return state
            time.sleep(0.05)
        raise AssertionError("索引任务超时")

    def search(self, query: str, **body) -> dict:
        response = self.client.post("/api/search", json={"query": query, **body})
        assert response.status_code == 200, response.text
        return response.json()


# ---------------- 1. 预览：确认绑定最终目标 + 一次性令牌 ----------------


def test_confirm_binds_final_target_and_rejects_later_change(env: SimpleEnv) -> None:
    env.write("a.txt", "合同正文")
    preview = env.preview(op="classify", directory=str(env.docs),
                          dest_root=str(env.root / "out"))
    item = preview["items"][0]
    assert item["category"] == "文档"
    confirmed = env.confirm(preview["preview_id"], [item["item_id"]],
                            category_overrides={item["item_id"]: "合同"})
    assert confirmed.status_code == 200, confirmed.text
    token = confirmed.json()["approval_id"]
    assert confirmed.json()["targets"][item["item_id"]].endswith("合同/a.txt") \
        or confirmed.json()["targets"][item["item_id"]].endswith("合同\\a.txt")

    # 确认后改目标：整批拒绝，且没有任何文件被移动。
    changed = env.execute(preview["preview_id"], [item["item_id"]], token,
                          category_overrides={item["item_id"]: "发票"})
    assert changed.status_code == 409
    assert changed.json()["detail"]["code"] == "targets_changed"
    assert (env.docs / "a.txt").exists()
    assert not (env.root / "out").exists()

    # 沿用确认时的那组目标：执行成功，落点与确认一致。
    done = env.execute(preview["preview_id"], [item["item_id"]], token,
                       category_overrides={item["item_id"]: "合同"})
    assert done.status_code == 200, done.text
    assert done.json()["ok"] == 1
    assert (env.root / "out" / "合同" / "a.txt").exists()


def test_confirm_token_cannot_be_reused_even_after_restore(env: SimpleEnv) -> None:
    source = env.write("a.txt", "x")
    preview = env.preview(op="move", directory=str(env.docs),
                          dest_dir=str(env.root / "out"))
    item_id = preview["items"][0]["item_id"]
    token = env.confirm(preview["preview_id"], [item_id]).json()["approval_id"]
    first = env.execute(preview["preview_id"], [item_id], token)
    assert first.status_code == 200, first.text
    moved = env.root / "out" / "a.txt"
    assert moved.exists()

    # 文件恢复原位后，同一个确认令牌不能再次执行。
    shutil.move(str(moved), str(source))
    again = env.execute(preview["preview_id"], [item_id], token)
    assert again.status_code == 409
    assert again.json()["detail"]["code"] == "confirm_required"
    assert source.exists()
    assert not moved.exists()


def test_occupied_final_target_is_rejected_instead_of_renamed(env: SimpleEnv) -> None:
    env.write("a.txt", "x")
    occupied = env.root / "out" / "合同"
    occupied.mkdir(parents=True)
    (occupied / "a.txt").write_text("occupied", encoding="utf-8")

    preview = env.preview(op="classify", directory=str(env.docs),
                          dest_root=str(env.root / "out"))
    item_id = preview["items"][0]["item_id"]
    response = env.confirm(preview["preview_id"], [item_id],
                           category_overrides={item_id: "合同"})
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "not_ready"
    assert detail["conflicts"][0]["target"].endswith("a.txt")
    # 没有覆盖、没有改名，源文件仍在原处。
    assert (occupied / "a.txt").read_text(encoding="utf-8") == "occupied"
    assert (env.docs / "a.txt").exists()
    assert not list((occupied).glob("a (1).txt"))


# ---------------- 2. 索引取消 / 非递归：不得删除已有记录 ----------------


def test_cancelled_index_does_not_prune_existing_records(env: SimpleEnv) -> None:
    env.write("a.txt", "内容甲")
    env.write("b.txt", "内容乙")
    assert env.index()["status"] == "completed"
    assert env.search("内容甲")["total"] == 1

    original = indexer._index_file

    def cancel_after_first(path: Path, root: str) -> dict:
        result = original(path, root)
        for job in indexer._jobs.values():
            if job["status"] == "running":
                job["cancel"].set()
        return result

    env.monkeypatch.setattr(indexer, "_index_file", cancel_after_first)
    state = env.index()
    assert state["status"] == "cancelled"
    assert state["progress"]["removed"] == 0
    # 原文件还在，索引记录也必须在：搜索结果不能凭空消失。
    assert (env.docs / "a.txt").exists() and (env.docs / "b.txt").exists()
    assert env.search("内容甲")["total"] == 1
    assert env.search("内容乙")["total"] == 1


def test_non_recursive_index_does_not_prune_subdirectories(env: SimpleEnv) -> None:
    env.write("top.txt", "顶层内容")
    env.write("deep.txt", "深层内容", directory=env.docs / "sub")
    assert env.index()["status"] == "completed"
    assert env.search("深层")["total"] == 1

    state = env.index(recursive=False)
    assert state["progress"]["removed"] == 0
    assert state["progress"]["prune_skipped"]
    assert env.search("深层")["total"] == 1


# ---------------- 3. 重复清理：批次编号可用于整批撤销 ----------------


def test_duplicate_cleanup_records_operations_under_returned_thread(env: SimpleEnv) -> None:
    keeper = env.write("keep.txt", "same-content")
    victim = env.write("victim.txt", "same-content")
    import os

    os.utime(keeper, (1_700_000_000, 1_700_000_000))
    os.utime(victim, (1_600_000_000, 1_600_000_000))

    started = env.client.post("/api/duplicates/scan",
                              json={"directories": [str(env.docs)]})
    assert started.status_code == 202
    scan_id = started.json()["scan_id"]
    deadline = time.time() + 20
    while time.time() < deadline:
        state = env.client.get(f"/api/duplicates/{scan_id}").json()
        if state["status"] != "running":
            break
        time.sleep(0.05)
    assert state["status"] == "completed"

    response = env.client.post(f"/api/duplicates/{scan_id}/cleanup", json={"confirm": True})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["deleted"] == 1
    thread_id = body["thread_id"]
    assert thread_id.startswith("dedupe-")

    operations = db.operations_for_thread(thread_id)
    assert len(operations) >= 1, "返回的批次编号下必须能查到操作记录"
    assert all(op.action == "delete" for op in operations)
    assert any(op.target.endswith("victim.txt") for op in operations)


# ---------------- 4. 预览的内容分类受运行预算约束 ----------------


class _CountingLLM:
    def __init__(self) -> None:
        self.calls = 0

    def invoke(self, _messages):  # noqa: ANN001 - 测试桩
        self.calls += 1
        return type("_Response", (), {"content": "合同"})()


def _classify_step(docs: Path, out: Path) -> dict:
    return {
        "id": "p1",
        "description": "分类预览",
        "tool": "preview_batch",
        "args": {
            "op": "classify",
            "directory": str(docs),
            "dest_root": str(out),
            "mode": "content",
        },
    }


def test_preview_classification_respects_zero_budget(env: SimpleEnv) -> None:
    env.write("a.txt", "合同正文")
    llm = _CountingLLM()
    executor = ToolExecutor(llm)
    result = executor.execute(_classify_step(env.docs, env.root / "out"), model_call_limit=0)
    assert result["status"] == "ok", result.get("error")
    payload = result["result"]
    assert llm.calls == 0, "预算为 0 时不得调用模型"
    assert payload["model_calls"] == 0
    assert payload["counts"]["model_calls"] == 0
    assert payload["counts"]["fallback"] == 1
    assert executor.consume_model_calls() == 0


def test_preview_classification_counts_real_model_calls(env: SimpleEnv) -> None:
    env.write("a.txt", "合同正文")
    llm = _CountingLLM()
    executor = ToolExecutor(llm)
    result = executor.execute(_classify_step(env.docs, env.root / "out"), model_call_limit=5)
    assert result["status"] == "ok", result.get("error")
    assert llm.calls == 1
    assert result["result"]["model_calls"] == 1
    assert result["result"]["counts"]["model_calls"] == 1
    assert executor.consume_model_calls() == 1


# ---------------- 5. 搜索：移出索引目录仍可搜到，总数准确 ----------------


def test_search_finds_file_moved_out_of_indexed_directory(env: SimpleEnv) -> None:
    path = env.write("moving.txt", "迁移关键词丁")
    assert env.index()["status"] == "completed"
    other = env.root / "other"
    other.mkdir()
    moved = filesystem.move_file(str(path), str(other))

    result = env.search("迁移关键词丁")
    assert result["total"] == 1
    assert result["results"][0]["path"] == moved


def test_search_total_is_exact_with_small_pages(env: SimpleEnv) -> None:
    for index in range(12):
        env.write(f"n{index}.txt", f"季度报告 第 {index} 份")
    assert env.index()["status"] == "completed"

    first = env.search("报告", limit=1)
    assert first["total"] == 12
    assert len(first["results"]) == 1
    second = env.search("报告", limit=1, offset=1)
    assert second["total"] == 12
    assert len(second["results"]) == 1
    assert second["results"][0]["path"] != first["results"][0]["path"]


# ---------------- 6. 对话摘要带回 preview_id，供面板打开 ----------------


def test_task_summary_exposes_preview_ids() -> None:
    summary = ws_events.build_task_summary([{
        "step_id": "p1",
        "tool": "preview_batch",
        "description": "生成预览",
        "status": "ok",
        "result": {
            "preview_id": "abc123",
            "op": "move",
            "counts": {"total": 3, "ready": 3},
        },
    }])
    assert summary["previews"] == [{
        "preview_id": "abc123",
        "op": "move",
        "counts": {"total": 3, "ready": 3},
    }]


# ---------------- 7. 序号起始值 0 不能被当成“未填” ----------------


def test_rename_sequence_start_zero_is_honoured(env: SimpleEnv) -> None:
    env.write("a.txt", "1")
    env.write("b.txt", "2")
    page = env.preview(
        op="rename",
        directory=str(env.docs),
        use_sequence=True,
        sequence_start=0,
        sequence_padding=3,
    )
    targets = sorted(str(item["target_name"]) for item in page["items"])
    assert targets == ["a000.txt", "b001.txt"], f"起始序号 0 被改写：{targets}"


def test_rename_sequence_step_and_padding_are_honoured(env: SimpleEnv) -> None:
    env.write("a.txt", "1")
    env.write("b.txt", "2")
    page = env.preview(
        op="rename",
        directory=str(env.docs),
        use_sequence=True,
        sequence_start=0,
        sequence_step=10,
        sequence_padding=2,
    )
    targets = sorted(str(item["target_name"]) for item in page["items"])
    assert targets == ["a00.txt", "b10.txt"], f"步长/补零未生效：{targets}"


# ---------------- 8. 目录读不了时不得清理索引 ----------------


def test_unreadable_directory_blocks_pruning(env: SimpleEnv) -> None:
    env.write("top.txt", "顶层内容")
    env.write("deep.txt", "深层内容", directory=env.docs / "locked")
    assert env.index()["status"] == "completed"
    assert env.search("深层")["total"] == 1

    real_scandir = indexer.os.scandir

    def fake_scandir(path):  # noqa: ANN001 - 测试桩
        if Path(path).name == "locked":
            raise PermissionError("模拟拒绝访问")
        return real_scandir(path)

    env.monkeypatch.setattr(indexer.os, "scandir", fake_scandir)
    state = env.index()
    assert state["status"] == "completed"
    assert state["progress"]["unreadable"] >= 1
    # 读不到的目录里的文件既没被遍历到、也没被证实删除：不能清理。
    assert state["progress"]["removed"] == 0
    assert "无法读取" in state["progress"]["prune_skipped"]
    assert env.search("深层")["total"] == 1
    assert env.search("顶层")["total"] == 1
