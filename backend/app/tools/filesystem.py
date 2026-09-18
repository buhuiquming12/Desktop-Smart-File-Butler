"""文件系统工具：扫描、移动、重命名、建夹、删除。

安全约束：
- 所有路径经 ``security.resolve_in_sandbox`` 校验，越界拒绝。
- 删除 / 覆盖为高危操作，工具本身不执行，交由 Agent 图的 approval 节点处理。
- 每次操作落审计日志。
"""
from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path
from typing import List

from ..db import log_operation
from ..logging_conf import get_logger
from ..models import FileMeta
from ..security import resolve_in_sandbox

logger = get_logger(__name__)


def scan_directory(directory: str, recursive: bool = False) -> List[FileMeta]:
    """扫描目录，返回文件元数据列表。"""
    root = resolve_in_sandbox(directory, must_exist=True)
    if not root.is_dir():
        raise NotADirectoryError(f"不是目录: {root}")

    items: List[FileMeta] = []
    iterator = root.rglob("*") if recursive else root.iterdir()
    for p in iterator:
        try:
            stat = p.stat()
            items.append(
                FileMeta(
                    name=p.name,
                    path=str(p),
                    ext=p.suffix.lower().lstrip("."),
                    size=stat.st_size,
                    modified=datetime.fromtimestamp(stat.st_mtime),
                    is_dir=p.is_dir(),
                )
            )
        except OSError as exc:
            logger.warning("跳过无法读取的项: %s (%s)", p, exc)
    return items


def make_dir(path: str) -> str:
    """创建文件夹（含中间目录），幂等。"""
    target = resolve_in_sandbox(path)
    target.mkdir(parents=True, exist_ok=True)
    log_operation("make_dir", str(target), "ok")
    return str(target)


def _unique_dest(dest: Path) -> Path:
    """若目标已存在，追加序号避免覆盖（非高危路径）。"""
    if not dest.exists():
        return dest
    stem, suffix, parent = dest.stem, dest.suffix, dest.parent
    i = 1
    while True:
        candidate = parent / f"{stem} ({i}){suffix}"
        if not candidate.exists():
            return candidate
        i += 1


def _safe_child_name(name: str) -> str:
    """只接受纯文件名，阻止 Agent 借 new_name 跨目录或使用绝对路径。"""
    candidate = Path(name)
    if (
        not name
        or candidate.name != name
        or candidate.is_absolute()
        or name in {".", ".."}
        or "/" in name
        or "\\" in name
    ):
        raise ValueError(f"非法文件名: {name}")
    return name


def move_file(src: str, dest_dir: str, new_name: str | None = None) -> str:
    """移动文件到目标目录，可同时重命名。目标存在则自动改名避免覆盖。"""
    src_path = resolve_in_sandbox(src, must_exist=True)
    if src_path.is_dir():
        raise IsADirectoryError(f"move_file 仅支持文件: {src_path}")
    dest_root = resolve_in_sandbox(dest_dir)
    dest_root.mkdir(parents=True, exist_ok=True)
    if not dest_root.is_dir():
        raise NotADirectoryError(f"目标不是目录: {dest_root}")

    name = _safe_child_name(new_name) if new_name else src_path.name
    dest = resolve_in_sandbox(str(dest_root / name))
    dest = _unique_dest(dest)
    try:
        shutil.move(str(src_path), str(dest))
        log_operation("move", str(src_path), "ok", dest=str(dest))
        return str(dest)
    except OSError as exc:
        log_operation("move", str(src_path), "failed", dest=str(dest), detail=str(exc))
        raise


def rename_file(src: str, new_name: str) -> str:
    """在原目录内重命名文件。"""
    src_path = resolve_in_sandbox(src, must_exist=True)
    if src_path.is_dir():
        raise IsADirectoryError(f"rename_file 仅支持文件: {src_path}")
    name = _safe_child_name(new_name)
    dest = resolve_in_sandbox(str(src_path.with_name(name)))
    dest = _unique_dest(dest)
    try:
        src_path.rename(dest)
        log_operation("rename", str(src_path), "ok", dest=str(dest))
        return str(dest)
    except OSError as exc:
        log_operation("rename", str(src_path), "failed", dest=str(dest), detail=str(exc))
        raise


def delete_file(path: str) -> str:
    """删除文件或目录（高危）。仅应在审批通过后调用。"""
    target = resolve_in_sandbox(path, must_exist=True)
    try:
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink()
        log_operation("delete", str(target), "ok")
        return str(target)
    except OSError as exc:
        log_operation("delete", str(target), "failed", detail=str(exc))
        raise
