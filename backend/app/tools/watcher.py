"""P5：目录监听与自动整理规则。

实现方式是**轮询 + 稳定性判定**，而不是依赖额外的文件系统事件库：
- 新文件必须连续两次观察到的 ``(size, mtime_ns)`` 一致，且间隔不小于
  ``settle_seconds``，才认为写入完成（复制一半的大文件不会被提前搬走）；
- 已处理事件写入 ``watch_events``（rule_id + path + signature），因此重复触发、
  进程重启都不会重复搬同一个文件；重启后第一次轮询会把“没处理过”的文件补上；
- 只处理规则目录的**直接子文件**，并跳过回收站、链接、临时文件与目标目录，
  因此“移动到目标目录 → 又被监听到”的自循环不会发生；
- 规则只在用户明确授权的目录内工作，所有移动仍走沙箱校验、目录锁、审计与回滚；
- 一批新文件超过批量审批阈值时**不自动执行**，而是记录明确原因并在规则状态里展示，
  交由用户在「操作预览」中确认，避免自动规则成为绕过审批的通道。

注意：轮询线程属于本进程，应用退出后不会再执行任何整理——界面必须如实说明。
"""
from __future__ import annotations

import os
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from .. import db
from ..logging_conf import get_logger
from ..security import SandboxViolation, resolve_in_sandbox
from . import classify, filesystem
from .path_locks import path_locks

logger = get_logger(__name__)

