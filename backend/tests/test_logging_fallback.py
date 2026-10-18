"""日志配置的降级行为：写不了日志文件也必须能启动后端。"""
from __future__ import annotations

import logging
from pathlib import Path

from app import logging_conf


def test_unwritable_log_dir_degrades_to_console(tmp_path: Path) -> None:
    root = logging.getLogger()
    before = list(root.handlers)
    logging_conf._CONFIGURED = False
    try:
        # 用“把目录当文件”制造 OSError，避免依赖操作系统权限设置。
        not_a_dir = tmp_path / "not-a-dir"
        not_a_dir.write_text("x", encoding="utf-8")
        logging_conf.setup_logging(str(not_a_dir / "logs"))
        assert logging_conf._CONFIGURED is True
        # 至少有一个 StreamHandler 可用，后端不会因日志失败而无法导入。
        assert any(isinstance(handler, logging.StreamHandler) for handler in root.handlers)
    finally:
        for handler in list(root.handlers):
            if handler not in before:
                root.removeHandler(handler)
        logging_conf._CONFIGURED = False


def test_normal_log_dir_creates_file(tmp_path: Path) -> None:
    root = logging.getLogger()
    before = list(root.handlers)
    logging_conf._CONFIGURED = False
    try:
        target = tmp_path / "logs"
        logging_conf.setup_logging(str(target))
        assert (target / "butler.log").exists()
    finally:
        for handler in list(root.handlers):
            if handler not in before:
                handler.close()
                root.removeHandler(handler)
        logging_conf._CONFIGURED = False
