"""Frozen backend entry point."""
from __future__ import annotations

import argparse
import os


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    from app.main import app
    if args.self_test:
        if "/api/health" not in {route.path for route in app.routes}:
            raise RuntimeError("health route missing")
        print("butler-backend self-test ok")
        return 0
    import uvicorn
    uvicorn.run(app, host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", "8000")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
