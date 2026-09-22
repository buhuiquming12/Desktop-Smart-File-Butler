"""Backend-enforced authorization and cumulative mutation policy."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


MUTATION_KIND = {
    "move_file": "move",
    "batch_move": "move",
    "rename_file": "rename",
    "batch_rename": "rename",
    "delete_file": "delete",
}


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool = True
    requires_approval: bool = False
    reason: str = ""
    mutation_kind: str | None = None
    proposed_count: int = 0


class PolicyEngine:
    """Policy decisions never depend on model-provided risk labels."""

    def __init__(self, batch_threshold: int = 20) -> None:
        self.batch_threshold = max(1, batch_threshold)

    def evaluate(
        self,
        tool: str,
        *,
        state: dict[str, Any],
        proposed_count: int = 1,
    ) -> PolicyDecision:
        kind = MUTATION_KIND.get(tool)
        if kind is None:
            return PolicyDecision()
        count = max(0, int(proposed_count))
        intent = str(state.get("trusted_user_intent") or state.get("user_request") or "").casefold()
        terms = {
            "move": ("移动", "整理", "归档", "分类", "放到", "放入", "move", "organize", "archive", "classify"),
            "rename": ("重命名", "改名", "前缀", "后缀", "rename"),
            "delete": ("删除", "删掉", "清理", "delete", "remove"),
        }[kind]
        if not any(term in intent for term in terms):
            return PolicyDecision(False, False, f"原始用户请求未授权 {kind} 操作", kind, count)
        if kind == "delete":
            return PolicyDecision(True, True, "删除操作始终需要审批", kind, count)
        mutations = state.get("mutations") or {}
        already = sum(int(mutations.get(name, 0)) for name in ("move", "rename"))
        approved_limit = int(state.get("approved_mutation_limit") or 0)
        total = already + count
        needs = total > self.batch_threshold and total > approved_limit
        return PolicyDecision(
            True,
            needs,
            f"本次请求累计文件变更将达到 {total} 项（阈值 {self.batch_threshold}）",
            kind,
            count,
        )
