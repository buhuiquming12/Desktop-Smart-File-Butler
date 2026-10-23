"""测试用授权范围构造器。

执行器对**变更类工具**强制要求有效 ``authorization_scope``（fail closed），因此
直接调用工具的单测必须显式构造一份范围，而不是靠“没有 scope 就默认放行”。
这里只服务于测试，生产代码的授权范围始终由原始用户意图生成（``build_scope``）。
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Iterable, Optional

from app.policy.authorization import AuthorizationScope


def scope_for(
    roots: str | Path | Iterable[str | Path],
    *,
    effects: Iterable[str] = ("read", "create", "write", "move", "rename", "delete"),
    target_roots: Optional[Iterable[str | Path]] = None,
    filter_value: Optional[dict] = None,
    max_items: int = 500,
    ttl_seconds: int = 3600,
) -> AuthorizationScope:
    """构造一份“够用”的授权范围，供工具级单测使用。"""
    if isinstance(roots, (str, Path)):
        sources = [str(roots)]
    else:
        sources = [str(item) for item in roots]
    targets = (
        [str(item) for item in target_roots]
        if target_roots is not None
        else list(sources)
    )
    return AuthorizationScope(
        request_id="test-scope",
        thread_id="test-scope",
        allowed_effects=frozenset(str(item) for item in effects),
        source_roots=tuple(sources),
        target_roots=tuple(targets),
        filter=dict(filter_value or {}),
        max_items=int(max_items),
        recursive=True,
        destructive=("delete" in set(effects)),
        expires_at=time.time() + int(ttl_seconds),
        request_digest="test-scope-digest",
    )
