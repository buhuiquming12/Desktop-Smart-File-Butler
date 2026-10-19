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
# 反过来，剥路径也不能把真正的指令一起吃掉：``整理/归档`` 是并列动词而不是路径，
# ``downloads/report 整理一下`` 里的“整理”必须留下。因此分三层处理：
# 1. 整条消息就是一个路径（可带空格、可带引号）→ 整段剥掉；
# 2. 绝对路径（盘符 / UNC / POSIX）→ 剥掉；跨空格延续时遇到“像指令”的片段就停；
# 3. 相对路径（``downloads\scan``）→ 只剥不像动作枚举的片段。
# 所有替换都用等长空格，避免改变后续否定词窗口的位置。
_PATH_SEPARATORS = "/\\"

# 路径片段在此结束：空白、引号、句读与右括号（``(x86)`` 之类的目录名仍算路径）。
_SEGMENT_STOP = frozenset(" \t\r\n\"'“”‘’，。；;、）]}<>|")

_QUOTE_CLOSERS = {'"': '"', "'": "'", "“": "”", "‘": "’", "`": "`"}

# 绝对路径只能从这些位置开始，避免把自然语言里的斜杠当成路径。
_ABS_PATH_START = re.compile(
    r"[A-Za-z]:[\\/]"                 # D:\ 或 C:/
    r"|\\\\[^\s\\/]+[\\/][^\s\\/]+"   # \\server\share
    r"|(?<![\w.~-])/[\w.~]"           # /home/me
    r"|(?<![\w.~-])~[\\/]"            # ~/Downloads
)

# 相对路径：至少一个分隔符，两侧是路径字符（不含空格，否则会吞掉后面的动词）。
_RELATIVE_PATH = re.compile(r"(?<![\w.~-])[\w.~-]+(?:[\\/][\w.~-]+)+")

_BARE_PATH_PREFIX = re.compile(r"(?:[A-Za-z]:[\\/]|\\\\|/|~[\\/]|\.{1,2}[\\/])")
_BARE_PATH_CHARS = re.compile(r"[\w.~@#$%&+=!^()\[\]{}\-\\/: ]+")

# “整条消息是路径”这一判断的排除词：出现这些词说明消息其实是一条指令。
_PATH_ONLY_COMMAND_WORDS = (
    "然后", "接着", "并且", "之后", "帮我", "请", "把", "将", "再", "先",
)

# 路径后面的中文片段只有形如“删除”“整理一下”才算指令；“扫描件”“整理箱”这类
# 目录名里虽然含操作词，但后面跟着名词，仍属于路径。
_COMMAND_TAILS = (
    "", "了", "吧", "掉", "一下", "一遍", "这些", "那些", "全部", "所有",
    # 趋向补语：“移动到 out”“删除到回收站”里的“移 动”后面跟的是“到”。
    "到", "进", "入", "至", "成", "起来", "出去", "回来", "回去", "完", "走", "光",
)

# 紧贴路径的中文动作（``D:\scan删除``）：中文没有词边界，只能靠动词判断。
# 分隔符之后的最后一段不参与（``D:\资料\扫描`` 仍是目录名）。
_CJK_START = 0x2E80
_CJK_CONNECTIVES = ("的", "里", "中", "下", "上", "内", "外", "后", "前", "时", "处", "边")
_CJK_STRONG_COMMANDS = (
    "删除", "删掉", "清理", "移动", "移到", "挪到", "转移",
    "重命名", "改名", "整理", "归档", "分类", "新建", "创建",
)

# 路径之后单独出现的英文动作词是命令；scan/list 之类只读词与目录名无法区分
# （``D:\My scan``、``D:\scan list`` 都是合法目录名），因此仍按路径处理。
# 只读新任务通常把动词放在最前（``list D:\scan``），那条路径不受影响。
_ASCII_COMMAND_KINDS = ("move", "rename", "delete")


def _blank_spans(text: str, spans: list[tuple[int, int]]) -> str:
    """把给定区间替换成等长空格。"""
    if not spans:
        return text
    chars = list(text)
    for start, end in spans:
        chars[start:end] = " " * (end - start)
    return "".join(chars)


def _is_intent_word(segment: str) -> bool:
    word = segment.strip().casefold()
    return any(word == term for terms in _INTENT_TERMS.values() for term in terms)


def _only_intent_segments(text: str) -> bool:
    """``整理/归档``、``move/organize`` 是动作枚举，不是路径。"""
    segments = [part.strip() for part in re.split(r"[\\/]", text)]
    segments = [part for part in segments if part]
    return len(segments) >= 2 and all(_is_intent_word(part) for part in segments)


def _looks_like_command_chunk(chunk: str) -> bool:
    """中文片段是否是一个动作（“删除”“整理一下”），而不是路径名（“扫描件”）。"""
    lowered = chunk.strip().strip("，。；;、！!？?.,").casefold()
    if not lowered:
        return False
    for terms in _INTENT_TERMS.values():
        for term in terms:
            if lowered.startswith(term) and lowered[len(term):] in _COMMAND_TAILS:
                return True
    return False


