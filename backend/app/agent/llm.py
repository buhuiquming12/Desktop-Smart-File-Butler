"""LLM 工厂：按 MODEL_PROVIDER 环境变量返回 OpenAI 或 Ollama 的 ChatModel。

结构化输出不假设 provider 具备 function calling 能力：见 ``build_structured_llm``。
"""
from __future__ import annotations

import json
import re
from typing import Any, List, Optional, Sequence, Type, TypeVar

from langchain_core.exceptions import OutputParserException
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from pydantic import BaseModel, ValidationError

from ..llm_config import get_effective_config
from ..logging_conf import get_logger

logger = get_logger(__name__)

_T = TypeVar("_T", bound=BaseModel)


def build_llm(temperature: float = 0.1):
    """构造聊天模型。支持 openai / ollama。配置来自 .env 与前端保存的覆盖项。"""
    config = get_effective_config()
    provider = config.provider

    if provider == "openai":
        from langchain_openai import ChatOpenAI

        if not config.openai_api_key:
            raise RuntimeError("provider=openai 但未配置 API Key（请在设置界面或 .env 中填写）")
        kwargs = dict(
            model=config.openai_model,
            api_key=config.openai_api_key,
            temperature=temperature,
        )
        if config.openai_base_url:
            kwargs["base_url"] = config.openai_base_url
        logger.info("使用 OpenAI 模型: %s", config.openai_model)
        return ChatOpenAI(**kwargs)

    if provider == "ollama":
        from langchain_community.chat_models import ChatOllama

        logger.info("使用 Ollama 模型: %s", config.ollama_model)
        return ChatOllama(
            model=config.ollama_model,
            base_url=config.ollama_base_url,
            temperature=temperature,
        )

    raise ValueError(f"未知 provider: {provider}")


# --------------------------- 提示词驱动的 JSON 结构化输出 ---------------------------

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


def _invoke(
    runnable: Any, payload: Any, config: Any = None, **kwargs: Any
) -> Any:
    """调用 runnable，且只在确有额外参数时才转发。

    不少 runnable（含测试替身）签名是 ``invoke(input)`` 单参数，
    无条件按位置传 config 会把它们打挂，所以这里保持最少参数调用。
    """
    if config is None and not kwargs:
        return runnable.invoke(payload)
    return runnable.invoke(payload, config, **kwargs)


def _message_text(message: Any) -> str:
    """取出消息文本，兼容 content 为字符串或内容块列表两种形态。"""
    content = getattr(message, "content", message)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks: List[str] = []
        for block in content:
            if isinstance(block, str):
                chunks.append(block)
            elif isinstance(block, dict) and isinstance(block.get("text"), str):
                chunks.append(block["text"])
        return "".join(chunks)
    return "" if content is None else str(content)


def _balanced_object(text: str) -> Optional[str]:
    """截取 text 中第一个括号配平的 JSON 对象；字符串字面量内的括号不参与配对。"""
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    # 循环结束仍 depth>0 说明输出被截断，补齐缺失的右括号再交给调用方解析
    return text[start:] + "}" * depth if depth > 0 else None


def parse_json_object(text: str) -> Any:
    """容错解析模型返回的 JSON 对象。

    依次尝试：```json 围栏内容 → 整段文本 → 截取最外层配对对象 → 补齐被截断的右括号。
    全部失败抛 ``ValueError``。
    """
    if not text or not text.strip():
        raise ValueError("模型返回了空内容")
    for candidate in [*_FENCE_RE.findall(text), text]:
        candidate = candidate.strip()
        if not candidate:
            continue
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass
        balanced = _balanced_object(candidate)
        if balanced:
            try:
                return json.loads(balanced)
            except json.JSONDecodeError:
                continue
    raise ValueError(f"模型输出中找不到可解析的 JSON 对象：{text[:200]!r}")


def _json_instruction(schema: Type[BaseModel]) -> str:
    schema_json = json.dumps(schema.model_json_schema(), ensure_ascii=False, indent=2)
    return (
        "输出格式（最高优先级，覆盖以上任何关于输出格式的说明）：\n"
        "只输出一个 JSON 对象。不要使用 markdown 代码块，不要输出解释、前后缀或任何多余文字。\n"
        f"该 JSON 必须符合 {schema.__name__} 的 JSON Schema：\n{schema_json}"
    )


