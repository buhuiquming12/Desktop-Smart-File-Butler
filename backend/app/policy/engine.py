"""Backend-enforced authorization and cumulative mutation policy.

安全边界只由后端事实决定：
- 工具副作用来自 ``tools.contracts`` 的集中登记，而不是模型自报。
- 未知工具、缺少契约、参数无效一律拒绝，绝不默认放行。
- 移动/重命名（含 batch_classify）按同一用户请求累计，重规划不重置。
- 删除始终需要审批。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from ..tools.contracts import (
    CUMULATIVE_SIDE_EFFECTS,
    MUTATION_KIND,
    TOOL_CONTRACTS,
    validate_tool_args,
)


_INTENT_TERMS: dict[str, tuple[str, ...]] = {
    "move": (
        "移动", "移到", "挪到", "转移", "整理", "归档", "分类",
        "放到", "放进", "放入",
        "move", "organize", "archive", "classify",
    ),
    "rename": ("重命名", "改名", "前缀", "后缀", "rename"),
    "delete": ("删除", "删掉", "清理", "delete", "remove"),
    # 新建目录通常是移动/整理的从属动作，因此接受整理类意图。
    "create": (
        "新建", "创建", "建个", "mkdir", "create", "整理", "归档",
        "分类", "移动", "move", "organize",
    ),
    "write": ("摘要", "总结", "概括", "生成", "写入", "summarize", "summary", "write"),
    "preference": (
        "记住", "偏好", "以后", "下次", "默认", "保存", "设置",
        "忘记", "删除偏好", "清除偏好",
        "remember", "prefer", "default", "forget",
    ),
    "schedule": ("定时", "定期", "每天", "每周", "每月", "cron", "schedule"),
}


@dataclass(frozen=True)
class PolicyDecision:
    # 默认拒绝：任何漏填分支都必须显式放行，避免“忘记登记 = 获得权限”。
    allowed: bool = False
    requires_approval: bool = False
    reason: str = ""
    mutation_kind: Optional[str] = None
    proposed_count: int = 0
    side_effect: Optional[str] = None


class PolicyEngine:
    """Policy decisions never depend on model-provided risk labels."""

    def __init__(self, batch_threshold: int = 20) -> None:
        self.batch_threshold = max(1, batch_threshold)

    @staticmethod
    def _cumulative_mutations(state: dict[str, Any]) -> int:
        mutations = state.get("mutations") or {}
        return sum(int(mutations.get(name, 0)) for name in ("move", "rename"))

    def evaluate(
        self,
        tool: str,
        *,
        state: dict[str, Any],
        proposed_count: int = 1,
        args: Optional[dict[str, Any]] = None,
    ) -> PolicyDecision:
        kind = MUTATION_KIND.get(tool)
        contract = TOOL_CONTRACTS.get(tool)
        if contract is None:
            return PolicyDecision(False, False, f"未知工具或缺少工具契约: {tool}")
        if kind is None:
            kind = contract.side_effect

        # 参数校验发生在任何文件系统操作之前；无效参数不获得执行权限。
        if args is not None:
            _, error = validate_tool_args(tool, args)
            if error:
                return PolicyDecision(
                    False, False, error, kind, max(0, int(proposed_count)), kind
                )

        if kind == "read":
            return PolicyDecision(True, False, "", kind, max(0, int(proposed_count)), kind)

        count = max(0, int(proposed_count))
        intent = str(
            state.get("trusted_user_intent") or state.get("user_request") or ""
        ).casefold()
        terms = _INTENT_TERMS.get(kind, ())
        if not terms or not any(term in intent for term in terms):
            return PolicyDecision(
                False, False, f"原始用户请求未授权 {kind} 操作", kind, count, kind
            )

        if kind == "delete":
            return PolicyDecision(True, True, "删除操作始终需要审批", kind, count, kind)

        if kind not in CUMULATIVE_SIDE_EFFECTS:
            # 新建/写入/偏好/定时任务：有明确意图即可执行，不进入文件变更累计。
            return PolicyDecision(True, False, "", kind, count, kind)

        already = self._cumulative_mutations(state)
        approved_limit = int(state.get("approved_mutation_limit") or 0)
        total = already + count
        needs = total > self.batch_threshold and total > approved_limit
        return PolicyDecision(
            True,
            needs,
            f"本次请求累计文件变更将达到 {total} 项（阈值 {self.batch_threshold}）"
            if needs
            else "",
            kind,
            count,
            kind,
        )
