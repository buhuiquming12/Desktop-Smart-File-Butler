"""本地密钥存储（SecretStore）与日志脱敏。

安全目标
--------
- API Key **不**写入 SQLite 明文，也不进入 API 响应、日志、异常、WebSocket 事件、
  checkpoint、审计记录或前端状态。
- Windows 使用 DPAPI（当前用户作用域）加密后落盘；密文只能由同一 Windows 用户解开。
- 非 Windows / 无 DPAPI 的环境默认 fail closed（不可用），但提供可注入的
  ``MemorySecretStore`` 供测试与本地开发使用，绝不依赖真实系统凭据管理器。
- 平台相关 API 只在真正调用时导入，避免 Ubuntu CI 因导入失败而无法跑测试。

设计约束
--------
- ``get_secret`` 按需解密，不做进程级明文缓存；调用方用完即弃。
- 任何错误信息都不包含密钥本身；测试可注入读/写失败以验证迁移不会误删旧值。
- 明文值一旦被读取就登记到脱敏表，``redact_text`` 负责在出口处兜底替换。
"""
from __future__ import annotations

import hashlib
import os
import re
import sys
import tempfile
import threading
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from ..logging_conf import get_logger

logger = get_logger(__name__)

# OpenAI 及多数兼容服务的 Key 形态；用于日志兜底脱敏（不依赖已读取到的具体值）。
_KEY_PATTERN = re.compile(r"\bsk-[A-Za-z0-9_\-]{8,}\b")
_REDACTED = "***REDACTED***"

# 当前进程内已见过的密钥明文，仅用于日志/事件出口替换，不参与业务逻辑。
_known_secrets: set[str] = set()
_known_lock = threading.Lock()
_MAX_KNOWN = 8


class SecretStoreError(Exception):
    """密钥存储不可用或操作失败（消息中绝不包含密钥本身）。"""


class SecretStoreUnavailable(SecretStoreError):
    """当前平台/环境没有可用的持久化密钥存储。"""


def register_secret(value: Optional[str]) -> None:
    """登记一个密钥明文，供 ``redact_text`` 在日志/事件出口兜底脱敏。"""
    if not value or len(value) < 4:
        return
    with _known_lock:
        _known_secrets.add(value)
        while len(_known_secrets) > _MAX_KNOWN:
            _known_secrets.pop()


def clear_registered_secrets() -> None:
    """测试用：清空脱敏登记表。"""
    with _known_lock:
        _known_secrets.clear()


def redact_text(text: Any) -> str:
    """把已知密钥与常见 Key 形态替换为 ``***REDACTED***``。"""
    value = "" if text is None else str(text)
    if not value:
        return value
    with _known_lock:
        secrets = sorted(_known_secrets, key=len, reverse=True)
    # 快路径：没有登记过密钥、也不含 Key 形态前缀时，字符串一定不需要改写。
    # 这条路径被 checkpoint 落盘（每个节点返回值）反复调用，必须足够便宜。
    if not secrets and "sk-" not in value:
        return value
    for secret in secrets:
        if secret and secret in value:
            value = value.replace(secret, _REDACTED)
    return _KEY_PATTERN.sub(_REDACTED, value)


def redact_payload(value: Any, *, depth: int = 0) -> Any:
    """递归脱敏 JSON 可序列化结构；超过深度或未知类型按 ``str`` 处理。

    没有任何内容被改写时返回**原对象**（结构共享）：checkpoint 落盘前会反复处理
    同一份观察列表，逐层复制会让长任务变成 O(n²)。
    """
    if depth > 12:
        return value
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        changed = False
        cleaned: Dict[Any, Any] = {}
        for key, item in value.items():
            new_key = redact_text(key)
            new_value = redact_payload(item, depth=depth + 1)
            if new_key is not key or new_value is not item:
                changed = True
            cleaned[new_key] = new_value
        return cleaned if changed else value
    if isinstance(value, (list, tuple)):
        items = [redact_payload(item, depth=depth + 1) for item in value]
        if all(new is old for new, old in zip(items, value)):
            return value
        return items
    return value


