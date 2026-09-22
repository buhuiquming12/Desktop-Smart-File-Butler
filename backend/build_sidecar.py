"""Build the platform-native backend sidecar."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"
FRONTEND_DIST = ROOT / "frontend" / "dist"


def main() -> int:
    if not FRONTEND_DIST.is_dir():
        raise SystemExit("frontend/dist 不存在；请先运行 npm run build:web")
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
