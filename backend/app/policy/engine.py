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


_NEGATION_MARKERS = (
    "不要", "不用", "不需要", "不准", "不允许", "禁止", "无需", "先不", "别", "勿",
    "don't", "do not", "never",
)


def _is_negated(text: str, index: int) -> bool:
    """判断某个意图词是否被否定（“不要移动”“别整理”）。

    只看词前 3 个字符：足够覆盖中文否定词，又不会把远处的“别”算进来。
    """
    window = text[max(0, index - 3):index]
    return any(marker in window for marker in _NEGATION_MARKERS)


def intent_effects(intent: str) -> set[str]:
    """从用户原话推导“本次请求要求产生的副作用”。

    这是保守的词法判据，供两处共用：
    - PolicyEngine：某个副作用是否被本次请求授权；
    - 完成判定：用户要求的效果是否真的发生过（扫描不算完成“移动”）。
    """
    text = (intent or "").casefold()
    effects: set[str] = set()
    for kind, terms in _INTENT_TERMS.items():
        for term in terms:
            start = text.find(term)
            while start != -1:
                if not _is_negated(text, start):
                    effects.add(kind)
                    break
                start = text.find(term, start + 1)
            if kind in effects:
                break
    return effects


def is_task_switch(message: str) -> bool:
    """用户是否在澄清之后改口/换任务（而不是补充信息）。"""
    text = (message or "").casefold()
    markers = (
        "算了", "不用了", "不弄了", "取消", "重新开始", "换个", "换一个",
        "改主意", "先不", "别整理", "新任务", "never mind", "forget it",
    )
    return any(marker in text for marker in markers)


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
        )
        # 否定句不算授权：“不要移动”不能成为移动的依据。
        if kind not in intent_effects(intent):
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
