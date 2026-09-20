"""定时任务：用 APScheduler 定期触发 Agent 整理指定目录。

任务持久化在 SQLite（scheduled_jobs 表），进程启动时重新装载。
实际触发时调用注入的 runner 回调（在 main.py 中绑定到 Agent 运行逻辑）。
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import threading
from typing import Callable, Optional, Set

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from .. import db
from ..logging_conf import get_logger
from ..models import ScheduledJob

logger = get_logger(__name__)

_scheduler: Optional[BackgroundScheduler] = None
# runner(directory, instruction) -> None
_runner: Optional[Callable[[str, str], None]] = None
_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="butler-job")
_running_directories: Set[str] = set()
_running_lock = threading.Lock()


def init_scheduler(runner: Callable[[str, str], None]) -> None:
    """启动调度器并从 DB 装载已有任务。"""
    global _scheduler, _runner
    _runner = runner
    if _scheduler is None:
        _scheduler = BackgroundScheduler()
        _scheduler.start()
        logger.info("APScheduler 已启动")

    for job in db.list_jobs():
        if job.enabled:
            _register(job)


def _register(job: ScheduledJob) -> None:
    assert _scheduler is not None
    try:
        trigger = CronTrigger.from_crontab(job.cron)
    except ValueError as exc:
        logger.error("非法 cron 表达式 %s: %s", job.cron, exc)
        return

    _scheduler.add_job(
        _fire,
        trigger=trigger,
        id=job.job_id,
        replace_existing=True,
        args=[job.directory, job.instruction],
    )
    logger.info("已注册定时任务 %s cron=%s dir=%s", job.job_id, job.cron, job.directory)


def _fire(directory: str, instruction: str) -> None:
    with _running_lock:
        if directory in _running_directories:
            logger.info("跳过同目录重复定时任务: %s", directory)
            return
        _running_directories.add(directory)
    try:
        _executor.submit(_run_job, directory, instruction)
    except RuntimeError:
        # 线程池已关闭（进程正在退出，或测试中 lifespan 已 shutdown 过）：必须收回
        # 标记，否则该目录会被永久判定为“正在运行”，此后再也不会触发（B9）。
        with _running_lock:
            _running_directories.discard(directory)
        logger.error("定时任务无法提交执行（线程池已关闭）: %s", directory)


def _run_job(directory: str, instruction: str) -> None:
    try:
        if _runner is not None:
            _runner(directory, instruction)
    except Exception as exc:  # noqa: BLE001
        logger.exception("定时任务执行失败: %s", exc)
    finally:
        with _running_lock:
            _running_directories.discard(directory)


def _legacy_fire(directory: str, instruction: str) -> None:
    if _runner is None:
        logger.error("runner 未注入，跳过定时任务")
        return
    logger.info("触发定时整理: %s", directory)
    try:
        _runner(directory, instruction)
    except Exception as exc:  # noqa: BLE001
        logger.exception("定时任务执行失败: %s", exc)


def add_job(job: ScheduledJob) -> None:
    """新增/更新定时任务，持久化并注册。"""
    db.upsert_job(job)
    if _scheduler is not None:
        if job.enabled:
            _register(job)
        else:
            remove_job(job.job_id, delete_record=False)


def remove_job(job_id: str, delete_record: bool = True) -> None:
    if _scheduler is not None and _scheduler.get_job(job_id):
        _scheduler.remove_job(job_id)
    if delete_record:
        db.delete_job(job_id)
    logger.info("移除定时任务 %s", job_id)


def shutdown() -> None:
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
    _executor.shutdown(wait=False, cancel_futures=True)
