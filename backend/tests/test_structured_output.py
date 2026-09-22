"""回归：provider 不支持 function calling 时，结构化输出降级为提示词 JSON。

背景：``with_structured_output`` 默认走 function_calling，请求体带 ``tools``。
只实现聊天补全的 OpenAI 兼容服务会直接返回 400，导致规划/反思每轮必失败。
"""
from __future__ import annotations

import json
from typing import Any, List

import pytest
from langchain_core.exceptions import OutputParserException
from langchain_core.messages import HumanMessage
from pydantic import BaseModel, Field

from app.agent import llm as llm_module
from app.agent.llm import build_structured_llm, parse_json_object


class Plan(BaseModel):
    goal: str
    steps: List[str] = Field(default_factory=list)


class BadRequest(Exception):
    """模拟 openai.BadRequestError：服务端以 400 拒绝请求形态。"""

    status_code = 400


class ServerError(Exception):
    """模拟瞬时故障（500）：不应触发降级。"""

    status_code = 500


class _Reply:
    def __init__(self, content: str) -> None:
        self.content = content


class _FakeNative:
    """原生结构化输出替身：按脚本抛错或直接返回对象。"""

    def __init__(self, owner: "_FakeLLM", schema: Any) -> None:
        self._owner = owner
        self._schema = schema

    def invoke(self, messages: Any) -> Any:
        # 刻意保持单参数签名：真实调用点只传 messages，替身必须同样严格，
        # 否则实现里多塞一个位置参数这条路就测不出来。
        self._owner.native_calls += 1
        behavior = self._owner.native_behavior
        if isinstance(behavior, BaseException):
            raise behavior
        return behavior


class _FakeLLM:
    """测试替身：记录调用次数，并按顺序吐出预设回复。"""

    def __init__(self, replies: List[str] | None = None, native_behavior: Any = None) -> None:
        self.replies = list(replies or [])
        self.native_behavior = native_behavior
        self.native_builds = 0
        self.native_calls = 0
        self.prompt_calls: List[Any] = []

    def with_structured_output(self, schema: Any) -> _FakeNative:
        self.native_builds += 1
        return _FakeNative(self, schema)

    def invoke(self, messages: Any) -> _Reply:
        self.prompt_calls.append(messages)
        if not self.replies:
            raise AssertionError("提示词路径被调用了，但测试未准备回复")
        return _Reply(self.replies.pop(0))


def _payload(goal: str = "归档", steps: List[str] | None = None) -> str:
    return json.dumps({"goal": goal, "steps": steps or []}, ensure_ascii=False)


def _build(llm: Any, schema: Any = Plan) -> Any:
    """本文件固定按 auto 分支构造，避免读到真实配置。

    配置读取（structured_output_mode / 手动降级）由 test_structured_output_mode.py 覆盖。
    """
    return build_structured_llm(llm, schema, force_prompt=False)


# ------------------------------- 降级行为 -------------------------------


def test_native_success_does_not_degrade() -> None:
    fake = _FakeLLM(native_behavior=Plan(goal="归档", steps=["s1"]))
    structured = _build(fake)

    assert structured.invoke([HumanMessage(content="x")]) == Plan(goal="归档", steps=["s1"])
    assert structured.using_native is True
    assert fake.prompt_calls == []


def test_bad_request_degrades_to_prompt_json() -> None:
    fake = _FakeLLM(replies=[_payload()], native_behavior=BadRequest("unknown parameter: tools"))
    structured = _build(fake)

    assert structured.invoke([HumanMessage(content="x")]) == Plan(goal="归档")
    assert structured.using_native is False
    assert fake.native_calls == 1
    assert len(fake.prompt_calls) == 1  # 降级后立刻用提示词路径重试成功


def test_output_parser_error_degrades() -> None:
    """relay 收下 tools 但模型从不按工具协议回复时，同样降级。"""
    fake = _FakeLLM(
        replies=[_payload()], native_behavior=OutputParserException("no tool call found")
    )
    structured = _build(fake)

    assert structured.invoke([HumanMessage(content="x")]) == Plan(goal="归档")
    assert structured.using_native is False


