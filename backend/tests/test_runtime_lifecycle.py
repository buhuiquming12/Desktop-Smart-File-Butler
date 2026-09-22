from __future__ import annotations

import asyncio

from app import main


class Runtime:
    def __init__(self, name: str) -> None:
        self.name = name
        self.active_stream_count = 0
        self.retired = False
        self.closed = False

    def retain_stream(self) -> None:
        self.active_stream_count += 1

    def release_stream(self) -> None:
        self.active_stream_count -= 1
        if self.retired and not self.active_stream_count:
            self.closed = True

    def retire(self) -> None:
        self.retired = True
        if not self.active_stream_count:
            self.closed = True

    def state(self, _thread_id: str):
        return {"status": "completed", "final_response": self.name, "observations": []}


def test_running_stream_keeps_original_runtime_after_global_reset(monkeypatch) -> None:
    old = Runtime("old")
    new = Runtime("new")
    main._runtime = old  # type: ignore[assignment]

    def iterator():
        main.reset_runtime()
        main._runtime = new  # type: ignore[assignment]
        yield ("updates", {})

    monkeypatch.setattr(main, "connections", type("C", (), {"send": staticmethod(lambda *_: None)})())
    # No client means dispatch is a no-op; the terminal state must still come from old.
    result = asyncio.run(main._run_stream(iterator(), "runtime-1", None, runtime=old))
    assert result["final_response"] == "old"
    assert old.retired and old.closed
    assert not new.closed
    main._runtime = None