class SecretStore(ABC):
    """密钥存储抽象：保存 / 读取 / 删除 / 轮换 / 是否已配置。

    实现必须保证：``set_secret`` 成功返回前，密文已经可被同一存储读回；
    ``get_secret`` 失败时抛 ``SecretStoreError``，绝不返回空串冒充成功。
    """

    #: 供界面展示的存储后端标识（不含任何密钥信息）。
    backend: str = "unknown"
    #: 是否跨进程/重启持久化。
    persistent: bool = False

    @abstractmethod
    def is_configured(self, name: str) -> bool:
        """是否存在该密钥（只做存在性检查，不要求解密）。"""

    @abstractmethod
    def get_secret(self, name: str) -> Optional[str]:
        """读取密钥明文；不存在返回 None，读取失败抛 ``SecretStoreError``。"""

    @abstractmethod
    def set_secret(self, name: str, value: str) -> None:
        """保存或覆盖密钥。"""

    @abstractmethod
    def delete_secret(self, name: str) -> bool:
        """删除密钥，返回是否确有删除。"""

    def rotate_secret(self, name: str, value: str) -> None:
        """轮换：以新值覆盖旧值（默认与保存等价，持久化实现可加校验）。"""
        self.set_secret(name, value)

    def describe(self) -> Dict[str, Any]:
        """安全的元信息（绝不包含密钥）。"""
        return {"backend": self.backend, "persistent": bool(self.persistent)}


class MemorySecretStore(SecretStore):
    """可注入的内存实现：测试/本地开发替身，不触碰任何系统凭据管理器。"""

    backend = "memory"
    persistent = False

    def __init__(
        self,
        values: Optional[Dict[str, str]] = None,
        *,
        fail_reads: bool = False,
        fail_writes: bool = False,
    ) -> None:
        self._values: Dict[str, str] = dict(values or {})
        self.fail_reads = fail_reads
        self.fail_writes = fail_writes
        self.reads = 0
        self.writes = 0

    def is_configured(self, name: str) -> bool:
        return bool(self._values.get(name))

    def get_secret(self, name: str) -> Optional[str]:
        self.reads += 1
        if self.fail_reads:
            raise SecretStoreError("内存密钥存储被配置为读取失败（测试）")
        return self._values.get(name)

    def set_secret(self, name: str, value: str) -> None:
        if not value:
            raise SecretStoreError("密钥不能为空")
        if self.fail_writes:
            raise SecretStoreError("内存密钥存储被配置为写入失败（测试）")
        self.writes += 1
        self._values[name] = value

    def delete_secret(self, name: str) -> bool:
        return self._values.pop(name, None) is not None


class UnavailableSecretStore(SecretStore):
    """无持久化密钥存储时的 fail-closed 实现（非 Windows 默认）。

    读取一律返回 None（等价于“未配置”），写入/轮换抛 ``SecretStoreUnavailable``。
    绝不退化为明文文件，也不把密钥塞进 SQLite。
    """

    backend = "unavailable"
    persistent = False

    def __init__(self, reason: str = "当前平台没有可用的本地密钥存储") -> None:
        self.reason = reason

    def is_configured(self, name: str) -> bool:
        return False

    def get_secret(self, name: str) -> Optional[str]:
        return None

    def set_secret(self, name: str, value: str) -> None:
        raise SecretStoreUnavailable(
            f"{self.reason}（需要 Windows DPAPI；请注入 SecretStore 或使用环境变量配置）"
        )

    def rotate_secret(self, name: str, value: str) -> None:
        self.set_secret(name, value)

    def delete_secret(self, name: str) -> bool:
        # 删除是幂等清理：即使后端不可用也应允许清除遗留明文记录。
        return False

    def describe(self) -> Dict[str, Any]:
        return {
            "backend": self.backend,
            "persistent": False,
            "reason": self.reason,
        }


