"""纯 REST 业务处理函数（路由层可直接调用）。"""
from __future__ import annotations

from typing import Any, Callable, Iterable


def rollback_summary(thread_id: str, operations: Iterable[Any], preflight: Callable[[Any], dict], restore: Callable[[Any], dict]) -> dict:
    """对一组操作执行预检并按逆序回滚，返回前端可直接展示的分级结果。"""
    reversible = [op for op in operations if op.action in ("move", "rename", "delete") and op.status == "ok" and op.dest]
    results = []
    ready = []
    for op in reversed(reversible):
        check = preflight(op)
        if check.get("status") == "ok":
            ready.append(op)
        else:
            results.append({"op_id": op.id, "status": check.get("status", "failed"), "detail": check.get("detail", "预检失败")})
    for op in ready:
        try:
            results.append({"op_id": op.id, **restore(op)})
        except Exception as exc:  # noqa: BLE001
            results.append({"op_id": op.id, "status": "failed", "detail": str(exc)})
    return {
        "thread_id": thread_id,
        "total": len(results),
        "ok": sum(item["status"] == "ok" for item in results),
        "skipped": sum(item["status"] == "skipped" for item in results),
        "failed": sum(item["status"] == "failed" for item in results),
        "results": results,
    }
