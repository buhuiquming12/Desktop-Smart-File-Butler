"""有效外部工具配置：DB 覆盖 > .env 默认 > 系统默认（PATH / 工具自身查找）。

与 ``llm_config`` / ``sandbox_config`` 同构：桌面应用允许用户在设置界面里指定本机
外部工具（当前是 Tesseract）的路径，而不必手改 backend/.env。所有外部工具的路径配置
都集中在这里，不再散落到各业务文件里读 ``get_settings()``。

约定：
- 空字符串表示清除该键的覆盖，回退到 .env；
- 只保存**可执行文件路径 / 数据目录**，不接受任何命令行参数，保存时也只校验
  “存在且类型正确”。传给外部程序的参数由使用方自行拼装（见 tools/extract.py）；
- 与沙箱无关：tesseract.exe 是外部程序，不是 Agent 的文件操作目标，因此**不经过**
  ``security.resolve_in_sandbox``，也不需要位于授权目录内；
- **故意不加缓存**：设置界面保存后必须立即生效（与 sandbox_config 同样的取舍）。

新增工具（ffmpeg / pandoc / libreoffice …）只需在 ``_SPECS`` 里追加一条，
API 与前端即按同一套键值读写。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import db
from .config import get_settings

_MAX_PATH_LEN = 500


@dataclass(frozen=True)
class ToolSpec:
    """一个可配置的外部工具路径条目。"""
    key: str      # 数据库 / API 中的键名
    tool: str     # 所属外部工具，便于按工具分组展示与未来扩展
    label: str    # 中文名，用于人类可读的报错
    kind: str     # file = 可执行文件；dir = 数据目录


_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec("tesseract_cmd", "tesseract", "Tesseract 可执行文件", "file"),
    ToolSpec("tessdata_dir", "tesseract", "Tesseract 语言包目录", "dir"),
)
_SPEC_BY_KEY = {spec.key: spec for spec in _SPECS}
ALLOWED_KEYS: tuple[str, ...] = tuple(spec.key for spec in _SPECS)

# 生效来源：database = 界面保存的覆盖项；env = .env 默认值。
SOURCE_DATABASE = "database"
SOURCE_ENV = "env"


@dataclass
class EffectiveExternalToolsConfig:
    """当前生效的外部工具配置；空串表示交给系统默认查找。"""
    tesseract_cmd: str
    tessdata_dir: str


class InvalidToolConfig(ValueError):
    """提交的外部工具路径不存在、类型不对或路径本身非法。"""


def tool_specs() -> tuple[ToolSpec, ...]:
    return _SPECS


def validate_overrides(values: dict[str, str]) -> dict[str, str]:
    """校验并规范化待保存的覆盖项；空串表示清除覆盖（回退 .env）。

    返回规范化后的绝对路径字符串。任一项非法都抛 ``InvalidToolConfig``，
    调用方（REST 层）据此返回 422 与人类可读的中文提示。
    未知键直接忽略，与 ``llm_config.save_overrides`` 保持一致。
    """
    normalized: dict[str, str] = {}
    for key, raw in values.items():
        spec = _SPEC_BY_KEY.get(key)
        if spec is None:
            continue
        path = _validate_one(spec, raw)
        normalized[key] = str(path) if path is not None else ""
    return normalized


def _validate_one(spec: ToolSpec, raw: str) -> Path | None:
    value = (raw or "").strip()
    if not value:
        return None
    if len(value) > _MAX_PATH_LEN:
        raise InvalidToolConfig(f"{spec.label}路径过长（最多 {_MAX_PATH_LEN} 个字符）")
    if "\0" in value:
        raise InvalidToolConfig(f"{spec.label}路径包含非法字符")
    try:
        path = Path(value).expanduser().resolve()
    except (OSError, RuntimeError) as exc:
        raise InvalidToolConfig(f"{spec.label}路径无法解析：{value}") from exc
    if spec.kind == "file" and not path.is_file():
        raise InvalidToolConfig(f"{spec.label}不存在：{path}")
    if spec.kind == "dir" and not path.is_dir():
        raise InvalidToolConfig(f"{spec.label}不存在：{path}")
    return path


def get_effective_config() -> EffectiveExternalToolsConfig:
    """合并 .env 默认值与 DB 覆盖，返回当前生效的外部工具配置。"""
    settings = get_settings()
    overrides = db.get_tool_config()

    def pick(key: str, default: str) -> str:
        value = (overrides.get(key) or "").strip()
        return value or (default or "").strip()

    return EffectiveExternalToolsConfig(
        tesseract_cmd=pick("tesseract_cmd", settings.tesseract_cmd),
        tessdata_dir=pick("tessdata_dir", settings.tessdata_dir),
    )


def save_overrides(values: dict[str, str]) -> None:
    """仅保存受支持的键；其余忽略。空字符串表示清除该覆盖、回退到 .env。"""
    filtered = {key: value for key, value in values.items() if key in _SPEC_BY_KEY}
    if filtered:
        db.set_tool_config(filtered)


def sources() -> dict[str, str]:
    """每个键当前生效来源（database / env），供界面提示“当前来自 .env”。"""
    overrides = db.get_tool_config()
    return {
        key: (SOURCE_DATABASE if (overrides.get(key) or "").strip() else SOURCE_ENV)
        for key in ALLOWED_KEYS
    }