def test_degradation_persists_across_calls() -> None:
    fake = _FakeLLM(replies=[_payload(), _payload(goal="第二次")], native_behavior=BadRequest("tools unsupported"))
    structured = _build(fake)

    structured.invoke([HumanMessage(content="1")])
    second = structured.invoke([HumanMessage(content="2")])

    assert second == Plan(goal="第二次")
    assert fake.native_calls == 1  # 只探测一次，后续不再打原生路径
    assert len(fake.prompt_calls) == 2


def test_transient_error_propagates_without_degrading() -> None:
    fake = _FakeLLM(native_behavior=ServerError("boom"))
    structured = _build(fake)

    with pytest.raises(ServerError):
        structured.invoke([HumanMessage(content="x")])

    assert structured.using_native is True  # 瞬时故障不得被当作能力缺失
    assert fake.prompt_calls == []


def test_unrelated_400_propagates_without_degrading() -> None:
    fake = _FakeLLM(native_behavior=BadRequest("model does not exist"))
    structured = _build(fake)
    with pytest.raises(BadRequest, match="model does not exist"):
        structured.invoke([HumanMessage(content="x")])
    assert structured.using_native is True
    assert fake.prompt_calls == []


# ------------------------------- 提示词路径 -------------------------------


def test_instruction_carries_schema_and_keeps_original_messages() -> None:
    fake = _FakeLLM(replies=[_payload()], native_behavior=BadRequest("function calling unsupported"))
    structured = _build(fake)

    structured.invoke([HumanMessage(content="原始请求")])

    sent = fake.prompt_calls[0]
    assert sent[0].content == "原始请求"
    assert "JSON Schema" in sent[-1].content
    assert '"goal"' in sent[-1].content


def test_retries_after_invalid_json_and_feeds_error_back() -> None:
    fake = _FakeLLM(
        replies=["抱歉，我来解释一下：先扫描目录。", _payload()],
        native_behavior=BadRequest("tools unsupported"),
    )
    structured = _build(fake)

    assert structured.invoke([HumanMessage(content="x")]) == Plan(goal="归档")
    assert len(fake.prompt_calls) == 2
    repair_prompt = fake.prompt_calls[1][-1].content
    assert "Plan" in repair_prompt  # 把校验错误回灌给模型


def test_raises_with_context_after_all_attempts_exhausted() -> None:
    fake = _FakeLLM(replies=["不是 JSON", "仍然不是 JSON"], native_behavior=BadRequest("tools unsupported"))
    structured = _build(fake)

    with pytest.raises(ValueError, match="Plan"):
        structured.invoke([HumanMessage(content="x")])

    assert len(fake.prompt_calls) == 2


def test_schema_violation_triggers_retry() -> None:
    """是合法 JSON 但不符合 schema（缺 goal）时，也必须重试而不是直接返回。"""
    fake = _FakeLLM(replies=[json.dumps({"steps": []}), _payload()],
                    native_behavior=BadRequest("tools unsupported"))
    structured = _build(fake)

    assert structured.invoke([HumanMessage(content="x")]) == Plan(goal="归档")
    assert len(fake.prompt_calls) == 2


# ------------------------------- JSON 解析 -------------------------------


def test_parse_plain_json() -> None:
    assert parse_json_object('{"goal": "g"}') == {"goal": "g"}


def test_parse_fenced_json() -> None:
    assert parse_json_object('```json\n{"goal": "g"}\n```') == {"goal": "g"}
    assert parse_json_object('```\n{"goal": "g"}\n```') == {"goal": "g"}


def test_parse_json_with_surrounding_prose() -> None:
    text = '好的，这是结果：\n{"goal": "g", "steps": ["a"]}\n希望有帮助。'
    assert parse_json_object(text) == {"goal": "g", "steps": ["a"]}


