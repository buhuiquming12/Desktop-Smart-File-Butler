"""强类型授权范围（AuthorizationScope）。

设计原则
--------
- 授权范围**只由原始用户请求**（``trusted_user_intent``）在后端生成；文件名、
  OCR、正文、工具输出和模型生成内容都只是不可信数据，永远不能扩大范围。
- 澄清回答只能补齐同一任务缺失的信息：授权副作用集合取上一轮范围，绝不因补充
  一句话而扩张；换任务 / 只读请求 / 已完成请求都不会继承上一个任务的 mutation 授权。
- 工具执行前必须同时满足：工具已登记、参数合法、副作用在范围内、源/目标路径落在
  授权根目录内、过滤条件没有放宽、数量不超过 ``max_items``。
- 失败一律 fail closed：缺少范围、范围过期、路径越界、过滤扩大都直接拒绝。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from ..logging_conf import get_logger
from ..security import is_within_sandbox
from ..tools.contracts import TOOL_CONTRACTS, ToolContract

logger = get_logger(__name__)

# 与 tools.contracts 的 SideEffect 保持一致；read 永远允许（只读不改变磁盘状态）。
EFFECTS = frozenset(
    {"read", "create", "write", "move", "rename", "delete", "preference", "schedule"}
)
MUTATION_EFFECTS = frozenset({"create", "write", "move", "rename", "delete"})

# 授权范围有效期：默认 1 小时。等待审批的时间也计入，过期后必须重新发起请求。
DEFAULT_SCOPE_TTL_SECONDS = 3600
DEFAULT_MAX_ITEMS = 500


class ScopeError(Exception):
    """授权范围校验失败；``code`` 供 REST/事件层映射为稳定错误码。"""

    def __init__(self, message: str, *, code: str = "scope_denied") -> None:
        super().__init__(message)
        self.message = message
        self.code = code


@dataclass(frozen=True)
class ScopeDecision:
    allowed: bool
    reason: str = ""
    code: str = ""
    effect: str = ""


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def request_digest(thread_id: str, request_id: str, intent: str) -> str:
    """请求指纹：绑定 thread、request 与原始意图文本（不含任何模型输出）。"""
    return _sha256(
        {
            "thread_id": thread_id,
            "request_id": request_id,
            "intent": " ".join((intent or "").split()),
        }
    )


def _norm_path(value: str | Path) -> str:
    try:
        return os.path.normcase(os.path.abspath(str(value)))
    except (OSError, ValueError):
        return str(value)


def _within(path: str | Path, roots: Sequence[str]) -> bool:
    """路径是否位于任一授权根目录内（含根本身）。"""
    if not roots:
        return False
    target = _norm_path(path)
    for root in roots:
        base = _norm_path(root)
        if target == base or target.startswith(base + os.sep):
            return True
    return False


@dataclass(frozen=True)
class AuthorizationScope:
    """一次用户请求的授权契约快照（JSON 可序列化，不含密钥）。"""

    request_id: str
    thread_id: str
    allowed_effects: frozenset[str] = field(default_factory=frozenset)
    source_roots: Tuple[str, ...] = ()
    target_roots: Tuple[str, ...] = ()
    filter: Mapping[str, Any] = field(default_factory=dict)
    # 用户点名的具体文件：非空时，变更类工具（移动/重命名/删除）只能作用于这些文件。
    # 父目录仍留在 source_roots 里供扫描/摘要等只读与派生操作用，但**不能**据此扩大
    # 到同目录的其它文件。
    file_whitelist: Tuple[str, ...] = ()
    max_items: int = DEFAULT_MAX_ITEMS
    recursive: bool = True
    destructive: bool = False
    expires_at: float = 0.0
    request_digest: str = ""
    origin: str = "user_intent"

    # ---------- 序列化 ----------

    def to_dict(self) -> Dict[str, Any]:
        return {
            "request_id": self.request_id,
            "thread_id": self.thread_id,
            "allowed_effects": sorted(self.allowed_effects),
            "source_roots": list(self.source_roots),
            "target_roots": list(self.target_roots),
            "filter": dict(self.filter),
            "file_whitelist": list(self.file_whitelist),
            "max_items": int(self.max_items),
            "recursive": bool(self.recursive),
            "destructive": bool(self.destructive),
            "expires_at": float(self.expires_at),
            "request_digest": self.request_digest,
            "origin": self.origin,
        }

    @classmethod
    def from_dict(cls, value: Optional[Mapping[str, Any]]) -> Optional["AuthorizationScope"]:
        if not value:
            return None
        try:
            effects = frozenset(str(item) for item in value.get("allowed_effects") or [])
            return cls(
                request_id=str(value.get("request_id") or ""),
                thread_id=str(value.get("thread_id") or ""),
                allowed_effects=effects,
                source_roots=tuple(str(item) for item in value.get("source_roots") or []),
                target_roots=tuple(str(item) for item in value.get("target_roots") or []),
                filter=dict(value.get("filter") or {}),
                file_whitelist=tuple(
                    str(item) for item in value.get("file_whitelist") or ()
                ),
                max_items=int(value.get("max_items") or DEFAULT_MAX_ITEMS),
                recursive=bool(value.get("recursive", True)),
                destructive=bool(value.get("destructive", False)),
                expires_at=float(value.get("expires_at") or 0.0),
                request_digest=str(value.get("request_digest") or ""),
                origin=str(value.get("origin") or "user_intent"),
            )
        except (TypeError, ValueError):
            return None

    # ---------- 判定 ----------

    def digest(self) -> str:
        return _sha256(self.to_dict())

    def expired(self, *, now: Optional[float] = None) -> bool:
        moment = time.time() if now is None else now
        return bool(self.expires_at) and moment > float(self.expires_at)

    def allows_effect(self, effect: str) -> bool:
        return effect == "read" or effect in self.allowed_effects


# ---------------- 路径与过滤条件提取 ----------------

_ABS_START = re.compile(
    r"[A-Za-z]:[\\/]"                 # D:\ 或 C:/
    r"|\\\\[^\s\\/]+[\\/][^\s\\/]+"   # \\server\share
    r"|(?<![\w.~-])/[\w.~]"           # /home/me
    r"|(?<![\w.~-])~[\\/]"            # ~/Downloads
)

_QUOTED_PATH = re.compile(r"[\"“”'`]([^\"“”'`\n]{2,})[\"“”'`]")

_STOP_CHARS = frozenset("\"'“”‘’，。；;、）]}>|\n\r\t")

# 空格之后的这些词说明句子已经离开路径，进入指令。
_CJK_STOP_PREFIXES = (
    "然后", "接着", "并且", "再把", "再", "并", "把", "将", "整理", "归档", "分类",
    "移动", "移到", "挪到", "转移", "删除", "删掉", "清理", "重命名", "改名",
    "复制", "放到", "放进", "还有", "只", "仅", "不要", "别",
)

# 英文动作/连接词按整词匹配（避免 “torrent” 被当成 “to”）。
_ASCII_STOP_WORDS = frozenset(
    {
        "then", "and", "to", "into", "onto", "from", "in", "at",
        "move", "delete", "rename", "organize", "archive", "classify",
        "only", "please", "just", "all", "these", "those", "them",
    }
)

# 紧贴路径的中文连接词（“X 里的 PDF”）：说明已经离开路径。
_CJK_CONNECTIVES = frozenset("的里中下内外上后前边之")

# 目标侧标记：出现这些词说明后面的路径是“移动到哪里”。
_TARGET_MARKERS = ("到", "至", "进", "入", "放到", "移入", "放入", "into ", "to ", "target ")

# 显式收窄标记：只有用户明确说“只/仅”时才据此限制扩展名，避免把
# “先移动 txt、其余分类”这类多子任务误读成单一扩展名授权。
_NARROW_MARKERS = ("只", "仅", "only", "只处理", "只看", "只移动", "只整理")

_EXTENSION = re.compile(r"(?<![\w.])(?:\.)?([A-Za-z0-9]{1,8})(?:\s*文件|\s*files?)?", re.IGNORECASE)
_KNOWN_EXTENSIONS = {
    "pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "txt", "md", "csv", "log",
    "json", "png", "jpg", "jpeg", "gif", "bmp", "tiff", "webp", "mp3", "mp4", "zip",
    "rar", "7z", "html", "xml", "yaml", "yml", "py", "js", "ts", "exe",
}

_TARGET_RESTRICT_MARKERS = (
    "不递归", "不要递归", "当前目录", "只看这一层", "仅当前", "non-recursive", "not recursive",
)


# 路径之后的这些名词说明路径已经结束（“新建 D:\backup 文件夹”“move to x folder”）。
_CJK_TAIL_NOUNS = ("文件夹", "目录", "文件", "位置")
_ASCII_TAIL_NOUNS = ("folder", "directory", "file", "files", "dir")


def _next_chunk(text: str, start: int) -> str:
    end = start
    while end < len(text) and text[end] not in _STOP_CHARS and text[end] not in " \t":
        end += 1
    return text[start:end]


def _continues_path(chunk: str, next_chunk: str) -> bool:
    """空格之后的片段是否仍属于路径。

    带空格的路径（``D:\\My Documents\\backup``、``D:\\Desktop Smart File Butler\\x``）
    要继续；动作词、连接词、CJK 名词（“文件夹/目录”）与“后面不再有路径片段”的
    英文名词则说明路径已经结束。
    """
    lowered = (chunk or "").casefold()
    if not lowered:
        return False
    if lowered in _ASCII_STOP_WORDS:
        return False
    if any(lowered.startswith(word) for word in _CJK_STOP_PREFIXES):
        return False
    if all(char in _CJK_CONNECTIVES for char in lowered):
        return False
    # 紧贴路径开口的连接词（“sub 里的文件”“root 中的 pdf”）说明路径已结束。
    if lowered[0] in _CJK_CONNECTIVES:
        return False
    if any(lowered.startswith(word) for word in _CJK_TAIL_NOUNS):
        return False
    if any(lowered.startswith(word) for word in _ASCII_TAIL_NOUNS):
        # “File Butler\...” 里的 File 仍是路径片段：下一个片段含分隔符才继续。
        return any(sep in (next_chunk or "") for sep in ("/", "\\"))
    return True


def _consume_path(text: str, start: int) -> int:
    """从 start 开始吃掉一个路径；空格后若出现指令词/名词则停止。"""
    length = len(text)
    index = start
    while index < length:
        char = text[index]
        if char in _STOP_CHARS:
            break
        if char in " \t":
            cursor = index
            while cursor < length and text[cursor] in " \t":
                cursor += 1
            end = cursor
            while end < length and text[end] not in _STOP_CHARS and text[end] not in " \t":
                end += 1
            chunk = text[cursor:end]
            if not chunk or not _continues_path(chunk, _next_chunk(text, end + 1)):
                break
            index = end
            continue
        index += 1
    return index


def _trim(raw: str) -> str:
    value = raw.strip().strip("\"'“”‘’`")
    value = value.rstrip(" .,;:!?，。；：！？、")
    return value


def extract_paths(text: str) -> list[tuple[str, int, int]]:
    """提取文本中形如路径的片段，返回 ``(raw, start, end)``。

    只用于**可信用户意图**的授权范围推导；文件正文/工具输出绝不能进入这里。
    """
    value = text or ""
    spans: list[tuple[str, int, int]] = []
    seen: set[tuple[int, int]] = set()

    for match in _QUOTED_PATH.finditer(value):
        candidate = _trim(match.group(1))
        if any(sep in candidate for sep in ("/", "\\")):
            spans.append((candidate, match.start(1), match.end(1)))
            seen.add((match.start(1), match.end(1)))

    position = 0
    while position < len(value):
        match = _ABS_START.search(value, position)
        if match is None:
            break
        end = _consume_path(value, match.start())
        if end <= match.start():
            position = match.end()
            continue
        raw = _trim(value[match.start():end])
        # 引号路径已经在上面的 _QUOTED_PATH 分支登记过；这里按**区间**跳过，
        # 不能对字符串调用 start()/end()（原本会导致 organize "C:\目录" 直接 AttributeError）。
        span = (match.start(), end)
        if raw and not any(
            span[0] >= start and span[1] <= stop for start, stop in seen
        ):
            spans.append((raw, *span))
        position = max(end, match.end())
    return spans


def _target_like(text: str, start: int) -> bool:
    window = (text[max(0, start - 16):start] or "").casefold()
    return any(marker in window for marker in _TARGET_MARKERS)


def _resolve_intent_path(
    raw: str, *, sandbox_roots: Sequence[Path]
) -> tuple[Optional[Path], str]:
    """把用户点名的路径解析成 ``(授权根目录, 精确文件)``。

    用户指定的是一个**存在的文件**时，授权根仍是它的父目录（扫描、提取、输出摘要
    等只读/派生操作还需要目录上下文），但该文件同时记入精确白名单：变更类工具
    （移动/重命名/删除）只能作用于白名单内的文件，避免“指定 a.pdf 却授权了整个目录”。
    """
    try:
        path = Path(raw).expanduser()
        resolved = path.resolve()
    except (OSError, RuntimeError):
        return None, ""
    if not is_within_sandbox(resolved, list(sandbox_roots)):
        # 越界路径不会成为授权根；后续实际执行仍会被沙箱拒绝。
        return None, ""
    if resolved.exists() and resolved.is_file():
        return resolved.parent, str(resolved)
    return resolved, ""


def _normalized_root(raw: str, *, sandbox_roots: Sequence[Path]) -> Optional[Path]:
    """只取授权根目录（保留给既有调用）。"""
    return _resolve_intent_path(raw, sandbox_roots=sandbox_roots)[0]


def _derive_roots(
    intent: str,
    *,
    sandbox_roots: Sequence[Path],
    default_root: Optional[Path],
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    source: list[str] = []
    target: list[str] = []
    files: list[str] = []
    for raw, start, _end in extract_paths(intent):
        root, exact = _resolve_intent_path(raw, sandbox_roots=sandbox_roots)
        if root is None:
            continue
        bucket = target if _target_like(intent, start) else source
        value = str(root)
        if value not in bucket:
            bucket.append(value)
        # 只有“要处理的源文件”才进白名单；目标侧点名的路径不做限制。
        if exact and bucket is source and exact not in files:
            files.append(exact)

    if not source and not target:
        # 用户没有明确路径：退回“默认管理目录”，再退回全部沙箱根目录（仍不越界）。
        fallback = str(default_root) if default_root is not None else ""
        if fallback:
            source = [fallback]
        else:
            source = [str(item) for item in sandbox_roots]
    elif not source:
        # 只说了目标（“把文件移到 X”）：源目录允许默认目录/全部沙箱根目录。
        if default_root is not None:
            source = [str(default_root)]
        else:
            source = [str(item) for item in sandbox_roots]
    if not target:
        target = list(source)
    return tuple(source), tuple(target), tuple(files)


def _derive_filter(intent: str) -> Dict[str, Any]:
    """只从“只/仅/only”这类显式收窄语句里提取扩展名白名单。

    路径本身不参与匹配（否则 ``D:\\pdf`` 这类目录名会被误当成“只处理 pdf”）。
    """
    text = intent or ""
    spans = [(start, end) for _raw, start, end in extract_paths(text)]
    masked = list(text)
    for start, end in spans:
        masked[start:end] = " " * (end - start)
    masked_text = "".join(masked)
    lowered = masked_text.casefold()
    extensions: list[str] = []
    for marker in _NARROW_MARKERS:
        index = lowered.find(marker)
        while index != -1:
            window = masked_text[index:index + 200]
            for match in _EXTENSION.finditer(window):
                token = match.group(1).casefold()
                if token in _KNOWN_EXTENSIONS and token not in extensions:
                    extensions.append(token)
            index = lowered.find(marker, index + 1)
    return {"extensions": extensions} if extensions else {}


def _pref_int(name: str, default: int) -> int:
    try:
        from .. import db

        raw = db.get_preference(name)
        value = int(raw) if raw is not None else default
    except Exception:  # noqa: BLE001 - 配置不可读时使用安全默认值
        return default
    return value if value > 0 else default


def build_scope(
    intent: str,
    *,
    request_id: str,
    thread_id: str,
    prior: Optional[AuthorizationScope] = None,
    now: Optional[float] = None,
) -> AuthorizationScope:
    """由**原始用户意图**生成授权范围；``prior`` 仅用于澄清续问的收紧合并。"""
    from .engine import intent_effects  # 延迟导入避免 engine ↔ authorization 循环依赖
    from ..sandbox_config import effective_roots
    from ..workspace_config import effective_default_root

    moment = time.time() if now is None else now
    sandbox_roots = list(effective_roots())
    default_root = effective_default_root()
    source_roots, target_roots, file_whitelist = _derive_roots(
        intent, sandbox_roots=sandbox_roots, default_root=default_root
    )
    effects = intent_effects(intent)
    scope_filter = _derive_filter(intent)
    max_items = _pref_int("authorization_max_items", DEFAULT_MAX_ITEMS)
    ttl = _pref_int("authorization_scope_ttl_seconds", DEFAULT_SCOPE_TTL_SECONDS)
    recursive = not any(marker.casefold() in (intent or "").casefold() for marker in _TARGET_RESTRICT_MARKERS)
    expires_at = float(moment + ttl)

    origin = "user_intent"
    if prior is not None:
        origin = "clarification"
        # 澄清只补充信息：副作用集合绝不扩张，数量上限与有效期只减不增。
        effects = set(prior.allowed_effects)
        merged_sources = list(dict.fromkeys([*prior.source_roots, *source_roots]))
        merged_targets = list(dict.fromkeys([*prior.target_roots, *target_roots]))
        source_roots = tuple(merged_sources)
        target_roots = tuple(merged_targets)
        max_items = min(int(prior.max_items), int(max_items))
        recursive = bool(prior.recursive and recursive)
        expires_at = min(float(prior.expires_at), expires_at)
        # 精确文件白名单只允许收紧：上一轮点名了文件，这一轮补充的话不能把白名单放宽；
        # 交集为空时保留上一轮（绝不让“凑不出交集”退化成“不限制”）。
        previous_files = list(prior.file_whitelist or ())
        if previous_files and file_whitelist:
            common = [
                item for item in previous_files
                if any(_norm_path(item) == _norm_path(new) for new in file_whitelist)
            ]
            file_whitelist = tuple(common or previous_files)
        elif previous_files:
            file_whitelist = tuple(previous_files)
        current = dict(prior.filter or {})
        allowed_ext = sorted(
            set(str(item).casefold() for item in current.get("extensions") or [])
            & set(str(item).casefold() for item in scope_filter.get("extensions") or [])
        )
        scope_filter = {"extensions": allowed_ext} if allowed_ext else dict(current)

    return AuthorizationScope(
        request_id=str(request_id),
        thread_id=str(thread_id),
        allowed_effects=frozenset(effects),
        source_roots=source_roots,
        target_roots=target_roots,
        filter=scope_filter,
        file_whitelist=tuple(file_whitelist),
        max_items=int(max_items),
        recursive=bool(recursive),
        destructive=("delete" in effects),
        expires_at=expires_at,
        request_digest=request_digest(thread_id, request_id, intent),
        origin=origin,
    )


# ---------------- 过滤条件与工具参数校验 ----------------

def _as_extensions(values: Any) -> set[str]:
    return {
        str(item).casefold().lstrip(".")
        for item in (values or [])
        if str(item).strip()
    }


def check_filter(scope: AuthorizationScope, requested: Mapping[str, Any]) -> ScopeDecision:
    """请求的过滤条件必须落在范围内；空过滤条件等于“全选”，属于放宽。"""
    allowed = dict(scope.filter or {})
    requested = dict(requested or {})
    allowed_ext = _as_extensions(allowed.get("extensions"))
    if allowed_ext:
        requested_ext = _as_extensions(requested.get("extensions"))
        if not requested_ext:
            return ScopeDecision(
                False,
                "过滤条件扩大了授权范围：用户只授权了 "
                + "、".join(sorted(allowed_ext))
                + "，但请求未限制扩展名",
                "filter_expanded",
            )
        extra = requested_ext - allowed_ext
        if extra:
            return ScopeDecision(
                False,
                "过滤条件超出授权范围：" + "、".join(sorted(extra)),
                "filter_expanded",
            )
    allowed_names = {str(item).casefold() for item in (allowed.get("names") or [])}
    if allowed_names:
        requested_names = {str(item).casefold() for item in (requested.get("names") or [])}
        if not requested_names:
            return ScopeDecision(False, "过滤条件扩大了授权范围（文件名）", "filter_expanded")
        if requested_names - allowed_names:
            return ScopeDecision(False, "过滤条件超出授权范围（文件名）", "filter_expanded")
    allowed_contains = str(allowed.get("name_contains") or "")
    if allowed_contains:
        requested_contains = str(requested.get("name_contains") or "")
        if allowed_contains.casefold() not in requested_contains.casefold():
            return ScopeDecision(False, "过滤条件扩大了授权范围（名称包含）", "filter_expanded")
    for bound, key in (("min_size", "min_size"), ("max_size", "max_size")):
        limit = allowed.get(bound)
        if limit is None:
            continue
        value = requested.get(key)
        if value is None:
            return ScopeDecision(False, f"过滤条件扩大了授权范围（{bound}）", "filter_expanded")
        try:
            if bound == "min_size" and int(value) < int(limit):
                return ScopeDecision(False, "过滤条件扩大了授权范围（min_size）", "filter_expanded")
            if bound == "max_size" and int(value) > int(limit):
                return ScopeDecision(False, "过滤条件扩大了授权范围（max_size）", "filter_expanded")
        except (TypeError, ValueError):
            return ScopeDecision(False, f"过滤条件无效（{bound}）", "filter_expanded")
    return ScopeDecision(True)


def _file_stat(path: str) -> tuple[int, int]:
    try:
        stat = Path(path).stat()
        return int(stat.st_size), int(stat.st_mtime_ns)
    except OSError:
        return -1, -1


def path_filter_decision(
    scope: AuthorizationScope, value: str, *, label: str = "源文件"
) -> ScopeDecision:
    """单文件工具也必须满足授权范围的过滤条件（扩展名 / 文件名 / 大小）。

    ``check_filter`` 只比对“请求的过滤条件”，而单文件工具根本没有 filter 参数：
    用户说“只移动 PDF”时，move_file 传一个 JPG 曾经能直接通过。这里把范围里的
    过滤条件逐条落到具体文件上，扩展名/文件名/大小不符合就拒绝。
    """
    filters = dict(scope.filter or {})
    if not filters or not value:
        return ScopeDecision(True)
    path = Path(str(value))
    extensions = _as_extensions(filters.get("extensions"))
    if extensions and path.suffix.casefold().lstrip(".") not in extensions:
        return ScopeDecision(
            False,
            f"{label}不在本次授权范围内：用户只授权了 "
            + "、".join(sorted(extensions))
            + f"，而 {path.name} 不符合",
            "filter_mismatch",
        )
    names = {str(item).casefold() for item in (filters.get("names") or [])}
    if names and path.name.casefold() not in names:
        return ScopeDecision(
            False, f"{label}不在本次授权的文件名内：{path.name}", "filter_mismatch"
        )
    contains = str(filters.get("name_contains") or "")
    if contains and contains.casefold() not in path.name.casefold():
        return ScopeDecision(
            False, f"{label}名称不包含本次授权要求的关键字：{contains}", "filter_mismatch"
        )
    min_size = filters.get("min_size")
    max_size = filters.get("max_size")
    if min_size is None and max_size is None:
        return ScopeDecision(True)
    size, _mtime = _file_stat(str(value))
    if size < 0:
        return ScopeDecision(
            False, f"{label}不可读取大小，无法核验授权过滤条件：{path.name}", "filter_mismatch"
        )
    if min_size is not None and size < int(min_size):
        return ScopeDecision(
            False, f"{label}小于本次授权的最小大小：{path.name}", "filter_mismatch"
        )
    if max_size is not None and size > int(max_size):
        return ScopeDecision(
            False, f"{label}大于本次授权的最大大小：{path.name}", "filter_mismatch"
        )
    return ScopeDecision(True)


def in_file_whitelist(scope: AuthorizationScope, value: str) -> bool:
    """精确文件白名单：范围里没有点名文件时不限制；点名了就只认这些文件。"""
    if not scope.file_whitelist:
        return True
    target = _norm_path(value)
    return any(_norm_path(item) == target for item in scope.file_whitelist)


def _whitelist_decision(
    scope: AuthorizationScope, value: str, *, label: str = "源文件"
) -> ScopeDecision:
    if in_file_whitelist(scope, value):
        return ScopeDecision(True)
    return ScopeDecision(
        False,
        f"{label}不在本次授权的文件清单内：{value}"
        "（用户只点名了 "
        + "、".join(Path(item).name for item in scope.file_whitelist[:5])
        + "）",
        "file_not_authorized",
    )


def _batch_paths(args: Mapping[str, Any]) -> list[str]:
    """批量工具将要处理的源文件清单；清单不可用时返回空列表（由上游另行拒绝）。"""
    from ..tools import manifests

    try:
        matched = manifests.match(
            str(args.get("scan_id") or ""), dict(args.get("filter") or {})
        )
    except Exception:  # noqa: BLE001 - 失效清单由 _manifest_directory 单独拒绝
        return []
    return [str(item.get("path") or "") for item in matched if item.get("path")]


# 预览操作的“具体副作用”：预览本身是只读的，确认/执行时才产生这些效果。
PREVIEW_OP_EFFECT: dict[str, str] = {
    "move": "move",
    "classify": "move",
    "rename": "rename",
    "delete": "delete",
}


def authorize_preview_selection(
    scope: AuthorizationScope,
    *,
    op: str,
    sources: Sequence[str],
    targets: Sequence[str] = (),
) -> ScopeDecision:
    """预览确认/执行阶段对**具体操作**重新做完整授权校验。

    预览生成是只读的，因此只读范围也能生成“移动/重命名”预览；真正的副作用发生在
    确认与执行，这两步必须重新确认：操作类型被授权、源文件在范围内（含精确文件
    白名单与过滤条件）、目标目录在范围内、条目数不超过上限。
    """
    effect = PREVIEW_OP_EFFECT.get(str(op), "")
    if not effect:
        return ScopeDecision(False, f"不支持的预览操作：{op}", "unknown_tool")
    if not scope.request_digest:
        return ScopeDecision(False, "缺少授权范围指纹，拒绝执行", "missing_scope")
    if scope.expired():
        return ScopeDecision(False, "授权范围已过期，请重新发起请求", "scope_expired")
    if not scope.allows_effect(effect):
        return ScopeDecision(
            False, f"原始用户请求未授权 {effect} 操作", "effect_not_allowed", effect
        )
    items = [str(item) for item in sources if str(item)]
    if not items:
        return ScopeDecision(False, "没有选中任何文件", "empty_selection")
    if len(items) > int(scope.max_items):
        return ScopeDecision(
            False,
            f"本次操作将处理 {len(items)} 项，超过授权上限 {scope.max_items}",
            "max_items_exceeded",
        )
    for source in items:
        check = _path_decision(source, scope.source_roots, label="源文件")
        if not check.allowed:
            return check
        check = _whitelist_decision(scope, source)
        if not check.allowed:
            return check
        check = path_filter_decision(scope, source)
        if not check.allowed:
            return check
    if effect != "delete":
        for target in targets:
            parent = str(Path(str(target)).parent)
            check = _path_decision(
                parent, scope.target_roots, label="目标目录", code="target_outside_scope"
            )
            if not check.allowed:
                return check
    return ScopeDecision(True, effect=effect)


def _safe_bare_name(name: str) -> bool:
    value = (name or "").strip()
    return bool(value) and not any(sep in value for sep in ("/", "\\")) and value not in (".", "..")


def _manifest_directory(scan_id: str) -> tuple[str, str]:
    from ..tools import manifests

    try:
        manifest = manifests.load(str(scan_id))
    except Exception:  # noqa: BLE001 - 清单不存在/失效一律拒绝
        return "", "扫描清单不存在或已失效"
    return str(manifest.get("directory") or ""), ""


def _path_decision(
    value: str, roots: Sequence[str], *, label: str, code: str = "path_outside_scope"
) -> ScopeDecision:
    if not value:
        return ScopeDecision(False, f"缺少{label}", code)
    if not _within(value, roots):
        allowed = "、".join(roots) or "(无)"
        return ScopeDecision(
            False,
            f"{label}超出本次授权范围：{value}（授权根目录：{allowed}）",
            code,
        )
    return ScopeDecision(True)


def authorize_tool_call(
    scope: AuthorizationScope,
    tool: str,
    args: Mapping[str, Any],
    *,
    proposed_count: int = 1,
) -> ScopeDecision:
    """执行前的授权契约校验（不含数量阈值审批，那由 PolicyEngine 处理）。"""
    contract: Optional[ToolContract] = TOOL_CONTRACTS.get(tool)
    if contract is None:
        return ScopeDecision(False, f"未知工具或缺少工具契约: {tool}", "unknown_tool")
    effect = contract.side_effect
    if effect != "read":
        if not scope.request_digest:
            return ScopeDecision(False, "缺少授权范围指纹，拒绝执行", "missing_scope")
        if scope.expired():
            return ScopeDecision(
                False, "授权范围已过期，请重新发起请求", "scope_expired"
            )
        if not scope.allows_effect(effect):
            return ScopeDecision(
                False, f"原始用户请求未授权 {effect} 操作", "effect_not_allowed", effect
            )
    union_roots = tuple(dict.fromkeys([*scope.source_roots, *scope.target_roots]))
    count = max(0, int(proposed_count))

    if tool in {"scan_directory", "extract_text", "classify_file", "search_content"}:
        raw = str(args.get("directory") or args.get("file_path") or args.get("path") or "")
        if raw and not _within(raw, union_roots):
            return ScopeDecision(
                False, f"路径超出本次授权范围：{raw}", "path_outside_scope"
            )
        if tool == "scan_directory" and not scope.recursive and bool(args.get("recursive")):
            return ScopeDecision(
                False, "本次授权不包含递归扫描子目录", "recursive_not_allowed"
            )
        return ScopeDecision(True, effect=effect)

    if tool in {"request_clarification"}:
        return ScopeDecision(True, effect=effect)

    if tool == "make_dir":
        return _path_decision(str(args.get("path") or ""), scope.target_roots, label="目标目录", code="target_outside_scope")

    if tool == "move_file":
        source = str(args.get("src") or "")
        check = _path_decision(source, scope.source_roots, label="源路径")
        if not check.allowed:
            return check
        # 单文件工具没有 filter 参数，必须把范围里的过滤条件落到这个文件上，
        # 否则“只移动 PDF”的请求可以用 move_file 搬走 JPG。
        check = _whitelist_decision(scope, source)
        if not check.allowed:
            return check
        check = path_filter_decision(scope, source)
        if not check.allowed:
            return check
        check = _path_decision(str(args.get("dest_dir") or ""), scope.target_roots, label="目标目录", code="target_outside_scope")
        if not check.allowed:
            return check
        new_name = str(args.get("new_name") or "")
        if new_name and not _safe_bare_name(new_name):
            return ScopeDecision(False, "new_name 只能是文件名，不能包含路径", "invalid_target")
        return ScopeDecision(True, effect=effect)

    if tool == "rename_file":
        source = str(args.get("src") or "")
        check = _path_decision(source, scope.source_roots, label="源路径")
        if not check.allowed:
            return check
        check = _whitelist_decision(scope, source)
        if not check.allowed:
            return check
        check = path_filter_decision(scope, source)
        if not check.allowed:
            return check
        if not _safe_bare_name(str(args.get("new_name") or "")):
            return ScopeDecision(False, "new_name 只能是文件名，不能包含路径", "invalid_target")
        return ScopeDecision(True, effect=effect)

    if tool == "delete_file":
        if not scope.destructive:
            return ScopeDecision(False, "原始用户请求未授权 delete 操作", "effect_not_allowed", effect)
        path = str(args.get("path") or "")
        check = _path_decision(path, union_roots, label="删除路径")
        if not check.allowed:
            return check
        check = _whitelist_decision(scope, path, label="删除路径")
        if not check.allowed:
            return check
        return path_filter_decision(scope, path, label="删除路径")

    if tool == "write_summary":
        source = str(args.get("file_path") or "")
        check = _path_decision(source, scope.source_roots, label="源文件")
        if not check.allowed:
            return check
        check = _whitelist_decision(scope, source)
        if not check.allowed:
            return check
        check = path_filter_decision(scope, source)
        if not check.allowed:
            return check
        return _path_decision(
            str(args.get("output_dir") or ""), scope.target_roots,
            label="输出目录", code="target_outside_scope",
        )

    if tool == "create_schedule":
        return _path_decision(
            str(args.get("directory") or ""), union_roots, label="定时任务目录"
        )

    if tool in {"set_preference", "delete_preference"}:
        return ScopeDecision(True, effect=effect)

    if tool in {"batch_move", "batch_rename", "batch_classify"}:
        directory, error = _manifest_directory(str(args.get("scan_id") or ""))
        if error:
            return ScopeDecision(False, error, "stale_scan")
        check = _path_decision(directory, scope.source_roots, label="扫描目录")
        if not check.allowed:
            return check
        if count > scope.max_items:
            return ScopeDecision(
                False,
                f"本次操作将处理 {count} 项，超过授权上限 {scope.max_items}"
                "（如需处理更多，可在偏好中调整 authorization_max_items 后重新发起请求）",
                "max_items_exceeded",
            )
        filter_decision = check_filter(scope, dict(args.get("filter") or {}))
        if not filter_decision.allowed:
            return filter_decision
        if scope.file_whitelist:
            # 用户点名了具体文件：批量工具也只能处理这些文件，不能“顺带”处理同目录其它文件。
            outside = [
                path for path in _batch_paths(args)
                if not in_file_whitelist(scope, path)
            ]
            if outside:
                return ScopeDecision(
                    False,
                    "本次授权只包含点名的文件，批量操作还会处理其他文件："
                    + str(outside[0]),
                    "file_not_authorized",
                )
        if tool == "batch_move":
            return _path_decision(
                str(args.get("dest_dir") or ""), scope.target_roots,
                label="目标目录", code="target_outside_scope",
            )
        if tool == "batch_classify":
            return _path_decision(
                str(args.get("dest_root") or ""), scope.target_roots,
                label="目标根目录", code="target_outside_scope",
            )
        return ScopeDecision(True, effect=effect)

    if tool == "preview_batch":
        directory, error = ("", "") if args.get("directory") else _manifest_directory(str(args.get("scan_id") or ""))
        if error:
            return ScopeDecision(False, error, "stale_scan")
        check = _path_decision(
            str(args.get("directory") or directory),
            scope.source_roots,
            label="预览目录",
        )
        if not check.allowed:
            return check
        filter_decision = check_filter(scope, dict(args.get("filter") or {}))
        if not filter_decision.allowed:
            return filter_decision
        op = str(args.get("op") or "")
        target = str(args.get("dest_dir") or args.get("dest_root") or "")
        if op in {"move", "classify"} and target:
            target_decision = _path_decision(
                target, scope.target_roots, label="目标目录", code="target_outside_scope"
            )
            if not target_decision.allowed:
                return target_decision
        if op == "delete" and not scope.destructive:
            return ScopeDecision(False, "原始用户请求未授权 delete 预览执行", "effect_not_allowed")
        return ScopeDecision(True, effect=effect)

    # 兜底：契约已登记但没写授权分支时 fail closed。
    return ScopeDecision(False, f"工具 {tool} 未登记授权校验规则", "unregistered_rule", effect)