DEFAULT_POLL_SECONDS = 2.0
DEFAULT_SETTLE_SECONDS = 5
CONTENT_BUDGET_PER_POLL = 10
# 明显的“还没写完”的文件：等待它们稳定即可，但这里先排除，避免无谓的哈希与分类。
_TEMP_PATTERNS = re.compile(
    r"(^~\$|\.tmp$|\.temp$|\.part$|\.crdownload$|\.download$|\.!ut$)", re.IGNORECASE
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _signature(path: Path) -> Optional[tuple[int, int]]:
    try:
        stat = path.stat()
        return int(stat.st_size), int(stat.st_mtime_ns)
    except OSError:
        return None


def validate_rule(directory: str, dest_root: str) -> tuple[str, str]:
    """校验规则目录与目标根目录；返回规范化后的 (directory, dest_root)。"""
    source = str(resolve_in_sandbox(directory, must_exist=True))
    if not Path(source).is_dir():
        raise ValueError(f"规则目录不是目录: {source}")
    target = str(resolve_in_sandbox(dest_root))
    if Path(source).resolve() == Path(target).resolve():
        raise ValueError("目标根目录不能与监听目录相同（会形成自我移动循环）")
    return source, target


class RuleWatcher:
    def __init__(self, poll_interval: float = DEFAULT_POLL_SECONDS) -> None:
        self.poll_interval = max(0.2, float(poll_interval))
        self._observations: dict[str, dict[str, tuple[tuple[int, int], float]]] = {}
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self.on_event: Optional[Callable[[dict], None]] = None

    # ---------------- 生命周期 ----------------

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="butler-watcher", daemon=True)
        self._thread.start()
        logger.info("目录监听已启动（轮询间隔 %.1fs）", self.poll_interval)

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=2)
        self._thread = None
        logger.info("目录监听已停止")

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _loop(self) -> None:
        while not self._stop.wait(self.poll_interval):
            try:
                for rule in db.list_rules():
                    if self._stop.is_set():
                        return
                    if not int(rule.get("enabled") or 0):
                        continue
                    try:
                        self.poll_rule(rule)
                    except Exception:  # noqa: BLE001 - 单条规则失败不能拖停整个监听
                        logger.exception("整理规则执行失败 rule=%s", rule.get("rule_id"))
            except Exception:  # noqa: BLE001
                logger.exception("目录监听轮询失败")

    # ---------------- 单次轮询 ----------------

    def _candidates(self, rule: dict, now: float) -> list[dict]:
        """收集已稳定的候选文件（直接子文件）。"""
        directory = Path(str(rule["directory"]))
        dest_root = Path(str(rule["dest_root"]))
        settle = max(0, int(rule.get("settle_seconds") or DEFAULT_SETTLE_SECONDS))
        rule_id = str(rule["rule_id"])
        with self._lock:
            observed = self._observations.setdefault(rule_id, {})
        stable: list[dict] = []
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    if self._stop.is_set():
                        return []
                    name = entry.name
                    if name == filesystem.TRASH_DIRNAME or _TEMP_PATTERNS.search(name):
                        continue
                    try:
                        if entry.is_symlink() or entry.is_dir(follow_symlinks=False):
                            continue
                    except OSError:
                        continue
                    path = Path(entry.path)
                    # 目标目录内的文件不参与监听，彻底避免“搬进去又被搬出来”。
                    try:
                        path.resolve(strict=False).relative_to(dest_root.resolve(strict=False))
                        continue
                    except ValueError:
                        pass
                    except OSError:
                        continue
                    signature = _signature(path)
                    if signature is None:
                        continue
                    previous = observed.get(str(path))
                    if previous is None or previous[0] != signature:
                        observed[str(path)] = (signature, now)
                        continue
                    if now - previous[1] < settle:
                        continue
                    stable.append({"path": str(path), "signature": signature, "size": signature[0]})
        except OSError as exc:
            raise SandboxViolation(f"无法读取规则目录 {directory}: {exc}") from exc
        # 清理已消失的观察记录，避免长期运行后累积（与本次是否稳定无关）。
        with self._lock:
            for path in list(observed):
                if not Path(path).exists():
                    observed.pop(path, None)
        stable.sort(key=lambda item: item["path"])
        return stable

    def poll_rule(self, rule: dict, *, now: Optional[float] = None) -> dict:
        rule_id = str(rule["rule_id"])
        started = _now()
        candidates = self._candidates(rule, now if now is not None else time.monotonic())
        pending = [
            item for item in candidates
            if not db.has_watch_event(rule_id, item["path"], f"{item['signature'][0]}:{item['signature'][1]}")
        ]
        if not pending:
            return {"rule_id": rule_id, "processed": 0, "skipped": 0, "reason": ""}

        limit = _approval_threshold()
        if len(pending) > limit:
            message = (
                f"发现 {len(pending)} 个新文件，超过自动处理上限 {limit}，"
                "未自动移动；请在「操作预览」中确认后处理"
            )
            for item in pending:
                db.record_watch_event(rule_id, item["path"], f"{item['signature'][0]}:{item['signature'][1]}", "skipped:threshold")
            db.update_rule_runtime(rule_id, last_run_at=started, last_result=message)
            db.log_operation("rule_batch_skipped", str(rule["directory"]), "skipped",
                             detail=message, thread_id=f"rule-{rule_id}")
            self._emit({"rule_id": rule_id, "status": "skipped", "message": message})
            return {"rule_id": rule_id, "processed": 0, "skipped": len(pending), "reason": message}

        budget = classify.Budget(remaining=CONTENT_BUDGET_PER_POLL)
        processed = 0
        failures: list[str] = []
        thread_id = f"rule-{rule_id}"
        for item in pending:
            path = Path(item["path"])
            try:
                if rule["mode"] == "content":
                    result = classify.classify_path(path, llm=_classify_llm(), mode="content", budget=budget)
                    category = result["category"]
                else:
                    category = classify.classify_path(path, mode="extension")["category"]
                target_dir = Path(str(rule["dest_root"])) / category
                with path_locks.acquire(path.parent, target_dir):
                    with db.operation_thread(thread_id):
                        actual = filesystem.move_file(str(path), str(target_dir))
                db.record_watch_event(rule_id, str(path), f"{item['signature'][0]}:{item['signature'][1]}", f"moved:{actual}")
                processed += 1
                try:
                    from . import indexer

                    indexer.note_path_moved(str(path), actual)
                except Exception:  # noqa: BLE001
                    logger.debug("自动整理后更新索引失败", exc_info=True)
            except Exception as exc:  # noqa: BLE001 - 单文件失败不影响其它文件
                logger.warning("自动整理失败 %s: %s", path, exc)
                failures.append(f"{path.name}: {exc}")
                db.record_watch_event(rule_id, str(path), f"{item['signature'][0]}:{item['signature'][1]}", f"failed:{exc}"[:400])
        message = f"处理 {processed} 个文件"
        if failures:
            message += f"，失败 {len(failures)} 个（{failures[0]}）"
        db.update_rule_runtime(rule_id, last_run_at=started, last_result=message, processed_delta=processed)
        self._emit({"rule_id": rule_id, "status": "processed", "message": message})
        return {"rule_id": rule_id, "processed": processed, "skipped": 0, "reason": message}

    def _emit(self, payload: dict) -> None:
        callback = self.on_event
        if callback is None:
            return
        try:
            callback(payload)
        except Exception:  # noqa: BLE001 - 通知失败不影响整理结果
            logger.debug("整理事件回调失败", exc_info=True)


def _approval_threshold() -> int:
    raw = db.get_preference("batch_approval_threshold")
    try:
        value = int(raw) if raw is not None else 20
    except (TypeError, ValueError):
        return 20
    return value if value > 0 else 20


