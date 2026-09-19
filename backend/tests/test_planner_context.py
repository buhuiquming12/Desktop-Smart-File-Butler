"""P1-5 回归：喂给规划/反思的观察被压缩，完整清单不反复回灌。"""
from __future__ import annotations

import json

from app.agent import graph as g


def _scan_obs(n: int) -> dict:
    items = [
        {"name": f"f{i}.pdf" if i % 2 == 0 else f"f{i}.jpg",
         "path": f"/root/f{i}", "ext": "pdf" if i % 2 == 0 else "jpg",
         "size": 1, "modified": "2026-01-01T00:00:00", "is_dir": False}
        for i in range(n)
    ]
    return {
        "step_id": "s1", "tool": "scan_directory", "description": "扫描",
        "status": "ok", "error": "",
        "result": {"items": items, "total": n, "truncated": False},
    }


def test_scan_result_summarized() -> None:
    obs = _scan_obs(500)
    slim = g._summarize_observation(obs)
    result = slim["result"]
    assert result["total"] == 500
    assert len(result["sample"]) == g._PLANNER_SAMPLE  # 只保留前 20 条
    # 扩展名分布覆盖全部 500 条
    assert sum(result["extension_distribution"].values()) == 500
    assert result["extension_distribution"] == {"pdf": 250, "jpg": 250}
    # 摘要显著小于原始明细
    assert len(json.dumps(slim, default=str)) < len(json.dumps(obs, default=str)) / 5


def test_long_text_truncated() -> None:
    obs = {"step_id": "s2", "tool": "extract_text", "description": "提取",
           "status": "ok", "error": "", "result": "x" * 8000}
    slim = g._summarize_observation(obs)
    assert len(slim["result"]) < 8000
    assert "已截断" in slim["result"]


def test_small_result_preserved() -> None:
    obs = {"step_id": "s3", "tool": "move_file", "description": "移动",
           "status": "ok", "error": "", "result": "/root/dest/a.txt"}
    slim = g._summarize_observation(obs)
    assert slim["result"] == "/root/dest/a.txt"


def test_error_preserved() -> None:
    obs = {"step_id": "s4", "tool": "move_file", "description": "移动",
           "status": "failed", "error": "目标越界", "result": None}
    slim = g._summarize_observation(obs)
    assert slim["error"] == "目标越界"
    assert slim["status"] == "failed"
