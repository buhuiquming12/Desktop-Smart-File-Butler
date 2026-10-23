"""P3 回归：批量按内容分类、缓存、预算回退与预览中的可改类别。"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from app import db
from app.agent.tool_executor import ToolExecutor
from app.config import get_settings
from app.tools import classify, preview
from scopes import scope_for


class FakeLLM:
    def __init__(self, category: str = "财务") -> None:
        self.category = category
        self.calls = 0

    def invoke(self, _messages):
        self.calls += 1
        return SimpleNamespace(content=self.category)


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


def _scan(executor: ToolExecutor, directory: Path) -> str:
    observation = executor.execute({
        "id": "s", "tool": "scan_directory", "args": {"directory": str(directory)},
    })
    assert observation["status"] == "ok", observation
    return observation["result"]["scan_id"]


def test_batch_classify_content_uses_model(sandbox: Path) -> None:
    source = sandbox / "inbox"
    source.mkdir()
    (source / "a.txt").write_text("本季度财务报表：收入一百万。", encoding="utf-8")
    (source / "b.txt").write_text("另一份财务数据说明。", encoding="utf-8")
    llm = FakeLLM("财务")
    executor = ToolExecutor(llm)
    scan_id = _scan(executor, source)
    result = executor.execute(
        {
            "id": "c", "tool": "batch_classify",
            "args": {"scan_id": scan_id, "dest_root": str(sandbox / "out"), "mode": "content"},
        },
        authorization_scope=scope_for(source, target_roots=[sandbox / "out"]),
    )["result"]
    assert result["mode"] == "content"
    assert result["success"] == 2
    assert result["categories"] == {"财务": 2}
    assert result["fallback"] == 0
    assert llm.calls == 2
    assert (sandbox / "out" / "财务" / "a.txt").exists()


def test_content_classification_is_cached(sandbox: Path) -> None:
    path = sandbox / "memo.txt"
    path.write_text("随机正文内容", encoding="utf-8")
    llm = FakeLLM("备忘录")
    first = classify.classify_path(path, llm=llm, mode="content")
    assert first["category"] == "备忘录"
    second = classify.classify_path(path, llm=llm, mode="content")
    assert second["category"] == "备忘录"
    assert "缓存" in second["basis"]
    assert llm.calls == 1


def test_content_mode_falls_back_with_reason(sandbox: Path) -> None:
    source = sandbox / "inbox2"
    source.mkdir()
    (source / "a.txt").write_text("正文", encoding="utf-8")
    executor = ToolExecutor(None)
    scan_id = _scan(executor, source)
    result = executor.execute(
        {
            "id": "c", "tool": "batch_classify",
            "args": {"scan_id": scan_id, "dest_root": str(sandbox / "out2"), "mode": "content"},
        },
        authorization_scope=scope_for(source, target_roots=[sandbox / "out2"]),
    )["result"]
    assert result["fallback"] == 1
    assert result["fallback_items"][0]["reason"] == "模型未配置"
    # 回退到扩展名分类，仍移动到明确目录
    assert (sandbox / "out2" / "文档" / "a.txt").exists()


def test_budget_exhaustion_falls_back(sandbox: Path) -> None:
    first = sandbox / "one.txt"
    second = sandbox / "two.txt"
    first.write_text("内容一", encoding="utf-8")
    second.write_text("内容二", encoding="utf-8")
    llm = FakeLLM("归档")
    budget = classify.Budget(remaining=1)
    results = [
        classify.classify_path(path, llm=llm, mode="content", budget=budget)
        for path in (first, second)
    ]
    assert results[0]["fallback"] is False
    assert results[1]["fallback"] is True
    assert results[1]["fallback_reason"] == "达到分类预算上限"
    assert budget.used == 1


def test_preview_reports_basis_and_allows_category_override(sandbox: Path) -> None:
    source = sandbox / "inbox3"
    source.mkdir()
    (source / "a.txt").write_text("餐费报销单据", encoding="utf-8")
    llm = FakeLLM("报销单")
    page = preview.create({
        "op": "classify",
        "directory": str(source),
        "dest_root": str(sandbox / "sorted"),
        "mode": "content",
    }, llm=llm)
    item = page["items"][0]
    assert item["category"] == "报销单"
    assert "按内容" in item["basis"]
    assert item["editable_category"] is True
    assert item["target"].endswith(str(Path("报销单") / "a.txt"))

    # 类别覆盖必须在“确认”这一步提交：确认会把最终落点一起绑定，执行时只认这一组。
    confirmation = preview.confirm(
        page["preview_id"], [item["item_id"]],
        category_overrides={item["item_id"]: "自定义分类"},
    )
    assert confirmation["targets"][item["item_id"]].endswith(
        str(Path("自定义分类") / "a.txt")
    )
    result = preview.execute(
        page["preview_id"],
        [item["item_id"]],
        confirm_token=confirmation["approval_id"],
        category_overrides={item["item_id"]: "自定义分类"},
    )
    assert result["ok"] == 1
    assert (sandbox / "sorted" / "自定义分类" / "a.txt").exists()


def test_preview_rejects_unsafe_category_override(sandbox: Path) -> None:
    source = sandbox / "inbox4"
    source.mkdir()
    (source / "a.txt").write_text("内容", encoding="utf-8")
    page = preview.create({
        "op": "classify", "directory": str(source), "dest_root": str(sandbox / "sorted2"),
    })
    item = page["items"][0]
    # 越界类别在确认阶段就被拒绝：既不会建目录，也不会移动文件。
    with pytest.raises(preview.PreviewError) as rejected:
        preview.confirm(
            page["preview_id"], [item["item_id"]],
            category_overrides={item["item_id"]: "../escape"},
        )
    assert rejected.value.code == "invalid"
    assert (source / "a.txt").exists()
    assert not (sandbox / "escape").exists()
