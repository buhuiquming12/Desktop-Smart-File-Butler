"""P6：回收站读取与恢复。

数据来源是 ``operation_log`` 里成功的 ``delete`` 记录（软删除已把文件移入
``.butler-trash/``）。是否“仍在回收站”由回收站文件是否仍存在决定，因此撤销、
手工删除回收站内容后列表会自动收敛，不需要额外状态字段。

恢复复用 ``filesystem.restore_operation``：原位置被占用时会自动改名，**绝不覆盖**，
并逐项返回结果（成功/跳过/失败 + 实际还原路径）。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from .. import db
from ..logging_conf import get_logger
from ..security import resolve_in_sandbox
from . import filesystem

logger = get_logger(__name__)


def task_label(thread_id: Optional[str]) -> str:
    """把内部 thread_id 翻译成用户可理解的“所属任务”。"""
    value = thread_id or ""
    if not value:
        return "未知来源"
    if value.startswith("preview-"):
        return "操作预览"
    if value.startswith("dedupe-"):
        return "重复文件清理"
    if value.startswith("rule-"):
        return "自动整理规则"
    if value.startswith("scheduled-"):
        return "定时任务"
    return "对话会话"


def _size_of(path: Path) -> int:
    """文件取 size；目录做有界递归求和（避免超大目录拖慢列表）。"""
    try:
        if path.is_file():
            return int(path.stat().st_size)
        if not path.is_dir():
            return 0
    except OSError:
        return 0
    total = 0
    count = 0
    try:
        for child in path.rglob("*"):
            count += 1
            if count > 5000:
                break
            try:
                if child.is_file():
                    total += int(child.stat().st_size)
            except OSError:
                continue
    except OSError:
        return total
    return total


def list_entries(*, query: str = "", limit: int = 200, offset: int = 0) -> dict:
    entries: list[dict] = []
    needle = (query or "").strip().casefold()
    for op in db.trashed_operations(limit=1000):
        if not op.dest:
            continue
        trash_path = Path(op.dest)
        # 已被恢复（撤销 / 手工清空）的记录不再展示。
        if not trash_path.exists():
            continue
        original = Path(op.target)
        if needle and needle not in str(op.target).casefold() and needle not in original.name.casefold():
            continue
        conflict = original.exists()
        entries.append({
            "op_id": int(op.id or 0),
            "original_path": str(op.target),
            "name": original.name,
            "trash_path": str(trash_path),
            "deleted_at": op.ts.isoformat(),
            "size": _size_of(trash_path),
            "thread_id": op.thread_id or "",
            "task": task_label(op.thread_id),
            "detail": op.detail or "",
            "conflict": conflict,
        })
    total = len(entries)
    offset = max(0, int(offset))
    limit = max(1, min(500, int(limit)))
    return {
        "total": total,
        "offset": offset,
        "limit": limit,
        "entries": entries[offset:offset + limit],
    }


def restore(op_ids: list[int]) -> dict:
    """逐项恢复；单项失败不影响其它项，冲突时不覆盖。"""
    results: list[dict] = []
    for op_id in op_ids:
        op = db.get_operation(int(op_id))
        if op is None:
            results.append({"op_id": op_id, "status": "failed", "detail": "操作记录不存在"})
            continue
        if op.action != "delete" or op.status != "ok" or not op.dest:
            results.append({"op_id": op_id, "status": "skipped", "detail": "该记录不是可恢复的软删除"})
            continue
        try:
            # 只读校验：不把回收站之外（例如已被清空或越界）的路径当可恢复项。
            resolve_in_sandbox(op.dest)
            resolve_in_sandbox(op.target)
        except Exception as exc:  # noqa: BLE001
            results.append({"op_id": op_id, "status": "failed", "detail": str(exc)})
            continue
        if not Path(op.dest).exists():
            results.append({"op_id": op_id, "status": "skipped", "detail": "回收站中的文件已不存在"})
            continue
        try:
            outcome = filesystem.restore_operation(op)
        except Exception as exc:  # noqa: BLE001 - 单项异常收敛成结果
            logger.exception("恢复回收站文件失败 op=%s", op_id)
            results.append({"op_id": op_id, "status": "failed", "detail": str(exc)})
            continue
        results.append({
            "op_id": op_id,
            "original_path": op.target,
            "status": outcome.get("status", "failed"),
            "detail": outcome.get("detail", ""),
            "restored_to": outcome.get("restored_to", ""),
            "conflict": outcome.get("restored_to", "") not in {"", op.target},
        })
    ok = sum(1 for item in results if item["status"] == "ok")
    skipped = sum(1 for item in results if item["status"] == "skipped")
    failed = sum(1 for item in results if item["status"] == "failed")
    return {
        "ok": ok,
        "skipped": skipped,
        "failed": failed,
        "results": results,
    }


def stats() -> dict[str, Any]:
    entries = list_entries(limit=500)["entries"]
    return {
        "count": len(entries),
        "total_bytes": sum(entry["size"] for entry in entries),
    }
