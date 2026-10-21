"""P2：本地内容索引与关键词搜索（中文可用）。

实现选择：
- 正文由现有的 PDF / DOCX / 文本 / OCR 提取链路产出，不引入向量库或云服务；
- SQLite FTS5 可用时用它检索，**中文按单字切分后以短语查询**恢复“相邻字符”语义
  （``报告`` → ``"报 告"``），因此中文子串检索可用，而不是只对英文生效；
- FTS5 不可用时回退到 ``LIKE '%关键词%'`` 子串匹配，行为对中文同样正确；
- 索引是持久的（SQLite），每次 ``sync`` 会按 size+mtime 增量更新，并删除已消失文件；
- 搜索时重新校验授权目录：旧索引里的路径一旦越界（授权被收缩、目录被移动）就不再
  返回内容，避免通过历史索引泄露已撤销的授权。

为后续语义检索保留 ``SearchBackend`` 协议：新后端只需实现 ``search`` 并注册，
REST 层通过 ``backend`` 参数选择；当前只注册真实可用的 ``keyword`` 后端。
"""
from __future__ import annotations

import os
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional, Protocol

from .. import db
from ..logging_conf import get_logger
from ..security import SandboxViolation, is_within_sandbox, resolve_in_sandbox
from . import extract, filesystem
from .classify import CONTENT_EXTS

logger = get_logger(__name__)

MAX_INDEX_CHARS = 200_000
INDEXABLE_EXTS = CONTENT_EXTS
MAX_FILES_DEFAULT = 50_000
MAX_INDEXED_BYTES = 50 * 1024 * 1024  # 与 extract 的读取上限一致

_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_TOKEN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+|[A-Za-z0-9_]+")

_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()


class IndexingError(Exception):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _fts_available() -> bool:
    with db.connection() as conn:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='content_index_fts'"
        ).fetchone()
    return row is not None


# ---------------- 文本切分（写索引与查询共用） ----------------

def segment_for_fts(text: str) -> str:
    """把正文切成 FTS5 可检索的形式：CJK 逐字加空格，其余原样保留。

    unicode61 分词器不切中文，整段“董事会决议”会变成一个 token；逐字加空格后
    ``董事会`` 变成 ``董 事 会``，三个词元相邻，可以用短语查询精确匹配子串。
    """
    parts: list[str] = []
    buffer: list[str] = []
    for char in text or "":
        if _CJK.match(char):
            if buffer:
                parts.append("".join(buffer))
                buffer = []
            parts.append(char)
        else:
            buffer.append(char)
    if buffer:
        parts.append("".join(buffer))
    return " ".join(part for part in parts if part.strip())


def build_fts_query(query: str) -> str:
    """把用户输入转成 FTS5 查询；CJK 连续片段用短语保证相邻。"""
    terms: list[str] = []
    for match in _TOKEN.finditer(query or ""):
        token = match.group(0)
        if _CJK.search(token):
            if len(token) == 1:
                terms.append(f'"{token}"')
            else:
                terms.append('"' + " ".join(token) + '"')
        else:
            terms.append(f'"{token}"*')
    if not terms:
        raise IndexingError("搜索关键词不能为空")
    return " AND ".join(terms)


# ---------------- 后端接口（为语义检索预留） ----------------

class SearchBackend(Protocol):
    name: str

    def search(self, query: str, *, limit: int, offset: int) -> tuple[list[dict], int]:
        ...


class KeywordBackend:
    """SQLite 关键词检索（FTS5 优先，缺失时子串匹配）。"""

    name = "keyword"

    def search(self, query: str, *, limit: int, offset: int) -> tuple[list[dict], int]:
        return _keyword_search(query, limit=limit, offset=offset)


KEYWORD_BACKEND = KeywordBackend()
BACKENDS: dict[str, SearchBackend] = {KEYWORD_BACKEND.name: KEYWORD_BACKEND}


def backend(name: str) -> SearchBackend:
    selected = BACKENDS.get(name or "keyword")
    if selected is None:
        raise IndexingError(f"未知的检索后端: {name}（当前只提供 keyword）")
    return selected


