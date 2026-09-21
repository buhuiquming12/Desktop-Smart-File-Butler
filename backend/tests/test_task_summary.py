"""任务完成结构化摘要（体验优化）的纯函数回归。"""
from __future__ import annotations

from app.api.events import build_task_summary


def test_build_task_summary_counts_statuses_and_collects_paths() -> None:
    observations = [
        {
            "step_id": "s1",
            "tool": "scan_directory",
            "description": "扫描下载目录",
            "args": {"directory": r"C:\Downloads"},
            "status": "ok",
            "result": {"items": [{"path": r"C:\Downloads\a.txt"}], "total": 1},
        },
        {
            "tool": "move_file",
            "description": "移动文件到归档",
            "args": {"src": r"C:\Downloads\a.txt", "dest_dir": r"C:\Archive", "new_name": ""},
            "status": "ok",
            "result": "ok",
        },
        {
            "tool": "delete_file",
            "description": "删除临时文件",
            "args": {"path": r"C:\Downloads\tmp.txt"},
            "status": "failed",
            "error": "权限不足",
        },
        {
            "tool": "rename_file",
            "description": "批量重命名被拒绝",
            "args": {"src": r"C:\Downloads\b.txt", "new_name": "b2.txt"},
            "status": "rejected",
            "result": "用户拒绝批量操作",
        },
    ]
    summary = build_task_summary(observations)
    assert summary["ok"] == 2
    assert summary["failed"] == 1
    assert summary["skipped"] == 1
    assert any(file == r"C:\Downloads\a.txt" for file in summary["files"])
    assert any(file == r"C:\Downloads\tmp.txt" for file in summary["files"])
    assert any(file == r"C:\Archive" for file in summary["files"])

    operations = summary["operations"]
    assert len(operations) == 4
    assert operations[1]["tool_label"] == "移动文件"
    assert operations[1]["status_label"] == "成功"
    assert operations[1]["dest"] == r"C:\Archive"
    assert operations[2]["status_label"] == "失败"
    assert operations[3]["status_label"] == "跳过"


def test_build_task_summary_empty() -> None:
    summary = build_task_summary([])
    assert summary == {"ok": 0, "failed": 0, "skipped": 0, "files": [], "operations": []}