def _repair_instruction(schema: Type[BaseModel], error: Exception) -> str:
    return (
        f"上一次输出无法解析为符合 {schema.__name__} 的 JSON，错误：{error}。\n"
        "请重新输出一个修正后的 JSON 对象，只输出 JSON 本身。"
    )


class PromptJSONStructured:
    """提示词驱动的 JSON 结构化输出，不依赖 provider 的 tools / response_format。

    部分 OpenAI 兼容服务只实现聊天补全：请求体里出现 ``tools`` 就直接 400，
    且会静默忽略 ``response_format``。这类服务只能靠提示词约束输出，再在本地解析。
    """

    def __init__(self, llm: Any, schema: Type[_T], *, max_attempts: int = 2) -> None:
        self._llm = llm
        self._schema = schema
        self._instruction = _json_instruction(schema)
        self._max_attempts = max(1, max_attempts)

    def invoke(
        self, messages: Sequence[BaseMessage], config: Any = None, **kwargs: Any
    ) -> _T:
        conversation = [*messages, HumanMessage(content=self._instruction)]
        last_error: Optional[Exception] = None
        for _ in range(self._max_attempts):
            reply = _invoke(self._llm, conversation, config, **kwargs)
            text = _message_text(reply)
            try:
                return self._schema.model_validate(parse_json_object(text))
            except (ValueError, ValidationError) as exc:
                # 把校验错误回灌给模型再试一次，比直接丢弃整轮任务更划算
                last_error = exc
                conversation = [
                    *conversation,
                    AIMessage(content=text or ""),
                    HumanMessage(content=_repair_instruction(self._schema, exc)),
                ]
        raise ValueError(
            f"模型连续 {self._max_attempts} 次未返回符合 {self._schema.__name__} 的 JSON：{last_error}"
        )


def _is_provider_capability_error(exc: BaseException) -> bool:
    """判断异常是否表示「该 provider 不支持原生结构化输出」，而非瞬时故障。

    只认服务端拒绝请求形态的信号：400 / 未实现 / 模型没按工具协议回复。
    限流、超时、连接失败等仍向上抛，避免误降级把真实原因盖掉。
    """
    if isinstance(exc, (NotImplementedError, OutputParserException)):
        return True
    return getattr(exc, "status_code", None) == 400


class StructuredOutputRunnable:
    """先走 provider 原生结构化输出，遇到「不支持」类错误后永久降级为提示词 JSON。

    ``AgentRuntime`` 是进程级单例（改模型设置才重建），所以能力探测只需付一次成本：
    首次失败即记住结论，同进程内后续调用直接走提示词路径。

    降级标记不加锁：并发首次失败只会各自判定一次，结论一致，无需串行化。
    """

    def __init__(self, llm: Any, schema: Type[_T]) -> None:
        self._schema = schema
        self._native = llm.with_structured_output(schema)
        self._prompt = PromptJSONStructured(llm, schema)
        self._use_native = True

    @property
    def using_native(self) -> bool:
        """是否仍走 provider 原生结构化输出（供诊断与测试使用）。"""
        return self._use_native

    def invoke(
        self, messages: Sequence[BaseMessage], config: Any = None, **kwargs: Any
    ) -> _T:
        if self._use_native:
            try:
                return _invoke(self._native, messages, config, **kwargs)
            except Exception as exc:  # noqa: BLE001
                if not _is_provider_capability_error(exc):
                    raise
                self._use_native = False
                logger.warning(
                    "provider 不支持原生结构化输出（%s: %s），本进程内改用提示词 JSON 模式",
                    type(exc).__name__,
                    exc,
                )
        return self._prompt.invoke(messages, config, **kwargs)


def build_structured_llm(llm: Any, schema: Type[_T]) -> StructuredOutputRunnable:
    """为 schema 构造带自动降级的结构化输出可运行对象。"""
    return StructuredOutputRunnable(llm, schema)
