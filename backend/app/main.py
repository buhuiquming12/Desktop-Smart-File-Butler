"""FastAPI 入口：REST、WebSocket、Agent 事件流与生命周期管理。"""
from __future__ import annotations

import asyncio
import json
import threading
import uuid
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Dict, Iterable, Optional, Set, Tuple

from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import ValidationError

from . import db
from .agent.graph import AgentRuntime
from .config import get_settings
from .logging_conf import get_logger, setup_logging
from .models import (
    ApprovalResponse,
    ChatRequest,
    JobCreate,
    PreferenceUpdate,
    ScheduledJob,
    WSEvent,
    WSEventType,
)
from .security import SandboxViolation, resolve_in_sandbox
from .tools import scheduler

settings = get_settings()
setup_logging(settings.log_dir)
logger = get_logger(__name__)

_runtime: Optional[AgentRuntime] = None
_runtime_lock = threading.Lock()
_background_tasks: Set[asyncio.Task[Any]] = set()


def get_runtime() -> AgentRuntime:
    """惰性构造运行时，使健康检查不依赖模型服务是否已经配置。"""
    global _runtime
    if _runtime is None:
        with _runtime_lock:
            if _runtime is None:
                _runtime = AgentRuntime()
    return _runtime


class ConnectionManager:
    """维护 Electron 渲染进程的 WebSocket 连接。"""

    def __init__(self) -> None:
        self._connections: Dict[str, WebSocket] = {}

    async def connect(self, client_id: str, websocket: WebSocket) -> None:
        await websocket.accept()
        old = self._connections.get(client_id)
        self._connections[client_id] = websocket
        if old and old is not websocket:
            try:
                await old.close(code=1000, reason="由新连接替换")
            except RuntimeError:
                pass

    def disconnect(self, client_id: str, websocket: WebSocket) -> None:
        if self._connections.get(client_id) is websocket:
            self._connections.pop(client_id, None)

    async def send(self, client_id: str, event: WSEvent) -> None:
        websocket = self._connections.get(client_id)
        if websocket is None:
            return
        try:
            await websocket.send_json(event.model_dump(mode="json"))
        except (RuntimeError, WebSocketDisconnect):
            self.disconnect(client_id, websocket)


connections = ConnectionManager()


def _scheduled_runner(directory: str, instruction: str) -> None:
    """APScheduler 线程中的无 UI 执行入口；危险步骤仍会在 interrupt 处暂停。"""
    thread_id = f"scheduled-{uuid.uuid4().hex}"
    message = f"在目录 {directory} 执行以下定时整理任务：{instruction}"
    try:
        for _ in get_runtime().start_stream(message, thread_id):
            pass
        state = get_runtime().state(thread_id)
        if state.get("status") == "waiting_approval":
            logger.warning("定时任务 %s 需要危险操作审批，已暂停等待用户手动处理", thread_id)
        else:
            logger.info("定时任务 %s 结束: %s", thread_id, state.get("status"))
    except Exception:  # noqa: BLE001
        logger.exception("定时 Agent 任务失败 thread=%s", thread_id)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    db.init_db()
    scheduler.init_scheduler(_scheduled_runner)
    logger.info("桌面智能文件管家后端已启动")
    try:
        yield
    finally:
        scheduler.shutdown()
        for task in list(_background_tasks):
            task.cancel()
        logger.info("桌面智能文件管家后端已停止")


app = FastAPI(
    title="桌面智能文件管家 API",
    version="0.1.0",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://127.0.0.1:5173",
        "http://localhost:5173",
        "file://",
    ],
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["Content-Type"],
)


def _next_update(iterator: Iterable[Dict[str, Any]]) -> Tuple[bool, Optional[Dict[str, Any]]]:
    """在线程里取生成器下一项，避免 StopIteration 穿过 asyncio Future。"""
    try:
        return True, next(iterator)  # type: ignore[arg-type]
    except StopIteration:
        return False, None


def _event(event_type: WSEventType, thread_id: str, **payload: Any) -> WSEvent:
    return WSEvent(type=event_type, thread_id=thread_id, payload=payload)


