"""FastAPI 入口：REST、WebSocket、Agent 事件流与生命周期管理。"""
from __future__ import annotations

import asyncio
import json
import threading
import uuid
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Dict, Iterable, Optional, Set, Tuple

import sys
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse

from . import db
from .api import events as ws_events
from .api.auth import (
    get_session_token,
    origin_allowed as _origin_allowed,
    session_file_path as _session_file_path,
    token_valid as _token_valid,
    write_session_file as _write_session_file,
)
from .api.connections import ConnectionManager
from .api.management import build_management_router
from .agent.graph import AgentRuntime
from .config import get_settings
from .logging_conf import get_logger, setup_logging
from .models import (
    ApprovalResponse,
    ChatRequest,
    WSEvent,
    WSEventType,
)
from .runtime import threads as thread_registry
from .tools import scheduler

settings = get_settings()
setup_logging(settings.log_dir)
logger = get_logger(__name__)

_runtime: Optional[AgentRuntime] = None
_runtime_lock = threading.Lock()
_background_tasks: Set[asyncio.Task[Any]] = set()
# 用户请求中止的会话 id；执行循环在每个 update 边界检查并停止（P1-4）。
_cancel_requested = thread_registry.cancel_requested
_thread_locks = thread_registry.thread_locks
_active_threads = thread_registry.active_threads
_main_loop: Optional[asyncio.AbstractEventLoop] = None


def get_runtime() -> AgentRuntime:
    """惰性构造运行时，使健康检查不依赖模型服务是否已经配置。"""
    global _runtime
    if _runtime is None:
        with _runtime_lock:
            if _runtime is None:
                _runtime = AgentRuntime()
    return _runtime


def reset_runtime() -> None:
    """退休旧运行时；新请求用新实例，旧实例待活动流结束后关闭。"""
    global _runtime
    with _runtime_lock:
        previous = _runtime
        _runtime = None
    if previous is not None:
        retire = getattr(previous, "retire", None)
        if callable(retire):
            retire()


connections = ConnectionManager()


def _thread_lock(thread_id: str) -> asyncio.Lock:
    return thread_registry.thread_lock(thread_id)


def _register_thread(thread_id: str) -> None:
    thread_registry.register(thread_id)


def _unregister_thread(thread_id: str) -> None:
    thread_registry.unregister(thread_id)


def _request_cancel(thread_id: str) -> bool:
    return thread_registry.request_cancel(thread_id, lambda value: get_runtime().state(value))


def _scheduled_runner(directory: str, instruction: str) -> None:
    """APScheduler 线程入口；把实际流消费交回主事件循环并广播给 UI。"""
    thread_id = f"scheduled-{uuid.uuid4().hex}"
    message = f"在目录 {directory} 执行以下定时整理任务：{instruction}"
    loop = _main_loop
    if loop is None or loop.is_closed():
        logger.error("定时任务无法启动：主事件循环不可用 thread=%s", thread_id)
        return
    try:
        runtime = get_runtime()
        iterator = runtime.start_stream(message, thread_id)
        future = asyncio.run_coroutine_threadsafe(
            _run_stream(iterator, thread_id, None, runtime=runtime, broadcast=True), loop
        )
        state = future.result()
        if state.get("status") == "waiting_approval":
            logger.warning("定时任务 %s 需要危险操作审批，已广播到活动中心", thread_id)
        else:
            logger.info("定时任务 %s 结束: %s", thread_id, state.get("status"))
    except Exception:  # noqa: BLE001
        logger.exception("定时 Agent 任务失败 thread=%s", thread_id)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    global _main_loop
    db.init_db()
    _main_loop = asyncio.get_running_loop()
    _write_session_file()
    scheduler.init_scheduler(_scheduled_runner)
    logger.info("桌面智能文件管家后端已启动")
    try:
        yield
    finally:
        scheduler.shutdown()
        for task in list(_background_tasks):
            task.cancel()
        _main_loop = None
        _thread_locks.clear()
        _active_threads.clear()
        _cancel_requested.clear()
        reset_runtime()
        logger.info("桌面智能文件管家后端已停止")