def _authorized(path: str) -> bool:
    """索引结果只按**当前授权**判断，不再要求仍在索引时的根目录内。

    文件被移出原索引目录、但仍在授权范围内时，索引会通过 ``note_path_moved`` 跟随
    它；旧实现额外要求路径位于 ``root`` 之下，导致这类文件被过滤掉（搜不到）。
    授权收缩时 ``is_within_sandbox`` 仍会拒绝，历史索引不会泄露已撤销的内容。
    """
    try:
        return is_within_sandbox(Path(path))
    except (OSError, RuntimeError):
        return False


def _snippet(content: str, query: str, *, width: int = 80) -> str:
    text = content or ""
    lowered = text.casefold()
    for match in _TOKEN.finditer(query or ""):
        token = match.group(0).casefold()
        index = lowered.find(token)
        if index >= 0:
            start = max(0, index - width // 2)
            end = min(len(text), index + len(token) + width // 2)
            prefix = "…" if start > 0 else ""
            suffix = "…" if end < len(text) else ""
            return f"{prefix}{text[start:end].replace(chr(10), ' ')}{suffix}"
    return text[:width].replace("\n", " ") + ("…" if len(text) > width else "")


def _matching_paths(query: str, *, use_fts: bool) -> list[str]:
    """返回**全部**命中路径（按相关度排序），不分页、不取正文。

    旧实现只取 ``limit * 4 + offset`` 行，于是 ``total`` 变成“这次取回来的行数”：
    12 条命中、每页 1 条时会错误显示 4。这里分两步——先取全部路径用于精确计数与
    分页，再按当前页的路径取正文，既准确又不用把正文全读进内存。
    """
    if use_fts:
        expression = build_fts_query(query)
        sql = (
            "SELECT f.path FROM content_index_fts f"
            " JOIN content_index i ON i.path = f.path"
            " WHERE content_index_fts MATCH ?"
            " ORDER BY bm25(content_index_fts)"
        )
        with db.connection() as conn:
            return [row["path"] for row in conn.execute(sql, (expression,)).fetchall()]
    with db.connection() as conn:
        rows = conn.execute(
            "SELECT path FROM content_index WHERE content LIKE ? ORDER BY indexed_at DESC",
            (f"%{query}%",),
        ).fetchall()
    return [row["path"] for row in rows]


def _rows_for(paths: list[str]) -> dict[str, Any]:
    """按路径取索引行；分批 IN 查询，避免 SQLite 变量数量上限。"""
    rows: dict[str, Any] = {}
    for start in range(0, len(paths), 400):
        chunk = paths[start:start + 400]
        placeholders = ",".join("?" * len(chunk))
        with db.connection() as conn:
            fetched = conn.execute(
                "SELECT path, root, size, mtime_ns, ext, status, truncated, content, indexed_at"
                f" FROM content_index WHERE path IN ({placeholders})",
                tuple(chunk),
            ).fetchall()
        for row in fetched:
            rows[row["path"]] = row
    return rows


def _keyword_search(query: str, *, limit: int, offset: int) -> tuple[list[dict], int]:
    query = (query or "").strip()
    if not query:
        raise IndexingError("搜索关键词不能为空")
    limit = max(1, min(200, int(limit)))
    offset = max(0, int(offset))
    use_fts = _fts_available()
    paths = _matching_paths(query, use_fts=True) if use_fts else []
    if not paths:
        # FTS 无命中（或不可用）时回退到子串匹配，保证中文短词与部分匹配可用。
        paths = _matching_paths(query, use_fts=False)

    authorized = [path for path in paths if _authorized(path)]
    total = len(authorized)
    page_paths = authorized[offset:offset + limit]
    rows = _rows_for(page_paths)
    results = [{
        "path": row["path"],
        "name": Path(row["path"]).name,
        "size": int(row["size"] or 0),
        "ext": row["ext"],
        "status": row["status"],
        "truncated": bool(row["truncated"]),
        "indexed_at": row["indexed_at"],
        "snippet": _snippet(row["content"] or "", query),
    } for row in (rows[path] for path in page_paths if path in rows)]
    return results, total


def search(query: str, *, limit: int = 50, offset: int = 0, backend_name: str = "keyword") -> dict:
    backend(backend_name)
    results, total = backend(backend_name).search(query, limit=limit, offset=offset)
    return {
        "query": query,
        "backend": backend_name,
        "total": total,
        "offset": offset,
        "limit": limit,
        "results": results,
        "capabilities": {"fts5": _fts_available(), "semantic": False},
    }


# ---------------- 索引 ----------------


def _index_file(path: Path, root: str) -> dict:
    """读取并写索引；返回该文件的索引状态。任何失败都收敛成状态值。"""
    try:
        stat = path.stat()
    except OSError as exc:
        return {"status": "failed", "error": f"无法读取文件: {exc}", "truncated": False}
    size, mtime_ns = int(stat.st_size), int(stat.st_mtime_ns)
    existing = None
    with db.connection() as conn:
        existing = conn.execute(
            "SELECT size, mtime_ns, status FROM content_index WHERE path=?", (str(path),)
        ).fetchone()
    if existing is not None and int(existing["size"]) == size and int(existing["mtime_ns"]) == mtime_ns:
        return {"status": existing["status"], "unchanged": True, "truncated": False}
    ext = path.suffix.lower().lstrip(".")
    if ext not in INDEXABLE_EXTS:
        _upsert(path, root, size, mtime_ns, ext, "unsupported", "", "", False)
        return {"status": "unsupported", "truncated": False}
    if size > MAX_INDEXED_BYTES:
        _upsert(path, root, size, mtime_ns, ext, "failed", "文件过大，未建立索引", "", False)
        return {"status": "failed", "error": "文件过大", "truncated": False}
    try:
        text = extract.extract_text(str(path), max_chars=MAX_INDEX_CHARS)
    except Exception as exc:  # noqa: BLE001 - 单个文件提取失败不影响整批
        _upsert(path, root, size, mtime_ns, ext, "failed", str(exc)[:300], "", False)
        return {"status": "failed", "error": str(exc), "truncated": False}
    truncated = "[已截断]" in text or len(text) >= MAX_INDEX_CHARS
    if not text.strip() or text.startswith("["):
        _upsert(path, root, size, mtime_ns, ext, "empty", "", "", False)
        return {"status": "empty", "truncated": False}
    _upsert(path, root, size, mtime_ns, ext, "indexed", "", text, truncated)
    return {"status": "indexed", "truncated": truncated}


def _upsert(path: Path, root: str, size: int, mtime_ns: int, ext: str,
            status: str, error: str, content: str, truncated: bool) -> None:
    with db.connection() as conn:
        conn.execute(
            "INSERT INTO content_index"
            " (path, root, size, mtime_ns, ext, status, error, content, truncated, indexed_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(path) DO UPDATE SET root=excluded.root, size=excluded.size,"
            " mtime_ns=excluded.mtime_ns, ext=excluded.ext, status=excluded.status,"
            " error=excluded.error, content=excluded.content,"
            " truncated=excluded.truncated, indexed_at=excluded.indexed_at",
            (str(path), root, size, mtime_ns, ext, status, error, content,
             int(truncated), _now()),
        )
        if _fts_available():
            conn.execute("DELETE FROM content_index_fts WHERE path=?", (str(path),))
            if content:
                conn.execute(
                    "INSERT INTO content_index_fts (path, body) VALUES (?, ?)",
                    (str(path), segment_for_fts(content)),
                )


def _delete_paths(paths: Iterable[str]) -> None:
    for path in paths:
        with db.connection() as conn:
            conn.execute("DELETE FROM content_index WHERE path=?", (path,))
            if _fts_available():
                conn.execute("DELETE FROM content_index_fts WHERE path=?", (path,))


def note_path_moved(old: str, new: str) -> None:
    """移动/重命名后的索引增量更新（best-effort，失败不影响文件操作）。

    文件被移出原索引目录时把 ``root`` 改成新位置所在的沙箱根：否则后续对该目录做
    增量清理时，会把这个仍然存在的文件的索引记录当成“已删除”删掉，搜索结果随之消失。
    """
    try:
        with db.connection() as conn:
            row = conn.execute("SELECT * FROM content_index WHERE path=?", (old,)).fetchone()
            if row is None:
                return
            root = str(row["root"] or "")
            new_root = root
            try:
                inside = Path(new).resolve(strict=False).relative_to(
                    Path(root).resolve(strict=False)
                )
                inside_name = str(inside)
            except (ValueError, OSError):
                inside_name = ""
            if not inside_name:
                # 已不在原索引目录内：改用新位置所属的沙箱根，避免被误清理。
                from ..security import sandbox_root_for

                try:
                    new_root = str(sandbox_root_for(Path(new)))
                except SandboxViolation:
                    new_root = root
            conn.execute(
                "INSERT INTO content_index (path, root, size, mtime_ns, ext, status, error,"
                " content, truncated, indexed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(path) DO UPDATE SET size=excluded.size,"
                " mtime_ns=excluded.mtime_ns, content=excluded.content, root=excluded.root",
                (new, new_root, row["size"], row["mtime_ns"], row["ext"], row["status"],
                 row["error"], row["content"], row["truncated"], _now()),
            )
            conn.execute("DELETE FROM content_index WHERE path=?", (old,))
            if _fts_available():
                conn.execute("DELETE FROM content_index_fts WHERE path=?", (old,))
                conn.execute("DELETE FROM content_index_fts WHERE path=?", (new,))
                if row["content"]:
                    conn.execute(
                        "INSERT INTO content_index_fts (path, body) VALUES (?, ?)",
                        (new, segment_for_fts(row["content"])),
                    )
    except Exception:  # noqa: BLE001
        logger.exception("更新内容索引路径失败 %s -> %s", old, new)


def note_path_removed(path: str) -> None:
    try:
        _delete_paths([path])
    except Exception:  # noqa: BLE001
        logger.exception("移除内容索引失败 %s", path)


def _iter_indexable(root: Path, *, recursive: bool, max_files: int,
                    cancel: threading.Event, progress: dict):
    """流式枚举可索引文件。

    提前结束有两种原因：取消、以及文件数超过 ``max_files``。两者都会让“本次没遍历到”
    变得没有意义，因此必须在 ``progress["truncated"]`` 里显式记下来，供清理阶段判断
    是否可以安全地删除陈旧索引记录。

    读不了的目录/条目也一样：里面的文件既没有进入 ``seen``、也没有被证实消失，
    因此单独计数到 ``progress["unreadable"]``，同样禁止清理。
    """
    max_depth = 20 if recursive else 1
    stack = [(str(root), 1)]
    while stack:
        if cancel.is_set():
            return
        current, depth = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    if cancel.is_set():
                        return
                    if entry.name == filesystem.TRASH_DIRNAME:
                        continue
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            is_junction = getattr(os.path, "isjunction", None)
                            if is_junction is not None and is_junction(entry.path):
                                continue
                            if depth < max_depth:
                                stack.append((entry.path, depth + 1))
                            continue
                    except OSError:
                        # 判断不了类型就读不到内容：不能当成“文件已不存在”。
                        progress["unreadable"] += 1
                        continue
                    progress["scanned"] += 1
                    if progress["scanned"] > max_files:
                        progress["truncated"] = True
                        progress["truncated_reason"] = f"文件数超过上限 {max_files}"
                        return
                    yield Path(entry.path)
        except OSError as exc:
            progress["unreadable"] += 1
            progress["errors"].append({"path": current, "error": str(exc)})


def _run_job(job_id: str, root: str, recursive: bool, max_files: int) -> None:
    job = _jobs[job_id]
    progress = job["progress"]
    cancel: threading.Event = job["cancel"]
    seen: set[str] = set()
    try:
        progress["phase"] = "indexing"
        for path in _iter_indexable(Path(root), recursive=recursive, max_files=max_files,
                                    cancel=cancel, progress=progress):
            if cancel.is_set():
                raise IndexingError("已取消")
            progress["current"] = str(path)
            seen.add(str(path))
            result = _index_file(path, root)
            if result.get("unchanged"):
                progress["unchanged"] += 1
            elif result["status"] == "indexed":
                progress["indexed"] += 1
            elif result["status"] in {"empty", "unsupported"}:
                progress["skipped"] += 1
            else:
                progress["failed"] += 1
                progress["errors"].append({"path": str(path), "error": result.get("error", "")})
            progress["total"] = len(seen)
        if cancel.is_set():
            raise IndexingError("已取消")
        # 增量删除只允许在“确实遍历完整个范围”时执行：
        # - 取消的任务只遍历了一部分，未遍历到的文件仍然存在，不能当已删除；
        # - 触到 max_files 上限的遍历同样不完整；
        # - 有目录/条目读不了时，里面的文件既没被遍历到、也未被证实消失；
        # - 非递归遍历只覆盖当前层，不能据此删除子目录里的索引记录。
        # 以上任一情况都只报告计数，绝不动已有索引。
        if recursive and not progress.get("truncated") and not progress.get("unreadable"):
            progress["phase"] = "pruning"
            with db.connection() as conn:
                rows = conn.execute(
                    "SELECT path FROM content_index WHERE root=?", (root,)
                ).fetchall()
            stale = [row["path"] for row in rows if row["path"] not in seen]
            if stale:
                _delete_paths(stale)
            progress["removed"] = len(stale)
        elif progress.get("unreadable"):
            progress["prune_skipped"] = (
                f"{progress['unreadable']} 个目录/条目无法读取，未做增量清理"
            )
        else:
            progress["prune_skipped"] = (
                progress.get("truncated_reason") or "非递归遍历不清理索引"
            )
        progress["phase"] = "done"
        job["status"] = "completed"
    except IndexingError as exc:
        job["status"] = "cancelled" if cancel.is_set() else "failed"
        job["error"] = str(exc)
        progress["phase"] = "cancelled" if cancel.is_set() else "failed"
    except (SandboxViolation, FileNotFoundError, NotADirectoryError) as exc:
        job["status"] = "failed"
        job["error"] = str(exc)
        progress["phase"] = "failed"
    except Exception as exc:  # noqa: BLE001
        logger.exception("内容索引任务失败")
        job["status"] = "failed"
        job["error"] = str(exc)
        progress["phase"] = "failed"
    finally:
        job["finished_at"] = _now()


def start_index(directory: str, *, recursive: bool = True,
                max_files: int = MAX_FILES_DEFAULT) -> dict:
    root = str(resolve_in_sandbox(directory, must_exist=True))
    if not Path(root).is_dir():
        raise IndexingError(f"不是目录: {root}")
    for job in _jobs.values():
        if job["root"] == root and job["status"] == "running":
            return {"job_id": job["job_id"], "status": "running", "reused": True}
    job_id = uuid.uuid4().hex
    _jobs[job_id] = {
        "job_id": job_id,
        "root": root,
        "created_at": _now(),
        "status": "running",
        "error": "",
        "cancel": threading.Event(),
        "progress": {
            "phase": "queued", "scanned": 0, "indexed": 0, "skipped": 0,
            "failed": 0, "unchanged": 0, "removed": 0, "total": 0,
            "current": "", "errors": [],
            "bytes": 0, "finished_at": "",
            # 遍历是否完整：取消或触到上限时为 True，此时不得清理陈旧索引。
            "truncated": False, "truncated_reason": "", "prune_skipped": "",
            # 读不了的目录/条目数：>0 时同样禁止清理（否则会把没遍历到的文件当已删除）。
            "unreadable": 0,
        },
    }
    thread = threading.Thread(
        target=_run_job, args=(job_id, root, recursive, int(max_files)),
        name=f"index-{job_id[:8]}", daemon=True,
    )
    thread.start()
    return {"job_id": job_id, "status": "running", "reused": False}


def job_status(job_id: str) -> dict:
    job = _jobs.get(job_id)
    if job is None:
        raise IndexingError(f"索引任务不存在: {job_id}")
    progress = dict(job["progress"])
    progress["errors"] = list(progress.get("errors", []))[:20]
    return {
        "job_id": job["job_id"],
        "root": job["root"],
        "status": job["status"],
        "error": job["error"],
        "created_at": job["created_at"],
        "finished_at": job.get("finished_at", ""),
        "progress": progress,
    }


def cancel_index(job_id: str) -> dict:
    job = _jobs.get(job_id)
    if job is None:
        raise IndexingError(f"索引任务不存在: {job_id}")
    job["cancel"].set()
    return {"job_id": job_id, "status": "cancelling"}


def index_stats() -> dict:
    with db.connection() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS count FROM content_index GROUP BY status"
        ).fetchall()
        total_size = conn.execute(
            "SELECT COALESCE(SUM(size), 0) AS total FROM content_index"
        ).fetchone()
    counts = {row["status"]: int(row["count"]) for row in rows}
    return {
        "documents": sum(counts.values()),
        "by_status": counts,
        "indexed_bytes": int(total_size["total"] if total_size else 0),
        "fts5": _fts_available(),
        "jobs": [
            job_status(job_id) for job_id in list(_jobs)[-5:]
        ],
    }


def reset_jobs() -> None:
    """测试辅助：清空内存中的任务表与会话状态。"""
    with _jobs_lock:
        _jobs.clear()
