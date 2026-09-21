"""有效模型配置：在 .env 默认值之上叠加前端保存到 SQLite 的覆盖项。

前端可通过设置界面写入 provider / base_url / model / api_key；这些值持久化在
``llm_config`` 表中，优先级高于 .env。API Key 存于本地库，但绝不通过接口回传前端。
"""
from __future__ import annotations

from dataclasses import dataclass

from . import db
from .config import get_settings

# 允许被前端覆盖的键。使用普通命名，避免 pydantic 的 model_ 保护命名空间告警。
_ALLOWED_KEYS = {
    "provider",
    "openai_base_url",
    "openai_model",
    "openai_api_key",
    "ollama_base_url",
    "ollama_model",
    "structured_output_mode",
}

# 结构化输出模式：auto = 原生优先、失败自动降级；prompt = 手动强制降级为提示词 JSON。
STRUCTURED_OUTPUT_MODES = ("auto", "prompt")
_DEFAULT_MODE = "auto"


@dataclass
class EffectiveLLMConfig:
    provider: str
    openai_base_url: str
    openai_model: str
    openai_api_key: str
    ollama_base_url: str
    ollama_model: str
    structured_output_mode: str


def normalize_mode(value: str) -> str:
    """把任意来源的模式值收敛到受支持取值；无法识别时回退默认值。

    DB 里可能是历史遗留或手改的值，这里兜底，避免一个坏值让结构化输出直接不可用。
    """
    candidate = (value or "").strip().lower()
    return candidate if candidate in STRUCTURED_OUTPUT_MODES else _DEFAULT_MODE


def get_effective_config() -> EffectiveLLMConfig:
    """合并 .env 默认值与 DB 覆盖，返回当前生效的模型配置。"""
    settings = get_settings()
    overrides = db.get_llm_config()

    def pick(key: str, default: str) -> str:
        value = overrides.get(key)
        return value if value else default

    return EffectiveLLMConfig(
        provider=pick("provider", settings.model_provider).lower(),
        openai_base_url=pick("openai_base_url", settings.openai_base_url or ""),
        openai_model=pick("openai_model", settings.openai_model),
        openai_api_key=pick("openai_api_key", settings.openai_api_key),
        ollama_base_url=pick("ollama_base_url", settings.ollama_base_url),
        ollama_model=pick("ollama_model", settings.ollama_model),
        structured_output_mode=normalize_mode(
            pick("structured_output_mode", settings.structured_output_mode)
        ),
    )


def save_overrides(values: dict[str, str]) -> None:
    """仅保存受支持的键；其余忽略。空字符串表示清除该覆盖、回退到 .env。"""
    filtered = {k: v for k, v in values.items() if k in _ALLOWED_KEYS}
    if filtered:
        db.set_llm_config(filtered)
