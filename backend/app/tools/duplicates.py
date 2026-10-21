"""P1：重复文件检测（按大小分组 → 按需哈希 → 流式读取）。

关键约束：
- 文件名相同不代表内容相同：只有大小一致且内容哈希一致才算重复。
- 跳过回收站、符号链接与目录联接；硬链接按 ``(st_dev, st_ino)`` 识别，可回收空间
  只按“不同 inode 的份数”计算，避免把同一份数据算成多份。
- 扫描期间文件发生变化时重新计算一次；仍然变化则标记为 ``changed`` 并排除，
  不把不确定的结果当作重复。
- 大文件用固定大小的块流式读取，内存占用与文件大小无关。
- 检测本身永不删除文件；清理需显式确认并重新核验内容，走软删除/审计/回滚链路。
"""
from __future__ import annotations

import hashlib
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .. import db
from ..logging_conf import get_logger
from ..security import SandboxViolation, is_within_sandbox, resolve_in_sandbox
from . import filesystem
from .path_locks import path_locks

logger = get_logger(__name__)

CHUNK_SIZE = 1024 * 1024          # 1 MiB：大文件流式读取块
PARTIAL_SIZE = 64 * 1024          # 首 64 KiB 先做粗筛，避免整盘全量读
MIN_SIZE_DEFAULT = 1              # 0 字节文件默认不参与（空文件互相“重复”没有意义）
MAX_FILES_DEFAULT = 200_000
SCAN_TTL_SECONDS = 3600

_scans: dict[str, dict] = {}
_lock = threading.Lock()


