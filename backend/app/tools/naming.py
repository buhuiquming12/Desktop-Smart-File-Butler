"""确定性重命名规则。

预览（P0）与实际执行（P4）必须使用**同一套**命名逻辑，否则会出现“预览说 A、
执行成 B”。因此这里只放纯函数：输入文件名、时间与序号，输出新文件名或明确的
失败原因，不做任何文件系统读写。

规则顺序（无模板时）::

    prefix + seq(可选，prefix 位置) + 主体 + 日期(可选) + seq(可选，suffix 位置) + suffix

- 主体 = 原文件名（不含扩展名）经“查找替换”后的结果；
- 扩展名永远保留，不会因为规则被吃掉；
- 日期来源必须由调用方显式给出（修改时间 / 创建时间 / 当前时间 / 正文提取），
  取不到时不猜：调用方把 ``date=None`` 传进来，本模块拒绝生成含日期的名字。

模板模式支持 ``{name}``、``{seq}``、``{date}``、``{ext}`` 四个占位符，未知占位符
直接报错，避免用户写了 ``{filename}`` 却得到字面量。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Optional

# Windows 非法字符 + 控制字符；跨平台统一按最严格的标准校验。
_ILLEGAL_CHARS = set('<>:"/\\|?*')
_CONTROL_LIMIT = 32
_MAX_NAME_LENGTH = 200
# Windows 保留设备名（不含扩展名时也不能使用）。
_RESERVED_NAMES = {
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}

# 日期模板：只支持明确的年月日时分秒占位，避免 strftime 的平台差异。
_DATE_TOKENS = ("YYYY", "YY", "MM", "DD", "HH", "mm", "ss")


def safe_category(name: str) -> str:
    """把类别词规范成单层安全目录名：拒绝路径分隔符与空值。"""
    value = (name or "").strip()
    if not value:
        raise ValueError("类别名不能为空")
    if any(char in _ILLEGAL_CHARS or ord(char) < _CONTROL_LIMIT for char in value):
        raise ValueError(f"类别名包含非法字符: {name}")
    if value in {".", ".."}:
        raise ValueError(f"非法类别名: {name}")
    return value


def format_date(value: datetime, pattern: str) -> str:
    """按 ``YYYY-MM-DD`` 之类的模板格式化日期。

    不用 ``strftime``：``%Y`` 与 ``%y`` 差异、Windows 的本地化都容易出错，这里逐
    占位符替换，并把 ``YYYY`` 先于 ``YY`` 处理，避免部分匹配。
    """
    text = pattern or "YYYYMMDD"
    for token in sorted(_DATE_TOKENS, key=len, reverse=True):
        replacement = {
            "YYYY": f"{value.year:04d}",
            "YY": f"{value.year % 100:02d}",
            "MM": f"{value.month:02d}",
            "DD": f"{value.day:02d}",
            "HH": f"{value.hour:02d}",
            "mm": f"{value.minute:02d}",
            "ss": f"{value.second:02d}",
        }[token]
        text = text.replace(token, replacement)
    return text


def validate_file_name(name: str) -> str:
    """校验最终文件名；返回空字符串表示合法，否则返回中文原因。"""
    value = (name or "").strip()
    if not value:
        return "文件名为空"
    if value in {".", ".."}:
        return "文件名不能是 . 或 .."
    if len(value) > _MAX_NAME_LENGTH:
        return f"文件名过长（{len(value)} 字符，上限 {_MAX_NAME_LENGTH}）"
    if any(char in _ILLEGAL_CHARS for char in value):
        return '文件名包含非法字符 < > : " / \\ | ? *'
    if any(ord(char) < _CONTROL_LIMIT for char in value):
        return "文件名包含控制字符"
    if value.endswith(" ") or value.endswith("."):
        return "文件名不能以空格或点结尾"
    stem = value.rsplit(".", 1)[0].strip().lower()
    if stem in _RESERVED_NAMES:
        return f"{stem} 是 Windows 保留名称"
    return ""


def _replace(core: str, find: str, replace: str, case_sensitive: bool) -> str:
    if not find:
        return core
    if case_sensitive:
        return core.replace(find, replace)
    # 大小写不敏感替换：用 re.sub 并转义，保留原大小写无关的语义。
    return re.sub(re.escape(find), lambda _: replace, core, flags=re.IGNORECASE)


@dataclass(frozen=True)
class RenameRule:
    """一条重命名规则；所有字段都可为空，空表示不参与命名。"""

    prefix: str = ""
    suffix: str = ""
    find: str = ""
    replace: str = ""
    case_sensitive: bool = False
    template: str = ""
    sequence_start: int = 1
    sequence_step: int = 1
    sequence_padding: int = 3
    sequence_position: str = "suffix"  # prefix | suffix
    date_format: str = "YYYYMMDD"


def rule_needs_sequence(rule: RenameRule, requested: bool) -> bool:
    """模板里写了 {seq} 就一定需要序号；否则以调用方声明为准。"""
    return requested or "{seq}" in (rule.template or "")


def rule_needs_date(rule: RenameRule, requested: bool) -> bool:
    return requested or "{date}" in (rule.template or "")


def build_name(
    *,
    stem: str,
    ext: str,
    rule: RenameRule,
    index: int = 0,
    date: Optional[datetime] = None,
    sequence: bool = False,
    use_date: bool = False,
) -> str:
    """生成最终文件名（含扩展名）。

    ``sequence`` / ``use_date`` 由调用方显式声明“本条规则要不要序号/日期”，
    因为“日期取不到”必须变成明确的跳过原因，而不是静默生成一个没有日期的名字。
    """
    seq_value = rule.sequence_start + index * rule.sequence_step
    seq_text = str(seq_value).zfill(max(1, rule.sequence_padding))
    date_text = format_date(date, rule.date_format) if (use_date and date) else ""

    if rule.template:
        values = {
            "{name}": stem,
            "{seq}": seq_text if sequence else "",
            "{date}": date_text,
            "{ext}": ext.lstrip("."),
        }
        rendered = rule.template
        for token, value in values.items():
            rendered = rendered.replace(token, value)
        leftover = re.search(r"\{[^}]*\}", rendered)
        if leftover:
            raise ValueError(f"模板包含未知占位符: {leftover.group(0)}")
        base = rendered
        # 模板里显式写了 {ext} 就不重复追加扩展名。
        name = base if ext.lstrip(".") and f".{ext.lstrip('.')}" in base else f"{base}{ext}"
    else:
        core = _replace(stem, rule.find, rule.replace, rule.case_sensitive)
        pieces = [rule.prefix]
        if sequence and rule.sequence_position == "prefix":
            pieces.append(seq_text)
        pieces.append(core)
        if date_text:
            pieces.append(date_text)
        if sequence and rule.sequence_position != "prefix":
            pieces.append(seq_text)
        pieces.append(rule.suffix)
        name = "".join(piece for piece in pieces if piece) + ext
    return name.strip()


def plan_names(
    records: Iterable[dict],
    rule: RenameRule,
    *,
    use_sequence: bool,
    use_date: bool,
    source_date: str = "modified",
) -> list[dict]:
    """为一组文件生成计划；重复目标名或非法名会被明确标记。

    ``records`` 每项需含 ``path`` / ``stem`` / ``ext`` / ``modified``（ISO 字符串或
    datetime）。返回项包含 ``target_name``、``status``、``reason``。
    """
    results: list[dict] = []
    seen: dict[str, int] = {}
    for index, record in enumerate(records):
        stem = str(record.get("stem") or "")
        ext = str(record.get("ext") or "")
        date_value = record.get("date")
        reason = ""
        target_name = ""
        if use_date and not isinstance(date_value, datetime):
            reason = "日期来源不可用，已跳过（不会猜测日期）"
        else:
            try:
                target_name = build_name(
                    stem=stem,
                    ext=ext,
                    rule=rule,
                    index=index,
                    date=date_value if isinstance(date_value, datetime) else None,
                    sequence=use_sequence,
                    use_date=use_date,
                )
            except ValueError as exc:
                reason = str(exc)
        if not reason:
            invalid = validate_file_name(target_name)
            if invalid:
                reason = invalid
        status = "ready" if not reason else "skipped"
        if not reason:
            key = target_name.casefold()
            if key in seen:
                status = "conflict"
                reason = "与同批次其他文件的目标名重复"
            else:
                seen[key] = index
        results.append({**record, "target_name": target_name, "status": status, "reason": reason})
    # 反向标记：重复时把先出现的那一项也标成冲突，避免“只报一个”的误导。
    duplicates = {
        item["target_name"].casefold()
        for item in results
        if item.get("reason") == "与同批次其他文件的目标名重复"
    }
    for item in results:
        if item["target_name"].casefold() in duplicates and item["status"] == "ready":
            item["status"] = "conflict"
            item["reason"] = "与同批次其他文件的目标名重复"
    return results
