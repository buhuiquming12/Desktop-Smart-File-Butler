"""LLM 工厂：按 MODEL_PROVIDER 环境变量返回 OpenAI 或 Ollama 的 ChatModel。"""
from __future__ import annotations

from ..llm_config import get_effective_config
from ..logging_conf import get_logger

logger = get_logger(__name__)


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