app = FastAPI(
    title="桌面智能文件管家 API",
    version="0.1.0",
    lifespan=lifespan,
)
# CORS 仅为开发模式服务：Vite dev server(:5173) 与后端(:8000)跨源。
# 生产（打包）模式下前端由下方 StaticFiles 与后端同源托管，不依赖 CORS，
# 因此这里不再保留永远不会被浏览器发送的字面量 "file://"。
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://127.0.0.1:5173",
        "http://localhost:5173",
    ],
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["Content-Type", "X-Butler-Token"],
)


@app.middleware("http")
async def _auth_guard(request: Request, call_next: Any) -> Any:
    """对 /api/* 强制会话令牌 + 本机来源校验（/api/health 除外，供就绪探测）。

    静态资源与 SPA（"/"、/assets/*）不校验，浏览器需先加载 index.html 才能取得令牌。
    OPTIONS 预检交由 CORS 中间件处理。
    """
    path = request.url.path
    if (
        path.startswith("/api/")
        and path != "/api/health"
        and request.method != "OPTIONS"
    ):
        if not _origin_allowed(request.headers.get("origin")):
            return JSONResponse({"detail": "非法来源，拒绝访问"}, status_code=403)
        if not _token_valid(request.headers.get("x-butler-token")):
            return JSONResponse({"detail": "缺少或非法的会话令牌"}, status_code=401)
    return await call_next(request)


app.include_router(build_management_router(reset_runtime))


def _next_update(iterator: Iterable[Dict[str, Any]]) -> Tuple[bool, Optional[Dict[str, Any]]]:
    """在线程里取生成器下一项，避免 StopIteration 穿过 asyncio Future。"""
    try:
        return True, next(iterator)  # type: ignore[arg-type]
    except StopIteration:
        return False, None


def _event(event_type: WSEventType, thread_id: str, **payload: Any) -> WSEvent:
    return WSEvent(type=event_type, thread_id=thread_id, payload=payload)


async def _fail_terminal(
    client_id: Optional[str],
    thread_id: str,
    message: str,
    *,
    detail: str = "",
    broadcast: bool = False,
) -> None:
    """广播唯一的终态失败事件（B1 错误广播不变量）。

    任何会话启动 / 执行失败都必须经由这里收尾：前端只认终态事件来解除 busy，
    一旦有失败路径绕过它，界面就会永久停在“处理中”。message 面向用户且可审计，
    detail 保留原始异常文本供排查。
    """
    if not client_id and not broadcast:
        return
    try:
        terminal = _event(
            WSEventType.done,
            thread_id,
            status="failed",
            message=message,
            error=detail or message,
            summary=ws_events.build_task_summary([]),
        )
        if broadcast:
            await connections.broadcast(terminal)
        elif client_id:
            await connections.send(client_id, terminal)
    except Exception:  # noqa: BLE001 - 兜底广播本身失败时不应再抛出，避免掩盖原始错误
        logger.exception("广播终态失败事件时出错 thread=%s", thread_id)


async def _emit_update(
    client_id: Optional[str], thread_id: str, update: Dict[str, Any]
) -> None:
    """转发流更新事件（实现见 api.events.emit_update，此处只做依赖注入）。"""
    await ws_events.emit_update(connections.send, get_runtime().state, client_id, thread_id, update)


async def _emit_token(client_id: Optional[str], thread_id: str, data: Any) -> None:
    """把 LLM 自由文本 token 转成 token 事件；结构化输出（内容为空）自然被过滤（P1-4）。"""
    await ws_events.emit_token(connections.send, client_id, thread_id, data)


async def _dispatch(client_id: Optional[str], thread_id: str, item: Any, runtime: Optional[AgentRuntime] = None) -> None:
    """区分多路 stream 输出：("messages"|"updates", data) 元组，或单模式 updates 字典。"""
    selected = runtime or get_runtime()
    await ws_events.dispatch(connections.send, selected.state, client_id, thread_id, item)


async def _dispatch_broadcast(thread_id: str, item: Any, runtime: Optional[AgentRuntime] = None) -> None:
    async def send_all(_: str, outgoing: WSEvent) -> None:
        await connections.broadcast(outgoing)

    selected = runtime or get_runtime()
    await ws_events.dispatch(send_all, selected.state, "broadcast", thread_id, item)


