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

from .. import db
from ..db import log_operation
from ..logging_conf import get_logger
from ..models import FileMeta, OperationLog
from ..security import SandboxViolation, resolve_in_sandbox, sandbox_root_for

logger = get_logger(__name__)

# 删除的文件先移入沙箱根下的回收站，使删除可撤销（见 P1-1）。
TRASH_DIRNAME = ".butler-trash"

# 可回滚的操作类型：move/rename 天然可逆，delete 通过回收站可逆。
_REVERSIBLE_ACTIONS = {"move", "rename", "delete"}


def scan_directory(directory: str, recursive: bool = False) -> List[FileMeta]:
    """扫描目录，返回文件元数据列表。"""
    root = resolve_in_sandbox(directory, must_exist=True)
    if not root.is_dir():
        raise NotADirectoryError(f"不是目录: {root}")

    items: List[FileMeta] = []
    iterator = root.rglob("*") if recursive else root.iterdir()
    for p in iterator:
        # 跳过回收站，避免已删除文件污染后续分类 / 整理（见 P1-1）。
        try:
            if TRASH_DIRNAME in p.relative_to(root).parts:
                continue
        except ValueError:
            pass
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
    """删除文件或目录（高危）。仅应在审批通过后调用。

    不做物理删除，而是移入所在沙箱根下的 ``.butler-trash/``，使删除可撤销（P1-1）。
    审计日志记录原路径(target)与回收站路径(dest)，回滚时据此还原。
    """
    target = resolve_in_sandbox(path, must_exist=True)
    root = sandbox_root_for(target)
    trash = root / TRASH_DIRNAME
    try:
        trash.mkdir(parents=True, exist_ok=True)
        dest = _unique_dest(trash / target.name)
        shutil.move(str(target), str(dest))
        log_operation("delete", str(target), "ok", dest=str(dest))
        return str(dest)
    except OSError as exc:
        log_operation("delete", str(target), "failed", detail=str(exc))
        raise


def restore_operation(op: OperationLog) -> dict:
    """逆转单条可回滚操作（move/rename/delete），把文件从 dest 移回 target。

    - 仅处理 status=="ok" 且带 dest 的 move/rename/delete。
    - 若原位置已被占用，还原到自动编号的新名并在结果中说明。
    - 回滚本身写审计日志（action="rollback"），归属原操作的 thread_id。
    返回 {status, detail, restored_to?}。status ∈ {ok, skipped, failed}。
    """
    if op.action not in _REVERSIBLE_ACTIONS or op.status != "ok" or not op.dest:
        return {"status": "skipped", "detail": f"操作 #{op.id}（{op.action}/{op.status}）不可回滚"}

    current = resolve_in_sandbox(op.dest)          # 文件当前所在（move 目标 / 回收站）
    original = resolve_in_sandbox(op.target)       # 原始位置
    if not current.exists():
        db.log_operation(
            "rollback", op.dest or "", "skipped",
            detail=f"撤销 {op.action} #{op.id} 失败：源已不存在",
            thread_id=op.thread_id,
        )
        return {"status": "skipped", "detail": f"源 {current} 已不存在，可能已被移动或再次删除"}

    try:
        original.parent.mkdir(parents=True, exist_ok=True)
        final = _unique_dest(original)
        renamed = final != original
        shutil.move(str(current), str(final))
        db.log_operation(
            "rollback", str(current), "ok", dest=str(final),
            detail=f"撤销 {op.action} #{op.id}" + ("（原位置被占用，已改名）" if renamed else ""),
            thread_id=op.thread_id,
        )
        result = {"status": "ok", "detail": f"已撤销 {op.action} #{op.id}", "restored_to": str(final)}
        if renamed:
            result["detail"] += "；原位置被占用，已还原为新名"
        return result
    except OSError as exc:
        db.log_operation(
            "rollback", str(current), "failed",
            detail=f"撤销 {op.action} #{op.id} 失败：{exc}",
            thread_id=op.thread_id,
        )
        return {"status": "failed", "detail": f"撤销失败：{exc}"}
