"""Build the platform-native backend sidecar."""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"
FRONTEND_DIST = ROOT / "frontend" / "dist"

# 重入标记：避免“找不到 PyInstaller → 换解释器 → 还是找不到”时无限递归。
_REEXEC_FLAG = "BUTLER_SIDECAR_REEXEC"


def _candidate_interpreters() -> list[str]:
    """项目 venv 的解释器候选（Windows 与 POSIX 布局都覆盖）。"""
    candidates = [
        BACKEND / ".venv" / "Scripts" / "python.exe",
        BACKEND / ".venv" / "bin" / "python",
        BACKEND / "venv" / "Scripts" / "python.exe",
        BACKEND / "venv" / "bin" / "python",
    ]
    return [str(path) for path in candidates if path.is_file()]


def _reexec_with_venv() -> int | None:
    """当前解释器没有 PyInstaller 时，改用项目 venv 重跑。

    返回 ``None`` 表示应该继续用当前解释器；返回整数表示已经（或应该）用它重跑。
    ``npm run build:sidecar`` 调用的是裸 ``python``，很容易落到一个没装依赖的
    全局解释器上——这里自动切到项目 venv，避免“命令跑了但 exe 没生成”的困惑。
    """
    if importlib.util.find_spec("PyInstaller") is not None:
        return None
    if os.environ.get(_REEXEC_FLAG) == "1":
        raise SystemExit(
            "当前解释器没有 PyInstaller，且项目 venv 中也没有：\n"
            f"  {sys.executable}\n"
            "请先安装依赖（含 pyinstaller）：\n"
            "  python -m pip install -r backend/requirements.txt"
        )
    explicit = os.environ.get("PYTHON", "").strip()
    for candidate in ([explicit] if explicit else []) + _candidate_interpreters():
        if not candidate or Path(candidate) == Path(sys.executable):
            continue
        env = {**os.environ, _REEXEC_FLAG: "1"}
        print(f"[build_sidecar] 当前解释器缺少 PyInstaller，改用 {candidate}", flush=True)
        return subprocess.call([candidate, str(Path(__file__).resolve()), *sys.argv[1:]],
                               cwd=str(ROOT), env=env)
    raise SystemExit(
        "没有找到可用的 Python 解释器（需要 PyInstaller）。请先安装依赖：\n"
        "  python -m pip install -r backend/requirements.txt\n"
        "或指定解释器：PYTHON=/path/to/python npm run build:sidecar"
    )


def main() -> int:
    reexec = _reexec_with_venv()
    if reexec is not None:
        return reexec
    if not FRONTEND_DIST.is_dir():
        raise SystemExit("frontend/dist 不存在；请先运行 npm run build:web")
    print(f"[build_sidecar] 使用解释器 {sys.executable}", flush=True)
    separator = ";" if sys.platform == "win32" else ":"
    command = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile",
               "--name", "butler-backend", "--paths", str(BACKEND),
               "--add-data", f"{FRONTEND_DIST}{separator}frontend/dist",
               "--exclude-module", "IPython", "--exclude-module", "matplotlib",
               "--exclude-module", "numpy", "--exclude-module", "pandas",
               "--exclude-module", "sphinx", "--exclude-module", "docutils",
               "--exclude-module", "jedi", "--exclude-module", "langchain",
               "--exclude-module", "pytest", "--exclude-module", "scipy",
               "--exclude-module", "pyarrow", "--exclude-module", "xarray",
               "--exclude-module", "dask", "--exclude-module", "distributed",
               "--exclude-module", "bokeh", "--exclude-module", "nltk",
               "--exclude-module", "tkinter", "--exclude-module", "PyQt5",
               "--exclude-module", "zmq", "--exclude-module", "tensorflow",
               "--exclude-module", "botocore", "--exclude-module", "boto3",
               "--exclude-module", "sqlalchemy", "--exclude-module", "grpc",
               "--exclude-module", "lz4", "--exclude-module", "fsspec",
               "--exclude-module", "langchain_community.llms",
               "--exclude-module", "langchain_community.vectorstores",
               "--exclude-module", "langchain_community.embeddings",
               "--distpath", str(BACKEND / "dist"), "--workpath", str(BACKEND / "build"),
               "--specpath", str(BACKEND), str(BACKEND / "sidecar.py")]
    return subprocess.call(command, cwd=ROOT)


if __name__ == "__main__":
    raise SystemExit(main())