async def _run_stream(
    iterator: Iterable[Dict[str, Any]],
    thread_id: str,
    client_id: Optional[str],
    *,
    runtime: Optional[AgentRuntime] = None,
    broadcast: bool = False,
    defer_cancel_once: bool = False,
) -> Dict[str, Any]:
    """逐项消费同步 LangGraph 流，同时向 Electron 推送进度。"""
    selected = runtime or get_runtime()
    retain = getattr(selected, "retain_stream", None)
    release = getattr(selected, "release_stream", None)
    if callable(retain):
        retain()
    try:
        while True:
            if thread_id in _cancel_requested:
                if defer_cancel_once:
                    # 等待审批的图必须先消费一次 reject resume，清掉持久化 interrupt；
                    # 下一轮再真正终止，避免 UI 已关闭而 checkpoint 永久待审批。
                    defer_cancel_once = False
                else:
                    _cancel_requested.discard(thread_id)
                    logger.info("会话 %s 被用户中止", thread_id)
                    cancel_state = getattr(selected, "cancel", None)
                    if callable(cancel_state):
                        try:
                            cancel_state(thread_id)
                        except Exception:  # noqa: BLE001 - 尚无首个 checkpoint 时仍要完成取消
                            logger.exception("写入取消 checkpoint 失败 thread=%s", thread_id)
                    terminal = _event(
                        WSEventType.done, thread_id,
                        status="cancelled",
                        message="任务已停止。当前步骤前的操作已保留，可在操作日志中查看或撤销。",
                        summary=ws_events.build_task_summary(selected.state(thread_id).get("observations", [])),
                    )
                    if broadcast:
                        await connections.broadcast(terminal)
                    elif client_id:
                        await connections.send(client_id, terminal)
                    return selected.state(thread_id)
            has_item, item = await asyncio.to_thread(_next_update, iterator)
            if not has_item:
                break
            if item:
                if broadcast:
                    await _dispatch_broadcast(thread_id, item, selected)
                else:
                    await _dispatch(client_id, thread_id, item, selected)

        state = selected.state(thread_id)
        if state.get("status") == "waiting_approval":
            pending = state.get("pending_approval") or {}
            if pending:
                approval_event = _event(WSEventType.approval_required, thread_id, **pending)
                if broadcast:
                    await connections.broadcast(approval_event)
                elif client_id:
                    await connections.send(client_id, approval_event)
            return state

        # 终态统一走 done（status=completed|failed），WSEventType.error 只留给非终态诊断（B1）。
        final_status = state.get("status")
        terminal = _event(
            WSEventType.done,
            thread_id,
            status=final_status,
            message=state.get("final_response", ""),
            error=state.get("error", ""),
            observations=state.get("observations", []),
            summary=ws_events.build_task_summary(state.get("observations", [])),
        )
        if broadcast:
            await connections.broadcast(terminal)
        elif client_id:
            await connections.send(
                client_id, terminal
            )
        return state
    except Exception as exc:  # noqa: BLE001
        logger.exception("Agent 执行失败 thread=%s", thread_id)
        await _fail_terminal(
            client_id, thread_id, f"任务执行失败：{exc}", detail=str(exc), broadcast=broadcast
        )
        return {"thread_id": thread_id, "status": "failed", "error": str(exc)}
    finally:
        if callable(release):
            release()


def _log_task_failure(task: asyncio.Task[Any]) -> None:
    """记录后台任务的未取异常。B1 中正是这类静默吞异常让界面卡死无迹可寻。"""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error("后台任务异常终止（前端可能未收到终态事件）", exc_info=exc)


def _track(coro: Any) -> None:
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    task.add_done_callback(_log_task_failure)


