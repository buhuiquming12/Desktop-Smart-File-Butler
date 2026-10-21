"""原句回归：路径后的只读任务，以及移动工具隐式创建目标目录。"""
from pathlib import Path

import pytest

from app import db
from app.agent.graph import _is_clarification_answer
from app.agent.outcome import evaluate_completion, record_verification
from app.agent.tool_executor import ToolExecutor
from app.config import get_settings
from app.policy.engine import intent_effects


@pytest.mark.parametrize("message", [
    r"D:\scan then list files",
    r"D:\My Documents\scan please show the files",
    r"D:\scan list all pdfs",
    "/home/me/my scan then check the folder",
    r"D:\scan" + "\tthen\tlist files",
    r"\\server\share\My scan and then count the files",
    r"D:\scan then list files and move files to archive",
])
def test_read_phrase_after_path_is_an_operation(message: str) -> None:
    assert "read" in intent_effects(message)
    assert _is_clarification_answer(message) is False


@pytest.mark.parametrize("message", [
    r"D:\My scan", r"D:\my scan folder", r"D:\scan list", r"D:\list files.txt",
    r"D:\then list files\report.txt", '"D:\\then list files"',
    "/home/me/my scan", r"D:\My Documents\scan",
])
def test_paths_with_command_words_are_still_parameters(message: str) -> None:
    assert intent_effects(message) == set()
    assert _is_clarification_answer(message) is True


@pytest.fixture()
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SANDBOX_ROOTS", str(tmp_path))
    monkeypatch.setenv("DB_PATH", str(tmp_path / "butler.db"))
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    get_settings.cache_clear()
    db._initialized = False
    db.init_db()
    try:
        yield tmp_path
    finally:
        get_settings.cache_clear()
        db._initialized = False


@pytest.mark.parametrize("tool", ["move_file", "batch_move"])
@pytest.mark.parametrize("user_intent", [
    "新建一个备份文件夹，然后把 PDF 移动进去",
    '新建一个 "backup" 文件夹，然后把 PDF 移动到该目录',
    "create a folder then move files into it",
])
def test_implicit_creation_satisfies_linked_goal(sandbox: Path, tool: str, user_intent: str) -> None:
    source = sandbox / "src"
    source.mkdir()
    pdf = source / "a.pdf"
    pdf.write_text("pdf", encoding="utf-8")
    target = sandbox / "backup"
    executor = ToolExecutor()
    if tool == "move_file":
        args = {"src": str(pdf), "dest_dir": str(target)}
    else:
        scan = executor.execute({"tool": "scan_directory", "args": {"directory": str(source)}})
        args = {"scan_id": scan["result"]["scan_id"], "dest_dir": str(target)}
    moved = record_verification(executor.execute({"tool": tool, "args": args}))
    assert moved["status"] == "ok"
    assert target.is_dir() and (target / "a.pdf").is_file()
    completion = evaluate_completion({"trusted_user_intent": user_intent, "observations": [moved]})
    assert completion["unmet_effects"] == []
    assert completion["outcome"] == "success"


@pytest.mark.parametrize("user_intent", [
    "把 PDF 移动到 out，另外新建一个空的备份文件夹",
    '新建一个 "backup" 文件夹，然后把 PDF 移动进去',
    '新建 backup 文件夹，然后把 PDF 移动进去',
])
def test_other_directory_cannot_satisfy_create_goal(sandbox: Path, user_intent: str) -> None:
    pdf = sandbox / "a.pdf"
    pdf.write_text("pdf", encoding="utf-8")
    moved = record_verification(ToolExecutor().execute({
        "tool": "move_file", "args": {"src": str(pdf), "dest_dir": str(sandbox / "out")},
    }))
    completion = evaluate_completion({"trusted_user_intent": user_intent, "observations": [moved]})
    assert completion["unmet_effects"] == ["create"]
    assert completion["outcome"] == "partial"


def test_disappeared_implicit_directory_is_not_completed(sandbox: Path) -> None:
    pdf = sandbox / "a.pdf"
    pdf.write_text("pdf", encoding="utf-8")
    target = sandbox / "backup"
    moved = record_verification(ToolExecutor().execute({
        "tool": "move_file", "args": {"src": str(pdf), "dest_dir": str(target)},
    }))
    (target / "a.pdf").rename(sandbox / "elsewhere.pdf")
    target.rmdir()
    completion = evaluate_completion({
        "trusted_user_intent": "新建一个备份文件夹，然后把 PDF 移动进去",
        "observations": [moved],
    })
    assert completion["outcome"] != "success"
    assert "create" in completion["unmet_effects"]


@pytest.mark.parametrize("correct_target", [True, False])
@pytest.mark.parametrize("quoted", [True, False])
def test_explicit_directory_path_must_match(sandbox: Path, correct_target: bool, quoted: bool) -> None:
    pdf = sandbox / "a.pdf"
    pdf.write_text("pdf", encoding="utf-8")
    requested = sandbox / ("backup folder" if quoted else "backup")
    actual = requested if correct_target else sandbox / "other"
    moved = record_verification(ToolExecutor().execute({
        "tool": "move_file", "args": {"src": str(pdf), "dest_dir": str(actual)},
    }))
    target_text = f'"{requested}"' if quoted else str(requested)
    completion = evaluate_completion({
        "trusted_user_intent": f"新建 {target_text} 文件夹，然后把 PDF 移动进去",
        "observations": [moved],
    })
    assert completion["outcome"] == ("success" if correct_target else "partial")
    assert completion["unmet_effects"] == ([] if correct_target else ["create"])