def _contains_command_phrase(text: str) -> bool:
    """片段中间是否也藏着动作（“里的文件整理一下”）。

    只看 ``_COMMAND_TAILS`` 允许的尾巴，所以“整理箱”“扫描件”仍算目录名。
    """
    lowered = text.casefold()
    for terms in _INTENT_TERMS.values():
        for term in terms:
            if term.isascii():
                continue
            start = lowered.find(term)
            while start != -1:
                if lowered[start + len(term):] in _COMMAND_TAILS:
                    return True
                start = lowered.find(term, start + 1)
    return False


def _is_command_chunk(chunk: str) -> bool:
    """片段是否是一条动作指令：中文“删除/整理一下”，或英文 move/rename/delete。

    英文只读词（scan/list/view）与目录名无法区分（``D:\\My scan``），仍按路径处理。
    """
    text = chunk.strip().strip("，。；;、！!？?.,")
    if not text:
        return False
    lowered = text.casefold()
    if lowered.isascii():
        if " " in lowered or any(sep in lowered for sep in _PATH_SEPARATORS):
            return False
        return any(
            lowered == term
            for kind in _ASCII_COMMAND_KINDS
            for term in _INTENT_TERMS[kind]
        )
    return _looks_like_command_chunk(lowered) or _contains_command_phrase(lowered)


def _looks_like_path(text: str) -> bool:
    value = text.strip()
    if not value:
        return False
    if _BARE_PATH_PREFIX.match(value):
        return True
    if not any(separator in value for separator in _PATH_SEPARATORS):
        return False
    return not _only_intent_segments(value)


def _strip_wrapping_quotes(value: str) -> str:
    if len(value) >= 2 and _QUOTE_CLOSERS.get(value[0]) == value[-1]:
        return value[1:-1].strip()
    return value


def _strong_command(run: str) -> bool:
    """中文片段是否以动作开头（“删除旧文件”也算，“扫描件”“整理箱”不算）。"""
    if _looks_like_command_chunk(run) or _contains_command_phrase(run):
        return True
    return run.startswith(_CJK_STRONG_COMMANDS)


def _cjk_starts_command(text: str, index: int) -> bool:
    """从 ``index`` 开始的中文片段是否已经离开路径（``D:\\scan删除``）。

    中文没有词边界，目录名同样可能含“扫描”“整理”，因此只在有明确信号时断开：
    - 片段后面紧跟拉丁字母/数字（``里的PDF…``、``删除2024``）；
    - 片段本身是动作短语，且不是分隔符之后的最后一段（``D:\\资料\\扫描`` 是目录名）。
    """
    end = index
    while end < len(text) and ord(text[end]) > _CJK_START:
        end += 1
    run = text[index:end]
    if not run:
        return False
    following = text[end:end + 1]
    previous = text[index - 1:index]
    # 紧贴路径（前面不是分隔符）的中文片段更像动词；分隔符之后的一段更像目录名。
    glued = bool(previous) and previous not in _PATH_SEPARATORS
    if following and following in " \t\u3000":
        # 空格之后是参数（``D:\scan移到 out``）；纯路径名（``D:\我的 文档``）不切断。
        return glued and _strong_command(run)
    if following and following in _PATH_SEPARATORS:
        return False
    if not following:
        if not glued:
            # 分隔符之后的最后一段仍按目录名处理（``D:\资料\扫描``）。
            return False
        return _strong_command(run)
    # 紧贴拉丁字母/数字：动作词，或“的/里/中…”这类依附前面的连接词，才说明离开路径。
    return _strong_command(run) or run.startswith(_CJK_CONNECTIVES)


def _is_bare_path(text: str) -> bool:
    """整条消息是否只是一个路径（例如用户只回答 ``D:\\My Documents\\scan``）。

    带空格的路径无法逐段判断“这个空格属于路径还是句子”，因此先看整体：
    消息以路径开头、只由路径字符组成、且没有指令信号时，整段都算参数。
    """
    value = _strip_wrapping_quotes(text.strip())
    value = value.rstrip(" .,;:!?，。；：！？、")
    if len(value) < 3:
        return False
    if any(char in value for char in "\"'“”‘’`"):
        return False
    if _BARE_PATH_CHARS.fullmatch(value) is None:
        return False
    if _BARE_PATH_PREFIX.match(value) is None:
        # 相对路径：第一个单词必须本身带分隔符，且不能是动作枚举。
        first = value.split(" ", 1)[0]
        if _RELATIVE_PATH.fullmatch(first) is None or _only_intent_segments(first):
            return False
    if any(word in value for word in _PATH_ONLY_COMMAND_WORDS):
        return False
    # 紧贴路径的中文动作（``D:\scan删除``）说明消息其实是一条指令。
    if any(
        ord(char) > _CJK_START and _cjk_starts_command(value, index)
        for index, char in enumerate(value)
    ):
        return False
    # 空格之后的片段是动作（``D:\scan 删除``、``D:\scan delete``）时同理。
    return not any(
        _is_command_chunk(match.group(0))
        for match in list(re.finditer(r"\S+", value))[1:]
    )


