"""定时任务线程池回归：同目录去重，以及提交失败时必须收回占位（B9）。"""
import pytest

from app.models import ScheduledJob
from app.tools import scheduler


class _Executor:
    def __init__(self):
        self.calls = []

    def submit(self, fn, *args):
        self.calls.append((fn, args))


class _DeadExecutor:
    """模拟已 shutdown 的线程池：submit 抛 RuntimeError。"""

    def __init__(self):
        self.calls = 0

    def submit(self, fn, *args):
        self.calls += 1
        raise RuntimeError("cannot schedule new futures after shutdown")


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


def test_submit_failure_releases_directory(monkeypatch):
    """提交失败必须收回占位，否则该目录被永久判定为“正在运行”而再也不触发。"""
    executor = _DeadExecutor()
    monkeypatch.setattr(scheduler, "_executor", executor)
    scheduler._running_directories.clear()

    scheduler._fire("C:/dead", "one")  # 不得向上抛异常

    assert "C:/dead" not in scheduler._running_directories, "占位未收回，该目录会永久失效"
    assert executor.calls == 1


def test_directory_recovers_after_submit_failure(monkeypatch):
    """线程池恢复后，同一目录仍应能正常触发（占位没有被永久占用）。"""
    monkeypatch.setattr(scheduler, "_executor", _DeadExecutor())
    scheduler._running_directories.clear()
    scheduler._fire("C:/flaky", "one")

    healthy = _Executor()
    monkeypatch.setattr(scheduler, "_executor", healthy)
    scheduler._fire("C:/flaky", "two")

    assert len(healthy.calls) == 1, "提交失败后的目录再也没能触发"


def test_invalid_cron_is_rejected_before_persistence(monkeypatch):
    persisted = []
    monkeypatch.setattr(scheduler.db, "upsert_job", persisted.append)
    job = ScheduledJob(
        job_id="bad-cron", directory="C:/tmp", instruction="整理", cron="not a cron"
    )

    with pytest.raises(ValueError):
        scheduler.add_job(job)

    assert persisted == []