async def _start_chat(
    request: ChatRequest,
    fallback_client: Optional[str] = None,
    *,
    registered: bool = False,
) -> str:
    thread_id = request.thread_id or uuid.uuid4().hex
    client_id = request.client_id or fallback_client
    if not registered:
        _register_thread(thread_id)
    runtime: Optional[AgentRuntime] = None
    try:
        async with _thread_lock(thread_id):
            # 启动也在终态兜底内：模型未配置时 AgentRuntime 构造就可能失败。
            try:
                runtime = get_runtime()
                iterator = runtime.start_stream(request.message.strip(), thread_id)
            except Exception as exc:  # noqa: BLE001
                logger.exception("启动会话失败 thread=%s", thread_id)
                await _fail_terminal(client_id, thread_id, f"无法启动会话：{exc}", detail=str(exc))
                return thread_id

            await _run_stream(iterator, thread_id, client_id, runtime=runtime)
            return thread_id
    finally:
        _unregister_thread(thread_id)
        # 正常/失败终态不应遗留取消标记；等待审批时则必须保留，让随后取得锁的
        # _resolve_cancelled_approval 能以 reject 清掉 interrupt 后写入 cancelled。
        try:
            state = runtime.state(thread_id) if runtime is not None else {}
        except Exception:  # noqa: BLE001
            state = {}
        if not state.get("pending_approval"):
            _cancel_requested.discard(thread_id)


async def _resume_approval(
    response: ApprovalResponse,
    fallback_client: Optional[str] = None,
    *,
    cancelling: bool = False,
) -> str:
    broadcast = response.thread_id.startswith("scheduled-")
    async with _thread_lock(response.thread_id):
        try:
            runtime = get_runtime()
            state = runtime.state(response.thread_id)
            pending = state.get("pending_approval") or {}
            if not pending:
                raise HTTPException(status_code=409, detail="该会话没有待审批操作")
            if pending.get("approval_id") != response.approval_id:
                raise HTTPException(status_code=409, detail="审批已过期或不匹配")

            cancelling = cancelling or response.thread_id in _cancel_requested
            decision = "reject" if cancelling else response.decision.value
            iterator = runtime.resume_stream(response.thread_id, decision)
        except HTTPException as exc:
            if fallback_client is None:
                raise
            logger.warning("恢复会话被拒 thread=%s: %s", response.thread_id, exc.detail)
            diagnostic = _event(WSEventType.error, response.thread_id, message=str(exc.detail))
            if broadcast:
                await connections.broadcast(diagnostic)
            else:
                await connections.send(fallback_client, diagnostic)
            return response.thread_id
        except Exception as exc:  # noqa: BLE001
            logger.exception("恢复会话失败 thread=%s", response.thread_id)
            await _fail_terminal(
                fallback_client,
                response.thread_id,
                f"无法恢复会话：{exc}",
                detail=str(exc),
                broadcast=broadcast,
            )
            return response.thread_id

        await _run_stream(
            iterator,
            response.thread_id,
            fallback_client,
            runtime=runtime,
            broadcast=broadcast,
            defer_cancel_once=cancelling,
        )
        return response.thread_id


async def _resolve_cancelled_approval(
    thread_id: str, client_id: Optional[str] = None
) -> None:
    """若会话正停在 interrupt，自动以 reject 恢复一次，使取消真正持久化。"""
    try:
        state = get_runtime().state(thread_id)
        pending = state.get("pending_approval") or {}
        approval_id = str(pending.get("approval_id") or "")
        if not approval_id:
            return
        await _resume_approval(
            ApprovalResponse(
                thread_id=thread_id,
                approval_id=approval_id,
                decision="reject",
            ),
            client_id,
            cancelling=True,
        )
    except Exception:  # noqa: BLE001 - 普通运行中取消仍由 _run_stream 消费
        logger.exception("清理待审批取消状态失败 thread=%s", thread_id)


# ---------------- 会话 REST ----------------


@app.post("/api/chat", status_code=status.HTTP_202_ACCEPTED)
async def chat(request: ChatRequest) -> Dict[str, str]:
    thread_id = request.thread_id or uuid.uuid4().hex
    request.thread_id = thread_id
    _register_thread(thread_id)
    _track(_start_chat(request, registered=True))
    return {"thread_id": thread_id, "status": "accepted"}


@app.get("/api/threads/{thread_id}")
def thread_state(thread_id: str) -> Dict[str, Any]:
    state = get_runtime().state(thread_id)
    if not state:
        raise HTTPException(status_code=404, detail="会话不存在")
    return {**state, "summary": ws_events.build_task_summary(state.get("observations", []))}


