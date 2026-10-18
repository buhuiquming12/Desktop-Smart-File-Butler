"""轻量运行追踪：模型用量采集（供应商不提供时标记 unknown）。

这里只保存聚合数字，不记录提示词或文件内容，避免把敏感数据写进日志/状态。
"""
from __future__ import annotations

import threading
from typing import Any, Dict, Optional

_lock = threading.Lock()
_usage: Dict[str, Dict[str, Any]] = {}


def _extract(chunk: Any) -> tuple[Optional[int], Optional[int]]:
    """从流式 chunk 读取 token 用量；不同供应商字段不一致，取不到就返回 (None, None)。"""
    metadata = getattr(chunk, "usage_metadata", None)
    if isinstance(metadata, dict):
        input_tokens = metadata.get("input_tokens")
        output_tokens = metadata.get("output_tokens")
        if isinstance(input_tokens, int) or isinstance(output_tokens, int):
            return (
                input_tokens if isinstance(input_tokens, int) else None,
                output_tokens if isinstance(output_tokens, int) else None,
            )
    response_metadata = getattr(chunk, "response_metadata", None)
    if isinstance(response_metadata, dict):
        usage = response_metadata.get("token_usage") or response_metadata.get("usage")
        if isinstance(usage, dict):
            input_tokens = usage.get("prompt_tokens", usage.get("input_tokens"))
            output_tokens = usage.get("completion_tokens", usage.get("output_tokens"))
            if isinstance(input_tokens, int) or isinstance(output_tokens, int):
                return (
                    input_tokens if isinstance(input_tokens, int) else None,
                    output_tokens if isinstance(output_tokens, int) else None,
                )
    return None, None


def record(thread_id: str, chunk: Any) -> None:
    if not thread_id:
        return
    input_tokens, output_tokens = _extract(chunk)
    if input_tokens is None and output_tokens is None:
        return
    with _lock:
        current = _usage.setdefault(
            thread_id,
            {"input": 0, "output": 0, "calls": 0, "source": "provider"},
        )
        if input_tokens:
            current["input"] = int(current.get("input") or 0) + input_tokens
        if output_tokens:
            current["output"] = int(current.get("output") or 0) + output_tokens
        current["calls"] = int(current.get("calls") or 0) + 1


def usage_for(thread_id: str) -> Dict[str, Any]:
    with _lock:
        current = _usage.get(thread_id)
        if not current:
            return {"input": None, "output": None, "calls": 0, "source": "unknown"}
        return dict(current)


def clear(thread_id: str) -> None:
    with _lock:
        _usage.pop(thread_id, None)
