"""构建脚本的编码健壮性测试。

CI（Windows runner）的控制台编码是 cp1252，``build_sidecar.py`` 打印中文日志会抛
``UnicodeEncodeError`` 让打包在第一步就失败。这里锁定修复后的行为：即使 stdout 是
严格模式的 cp1252，也能正常输出中文。
"""
from __future__ import annotations

import io
import sys

import build_sidecar


class _NoReconfigureStream:
    """模拟只有 write 的最小流对象（例如被包装后的日志流）。"""

    def __init__(self) -> None:
        self.chunks: list[str] = []

    def write(self, text: str) -> int:
        self.chunks.append(text)
        return len(text)

    def flush(self) -> None:
        return None


def test_configure_utf8_stdio_rewrites_cp1252_stream(monkeypatch) -> None:
    buffer = io.BytesIO()
    stream = io.TextIOWrapper(buffer, encoding="cp1252", errors="strict", newline="")
    monkeypatch.setattr(sys, "stdout", stream)
    monkeypatch.setattr(sys, "stderr", stream)

    build_sidecar._configure_utf8_stdio()
    print("[build_sidecar] 使用解释器 C:\\Python312\\python.exe")
    stream.flush()

    assert "使用解释器".encode("utf-8") in buffer.getvalue()


def test_configure_utf8_stdio_tolerates_streams_without_reconfigure(monkeypatch) -> None:
    stream = _NoReconfigureStream()
    monkeypatch.setattr(sys, "stdout", stream)
    monkeypatch.setattr(sys, "stderr", stream)

    build_sidecar._configure_utf8_stdio()

    print("[build_sidecar] 仍然可以写入")
    assert "".join(stream.chunks).strip() == "[build_sidecar] 仍然可以写入"


def test_build_env_forces_utf8_stdio() -> None:
    env = build_sidecar._build_env()

    assert env["PYTHONIOENCODING"] == "utf-8"
