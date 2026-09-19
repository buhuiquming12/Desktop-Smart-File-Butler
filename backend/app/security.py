"""安全层：路径沙箱校验 + 危险操作分级。

所有文件工具在触碰磁盘前，必须先经过 ``resolve_in_sandbox`` 校验，
确保目标路径落在用户授权的根目录内，越界立即抛出 ``SandboxViolation``。
"""
from __future__ import annotations

from pathlib import Path
from typing import List

from .config import get_settings
from .logging_conf import get_logger

logger = get_logger(__name__)


class SandboxViolation(Exception):
    """试图访问沙箱外的路径。"""


def _sandbox_roots() -> List[Path]:
    roots = get_settings().sandbox_root_paths
    if not roots:
        logger.warning("SANDBOX_ROOTS 未配置，所有文件操作都会被拒绝")
    return roots


def is_within_sandbox(path: Path, roots: List[Path] | None = None) -> bool:
    """判断 path 是否位于任一授权根目录内（含根目录本身）。"""
    roots = roots if roots is not None else _sandbox_roots()
    try:
        resolved = path.expanduser().resolve()
    except (OSError, RuntimeError):
        return False
    for root in roots:
        try:
            resolved.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def sandbox_root_for(path: Path, roots: List[Path] | None = None) -> Path:
    """返回包含 path 的沙箱根目录；不在任何根内则抛 SandboxViolation。"""
    roots = roots if roots is not None else _sandbox_roots()
    try:
        resolved = path.expanduser().resolve()
    except (OSError, RuntimeError) as exc:
        raise SandboxViolation(f"无法解析路径: {path} ({exc})") from exc
    for root in roots:
        try:
            resolved.relative_to(root)
            return root
        except ValueError:
            continue
    allowed = ", ".join(str(r) for r in roots) or "(未配置)"
    raise SandboxViolation(f"路径越界，拒绝访问: {resolved}. 允许的根目录: {allowed}")


def resolve_in_sandbox(raw_path: str, *, must_exist: bool = False) -> Path:
    """将用户/Agent 提供的路径解析为绝对路径，并校验沙箱边界。

    ``Path.resolve`` 会解析已有父级中的符号链接/Windows 重解析点，再进行
    ``relative_to`` 判断，因此通过链接跳出授权根目录的路径也会被拒绝。

    Raises:
        SandboxViolation: 路径越界。
        FileNotFoundError: must_exist=True 且路径不存在。
    """
    roots = _sandbox_roots()
    source = Path(raw_path).expanduser()
    try:
        resolved = source.resolve(strict=must_exist)
    except FileNotFoundError:
        raise
    except (OSError, RuntimeError) as exc:
        raise SandboxViolation(f"无法解析路径: {raw_path} ({exc})") from exc

    if not is_within_sandbox(resolved, roots):
        allowed = ", ".join(str(r) for r in roots) or "(未配置)"
        raise SandboxViolation(
            f"路径越界，拒绝访问: {resolved}. 允许的根目录: {allowed}"
        )

    if must_exist and not resolved.exists():
        raise FileNotFoundError(f"路径不存在: {resolved}")

    return resolved