class DuplicateError(Exception):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_link(entry: os.DirEntry) -> bool:
    """符号链接 / Windows 目录联接一律跳过（不跟随，也不作为文件参与哈希）。"""
    try:
        if entry.is_symlink():
            return True
        if os.name == "nt":
            import stat as stat_module

            flag = getattr(stat_module, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
            if flag and entry.stat(follow_symlinks=False).st_file_attributes & flag:
                return True
    except OSError:
        return True
    return False


def _skip_dir(path: str) -> bool:
    """目录符号链接与 Windows 目录联接都不下探。"""
    if os.path.islink(path):
        return True
    is_junction = getattr(os.path, "isjunction", None)
    if is_junction is not None:
        try:
            return bool(is_junction(path))
        except OSError:
            return True
    return False


def _iter_files(root: Path, *, max_files: int, progress: dict, cancel: threading.Event):
    for current, dirnames, filenames in os.walk(root, followlinks=False):
        if cancel.is_set():
            return
        # 原地修改 dirnames：跳过回收站与链接类目录，避免枚举到已删除文件或环。
        dirnames[:] = sorted(
            name for name in dirnames
            if name != filesystem.TRASH_DIRNAME and not _skip_dir(os.path.join(current, name))
        )
        for name in sorted(filenames):
            path = Path(current) / name
            try:
                if path.is_symlink():
                    continue
                stat = path.stat()
            except OSError:
                continue
            if not os.path.isfile(path):
                continue
            progress["scanned"] += 1
            if progress["scanned"] > max_files:
                raise DuplicateError(f"文件数超过上限 {max_files}，请缩小扫描范围")
            yield {
                "path": str(path),
                "size": int(stat.st_size),
                "mtime_ns": int(stat.st_mtime_ns),
                "mtime": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                "dev": int(getattr(stat, "st_dev", 0)),
                "ino": int(getattr(stat, "st_ino", 0)),
                "links": int(getattr(stat, "st_nlink", 1)),
            }


def _hash_file(path: Path, *, limit: Optional[int] = None,
               cancel: Optional[threading.Event] = None,
               progress: Optional[dict] = None) -> str:
    digest = hashlib.sha256()
    remaining = limit
    with open(path, "rb") as handle:
        while True:
            if cancel is not None and cancel.is_set():
                raise DuplicateError("已取消")
            size = CHUNK_SIZE if remaining is None else min(CHUNK_SIZE, remaining)
            if size <= 0:
                break
            block = handle.read(size)
            if not block:
                break
            digest.update(block)
            if progress is not None:
                progress["bytes_read"] += len(block)
            if remaining is not None:
                remaining -= len(block)
    return digest.hexdigest()


def _signature(entry: dict) -> tuple[int, int]:
    return int(entry["size"]), int(entry["mtime_ns"])


def _hash_stable(entry: dict, cancel: threading.Event, progress: dict) -> Optional[str]:
    """哈希并在前后各取一次签名；变化则重算一次，仍变化返回 None。"""
    path = Path(entry["path"])
    for _ in range(2):
        try:
            before = path.stat()
            digest = _hash_file(path, cancel=cancel, progress=progress)
            after = path.stat()
        except OSError:
            return None
        if (before.st_size, before.st_mtime_ns) == (after.st_size, after.st_mtime_ns):
            entry["size"], entry["mtime_ns"] = int(after.st_size), int(after.st_mtime_ns)
            return digest
    return None


def _suggest_keep(files: list[dict]) -> dict:
    """保留建议：修改时间最新（通常是当前版本）；并列时取路径最短的一份。"""
    ordered = sorted(files, key=lambda item: (-int(item["mtime_ns"]), len(item["path"]), item["path"]))
    return ordered[0]


def _build_groups(hashed: dict[str, list[dict]], change_progress) -> list[dict]:
    groups: list[dict] = []
    for digest, files in hashed.items():
        if len(files) < 2:
            continue
        files = sorted(files, key=lambda item: item["path"])
        keep = _suggest_keep(files)
        distinct_inodes = {(item["dev"], item["ino"]) for item in files}
        reclaimable = max(0, len(distinct_inodes) - 1) * int(files[0]["size"])
        seen_inodes: dict[tuple[int, int], str] = {}
        entries = []
        for item in files:
            key = (item["dev"], item["ino"])
            hardlink_of = ""
            if key in seen_inodes:
                hardlink_of = seen_inodes[key]
            else:
                seen_inodes[key] = item["path"]
            entries.append({
                "path": item["path"],
                "name": Path(item["path"]).name,
                "size": int(item["size"]),
                "modified": item["mtime"],
                "hardlink_of": hardlink_of,
                "keep": item["path"] == keep["path"],
                "delete_by_default": item["path"] != keep["path"],
            })
        groups.append({
            "group_id": digest[:16],
            "hash": digest,
            "size": int(files[0]["size"]),
            "count": len(files),
            "distinct_inodes": len(distinct_inodes),
            "reclaimable": reclaimable,
            "suggested_keep": keep["path"],
            "keep_reason": "修改时间最新，作为保留项（相同则取路径最短的一份）",
            "files": entries,
        })
        change_progress()
    groups.sort(key=lambda item: (-item["reclaimable"], item["group_id"]))
    return groups


def _run(scan_id: str, directories: list[str], min_size: int, max_files: int) -> None:
    scan = _scans[scan_id]
    progress = scan["progress"]
    cancel: threading.Event = scan["cancel"]
    try:
        entries: list[dict] = []
        for directory in directories:
            if cancel.is_set():
                raise DuplicateError("已取消")
            root = resolve_in_sandbox(directory, must_exist=True)
            progress["phase"] = "scanning"
            progress["current"] = str(root)
            for entry in _iter_files(root, max_files=max_files, progress=progress, cancel=cancel):
                if entry["size"] >= min_size:
                    entries.append(entry)
        progress["total_files"] = len(entries)

        by_size: dict[int, list[dict]] = {}
        for entry in entries:
            by_size.setdefault(entry["size"], []).append(entry)
        progress["phase"] = "partial_hash"
        progress["total_candidates"] = sum(len(group) for group in by_size.values() if len(group) > 1)

        # 第一步：只读首 64 KiB 做粗筛，缩小必须完整读取的文件集合。
        candidate_groups: dict[tuple[int, str], list[dict]] = {}
        for size, group in sorted(by_size.items()):
            if len(group) < 2:
                continue
            for entry in group:
                if cancel.is_set():
                    raise DuplicateError("已取消")
                if size <= PARTIAL_SIZE:
                    # 小文件直接整文件哈希，避免“粗筛 + 全量”两次读。
                    candidate_groups.setdefault((size, "full"), []).append(entry)
                    continue
                try:
                    head = _hash_file(Path(entry["path"]), limit=PARTIAL_SIZE, cancel=cancel, progress=progress)
                except (OSError, DuplicateError) as exc:
                    if isinstance(exc, DuplicateError) and cancel.is_set():
                        raise
                    progress["errors"].append({"path": entry["path"], "error": str(exc)})
                    continue
                candidate_groups.setdefault((size, head), []).append(entry)
                progress["hashed"] += 1

        progress["phase"] = "full_hash"
        hashed: dict[str, list[dict]] = {}
        changed: list[str] = []
        for (size, key), group in candidate_groups.items():
            if len(group) < 2:
                continue
            if key == "full":
                for entry in group:
                    if cancel.is_set():
                        raise DuplicateError("已取消")
                    digest = _hash_stable(entry, cancel, progress)
                    if digest is None:
                        changed.append(entry["path"])
                        continue
                    hashed.setdefault(f"{size}:{digest}", []).append(entry)
                    progress["hashed"] += 1
            else:
                for entry in group:
                    if cancel.is_set():
                        raise DuplicateError("已取消")
                    digest = _hash_stable(entry, cancel, progress)
                    if digest is None:
                        changed.append(entry["path"])
                        continue
                    hashed.setdefault(digest, []).append(entry)
                    progress["hashed"] += 1

        def bump() -> None:
            progress["groups_done"] += 1

        groups = _build_groups(hashed, bump)
        progress["phase"] = "done"
        progress["finished_at"] = _now()
        total_reclaimable = sum(group["reclaimable"] for group in groups)
        with _lock:
            scan["status"] = "completed"
            scan["groups"] = groups
            scan["changed"] = changed
            scan["summary"] = {
                "scanned_files": len(entries),
                "duplicate_groups": len(groups),
                "duplicate_files": sum(group["count"] for group in groups),
                "reclaimable_bytes": total_reclaimable,
                "changed_files": len(changed),
                "stale": bool(changed),
                "errors": len(progress["errors"]),
            }
    except DuplicateError as exc:
        with _lock:
            scan["status"] = "cancelled" if cancel.is_set() else "failed"
            scan["error"] = str(exc)
            scan["summary"] = {
                "scanned_files": progress["scanned"],
                "duplicate_groups": 0,
                "duplicate_files": 0,
                "reclaimable_bytes": 0,
                "changed_files": 0,
                "stale": False,
                "errors": len(progress["errors"]),
            }
    except (SandboxViolation, FileNotFoundError, NotADirectoryError) as exc:
        with _lock:
            scan["status"] = "failed"
            scan["error"] = str(exc)
    except Exception as exc:  # noqa: BLE001 - 扫描线程异常必须收敛成状态
        logger.exception("重复文件扫描失败")
        with _lock:
            scan["status"] = "failed"
            scan["error"] = str(exc)


def start_scan(
    directories: list[str],
    *,
    min_size: int = MIN_SIZE_DEFAULT,
    max_files: int = MAX_FILES_DEFAULT,
) -> dict:
    """启动后台扫描并立刻返回 scan_id（不阻塞事件循环）。"""
    if not directories:
        raise DuplicateError("请至少选择一个目录")
    normalized = [str(resolve_in_sandbox(value, must_exist=True)) for value in directories]
    for value in normalized:
        if not Path(value).is_dir():
            raise DuplicateError(f"不是目录: {value}")
    _evict()
    scan_id = uuid.uuid4().hex
    scan = {
        "scan_id": scan_id,
        "created_at": _now(),
        "directories": normalized,
        "status": "running",
        "error": "",
        "groups": [],
        "changed": [],
        "summary": {},
        "cancel": threading.Event(),
        "progress": {
            "phase": "queued",
            "scanned": 0,
            "hashed": 0,
            "total_files": 0,
            "total_candidates": 0,
            "groups_done": 0,
            "bytes_read": 0,
            "current": "",
            "errors": [],
            "finished_at": "",
        },
    }
    with _lock:
        _scans[scan_id] = scan
    thread = threading.Thread(
        target=_run, args=(scan_id, normalized, int(min_size), int(max_files)),
        name=f"dup-{scan_id[:8]}", daemon=True,
    )
    thread.start()
    return {"scan_id": scan_id, "status": "running"}


def _evict() -> None:
    """丢弃过期结果，避免长时间运行后内存无限增长。"""
    now = datetime.now(timezone.utc)
    with _lock:
        for key, scan in list(_scans.items()):
            try:
                created = datetime.fromisoformat(scan["created_at"])
            except (ValueError, TypeError):
                continue
            if scan["status"] == "running":
                continue
            if (now - created).total_seconds() > SCAN_TTL_SECONDS:
                _scans.pop(key, None)


def get(scan_id: str) -> dict:
    with _lock:
        scan = _scans.get(scan_id)
    if scan is None:
        raise DuplicateError(f"重复文件扫描不存在或已过期: {scan_id}")
    progress = dict(scan["progress"])
    progress["errors"] = list(progress.get("errors", []))[:20]
    return {
        "scan_id": scan["scan_id"],
        "created_at": scan["created_at"],
        "status": scan["status"],
        "error": scan["error"],
        "directories": scan["directories"],
        "originals": scan["directories"],
        "groups": scan["groups"],
        "changed": scan["changed"],
        "summary": scan["summary"],
        "progress": progress,
    }


def cancel(scan_id: str) -> dict:
    with _lock:
        scan = _scans.get(scan_id)
        if scan is None:
            raise DuplicateError(f"重复文件扫描不存在或已过期: {scan_id}")
        scan["cancel"].set()
    return {"scan_id": scan_id, "status": "cancelling"}


def cleanup(
    scan_id: str,
    *,
    keep_map: Optional[dict[str, str]] = None,
    group_ids: Optional[list[str]] = None,
) -> dict:
    """执行清理：重新核验内容后软删除。

    - 每组保留 ``keep_map[group_id]``，缺省用保留建议；
    - 重新计算保留项与被删项的内容哈希，不一致就跳过该文件；
    - 删除走 ``filesystem.delete_file``（回收站 + 审计 + 可按 thread 回滚）。
    """
    scan = get(scan_id)
    if scan["status"] != "completed":
        raise DuplicateError("扫描尚未完成，无法清理")
    chosen = set(group_ids or [group["group_id"] for group in scan["groups"]])
    keep_map = {str(key): str(value) for key, value in (keep_map or {}).items()}
    results: list[dict] = []
    freed = 0
    thread_id = f"dedupe-{scan_id}"
    # 删除必须真的落在返回的 thread_id 下：否则界面拿到的批次编号无法用于整批撤销，
    # 该任务下的操作记录会是 0 条。审计日志统一由这个上下文写入归属。
    with db.operation_thread(thread_id):
        for group in scan["groups"]:
            if group["group_id"] not in chosen:
                continue
            files = group["files"]
            keep_path = keep_map.get(group["group_id"]) or group["suggested_keep"]
            keeper = next((item for item in files if item["path"] == keep_path), None)
            if keeper is None:
                results.extend({
                    "path": item["path"], "group_id": group["group_id"],
                    "status": "skipped", "detail": "保留项不在该组内，已跳过整组",
                } for item in files)
                continue
            try:
                keeper_hash = _hash_file(Path(keeper["path"]))
            except OSError as exc:
                results.extend({
                    "path": item["path"], "group_id": group["group_id"],
                    "status": "skipped", "detail": f"保留项不可读：{exc}",
                } for item in files)
                continue
            for item in files:
                if item["path"] == keeper["path"]:
                    continue
                path = Path(item["path"])
                if not is_within_sandbox(path):
                    results.append({
                        "path": item["path"], "group_id": group["group_id"],
                        "status": "skipped", "detail": "路径已不在授权目录内",
                    })
                    continue
                try:
                    current_hash = _hash_file(path)
                except OSError as exc:
                    results.append({
                        "path": item["path"], "group_id": group["group_id"],
                        "status": "skipped", "detail": f"文件不可读：{exc}",
                    })
                    continue
                if current_hash != keeper_hash:
                    results.append({
                        "path": item["path"], "group_id": group["group_id"],
                        "status": "skipped",
                        "detail": "内容已与保留项不同（扫描后发生变化），未删除",
                    })
                    continue
                try:
                    with path_locks.acquire(path.parent):
                        trash_path = filesystem.delete_file(str(path))
                    results.append({
                        "path": item["path"], "group_id": group["group_id"],
                        "status": "ok", "detail": "已移入回收站", "trash_path": trash_path,
                    })
                    if not item["hardlink_of"]:
                        freed += int(item["size"])
                    from . import indexer  # 延迟导入：索引是可选能力

                    indexer.note_path_removed(str(path))
                except (SandboxViolation, FileNotFoundError) as exc:
                    results.append({
                        "path": item["path"], "group_id": group["group_id"],
                        "status": "skipped", "detail": str(exc),
                    })
                except Exception as exc:  # noqa: BLE001
                    logger.exception("清理重复文件失败 %s", path)
                    results.append({
                        "path": item["path"], "group_id": group["group_id"],
                        "status": "failed", "detail": str(exc),
                    })
    return {
        "scan_id": scan_id,
        "thread_id": thread_id,
        "deleted": sum(1 for item in results if item["status"] == "ok"),
        "skipped": sum(1 for item in results if item["status"] == "skipped"),
        "failed": sum(1 for item in results if item["status"] == "failed"),
        "freed_bytes": freed,
        "results": results,
    }
