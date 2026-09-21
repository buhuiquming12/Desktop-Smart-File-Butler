"""纯 REST 业务处理函数（路由层可直接调用）。"""
from __future__ import annotations

from typing import Any, Callable, Iterable


def rollback_summary(thread_id: str, operations: Iterable[Any], preflight: Callable[[Any], dict], restore: Callable[[Any], dict]) -> dict:
    """按逆序逐项预检并回滚，返回前端可直接展示的分级结果。

    不能先预检整条链再执行：例如 ``A→B`` 后又 ``B→C``，回滚前 B 必然不存在；
    只有先完成 ``C→B``，前一条操作的回滚源才会出现。
    """
    reversible = [op for op in operations if op.action in ("move", "rename", "delete") and op.status == "ok" and op.dest]
    results = []
    for op in reversed(reversible):
        check = preflight(op)
        if check.get("status") != "ok":
            results.append({"op_id": op.id, "status": check.get("status", "failed"), "detail": check.get("detail", "预检失败")})
            continue
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
