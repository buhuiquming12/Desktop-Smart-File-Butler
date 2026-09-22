"""全局配置：从环境变量 / .env 读取。"""
from __future__ import annotations

from functools import lru_cache
import json
from pathlib import Path
from typing import List

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        protected_namespaces=("settings_",),
    )

    # 模型
    model_provider: str = "openai"
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"
    openai_base_url: str | None = None
    ollama_base_url: str = "http://127.0.0.1:11434"
    ollama_model: str = "qwen2.5"

    # 结构化输出模式：auto（原生优先，失败自动降级）| prompt（手动降级，直接走提示词 JSON）
    structured_output_mode: str = "auto"

    # 安全沙箱：用 ; 分隔的目录列表
    sandbox_roots: str = ""

    # OCR
    tesseract_cmd: str = ""

    # 存储
    db_path: str = "./data/butler.db"
    log_dir: str = "./logs"

    # 服务
    host: str = "127.0.0.1"
    port: int = 8000

    @property
    def sandbox_root_paths(self) -> List[Path]:
        """解析并规范化允许操作的根目录列表。"""
        roots: List[Path] = []
        try:
            decoded = json.loads(self.sandbox_roots)
            values = decoded if isinstance(decoded, list) else []
        except (json.JSONDecodeError, TypeError):
            values = self.sandbox_roots.split(";")
        for raw in values:
            raw = raw.strip()
            if not raw:
                continue
            try:
                roots.append(Path(raw).expanduser().resolve())
            except (OSError, RuntimeError):
                # 无法解析的路径跳过，避免启动崩溃
                continue
        return roots

    def ensure_dirs(self) -> None:
        """确保数据 / 日志目录存在。"""
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        Path(self.log_dir).mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_dirs()
    return settings
