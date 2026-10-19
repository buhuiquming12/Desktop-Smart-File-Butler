"""Backend-enforced authorization and cumulative mutation policy.

安全边界只由后端事实决定：
- 工具副作用来自 ``tools.contracts`` 的集中登记，而不是模型自报。
- 未知工具、缺少契约、参数无效一律拒绝，绝不默认放行。
- 移动/重命名（含 batch_classify）按同一用户请求累计，重规划不重置。
- 删除始终需要审批。
"""
from __future__ import annotations

import re
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

# 路径是参数，不是指令：``D:\scan``、``/home/me/list`` 里的单词不能当成操作动词。
# 顺序有意义：先吃掉带盘符/UNC 的绝对路径，再处理 POSIX 绝对路径与相对路径，
# 否则相对路径规则会把绝对路径的尾巴再匹配一次（结果一样，但更难排查）。
_PATH_PATTERNS = (
    re.compile(r"[A-Za-z]:[\\/][^\s\"'“”‘’，。；;、）)]*"),
    re.compile(r"\\\\[^\s\"'“”‘’，。；;、）)]+"),
    re.compile(r"(?<![\w])/(?:[\w.~-]+/)*[\w.~-]*"),
    re.compile(r"[\w.~-]+(?:[\\/][\w.~ -]+)+"),
)


def strip_paths(text: str) -> str:
    """把路径片段替换成空格，只留下自然语言部分。

    典型场景：用户只回答 ``D:\\scan``，其中 ``scan`` 命中只读词表，会被误判成
    “这轮自带新操作”，从而清空上一轮授权。剥掉路径后该消息没有动词，就是纯补充。
    """
    masked = text
    for pattern in _PATH_PATTERNS:
        masked = pattern.sub(" ", masked)
    return masked


def _is_negated(text: str, index: int) -> bool:
    """判断某个意图词是否被否定（“不要移动”“别整理”）。

    只看词前 3 个字符：足够覆盖中文否定词，又不会把远处的“别”算进来。
    """
    window = text[max(0, index - 3):index]
    return any(marker in window for marker in _NEGATION_MARKERS)


def _term_positions(text: str, term: str) -> list[int]:
    """返回 term 在 text 中所有出现位置。

    ASCII 词要求词边界：否则 ``remove`` 里的 "move"、``already`` 里的 "read"
    会被当成操作意图——前者会给未授权的“移动”放行，后者会把纯补充信息误判成新任务。
    CJK 没有词边界，继续用子串匹配。
    """
    if term.isascii() and term.replace(" ", "").isalpha():
        pattern = rf"(?<![a-z]){re.escape(term)}(?![a-z])"
        return [match.start() for match in re.finditer(pattern, text)]
    positions: list[int] = []
    start = text.find(term)
    while start != -1:
        positions.append(start)
        start = text.find(term, start + 1)
    return positions


def intent_effects(intent: str) -> set[str]:
    """从用户原话推导“本次请求要求产生的副作用”。

    这是保守的词法判据，供三处共用：
    - PolicyEngine：某个副作用是否被本次请求授权；
    - 完成判定：用户要求的效果是否真的发生过（扫描不算完成“移动”）；
    - 澄清续问：这轮消息是否自带新操作（只读动作也算，见 _INTENT_TERMS["read"]）。

    匹配前先剥掉路径：路径是参数，不是指令。
    """
    text = strip_paths((intent or "").casefold())
    effects: set[str] = set()
    for kind, terms in _INTENT_TERMS.items():
        for term in terms:
            if any(not _is_negated(text, index) for index in _term_positions(text, term)):
                effects.add(kind)
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
    # 只读动作也必须登记：否则“只扫描一下”会被当成“没有任何操作”，
    # 在澄清续问里被误判为单纯补充信息，从而继承上一轮的移动授权。
    "read": (
        "扫描", "扫一下", "扫一遍", "查看", "看一眼", "看一下",
        "浏览", "列出", "列一下", "列个", "清单", "统计一下",
        "scan", "list", "browse", "view", "look at", "read",
    ),
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
