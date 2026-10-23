"""有效模型配置：在 .env 默认值之上叠加前端保存到 SQLite 的覆盖项。

安全约束（本地密钥保护）
------------------------
- API Key **不再**写入 ``llm_config`` 明文，而是保存到 ``SecretStore``
  （Windows DPAPI 密文，见 ``app/security/secrets.py``）。
- 历史版本写入 SQLite 的明文 Key 会在第一次读取时迁移：写入 SecretStore 并回读
  校验成功后才删除旧记录；失败则保留旧值并返回可诊断信息（绝不打印密钥）。
- ``get_effective_config`` 每次按需从 SecretStore 读取，不在进程内长期缓存完整密钥。
- 非 Windows 环境没有持久化存储时 fail closed：需要显式注入 SecretStore 或使用
  ``.env`` 的 ``OPENAI_API_KEY``；不会退化为明文文件。
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any

from . import db
from .config import get_settings
from .logging_conf import get_logger
from .security.secrets import (
    SecretStoreError,
    get_secret_store,
    is_secret_name,
    redact_text,
    register_secret,
)

logger = get_logger(__name__)

# SecretStore 中的键名；数据库里的历史键名保持不变以兼容既有安装。
SECRET_API_KEY = "llm.openai_api_key"
_LEGACY_API_KEY = "openai_api_key"

# 允许被前端覆盖的**非密钥**键。使用普通命名，避免 pydantic 的 model_ 保护命名空间告警。
_ALLOWED_KEYS = {
    "provider",
    "openai_base_url",
    "openai_model",
    "ollama_base_url",
    "ollama_model",
    "structured_output_mode",
}

# 结构化输出模式：auto = 原生优先、失败自动降级；prompt = 手动强制降级为提示词 JSON。
STRUCTURED_OUTPUT_MODES = ("auto", "prompt")
_DEFAULT_MODE = "auto"

_MIGRATION_LOCK = threading.Lock()
_migration: dict[str, Any] = {
    "status": "unknown",
    "message": "",
    "attempted": False,
}


@dataclass
class EffectiveLLMConfig:
    provider: str
    openai_base_url: str
    openai_model: str
    openai_api_key: str
    ollama_base_url: str
    ollama_model: str
    structured_output_mode: str
    # 诊断信息：只描述来源/状态，不包含任何密钥字符。
    api_key_source: str = "unset"
    api_key_diagnostic: str = ""


def normalize_mode(value: str) -> str:
    """把任意来源的模式值收敛到受支持取值；无法识别时回退默认值。

    DB 里可能是历史遗留或手改的值，这里兜底，避免一个坏值让结构化输出直接不可用。
    """
    candidate = (value or "").strip().lower()
    return candidate if candidate in STRUCTURED_OUTPUT_MODES else _DEFAULT_MODE


# ---------- 历史明文迁移 ----------

def migration_status() -> dict[str, Any]:
    """当前迁移状态（safe: 不含密钥）。"""
    with _MIGRATION_LOCK:
        return {
            "status": str(_migration.get("status") or "unknown"),
            "message": str(_migration.get("message") or ""),
            "attempted": bool(_migration.get("attempted")),
        }


def _set_migration(status: str, message: str = "") -> None:
    _migration.update(status=status, message=redact_text(message), attempted=True)


def reset_migration_state() -> None:
    """测试/诊断用：清空进程内迁移状态，让下一次读取重新尝试迁移。"""
    _migration.update(status="unknown", message="", attempted=False)


def migrate_legacy_api_key(*, force: bool = False) -> dict[str, Any]:
    """把 SQLite 中的历史明文 Key 迁移到 SecretStore。

    只有“写入后回读一致”才删除旧记录；任何失败都保留旧值并记录诊断（不含密钥）。
    ``force=True`` 用于前端显式“重试迁移”，否则每个进程只自动尝试一次。
    """
    with _MIGRATION_LOCK:
        if _migration.get("attempted") and not force:
            return {
                "status": str(_migration.get("status") or "unknown"),
                "message": str(_migration.get("message") or ""),
                "attempted": True,
            }
        legacy = db.get_llm_config_value(_LEGACY_API_KEY)
        if not legacy:
            _migration.update(status="not_needed", message="", attempted=True)
            return dict(_migration)

        store = get_secret_store()
        try:
            if store.is_configured(SECRET_API_KEY):
                existing = store.get_secret(SECRET_API_KEY)
                if existing == legacy:
                    # 之前已迁移成功，只是数据库残留未清：确认可读后再清理。
                    db.delete_llm_config_key(_LEGACY_API_KEY)
                    db.scrub_llm_config_plaintext()
                    register_secret(legacy)
                    _migration.update(
                        status="migrated",
                        message="历史明文 API Key 已存在于本地密钥存储，已清理数据库旧记录。",
                        attempted=True,
                    )
                else:
                    # 用户后来重新配置过：新密钥优先，旧明文记录是过期残留。
                    db.delete_llm_config_key(_LEGACY_API_KEY)
                    db.scrub_llm_config_plaintext()
                    register_secret(existing or "")
                    _migration.update(
                        status="superseded",
                        message="本地密钥存储已有更新的密钥，已清理数据库中的历史明文记录。",
                        attempted=True,
                    )
                return dict(_migration)

            store.set_secret(SECRET_API_KEY, legacy)
            readback = store.get_secret(SECRET_API_KEY)
            if readback != legacy:
                raise SecretStoreError("本地密钥存储回读校验不一致")
        except SecretStoreError as exc:
            # 关键：保留 SQLite 旧值，绝不因为存储失败而丢密钥。
            _migration.update(
                status="failed",
                message=(
                    "本地密钥迁移失败："
                    + redact_text(str(exc))
                    + "。旧明文记录仍保留在本地数据库中；"
                    "请检查本地密钥存储后重试迁移，或直接重新配置密钥。"
                ),
                attempted=True,
            )
            logger.warning("API Key 迁移到本地密钥存储失败，已保留旧记录: %s", redact_text(str(exc)))
            return dict(_migration)
        except Exception as exc:  # noqa: BLE001 - 任何异常都不得泄露密钥或删除旧值
            _migration.update(
                status="failed",
                message=(
                    f"本地密钥迁移失败（{type(exc).__name__}）。"
                    "旧明文记录仍保留；请重试迁移或重新配置密钥。"
                ),
                attempted=True,
            )
            logger.warning("API Key 迁移异常，已保留旧记录: %s", type(exc).__name__)
            return dict(_migration)

        db.delete_llm_config_key(_LEGACY_API_KEY)
        db.scrub_llm_config_plaintext()
        register_secret(legacy)
        _migration.update(
            status="migrated",
            message="已将历史明文 API Key 迁移到本地密钥存储，并清除数据库中的旧记录。",
            attempted=True,
        )
        logger.info("已完成 API Key 明文迁移（密钥内容不记录）")
        return dict(_migration)


# ---------- 密钥读写 ----------

def resolve_api_key() -> tuple[str, str, str]:
    """按需解析当前 API Key。

    返回 ``(value, source, diagnostic)``；``source`` 取值：
    ``secret_store`` / ``env`` / ``migration_failed`` / ``unavailable`` / ``unset``。
    任何失败都不抛密钥相关内容，只给出可诊断的脱敏说明。
    """
    migrate_legacy_api_key()
    store = get_secret_store()
    try:
        if store.is_configured(SECRET_API_KEY):
            value = store.get_secret(SECRET_API_KEY) or ""
            if value:
                register_secret(value)
                return value, "secret_store", ""
    except SecretStoreError as exc:
        diagnostic = "本地密钥读取失败：" + redact_text(str(exc))
        logger.warning("本地密钥读取失败，按未配置处理: %s", redact_text(str(exc)))
        return "", "unavailable", diagnostic

    status = migration_status()
    if status["status"] == "failed":
        return "", "migration_failed", status["message"]

    env_value = get_settings().openai_api_key or ""
    if env_value:
        register_secret(env_value)
        return env_value, "env", ""
    return "", "unset", ""


def api_key_configured() -> bool:
    value, _, _ = resolve_api_key()
    return bool(value)


def set_api_key(value: str) -> None:
    """保存/轮换 API Key：写入 SecretStore，并在成功后清理历史明文记录。"""
    key = (value or "").strip()
    if not key:
        raise ValueError("API Key 不能为空；如需清除请使用清除接口")
    store = get_secret_store()
    store.set_secret(SECRET_API_KEY, key)
    readback = store.get_secret(SECRET_API_KEY)
    if readback != key:
        raise SecretStoreError("本地密钥写入后回读校验不一致")
    register_secret(key)
    # 新密钥已被安全保存，历史明文记录不再需要（并抹掉页内残留字节）。
    db.delete_llm_config_key(_LEGACY_API_KEY)
    db.scrub_llm_config_plaintext()
    _set_migration("configured", "API Key 已保存到本地密钥存储。")
    logger.info("API Key 已更新到本地密钥存储（内容不记录）")


def rotate_api_key(value: str) -> None:
    """轮换 API Key（与保存同路径，语义上要求显式调用）。"""
    key = (value or "").strip()
    if not key:
        raise ValueError("API Key 不能为空")
    get_secret_store().rotate_secret(SECRET_API_KEY, key)
    register_secret(key)
    db.delete_llm_config_key(_LEGACY_API_KEY)
    db.scrub_llm_config_plaintext()
    _set_migration("rotated", "API Key 已轮换。")


def clear_api_key() -> bool:
    """清除本地密钥与历史明文记录；返回是否确实清除了密钥。"""
    removed = False
    try:
        removed = bool(get_secret_store().delete_secret(SECRET_API_KEY))
    except SecretStoreError as exc:
        # 删除失败不能静默：返回给调用方诊断，但仍尝试清理数据库明文。
        _set_migration("clear_failed", "清除本地密钥失败：" + redact_text(str(exc)))
        raise
    removed = db.delete_llm_config_key(_LEGACY_API_KEY) or removed
    db.scrub_llm_config_plaintext()
    _set_migration("cleared", "API Key 已清除。")
    logger.info("API Key 已清除（内容不记录）")
    return removed


def api_key_info() -> dict[str, Any]:
    """REST 响应用的密钥状态（绝不含明文）：只返回是否已配置与存储后端。"""
    value, source, diagnostic = resolve_api_key()
    store = get_secret_store()
    migration = migration_status()
    info: dict[str, Any] = {
        "configured": bool(value),
        "source": source,
        "backend": getattr(store, "backend", "unknown"),
        "persistent": bool(getattr(store, "persistent", False)),
        "migration": migration,
    }
    if diagnostic:
        info["diagnostic"] = redact_text(diagnostic)
    return info


# ---------- 有效配置 ----------

def get_effective_config() -> EffectiveLLMConfig:
    """合并 .env 默认值与 DB 覆盖，返回当前生效的模型配置。"""
    settings = get_settings()
    overrides = db.get_llm_config()
    api_key, api_key_source, api_key_diagnostic = resolve_api_key()

    def pick(key: str, default: str) -> str:
        value = overrides.get(key)
        return value if value else default

    return EffectiveLLMConfig(
        provider=pick("provider", settings.model_provider).lower(),
        openai_base_url=pick("openai_base_url", settings.openai_base_url or ""),
        openai_model=pick("openai_model", settings.openai_model),
        openai_api_key=api_key,
        ollama_base_url=pick("ollama_base_url", settings.ollama_base_url),
        ollama_model=pick("ollama_model", settings.ollama_model),
        structured_output_mode=normalize_mode(
            pick("structured_output_mode", settings.structured_output_mode)
        ),
        api_key_source=api_key_source,
        api_key_diagnostic=api_key_diagnostic,
    )


def save_overrides(values: dict[str, str]) -> None:
    """仅保存受支持的非密钥键；其余忽略。空字符串表示清除覆盖、回退 .env。

    密钥必须走 ``set_api_key`` / ``clear_api_key`` / ``rotate_api_key``：即使调用方
    误传 ``openai_api_key``，这里也只会转交 SecretStore，绝不会写回 SQLite 明文。
    """
    filtered = {
        k: v
        for k, v in values.items()
        if k in _ALLOWED_KEYS and not is_secret_name(k)
    }
    if filtered:
        db.set_llm_config(filtered)
    if _LEGACY_API_KEY in values:
        value = str(values.get(_LEGACY_API_KEY) or "").strip()
        if value:
            set_api_key(value)
        else:
            clear_api_key()
