"""复审意见回归：带空格的路径被误判为指令，路径匹配又吞掉真正的指令。

根因：``strip_paths`` 用四条正则顺序替换。盘符/UNC/POSIX 规则都在空白处截断，
所以 ``D:\\My scan`` 只剥掉 ``D:\\My``，剩下的 ``scan`` 命中只读词表，纯路径回答
被当成“这轮自带新操作”，上一轮授权被清空；相对路径规则的字符集又包含空格，
``downloads/report 整理一下`` 会把后面的“整理”一起吃掉，真正的移动指令消失。

修复后：整条消息是路径（可带空格/引号）时整段剥掉；绝对路径跨空格延续时遇到
操作词就停；相对路径里 ``整理/归档`` 这类动作枚举不再当成路径。
"""
from __future__ import annotations

import pytest

from app.agent import graph as graph_module
from app.policy.engine import intent_effects, strip_paths


# ---------------- 带空格的路径是参数 ----------------

@pytest.mark.parametrize(
    "answer",
    [
        r"D:\My scan",
        r"D:\my scan folder",
        r"D:\My Documents\scan",
        r"C:\Program Files\list",
        r"/home/me/My scan",
        r"/home/me/My Documents/list",
        r"\\server\share\My list",
        r"\\server\share\My Documents\scan",
        '"D:\\My scan"',
        "'/home/me/My Documents/list'",
        "D:\\New Folder\\scan",
        # 目录名里含操作词但后面跟着名词，仍是路径而不是指令。
        r"D:\我的 扫描件",
        r"D:\资料 整理箱",
    ],
)
def test_path_with_spaces_has_no_intent(answer: str) -> None:
    assert intent_effects(answer) == set(), answer


@pytest.mark.parametrize(
    "text",
    [
        r"D:\My scan",
        r"D:\My Documents\scan",
        '"D:\\My scan"',
    ],
)
def test_strip_paths_removes_whole_spaced_path(text: str) -> None:
    assert strip_paths(text.casefold()).strip() == "", strip_paths(text)


# ---------------- 路径之外的动词仍然保留 ----------------

@pytest.mark.parametrize(
    "text,expected",
    [
        (r"把 D:\My Documents\scan 移动到 out", "move"),
        (r"扫描 /home/me/My Documents", "read"),
        (r"删除 C:\Program Files\temp", "delete"),
        (r"整理一下 D:\My Documents\scan", "move"),
        (r"把 D:\My Documents\scan 重命名", "rename"),
        # 路径本身带空格，后面紧跟中文操作词：操作词不能被当成路径名。
        (r"D:\scan 删除", "delete"),
        (r"D:\scan 整理一下", "move"),
        (r"D:\scan 然后移动", "move"),
    ],
)
def test_verbs_after_spaced_paths_still_detected(text: str, expected: str) -> None:
    assert expected in intent_effects(text), intent_effects(text)


def test_negation_survives_spaced_paths() -> None:
    assert "move" not in intent_effects(r"不要移动 D:\My scan")


# ---------------- 路径匹配不能吞掉真正的指令 ----------------

@pytest.mark.parametrize(
    "text,expected",
    [
        ("整理/归档 这些文件", "move"),
        ("扫描/列出 目录", "read"),
        ("移动/重命名 这些文件", "move"),
        ("重命名/整理 backend/app 下的文件", "rename"),
        ("把 downloads/report 整理一下", "move"),
        # 空格曾让相对路径规则吃掉后面的动词。
        ("扫描 downloads/report 里的 PDF", "read"),
    ],
)
def test_path_matching_does_not_swallow_commands(text: str, expected: str) -> None:
    assert expected in intent_effects(text), intent_effects(text)


@pytest.mark.parametrize(
    "text",
    ["整理/归档", "扫描/列出", "移动/重命名", "move/organize"],
)
def test_intent_enumeration_is_not_masked(text: str) -> None:
    """并列动词整段保留，因此意图词不会被剥掉。"""
    assert strip_paths(text.casefold()) == text


# ---------------- 澄清判定 ----------------

@pytest.mark.parametrize(
    "answer",
    [
        r"D:\My scan",
        r"C:\Program Files\list",
        r"/home/me/My Documents/list",
        r"\\server\share\My list",
    ],
)
def test_spaced_path_answer_is_a_clarification_answer(answer: str) -> None:
    assert graph_module._is_clarification_answer(answer) is True, answer


@pytest.mark.parametrize(
    "message",
    [
        r"把 D:\My Documents\scan 移动到 out",
        "整理/归档 这些文件",
        "把 downloads/report 整理一下",
    ],
)
def test_message_with_command_is_not_a_clarification_answer(message: str) -> None:
    assert graph_module._is_clarification_answer(message) is False, message