def _mask_quoted_paths(text: str) -> str:
    spans: list[tuple[int, int]] = []
    index = 0
    while index < len(text):
        closer = _QUOTE_CLOSERS.get(text[index])
        if closer is None:
            index += 1
            continue
        end = text.find(closer, index + 1)
        if end == -1:
            index += 1
            continue
        if _looks_like_path(text[index + 1:end]):
            spans.append((index, end + 1))
        index = end + 1
    return _blank_spans(text, spans)


def _continues_path(chunk: str) -> bool:
    """空格之后的片段是否仍属于路径。

    含分隔符的片段（``Documents\\scan``）明显还是路径；动作片段（``整理一下``、
    ``delete``）则是指令，必须留在自然语言里；``整理箱`` 这类带名词的目录名保留。
    """
    if any(separator in chunk for separator in _PATH_SEPARATORS):
        return not _only_intent_segments(chunk)
    return not _is_command_chunk(chunk)


def _consume_path(text: str, start: int) -> int:
    length = len(text)
    index = start
    while index < length:
        char = text[index]
        if char in _PATH_SEPARATORS or char not in _SEGMENT_STOP:
            # 紧贴路径的中文动作（``D:\scan删除``）是句子的开始，不是路径的一部分。
            if ord(char) > _CJK_START and _cjk_starts_command(text, index):
                break
            index += 1
            continue
        if char not in " \t":
            break
        cursor = index
        while cursor < length and text[cursor] in " \t":
            cursor += 1
        end = cursor
        while end < length and text[end] not in _SEGMENT_STOP:
            end += 1
        chunk = text[cursor:end]
        if not chunk or not _continues_path(chunk):
            break
        index = end
    return index


def _mask_absolute_paths(text: str) -> str:
    spans: list[tuple[int, int]] = []
    position = 0
    while position < len(text):
        match = _ABS_PATH_START.search(text, position)
        if match is None:
            break
        end = _consume_path(text, match.start())
        if end > match.start():
            spans.append((match.start(), end))
        position = max(end, match.end())
    return _blank_spans(text, spans)


def _mask_relative_paths(text: str) -> str:
    def _replace(match: re.Match[str]) -> str:
        value = match.group(0)
        if _only_intent_segments(value):
            return value
        return " " * len(value)

    return _RELATIVE_PATH.sub(_replace, text)


def strip_paths(text: str) -> str:
    """把路径片段替换成空格，只留下自然语言部分。

    典型场景：用户只回答 ``D:\\scan``，其中 ``scan`` 命中只读词表，会被误判成
    “这轮自带新操作”，从而清空上一轮授权。路径也可能是 ``D:\\My Documents\\scan``
    这种带空格的，或者被引号包住；反过来，``整理/归档`` 这类并列动词、路径后面的
    “整理一下”都不能被当成路径剥掉。
    """
    if not text:
        return text
    if _is_bare_path(text):
        return " " * len(text)
    masked = _mask_quoted_paths(text)
    masked = _mask_absolute_paths(masked)
    return _mask_relative_paths(masked)


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


def explicit_intent_effects(intent: str) -> set[str]:
    """只从“显式动词”推导的副作用，用于完成判定区分目标与从属授权。

    ``整理``/``移动`` 会派生 ``create``（移动前可能需要建目标目录），但只有
    明确说“新建目录/文件夹”才把建目录本身当作目标：说了就必须真的建成，
    否则不能判定成功。“创建摘要文件”这类写入请求不算建目录目标。
    """
    text = strip_paths((intent or "").casefold())
    effects: set[str] = set()
    for kind, patterns in _EXPLICIT_PATTERNS.items():
        for pattern in patterns:
            if any(
                not _is_negated(text, match.start())
                for match in re.finditer(pattern, text)
            ):
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
    # 英文只读动词要覆盖日常说法（show/check/find/search…），否则这些新任务
    # 会被当成“没有动词”的补充信息，沿用上一轮的移动授权。
    "read": (
        "扫描", "扫一下", "扫一遍", "查看", "看一眼", "看一下", "看看", "看下",
        "浏览", "列出", "列一下", "列个", "列出来", "清单", "统计一下", "统计",
        "数一下", "清点", "有什么", "都有什么", "找一下",
        "scan", "list", "browse", "view", "look at", "look", "read", "show",
        "display", "check", "find", "search", "inspect", "peek", "count",
        "print", "enumerate", "what's in", "what is in", "tell me",
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

# 显式目标：只有“新建目录/文件夹”才是目标本身；“创建摘要文件”属于 write，不算建目录。
# ``(?!\s*的)`` 排除定语用法（“移动到新建的 out 文件夹”），那里建目录只是从属动作。
_EXPLICIT_PATTERNS: dict[str, tuple[str, ...]] = {
    "create": (
        r"(?:新建|创建|建个|建立一个?)(?!\s*的)[^，。；;\n]{0,12}?(?:目录|文件夹|folder|directory)",
        r"(?:目录|文件夹)[^，。；;\n]{0,6}?(?:新建|创建)(?!\s*的)",
        r"\bmkdir\b",
        r"\b(?:create|make)\s+(?:a\s+)?(?:new\s+)?(?:folder|directory)\b",
    ),
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
