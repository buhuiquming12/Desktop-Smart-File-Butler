"""默认管理目录：用户没有指定路径时，Agent 默认整理的位置。

与 ``sandbox_config`` 的分工：
- ``sandbox_config.effective_roots()`` 是**授权目录**，即最大的安全边界；所有实际
  磁盘操作仍由 ``security.resolve_in_sandbox`` 把关，Agent 绝对不能越界；
- ``default_managed_root`` 只是产品默认值：用户说“整理一下文件”而没给路径时，
  Agent 应该操作哪里。它本身**不授予任何权限**——必须位于某个授权目录内，越界时
  一律视为“未配置”，由 Agent 提示用户指定目录，绝不因此放宽沙箱校验。

存于 ``preferences`` 表的保留键 ``__default_managed_root__``：``__`` 前缀对普通偏好
接口（PUT /preferences/{key}）与 Agent 的 set_preference 都不可写，见
``db.validate_public_preference_key``。
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from . import db
from .logging_conf import get_logger
from .security import is_within_sandbox

logger = get_logger(__name__)

DEFAULT_ROOT_KEY = "__default_managed_root__"


class InvalidDefaultRoot(ValueError):
    """提交的默认管理目录不存在，或不在任何授权目录内。"""


def _resolve(raw: str) -> Optional[Path]:
    value = (raw or "").strip()
    if not value:
        return None
    try:
        return Path(value).expanduser().resolve()
    except (OSError, RuntimeError) as exc:
        logger.warning("默认管理目录无法解析: %s (%s)", value, exc)
        return None


def stored_default_root() -> Optional[Path]:
    """只解析存储值，不判断是否仍在授权目录内（界面据此提示“已失效”）。"""
    return _resolve(db.get_preference(DEFAULT_ROOT_KEY) or "")


def effective_default_root() -> Optional[Path]:
    """当前真正生效的默认管理目录；未配置 / 已失效时返回 None。

    授权目录变更后，原先保存的默认目录可能已经越界（甚至被删除）。此时返回 None
    等于“未配置”，Agent 会要求用户明确指定目录——安全优先于便利。
    """
    stored = stored_default_root()
    if stored is None:
        return None
    if not is_within_sandbox(stored):
        logger.warning("默认管理目录不在任何授权目录内，已忽略: %s", stored)
        return None
    if not stored.is_dir():
        logger.warning("默认管理目录已不存在，已忽略: %s", stored)
        return None
    return stored


def save_default_root(raw: str) -> Optional[Path]:
    """校验并保存默认管理目录；空串表示清除。返回保存后的路径（清除时为 None）。

    校验顺序：路径可解析 → 是已存在的目录 → 位于某个授权目录内。
    非法值抛 ``InvalidDefaultRoot``，由 REST 层转成 422 与中文提示。
    """
    if not (raw or "").strip():
        clear_default_root()
        return None
    path = _resolve(raw)
    if path is None:
        raise InvalidDefaultRoot(f"默认管理目录路径无法解析：{raw}")
    if not path.is_dir():
        raise InvalidDefaultRoot(f"默认管理目录不存在或不是目录：{path}")
    if not is_within_sandbox(path):
        # 局部导入：sandbox_config 与本模块相互引用，避免模块级循环 import。
        from .sandbox_config import effective_roots

        allowed = ", ".join(str(item) for item in effective_roots()) or "(未配置)"
        raise InvalidDefaultRoot(
            f"默认管理目录必须位于授权目录内：{path}。当前授权目录：{allowed}"
        )
    db.set_preference(DEFAULT_ROOT_KEY, str(path), allow_reserved=True)
    logger.info("默认管理目录已更新: %s", path)
    return path


def clear_default_root() -> None:
    """清除默认管理目录（回退为“未配置”）。"""
    db.set_preference(DEFAULT_ROOT_KEY, "", allow_reserved=True)


def prune_out_of_bounds() -> bool:
    """授权目录变更后调用：默认管理目录若已越界就清除，返回是否清除过。

    放在这里而不是 security / sandbox_config，是为了让“默认目录永远不能越界”这条
    不变量只有一处实现：读取时忽略（effective_default_root）+ 写入时清除（本函数）。
    """
    stored = stored_default_root()
    if stored is None or is_within_sandbox(stored):
        return False
    logger.info("授权目录已变更，清除越界的默认管理目录: %s", stored)
    clear_default_root()
    return True