async def _emit_update(
    client_id: Optional[str], thread_id: str, update: Dict[str, Any]
) -> None:
    if not client_id:
        return

    # LangGraph update 通常形如 {"node_name": {state delta}}。
    for node, delta in update.items():
        if node == "__interrupt__":
            state = get_runtime().state(thread_id)
            pending = state.get("pending_approval") or {}
            if pending:
                await connections.send(
                    client_id,
                    _event(WSEventType.approval_required, thread_id, **pending),
                )
            continue

        safe_delta = delta if isinstance(delta, dict) else {"value": str(delta)}
        await connections.send(
            client_id,
            _event(WSEventType.node, thread_id, node=node, status=safe_delta.get("status")),
        )

        current = safe_delta.get("current_step")
        if node == "act" and current:
            await connections.send(
                client_id,
                _event(
                    WSEventType.tool_call,
                    thread_id,
                    tool=current.get("tool", ""),
                    name=current.get("tool", ""),
                    task_id=current.get("id", ""),
                    detail=current.get("description", ""),
                    args=current.get("args", {}),
                ),
            )

        observations = safe_delta.get("observations")
        if observations:
            latest = observations[-1]
            await connections.send(
                client_id,
                _event(
                    WSEventType.tool_result,
                    thread_id,
                    tool=latest.get("tool", ""),
                    name=latest.get("tool", ""),
                    task_id=latest.get("step_id", ""),
                    detail=latest.get("error") or str(latest.get("result", "")),
                    success=latest.get("status") == "ok",
                    status=latest.get("status", ""),
                    observation=latest,
                ),
            )
            task_status = {
                "ok": "success",
                "failed": "failed",
                "rejected": "failed",
            }.get(latest.get("status"), "running")
            await connections.send(
                client_id,
                _event(
                    WSEventType.task,
                    thread_id,
                    task_id=latest.get("step_id"),
                    title=latest.get("tool") or "文件任务",
                    description=latest.get("description"),
                    detail=latest.get("error") or latest.get("description", ""),
                    status=task_status,
                    tool=latest.get("tool"),
                    error=latest.get("error", ""),
                ),
            )

        pending = safe_delta.get("pending_approval")
        if pending:
            await connections.send(
                client_id,
                _event(WSEventType.approval_required, thread_id, **pending),
            )


async def _run_stream(
    iterator: Iterable[Dict[str, Any]], thread_id: str, client_id: Optional[str]
) -> Dict[str, Any]:
    """逐项消费同步 LangGraph 流，同时向 Electron 推送进度。"""
    try:
        while True:
            has_item, update = await asyncio.to_thread(_next_update, iterator)
            if not has_item:
                break
            if update:
                await _emit_update(client_id, thread_id, update)

        state = get_runtime().state(thread_id)
        if state.get("status") == "waiting_approval":
            pending = state.get("pending_approval") or {}
            if client_id and pending:
                await connections.send(
                    client_id,
                    _event(WSEventType.approval_required, thread_id, **pending),
                )
            return state

        event_type = WSEventType.error if state.get("status") == "failed" else WSEventType.done
        if client_id:
            await connections.send(
                client_id,
                _event(
                    event_type,
                    thread_id,
                    status=state.get("status"),
                    message=state.get("final_response", ""),
                    error=state.get("error", ""),
                    observations=state.get("observations", []),
                ),
            )
        return state
    except Exception as exc:  # noqa: BLE001
        logger.exception("Agent 执行失败 thread=%s", thread_id)
        if client_id:
            await connections.send(
                client_id,
                _event(WSEventType.error, thread_id, message=str(exc)),
            )
        return {"thread_id": thread_id, "status": "failed", "error": str(exc)}


def _track(coro: Any) -> None:
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


async def _start_chat(request: ChatRequest, fallback_client: Optional[str] = None) -> str:
    thread_id = request.thread_id or uuid.uuid4().hex
    client_id = request.client_id or fallback_client
    iterator = get_runtime().start_stream(request.message.strip(), thread_id)
    await _run_stream(iterator, thread_id, client_id)
    return thread_id


async def _resume_approval(
    response: ApprovalResponse, fallback_client: Optional[str] = None
) -> str:
    state = get_runtime().state(response.thread_id)
    pending = state.get("pending_approval") or {}
    if not pending:
        raise HTTPException(status_code=409, detail="该会话没有待审批操作")
    if pending.get("approval_id") != response.approval_id:
        raise HTTPException(status_code=409, detail="审批已过期或不匹配")

    iterator = get_runtime().resume_stream(response.thread_id, response.decision.value)
    await _run_stream(iterator, response.thread_id, fallback_client)
    return response.thread_id


# ---------------- REST ----------------


@app.get("/api/health")
def health() -> Dict[str, Any]:
    return {
        "status": "ok",
        "model_provider": settings.model_provider,
        "sandbox_configured": bool(settings.sandbox_root_paths),
    }


@app.get("/api/config")
def public_config() -> Dict[str, Any]:
    """仅返回非敏感配置；API key 永不暴露给渲染进程。"""
    return {
        "model_provider": settings.model_provider,
        "openai_model": settings.openai_model,
        "ollama_model": settings.ollama_model,
        "ollama_base_url": settings.ollama_base_url,
        "sandbox_roots": [str(path) for path in settings.sandbox_root_paths],
        "ocr_enabled": bool(settings.tesseract_cmd),
    }


@app.post("/api/chat", status_code=status.HTTP_202_ACCEPTED)
async def chat(request: ChatRequest) -> Dict[str, str]:
    thread_id = request.thread_id or uuid.uuid4().hex
    request.thread_id = thread_id
    _track(_start_chat(request))
    return {"thread_id": thread_id, "status": "accepted"}


@app.get("/api/threads/{thread_id}")
def thread_state(thread_id: str) -> Dict[str, Any]:
    state = get_runtime().state(thread_id)
    if not state:
        raise HTTPException(status_code=404, detail="会话不存在")
    return state


