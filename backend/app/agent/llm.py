"""LLM 工厂：按 MODEL_PROVIDER 环境变量返回 OpenAI 或 Ollama 的 ChatModel。"""
from __future__ import annotations

from ..config import get_settings
from ..logging_conf import get_logger

logger = get_logger(__name__)


def build_llm(temperature: float = 0.1):
    """构造聊天模型。支持 openai / ollama。"""
    settings = get_settings()
    provider = settings.model_provider.lower()

    if provider == "openai":
        from langchain_openai import ChatOpenAI

        if not settings.openai_api_key:
            raise RuntimeError("MODEL_PROVIDER=openai 但未配置 OPENAI_API_KEY")
        kwargs = dict(
            model=settings.openai_model,
            api_key=settings.openai_api_key,
            temperature=temperature,
        )
        if settings.openai_base_url:
            kwargs["base_url"] = settings.openai_base_url
        logger.info("使用 OpenAI 模型: %s", settings.openai_model)
        return ChatOpenAI(**kwargs)

    if provider == "ollama":
        from langchain_community.chat_models import ChatOllama

        logger.info("使用 Ollama 模型: %s", settings.ollama_model)
        return ChatOllama(
            model=settings.ollama_model,
            base_url=settings.ollama_base_url,
            temperature=temperature,
        )

    raise ValueError(f"未知 MODEL_PROVIDER: {provider}")