def _classify_llm() -> Any:
    """内容分类所需的模型；未配置时返回 None（分类会自动回退到扩展名）。"""
    try:
        from ..agent.llm import build_llm

        return build_llm(temperature=0.1)
    except Exception:  # noqa: BLE001 - 模型不可用不应让规则线程崩溃
        return None


_watcher: Optional[RuleWatcher] = None


def start_watcher(poll_interval: float = DEFAULT_POLL_SECONDS,
                  on_event: Optional[Callable[[dict], None]] = None) -> RuleWatcher:
    global _watcher
    if _watcher is None:
        _watcher = RuleWatcher(poll_interval)
    _watcher.on_event = on_event
    _watcher.start()
    return _watcher


def stop_watcher() -> None:
    global _watcher
    if _watcher is not None:
        _watcher.stop()
        _watcher = None


def watcher_running() -> bool:
    return _watcher is not None and _watcher.running


# ---------------- 规则 CRUD（REST 与测试共用） ----------------


def list_rules() -> list[dict]:
    rules = db.list_rules()
    return [{
        "rule_id": rule["rule_id"],
        "directory": rule["directory"],
        "dest_root": rule["dest_root"],
        "mode": rule["mode"],
        "instruction": rule["instruction"],
        "enabled": bool(rule["enabled"]),
        "settle_seconds": int(rule["settle_seconds"]),
        "created_at": rule["created_at"],
        "updated_at": rule["updated_at"],
        "last_run_at": rule["last_run_at"] or "",
        "last_result": rule["last_result"] or "",
        "processed_count": int(rule["processed_count"]),
    } for rule in rules]


def create_rule(*, directory: str, dest_root: str, mode: str = "extension",
                instruction: str = "", enabled: bool = True,
                settle_seconds: int = DEFAULT_SETTLE_SECONDS) -> dict:
    if mode not in classify.MODES:
        raise ValueError("mode 仅支持 extension 或 content")
    source, target = validate_rule(directory, dest_root)
    now = _now()
    rule = {
        "rule_id": uuid.uuid4().hex,
        "directory": source,
        "dest_root": target,
        "mode": mode,
        "instruction": instruction,
        "enabled": enabled,
        "settle_seconds": max(1, int(settle_seconds)),
        "created_at": now,
        "updated_at": now,
        "last_run_at": "",
        "last_result": "",
        "processed_count": 0,
    }
    db.upsert_rule(rule)
    return rule


def update_rule(rule_id: str, **changes: Any) -> dict:
    current = db.get_rule(rule_id)
    if current is None:
        raise ValueError(f"整理规则不存在: {rule_id}")
    directory = str(changes.get("directory") or current["directory"])
    dest_root = str(changes.get("dest_root") or current["dest_root"])
    source, target = validate_rule(directory, dest_root)
    mode = str(changes.get("mode") or current["mode"])
    if mode not in classify.MODES:
        raise ValueError("mode 仅支持 extension 或 content")
    updated = {
        **current,
        "directory": source,
        "dest_root": target,
        "mode": mode,
        "instruction": str(changes.get("instruction", current["instruction"])),
        "enabled": bool(changes.get("enabled", bool(current["enabled"]))),
        "settle_seconds": max(1, int(changes.get("settle_seconds", current["settle_seconds"]))),
        "updated_at": _now(),
    }
    db.upsert_rule(updated)
    return updated


def delete_rule(rule_id: str) -> bool:
    return db.delete_rule(rule_id)


def run_rule_once(rule_id: str) -> dict:
    """手动触发一次（跳过稳定等待），用于验证规则与测试。"""
    rule = db.get_rule(rule_id)
    if rule is None:
        raise ValueError(f"整理规则不存在: {rule_id}")
    watcher = _watcher or RuleWatcher()
    now = time.monotonic()
    # 让候选直接视为“已稳定”：把观察时间回拨到 settle 之前。
    with watcher._lock:  # noqa: SLF001 - 同一模块内的受控访问
        observed = watcher._observations.setdefault(rule_id, {})
        for path in _direct_files(Path(rule["directory"])):
            signature = _signature(path)
            if signature is not None:
                observed[str(path)] = (signature, now - int(rule["settle_seconds"]) - 1)
    return watcher.poll_rule(rule, now=now)


def _direct_files(directory: Path) -> list[Path]:
    values: list[Path] = []
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                try:
                    if entry.is_symlink() or entry.is_dir(follow_symlinks=False):
                        continue
                except OSError:
                    continue
                values.append(Path(entry.path))
    except OSError:
        return []
    return values