def test_parse_truncated_json() -> None:
    # 输出在最外层对象中途被截断：补上缺失的右括号后仍能解出已完成字段
    assert parse_json_object('{"goal": "g"') == {"goal": "g"}


def test_parse_truncated_inside_nested_container_raises() -> None:
    # 边界（已知不足）：截断发生在嵌套容器内部时无法补全，按解析失败处理，
    # 由上层重试一次；这里是明确记录该限制，不是期望它成功。
    with pytest.raises(ValueError):
        parse_json_object('{"goal": "g", "steps": [')


def test_parse_ignores_braces_inside_strings() -> None:
    text = '{"goal": "含 } 和 { 的说明", "steps": []}'
    assert parse_json_object(text) == {"goal": "含 } 和 { 的说明", "steps": []}


def test_parse_rejects_non_json() -> None:
    with pytest.raises(ValueError):
        parse_json_object("这里没有任何 JSON")
    with pytest.raises(ValueError):
        parse_json_object("   ")


def test_config_forwarded_only_when_provided() -> None:
    """调用点只传 messages 时不得多塞位置参数；显式传 config 时必须转发。"""
    seen: List[Any] = []

    class _Recorder:
        def invoke(self, messages: Any, *args: Any, **kwargs: Any) -> Any:
            seen.append((args, kwargs))
            return Plan(goal="g")

    class _LLM:
        def with_structured_output(self, schema: Any) -> _Recorder:
            return _Recorder()

    structured = _build(_LLM())
    structured.invoke([HumanMessage(content="x")])
    assert seen[-1] == ((), {})

    structured.invoke([HumanMessage(content="x")], {"tags": ["t"]})
    assert seen[-1] == (({"tags": ["t"]},), {})


# ------------------------------- 手动降级（force_prompt） -------------------------------


def test_force_prompt_skips_native_entirely() -> None:
    """手动降级时连原生 runnable 都不构造，探测用的那次 400 请求也省掉。"""
    fake = _FakeLLM(replies=[_payload()], native_behavior=Plan(goal="不该被用到"))
    structured = build_structured_llm(fake, Plan, force_prompt=True)

    assert structured.using_native is False
    assert structured.invoke([HumanMessage(content="x")]) == Plan(goal="归档")
    assert fake.native_builds == 0
    assert fake.native_calls == 0
    assert len(fake.prompt_calls) == 1


def test_force_prompt_false_keeps_native_first() -> None:
    fake = _FakeLLM(native_behavior=Plan(goal="原生"))
    structured = build_structured_llm(fake, Plan, force_prompt=False)

    assert structured.invoke([HumanMessage(content="x")]) == Plan(goal="原生")
    assert structured.using_native is True
    assert fake.native_builds == 1
    assert fake.prompt_calls == []


def test_manual_downgrade_does_not_prevent_later_auto_native() -> None:
    """手动降级是每次构造时的决定，不应给后续 auto 实例留下粘性状态。"""
    forced = _FakeLLM(replies=[_payload()])
    build_structured_llm(forced, Plan, force_prompt=True).invoke([HumanMessage(content="x")])

    auto = _FakeLLM(native_behavior=Plan(goal="原生"))
    structured = build_structured_llm(auto, Plan, force_prompt=False)
    assert structured.invoke([HumanMessage(content="x")]) == Plan(goal="原生")
    assert structured.using_native is True


# ------------------------------- 降级判定 -------------------------------


def test_capability_error_classification() -> None:
    assert llm_module._is_provider_capability_error(BadRequest("unknown parameter: tools")) is True
    assert llm_module._is_provider_capability_error(BadRequest("invalid model name")) is False
    assert llm_module._is_provider_capability_error(NotImplementedError()) is True
    assert llm_module._is_provider_capability_error(ServerError("x")) is False
    assert llm_module._is_provider_capability_error(TimeoutError()) is False
