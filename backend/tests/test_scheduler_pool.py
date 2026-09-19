from app.tools import scheduler


class _Executor:
    def __init__(self):
        self.calls = []

    def submit(self, fn, *args):
        self.calls.append((fn, args))


def test_scheduler_submits_and_deduplicates_directory(monkeypatch):
    executor = _Executor()
    monkeypatch.setattr(scheduler, "_executor", executor)
    scheduler._running_directories.clear()
    scheduler._runner = lambda *_: None
    scheduler._fire("C:/same", "one")
    scheduler._fire("C:/same", "two")
    assert len(executor.calls) == 1
    executor.calls[0][0](*executor.calls[0][1])
    scheduler._fire("C:/same", "three")
    assert len(executor.calls) == 2