@app.post("/api/threads/{thread_id}/cancel", status_code=status.HTTP_202_ACCEPTED)
async def cancel_thread(thread_id: str) -> Dict[str, str]:
    """请求中止会话：执行循环在下一个步骤边界停止，已完成步骤保留（P1-4）。"""
    if not _request_cancel(thread_id):
        return {"thread_id": thread_id, "status": "not_running"}
    _track(_resolve_cancelled_approval(thread_id))
    return {"thread_id": thread_id, "status": "cancelling"}


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


# ---------------- WebSocket ----------------


@app.websocket("/ws/{client_id}")
async def websocket_endpoint(websocket: WebSocket, client_id: str) -> None:
    if not client_id or len(client_id) > 128:
        await websocket.close(code=1008, reason="非法 client_id")
        return
    # 握手校验：CORS 不作用于 WS，此处自行校验来源 + 令牌，非法一律 1008 关闭。
    if not _origin_allowed(websocket.headers.get("origin")):
        await websocket.close(code=1008, reason="非法来源")
        return
    if not _token_valid(websocket.query_params.get("token")):
        await websocket.close(code=1008, reason="缺少或非法的会话令牌")
        return
    await connections.connect(client_id, websocket)
    await connections.send(
        client_id,
        _event(WSEventType.node, "", node="connected", status="ready"),
    )
    try:
        while True:
            raw = await websocket.receive_text()
            # 每轮重置：解析失败时 thread_id 必须为空，绝不能沿用上一轮的值
            # （旧代码在 except HTTPException 里直接读 message，json.loads 抛错时
            # 要么 NameError，要么把上一轮的消息投递到错误的会话）。
            thread_id = ""
            try:
                message = json.loads(raw)
                if not isinstance(message, dict):
                    await connections.send(
                        client_id,
                        _event(WSEventType.error, "", message="消息格式错误：应为 JSON 对象"),
                    )
                    continue
                thread_id = str(message.get("thread_id") or "")
                message_type = message.get("type")
                if message_type == "chat":
                    request = ChatRequest(
                        message=message.get("message", ""),
                        thread_id=message.get("thread_id"),
                        client_id=client_id,
                    )
                    request.thread_id = request.thread_id or uuid.uuid4().hex
                    _register_thread(request.thread_id)
                    _track(_start_chat(request, client_id, registered=True))
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
                elif message_type == "cancel":
                    cancel_thread_id = message.get("thread_id", "")
                    if cancel_thread_id and _request_cancel(cancel_thread_id):
                        _track(_resolve_cancelled_approval(cancel_thread_id, client_id))
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
                    _event(WSEventType.error, thread_id, message=str(exc.detail)),
                )
    except WebSocketDisconnect:
        connections.disconnect(client_id, websocket)
    except Exception:  # noqa: BLE001
        logger.exception("WebSocket 异常 client=%s", client_id)
        connections.disconnect(client_id, websocket)


# ---------------- 静态前端（生产模式，前后端同源） ----------------
#
# 打包后 Electron 直接 loadURL 到本后端，index.html 与 /api、/ws 同源，
# 从根源消除 CORS 依赖（见 P0-1）。此挂载放在所有 API / WS 路由注册之后，
# 因此 /api/* 与 /ws/* 仍优先匹配；其余路径回落到静态资源。
def _frontend_dist() -> Optional[Path]:
    """定位打包后的前端产物目录；不存在（纯后端 / 开发模式）时返回 None。"""
    import os

    override = os.environ.get("FRONTEND_DIST")
    if override:
        candidate = Path(override).expanduser()
        return candidate if candidate.is_dir() else None
    if getattr(sys, "frozen", False):
        candidate = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)) / "frontend" / "dist"
        return candidate if candidate.is_dir() else None
    # backend/app/main.py -> parents[2] 为仓库根目录
    candidate = Path(__file__).resolve().parents[2] / "frontend" / "dist"
    return candidate if candidate.is_dir() else None


_dist = _frontend_dist()
if _dist is not None:
    app.mount("/", StaticFiles(directory=str(_dist), html=True), name="frontend")
    logger.info("已挂载前端静态资源: %s", _dist)
else:
    logger.info("未发现前端构建产物，跳过静态托管（开发模式使用 Vite dev server）")