@app.post("/api/approvals/respond", status_code=status.HTTP_202_ACCEPTED)
async def approval(response: ApprovalResponse) -> Dict[str, str]:
    # REST 调用没有可靠的客户端映射；调用方可随后查询 thread 状态。
    state = get_runtime().state(response.thread_id)
    pending = state.get("pending_approval") or {}
    if not pending:
        raise HTTPException(status_code=409, detail="该会话没有待审批操作")
    if pending.get("approval_id") != response.approval_id:
        raise HTTPException(status_code=409, detail="审批已过期或不匹配")
    _track(_resume_approval(response))
    return {"thread_id": response.thread_id, "status": "accepted"}


@app.get("/api/operations")
def operations(limit: int = Query(100, ge=1, le=500)) -> list[Dict[str, Any]]:
    return [item.model_dump(mode="json") for item in db.recent_operations(limit)]


@app.get("/api/preferences")
def preferences() -> list[Dict[str, str]]:
    return [item.model_dump() for item in db.all_preferences()]


@app.put("/api/preferences/{key}")
def update_preference(key: str, body: PreferenceUpdate) -> Dict[str, str]:
    normalized_key = key.strip()
    if not normalized_key or len(normalized_key) > 200:
        raise HTTPException(status_code=422, detail="偏好键不能为空且最多 200 字符")
    if any(
        secret in normalized_key.lower()
        for secret in ("api_key", "token", "secret", "password")
    ):
        raise HTTPException(status_code=422, detail="密钥、令牌和密码不能保存为偏好")
    db.set_preference(normalized_key, body.value)
    return {"key": normalized_key, "value": body.value}


@app.get("/api/jobs")
def jobs() -> list[Dict[str, Any]]:
    return [item.model_dump() for item in db.list_jobs()]


@app.post("/api/jobs", status_code=status.HTTP_201_CREATED)
def create_job(body: JobCreate) -> Dict[str, Any]:
    try:
        directory_path = resolve_in_sandbox(body.directory, must_exist=True)
        directory = str(directory_path)
        CronTrigger.from_crontab(body.cron)
    except (SandboxViolation, FileNotFoundError, NotADirectoryError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not directory_path.is_dir():
        raise HTTPException(status_code=422, detail="定时任务目标必须是目录")

    job = ScheduledJob(
        job_id=uuid.uuid4().hex,
        directory=directory,
        instruction=body.instruction,
        cron=body.cron,
        enabled=body.enabled,
    )
    scheduler.add_job(job)
    return job.model_dump()


@app.delete("/api/jobs/{job_id}")
def remove_job(job_id: str) -> Dict[str, str]:
    if not any(item.job_id == job_id for item in db.list_jobs()):
        raise HTTPException(status_code=404, detail="定时任务不存在")
    scheduler.remove_job(job_id)
    return {"job_id": job_id, "status": "deleted"}


# ---------------- WebSocket ----------------


@app.websocket("/ws/{client_id}")
async def websocket_endpoint(websocket: WebSocket, client_id: str) -> None:
    if not client_id or len(client_id) > 128:
        await websocket.close(code=1008, reason="非法 client_id")
        return
    await connections.connect(client_id, websocket)
    await connections.send(
        client_id,
        _event(WSEventType.node, "", node="connected", status="ready"),
    )
    try:
        while True:
            raw = await websocket.receive_text()
            try:
                message = json.loads(raw)
                message_type = message.get("type")
                if message_type == "chat":
                    request = ChatRequest(
                        message=message.get("message", ""),
                        thread_id=message.get("thread_id"),
                        client_id=client_id,
                    )
                    _track(_start_chat(request, client_id))
                elif message_type == "approval":
                    response = ApprovalResponse(
                        thread_id=message.get("thread_id", ""),
                        approval_id=message.get("approval_id", ""),
                        decision=message.get("decision", ""),
                    )
                    state = get_runtime().state(response.thread_id)
                    pending = state.get("pending_approval") or {}
                    if not pending or pending.get("approval_id") != response.approval_id:
                        await connections.send(
                            client_id,
                            _event(
                                WSEventType.error,
                                response.thread_id,
                                message="审批已过期或不匹配",
                            ),
                        )
                    else:
                        _track(_resume_approval(response, client_id))
                elif message_type == "ping":
                    await websocket.send_json({"type": "pong"})
                else:
                    await connections.send(
                        client_id,
                        _event(WSEventType.error, "", message="未知消息类型"),
                    )
            except (json.JSONDecodeError, ValidationError) as exc:
                await connections.send(
                    client_id,
                    _event(WSEventType.error, "", message=f"消息格式错误：{exc}"),
                )
            except HTTPException as exc:
                await connections.send(
                    client_id,
                    _event(WSEventType.error, message.get("thread_id", ""), message=str(exc.detail)),
                )
    except WebSocketDisconnect:
        connections.disconnect(client_id, websocket)
    except Exception:  # noqa: BLE001
        logger.exception("WebSocket 异常 client=%s", client_id)
        connections.disconnect(client_id, websocket)