class WindowsDpapiSecretStore(SecretStore):
    """Windows DPAPI（当前用户）加密文件存储。

    - 每个密钥一个文件：``<dir>/<slug>.dpapi``；
    - 文件内容 = 版本头 + DPAPI 密文，只有同一 Windows 用户能解密；
    - 写入采用临时文件 + ``os.replace``，避免部分写入留下损坏密文；
    - 保存后立即回读校验，校验失败则抛错（调用方据此保留旧值，不误删）。
    """

    backend = "windows-dpapi"
    persistent = True
    _HEADER = b"BUTLER-DPAPI-1\n"
    _ENTROPY = b"desktop-file-butler:secret-store:v1"

    def __init__(self, directory: Path) -> None:
        self._directory = Path(directory)

    @classmethod
    def available(cls) -> bool:
        """只在 Windows 且能加载 crypt32 时可用；非 Windows 返回 False。"""
        if sys.platform != "win32":
            return False
        try:
            import ctypes

            return bool(getattr(ctypes, "windll", None)) and hasattr(
                ctypes.windll, "crypt32"
            )
        except Exception:  # noqa: BLE001 - 能力探测失败按不可用处理
            return False

    # ---------- 路径与文件 ----------

    @staticmethod
    def _slug(name: str) -> str:
        cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", name or "").strip("._")
        if not cleaned:
            raise SecretStoreError("非法的密钥名称")
        return cleaned

    def _path(self, name: str) -> Path:
        return self._directory / f"{self._slug(name)}.dpapi"

    def _ensure_dir(self) -> None:
        self._directory.mkdir(parents=True, exist_ok=True)

    def is_configured(self, name: str) -> bool:
        try:
            path = self._path(name)
            return path.is_file() and path.stat().st_size > len(self._HEADER)
        except OSError:
            return False

    # ---------- DPAPI ----------

    @classmethod
    def _crypt(cls, data: bytes, *, protect: bool) -> bytes:
        """调用 CryptProtectData / CryptUnprotectData；失败抛 SecretStoreError。"""
        import ctypes
        from ctypes import wintypes

        class _DataBlob(ctypes.Structure):
            _fields_ = [
                ("cbData", wintypes.DWORD),
                ("pbData", ctypes.POINTER(ctypes.c_char)),
            ]

        crypt32 = ctypes.windll.crypt32
        kernel32 = ctypes.windll.kernel32
        blob_in = _DataBlob()
        buffer_in = ctypes.create_string_buffer(data, len(data))
        blob_in.cbData = len(data)
        blob_in.pbData = ctypes.cast(buffer_in, ctypes.POINTER(ctypes.c_char))
        entropy = ctypes.create_string_buffer(cls._ENTROPY, len(cls._ENTROPY))
        blob_entropy = _DataBlob()
        blob_entropy.cbData = len(cls._ENTROPY)
        blob_entropy.pbData = ctypes.cast(entropy, ctypes.POINTER(ctypes.c_char))
        blob_out = _DataBlob()

        # CRYPTPROTECT_UI_FORBIDDEN：后台进程不得弹系统对话框。
        flags = 0x1
        if protect:
            crypt32.CryptProtectData.argtypes = [
                ctypes.POINTER(_DataBlob),
                wintypes.LPCWSTR,
                ctypes.POINTER(_DataBlob),
                ctypes.c_void_p,
                ctypes.c_void_p,
                wintypes.DWORD,
                ctypes.POINTER(_DataBlob),
            ]
            crypt32.CryptProtectData.restype = wintypes.BOOL
            ok = crypt32.CryptProtectData(
                ctypes.byref(blob_in),
                "Desktop File Butler",
                ctypes.byref(blob_entropy),
                None,
                None,
                flags,
                ctypes.byref(blob_out),
            )
        else:
            crypt32.CryptUnprotectData.argtypes = [
                ctypes.POINTER(_DataBlob),
                ctypes.POINTER(wintypes.LPWSTR),
                ctypes.POINTER(_DataBlob),
                ctypes.c_void_p,
                ctypes.c_void_p,
                wintypes.DWORD,
                ctypes.POINTER(_DataBlob),
            ]
            crypt32.CryptUnprotectData.restype = wintypes.BOOL
            ok = crypt32.CryptUnprotectData(
                ctypes.byref(blob_in),
                None,
                ctypes.byref(blob_entropy),
                None,
                None,
                flags,
                ctypes.byref(blob_out),
            )
        if not ok:
            error = ctypes.get_last_error() if hasattr(ctypes, "get_last_error") else 0
            raise SecretStoreError(
                f"DPAPI {'加密' if protect else '解密'}失败（系统错误码 {int(error)}）"
            )
        try:
            return ctypes.string_at(blob_out.pbData, blob_out.cbData)
        finally:
            if blob_out.pbData:
                kernel32.LocalFree(blob_out.pbData)

    # ---------- 接口实现 ----------

    def get_secret(self, name: str) -> Optional[str]:
        if not self.is_configured(name):
            return None
        path = self._path(name)
        try:
            payload = path.read_bytes()
        except OSError as exc:
            raise SecretStoreError(f"无法读取本地密钥文件（{type(exc).__name__}）") from exc
        if not payload.startswith(self._HEADER):
            raise SecretStoreError("本地密钥文件格式无法识别")
        try:
            plain = self._crypt(payload[len(self._HEADER):], protect=False)
            return plain.decode("utf-8")
        except (SecretStoreError, UnicodeDecodeError):
            raise
        except Exception as exc:  # noqa: BLE001 - 绝不把密文/明文写进异常
            raise SecretStoreError(
                f"本地密钥解密失败（{type(exc).__name__}），可能由其他 Windows 用户创建"
            ) from exc

    def set_secret(self, name: str, value: str) -> None:
        if not value:
            raise SecretStoreError("密钥不能为空")
        self._ensure_dir()
        path = self._path(name)
        try:
            encrypted = self._crypt(value.encode("utf-8"), protect=True)
        except SecretStoreError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise SecretStoreError(
                f"本地密钥加密失败（{type(exc).__name__}）"
            ) from exc
        payload = self._HEADER + encrypted
        temporary: Optional[str] = None
        try:
            handle, temporary = tempfile.mkstemp(
                prefix=".secret-", suffix=".tmp", dir=str(self._directory)
            )
            with os.fdopen(handle, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            temporary = None
        except OSError as exc:
            raise SecretStoreError(
                f"无法写入本地密钥文件（{type(exc).__name__}）"
            ) from exc
        finally:
            if temporary:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass
        # 回读校验：确认写入的密文确实可读回同一明文，否则调用方会误删旧值。
        readback = self.get_secret(name)
        if readback != value:
            raise SecretStoreError("本地密钥写入后校验失败")

    def delete_secret(self, name: str) -> bool:
        path = self._path(name)
        try:
            path.unlink()
            return True
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise SecretStoreError(
                f"无法删除本地密钥文件（{type(exc).__name__}）"
            ) from exc

    def rotate_secret(self, name: str, value: str) -> None:
        # 先加密并校验新值，再覆盖；DPAPI + 原子替换保证不会出现半写状态。
        self.set_secret(name, value)


def default_store_directory() -> Path:
    """密钥文件目录：默认跟随数据库目录，便于测试隔离与用户数据迁移。"""
    from ..config import get_settings

    settings = get_settings()
    override = (getattr(settings, "secret_store_dir", "") or "").strip()
    base = Path(override).expanduser() if override else Path(settings.db_path).parent / "secrets"
    return base


def _build_default_store() -> SecretStore:
    if WindowsDpapiSecretStore.available():
        return WindowsDpapiSecretStore(default_store_directory())
    return UnavailableSecretStore(
        "当前平台不支持 Windows DPAPI，本地持久化密钥存储不可用"
    )


_store_override: Optional[SecretStore] = None
_store_lock = threading.Lock()


def get_secret_store() -> SecretStore:
    """返回当前生效的 SecretStore（可被测试/嵌入方注入）。

    默认实现**不缓存**：目录跟随 ``DB_PATH``，避免测试之间或用户在设置里改路径后
    继续读写旧目录。对象构造只做路径计算，开销可忽略。
    """
    with _store_lock:
        if _store_override is not None:
            return _store_override
    return _build_default_store()


def set_secret_store(store: Optional[SecretStore]) -> None:
    """注入 SecretStore（测试替身 / 桌面端集成）。传 None 恢复平台默认。"""
    global _store_override
    with _store_lock:
        _store_override = store


def reset_secret_store() -> None:
    """恢复平台默认并清空脱敏登记表（测试清理用）。"""
    global _store_override
    with _store_lock:
        _store_override = None
    clear_registered_secrets()


def secret_backend_info() -> Dict[str, Any]:
    """给 REST 响应用的存储元信息，绝不包含密钥。"""
    try:
        return get_secret_store().describe()
    except Exception as exc:  # noqa: BLE001 - 诊断接口本身不能拖垮配置读取
        return {"backend": "unknown", "persistent": False, "reason": type(exc).__name__}


def is_secret_name(name: str) -> bool:
    """是否为受保护的密钥类配置键（不允许走普通偏好接口）。"""
    return any(
        marker in (name or "").lower()
        for marker in ("api_key", "apikey", "token", "secret", "password")
    )


def digest_secret(value: str) -> str:
    """密钥指纹（仅用于“是否变化”的比对，不可逆，不泄露原文）。"""
    return hashlib.sha256((value or "").encode("utf-8")).hexdigest()[:16]


def iter_registered_secrets() -> Iterable[str]:
    """测试/诊断用：当前已知明文的只读快照（不用于业务逻辑）。"""
    with _known_lock:
        return tuple(_known_secrets)
