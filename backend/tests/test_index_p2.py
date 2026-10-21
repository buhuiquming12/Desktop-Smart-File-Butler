"""P2 回归：中文关键词检索、增量更新、授权收缩后不再泄露内容。"""
from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import db
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
    client = TestClient(app)
    client.headers.update({"X-Butler-Token": get_session_token()})
    yield SimpleEnv(root, docs, client, monkeypatch)
    get_settings.cache_clear()
    db._initialized = False


class SimpleEnv:
    def __init__(self, root: Path, docs: Path, client: TestClient,
                 monkeypatch: pytest.MonkeyPatch) -> None:
        self.root = root
        self.docs = docs
        self.client = client
        self.monkeypatch = monkeypatch

    def write(self, name: str, content: str) -> Path:
        path = self.docs / name
        path.write_text(content, encoding="utf-8")
        return path

    def index(self, directory: Path | None = None) -> dict:
        response = self.client.post(
            "/api/index/scan", json={"directory": str(directory or self.docs)}
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


def test_chinese_keyword_search(env: SimpleEnv) -> None:
    env.write("季度总结.txt", "本季度销售报告显示增长明显，需要董事会审阅。")
    env.write("other.txt", "unrelated english content")
    state = env.index()
    assert state["status"] == "completed"
    assert state["progress"]["indexed"] == 2

    result = env.search("报告")
    assert result["total"] == 1
    hit = result["results"][0]
    assert hit["name"] == "季度总结.txt"
    assert "报告" in hit["snippet"]
    assert result["capabilities"]["fts5"] in (True, False)


def test_chinese_search_works_without_fts5(env: SimpleEnv) -> None:
    """FTS5 不可用时必须仍然支持中文子串检索（回退 LIKE）。"""
    env.monkeypatch.setattr(indexer, "_fts_available", lambda: False)
    env.write("合同.txt", "甲方与乙方签订采购合同，金额一百万。")
    env.index()
    result = env.search("采购合同")
    assert result["total"] == 1
    assert result["results"][0]["name"] == "合同.txt"


def test_english_search_and_pagination(env: SimpleEnv) -> None:
    for index in range(3):
        env.write(f"note-{index}.txt", f"quarterly revenue report number {index}")
    env.index()
    result = env.search("revenue")
    assert result["total"] == 3
    page = env.search("revenue", limit=2)
    assert len(page["results"]) == 2
    rest = env.search("revenue", limit=2, offset=2)
    assert len(rest["results"]) == 1


def test_incremental_update_after_modification(env: SimpleEnv) -> None:
    path = env.write("draft.txt", "旧内容 苹果")
    env.index()
    assert env.search("苹果")["total"] == 1
    path.write_text("全新的香蕉内容", encoding="utf-8")
    env.index()
    assert env.search("苹果")["total"] == 0
    assert env.search("香蕉")["total"] == 1
    # 未变化文件不会被重复计入 indexed
    assert env.search("香蕉")["results"][0]["path"] == str(path)


def test_index_follows_move_rename_and_delete(env: SimpleEnv) -> None:
    path = env.write("before.txt", "索引跟随移动测试 关键词甲")
    env.index()
    env.client.post("/api/index/scan", json={"directory": str(env.docs)})  # 幂等触发
    moved = filesystem.move_file(str(path), str(env.docs / "sub"))
    assert env.search("关键词甲")["results"][0]["path"] == str(Path(moved))
    renamed = filesystem.rename_file(moved, "after.txt")
    assert env.search("关键词甲")["results"][0]["name"] == "after.txt"
    filesystem.delete_file(renamed)
    assert env.search("关键词甲")["total"] == 0


def test_index_prunes_removed_files(env: SimpleEnv) -> None:
    first = env.write("a.txt", "要被清理的索引 词语乙")
    env.write("b.txt", "保留内容")
    env.index()
    first.unlink()
    state = env.index()
    assert state["progress"]["removed"] >= 1
    assert env.search("词语乙")["total"] == 0


def test_search_respects_current_authorization(env: SimpleEnv) -> None:
    env.write("secret.txt", "高度机密 内容丙")
    env.index()
    assert env.search("内容丙")["total"] == 1
    # 收缩授权目录：旧索引不得再返回该文件内容。
    env.monkeypatch.setenv("SANDBOX_ROOTS", str(env.root / "docs" / "no-such-dir"))
    get_settings.cache_clear()
    assert env.search("内容丙")["total"] == 0


def test_unsupported_and_failed_files_are_marked(env: SimpleEnv) -> None:
    env.write("data.bin", "binary-ish")
    (env.docs / "broken.docx").write_text("not a real docx", encoding="utf-8")
    state = env.index()
    assert state["progress"]["skipped"] >= 1
    assert state["progress"]["failed"] >= 1
    stats = env.client.get("/api/index/stats").json()
    assert stats["by_status"].get("unsupported", 0) >= 1
    assert stats["by_status"].get("failed", 0) >= 1


def test_truncated_content_is_flagged(env: SimpleEnv) -> None:
    env.monkeypatch.setattr(indexer, "MAX_INDEX_CHARS", 50)
    env.write("long.txt", "关键词开头 " + "填充" * 200)
    env.index()
    result = env.search("关键词")
    assert result["total"] == 1
    assert result["results"][0]["truncated"] is True


def test_unknown_backend_is_rejected(env: SimpleEnv) -> None:
    env.write("a.txt", "内容")
    env.index()
    response = env.client.post("/api/search", json={"query": "内容", "backend": "semantic"})
    assert response.status_code == 422
    assert "keyword" in response.json()["detail"]
