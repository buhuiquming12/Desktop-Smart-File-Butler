"""FastAPI 入口：REST、WebSocket、Agent 事件流与生命周期管理。"""
from __future__ import annotations

import asyncio
import json
import threading
import uuid
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Dict, Iterable, Optional, Set, Tuple

import os
import secrets
import weakref
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.websockets import WebSocketState

import httpx

from . import db, llm_config, sandbox_config
from .api import events as ws_events
from .api import rest as rest_api
from .agent.graph import AgentRuntime
from .config import get_settings
from .logging_conf import get_logger, setup_logging
from .models import (
    ApprovalResponse,
    ChatRequest,
    JobCreate,
    LLMModelsRequest,
    LLMSettingsUpdate,
    PreferenceUpdate,
    SandboxSettingsUpdate,
    ScheduledJob,
    WSEvent,
    WSEventType,
)
from .security import SandboxViolation, resolve_in_sandbox
from .tools import filesystem, scheduler

settings = get_settings()
setup_logging(settings.log_dir)
logger = get_logger(__name__)

_runtime: Optional[AgentRuntime] = None
_runtime_lock = threading.Lock()
_background_tasks: Set[asyncio.Task[Any]] = set()
# 用户请求中止的会话 id；执行循环在每个 update 边界检查并停止（P1-4）。
_cancel_requested: Set[str] = set()
# 弱引用避免攻击者持续提交随机 thread_id 时让锁表永久增长。正在持有或等待锁的
# 协程都有强引用，因此不会在使用期间被回收。
_thread_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()
# REST/WS 接受聊天后、首个 checkpoint 写入前也允许取消；计数兼容同一 thread 的
# 多个排队请求，避免用一个 bool 时先结束的请求误删后一个请求的活动标记。
_active_threads: Dict[str, int] = {}
_main_loop: Optional[asyncio.AbstractEventLoop] = None

# ---------------- 会话令牌与来源校验（P0-2） ----------------
#
# 威胁：CORS 中间件不作用于 WebSocket，本机任意网页均可连上 /ws 驱动 Agent，
# 或 PUT /api/settings/llm 把 openai_base_url 改到攻击者服务器，再触发一次对话，
# 后端便会带着已保存的 API Key 以 Bearer 请求该地址——等于把密钥读走。
#
# 防线（两层，令牌为硬门槛，来源校验为纵深防御）：
#   1. 启动时生成一次性令牌，写入用户目录下的会话文件；Electron 主进程读取后经
#      preload 注入渲染进程。REST 用请求头 X-Butler-Token 携带，WS 用查询参数 token。
#      跨源网页拿不到该令牌，因此无法伪造请求。
#   2. 仅放行本机 origin（http/https + 127.0.0.1/localhost/::1）；opaque 的 "null"
#      origin（本地任意 html 文件）一律拒绝。
_SESSION_TOKEN = secrets.token_urlsafe(32)
_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


def get_session_token() -> str:
    """返回本次进程的会话令牌。"""
    return _SESSION_TOKEN


def _session_file_path() -> Path:
    override = os.environ.get("BUTLER_SESSION_FILE")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".desktop-smart-file-butler" / "session.json"


def _write_session_file() -> None:
    """把令牌与端口写入会话文件，供 Electron 主进程读取后注入渲染进程。"""
    path = _session_file_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"token": _SESSION_TOKEN, "host": settings.host, "port": settings.port}
        path.write_text(json.dumps(payload), encoding="utf-8")
        try:
            os.chmod(path, 0o600)  # best-effort：Windows 上权限位有限
        except OSError:
            pass
        logger.info("会话令牌已写入 %s（令牌本身不记录到日志）", path)
    except OSError:
        logger.exception("写入会话令牌文件失败: %s", path)


def _origin_allowed(origin: Optional[str]) -> bool:
    """判断请求来源是否为本机。缺省 Origin（非浏览器 / 同源 GET）放行，令牌仍是硬门槛。"""
    if not origin:
        return True
    if origin == "null":
        return False
    try:
        parsed = urlsplit(origin)
    except ValueError:
        return False
    return parsed.scheme in ("http", "https") and parsed.hostname in _LOCAL_HOSTS


def _token_valid(token: Optional[str]) -> bool:
    return bool(token) and secrets.compare_digest(token, _SESSION_TOKEN)


def get_runtime() -> AgentRuntime:
    """惰性构造运行时，使健康检查不依赖模型服务是否已经配置。"""
    global _runtime
    if _runtime is None:
        with _runtime_lock:
            if _runtime is None:
                _runtime = AgentRuntime()
    return _runtime


def reset_runtime() -> None:
    """丢弃已构造的运行时，使下一次会话按最新模型配置重建 LLM。"""
    global _runtime
    with _runtime_lock:
        _runtime = None


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
        """向指定客户端推送事件；对端已断开时静默丢弃，绝不向上抛（B2）。

        单个客户端的连接问题不得中断 Agent 执行：这里没有任何重试，事件发不出去
        就丢掉，前端的重连 + 兜底超时负责恢复（见 AgentSocket 与 App 的 busy 看门狗）。
        """
        websocket = self._connections.get(client_id)
        if websocket is None:
            return
        # 握手未完成或已被对端关闭：直接清理映射，避免向 dead client 反复推送。
        if websocket.application_state is not WebSocketState.CONNECTED:
            self.disconnect(client_id, websocket)
            return
        try:
            await websocket.send_json(event.model_dump(mode="json"))
        except (RuntimeError, WebSocketDisconnect):
            # 发送瞬间对端断开；后续事件会落到上面的分支。
            logger.debug("推送时发现连接已断开，已清理 client=%s", client_id)
            self.disconnect(client_id, websocket)
        except Exception:  # noqa: BLE001 - 推送失败不得冒泡到会话执行循环
            logger.exception("推送事件失败 client=%s type=%s", client_id, event.type)
            self.disconnect(client_id, websocket)

    async def broadcast(self, event: WSEvent) -> None:
        """向当前所有渲染进程广播后台/定时任务事件。"""
        client_ids = list(self._connections)
        if client_ids:
            await asyncio.gather(*(self.send(client_id, event) for client_id in client_ids))


connections = ConnectionManager()


def _thread_lock(thread_id: str) -> asyncio.Lock:
    """同一 thread 的启动/恢复必须串行，防止重复审批或并发对话重复执行工具。"""
    lock = _thread_locks.get(thread_id)
    if lock is None:
        lock = asyncio.Lock()
        _thread_locks[thread_id] = lock
    return lock


def _register_thread(thread_id: str) -> None:
    _active_threads[thread_id] = _active_threads.get(thread_id, 0) + 1


def _unregister_thread(thread_id: str) -> None:
    remaining = _active_threads.get(thread_id, 0) - 1
    if remaining > 0:
        _active_threads[thread_id] = remaining
    else:
        _active_threads.pop(thread_id, None)


def _request_cancel(thread_id: str) -> bool:
    """仅给真实活动/待审批会话登记取消，避免陈旧标记误杀未来同 ID 会话。"""
    if _active_threads.get(thread_id, 0) > 0:
        _cancel_requested.add(thread_id)
        return True
    try:
        state = get_runtime().state(thread_id)
    except Exception:  # noqa: BLE001 - 取消不存在的会话不应触发新的服务故障
        logger.exception("检查待取消会话失败 thread=%s", thread_id)
        return False
    if state.get("status") in {"perceiving", "planning", "running", "waiting_approval"}:
        _cancel_requested.add(thread_id)
        return True
    return False


def _scheduled_runner(directory: str, instruction: str) -> None:
    """APScheduler 线程入口；把实际流消费交回主事件循环并广播给 UI。"""
    thread_id = f"scheduled-{uuid.uuid4().hex}"
    message = f"在目录 {directory} 执行以下定时整理任务：{instruction}"
    loop = _main_loop
    if loop is None or loop.is_closed():
        logger.error("定时任务无法启动：主事件循环不可用 thread=%s", thread_id)
        return
    try:
        iterator = get_runtime().start_stream(message, thread_id)
        future = asyncio.run_coroutine_threadsafe(
            _run_stream(iterator, thread_id, None, broadcast=True), loop
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


async def _dispatch(client_id: Optional[str], thread_id: str, item: Any) -> None:
    """区分多路 stream 输出：("messages"|"updates", data) 元组，或单模式 updates 字典。"""
    await ws_events.dispatch(connections.send, get_runtime().state, client_id, thread_id, item)


async def _dispatch_broadcast(thread_id: str, item: Any) -> None:
    async def send_all(_: str, outgoing: WSEvent) -> None:
        await connections.broadcast(outgoing)

    await ws_events.dispatch(send_all, get_runtime().state, "broadcast", thread_id, item)


async def _run_stream(
    iterator: Iterable[Dict[str, Any]],
    thread_id: str,
    client_id: Optional[str],
    *,
    broadcast: bool = False,
    defer_cancel_once: bool = False,
) -> Dict[str, Any]:
    """逐项消费同步 LangGraph 流，同时向 Electron 推送进度。"""
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
                    runtime = get_runtime()
                    cancel_state = getattr(runtime, "cancel", None)
                    if callable(cancel_state):
                        try:
                            cancel_state(thread_id)
                        except Exception:  # noqa: BLE001 - 尚无首个 checkpoint 时仍要完成取消
                            logger.exception("写入取消 checkpoint 失败 thread=%s", thread_id)
                    terminal = _event(
                        WSEventType.done, thread_id,
                        status="cancelled",
                        message="任务已停止。当前步骤前的操作已保留，可在操作日志中查看或撤销。",
                        summary=ws_events.build_task_summary(runtime.state(thread_id).get("observations", [])),
                    )
                    if broadcast:
                        await connections.broadcast(terminal)
                    elif client_id:
                        await connections.send(client_id, terminal)
                    return runtime.state(thread_id)
            has_item, item = await asyncio.to_thread(_next_update, iterator)
            if not has_item:
                break
            if item:
                if broadcast:
                    await _dispatch_broadcast(thread_id, item)
                else:
                    await _dispatch(client_id, thread_id, item)

        state = get_runtime().state(thread_id)
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
    try:
        async with _thread_lock(thread_id):
            # 启动也在终态兜底内：模型未配置时 AgentRuntime 构造就可能失败。
            try:
                iterator = get_runtime().start_stream(request.message.strip(), thread_id)
            except Exception as exc:  # noqa: BLE001
                logger.exception("启动会话失败 thread=%s", thread_id)
                await _fail_terminal(client_id, thread_id, f"无法启动会话：{exc}", detail=str(exc))
                return thread_id

            await _run_stream(iterator, thread_id, client_id)
            return thread_id
    finally:
        _unregister_thread(thread_id)
        # 正常/失败终态不应遗留取消标记；等待审批时则必须保留，让随后取得锁的
        # _resolve_cancelled_approval 能以 reject 清掉 interrupt 后写入 cancelled。
        try:
            state = get_runtime().state(thread_id)
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
            state = get_runtime().state(response.thread_id)
            pending = state.get("pending_approval") or {}
            if not pending:
                raise HTTPException(status_code=409, detail="该会话没有待审批操作")
            if pending.get("approval_id") != response.approval_id:
                raise HTTPException(status_code=409, detail="审批已过期或不匹配")

            cancelling = cancelling or response.thread_id in _cancel_requested
            decision = "reject" if cancelling else response.decision.value
            iterator = get_runtime().resume_stream(response.thread_id, decision)
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


# ---------------- REST ----------------


@app.get("/api/health")
def health() -> Dict[str, Any]:
    config = llm_config.get_effective_config()
    return {
        "status": "ok",
        "model_provider": config.provider,
        "sandbox_configured": bool(sandbox_config.effective_roots()),
    }


@app.get("/api/config")
def public_config() -> Dict[str, Any]:
    """仅返回非敏感配置；API key 永不暴露给渲染进程。"""
    config = llm_config.get_effective_config()
    return {
        "model_provider": config.provider,
        "openai_model": config.openai_model,
        "openai_api_key_set": bool(config.openai_api_key),
        "ollama_model": config.ollama_model,
        "ollama_base_url": config.ollama_base_url,
        "sandbox_roots": [str(path) for path in sandbox_config.effective_roots()],
        "ocr_enabled": bool(settings.tesseract_cmd),
    }


# ---------------- 沙箱根目录（前端可配，DB 覆盖 .env） ----------------


@app.get("/api/settings/sandbox")
def get_sandbox_settings() -> Dict[str, Any]:
    """返回当前生效的沙箱根目录，以及是否来自 DB 覆盖。"""
    override = db.get_preference(sandbox_config.ROOTS_KEY)
    return {
        "roots": [str(p) for p in sandbox_config.effective_roots()],
        "source": "database" if override else "env",
        "env_roots": [str(p) for p in settings.sandbox_root_paths],
    }


@app.put("/api/settings/sandbox")
def update_sandbox_settings(body: SandboxSettingsUpdate) -> Dict[str, Any]:
    """保存沙箱根目录覆盖项（权限变更）。空列表清除覆盖、回退 .env。

    因 effective_roots 每次读库，保存后对后续所有文件操作立即生效。
    """
    try:
        sandbox_config.save_roots(body.roots)
    except sandbox_config.InvalidSandboxRoot as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return get_sandbox_settings()


# ---------------- 模型配置 ----------------


@app.get("/api/settings/llm")
def get_llm_settings() -> Dict[str, Any]:
    """返回当前生效的模型配置。API Key 不回传，仅以布尔标记是否已配置。"""
    config = llm_config.get_effective_config()
    return {
        "provider": config.provider,
        "openai_base_url": config.openai_base_url,
        "openai_model": config.openai_model,
        "openai_api_key_set": bool(config.openai_api_key),
        "ollama_base_url": config.ollama_base_url,
        "ollama_model": config.ollama_model,
        "structured_output_mode": config.structured_output_mode,
    }


@app.put("/api/settings/llm")
def update_llm_settings(body: LLMSettingsUpdate) -> Dict[str, Any]:
    """保存模型配置覆盖项并立即生效（下一次会话重建 LLM）。"""
    provided = body.model_dump(exclude_unset=True)
    if "provider" in provided and provided["provider"]:
        provider = str(provided["provider"]).lower()
        if provider not in ("openai", "ollama"):
            raise HTTPException(status_code=422, detail="provider 仅支持 openai 或 ollama")
        provided["provider"] = provider

    if provided.get("structured_output_mode"):
        mode = str(provided["structured_output_mode"]).lower()
        if mode not in llm_config.STRUCTURED_OUTPUT_MODES:
            raise HTTPException(
                status_code=422, detail="structured_output_mode 仅支持 auto 或 prompt"
            )
        provided["structured_output_mode"] = mode

    llm_config.save_overrides({k: (v if v is not None else "") for k, v in provided.items()})
    reset_runtime()
    return get_llm_settings()


@app.post("/api/settings/llm/models")
async def list_llm_models(body: LLMModelsRequest) -> Dict[str, Any]:
    """探测 OpenAI 兼容 / Ollama 服务的可用模型列表。

    未显式提供的字段回退到已保存配置；密钥留空则使用已保存密钥。
    """
    config = llm_config.get_effective_config()
    provider = (body.provider or config.provider).lower()

    try:
        if provider == "ollama":
            base = (body.base_url or config.ollama_base_url or "").rstrip("/")
            if not base:
                raise HTTPException(status_code=422, detail="缺少 Ollama Base URL")
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(f"{base}/api/tags")
                resp.raise_for_status()
                data = resp.json()
            models = sorted({item.get("name", "") for item in data.get("models", []) if item.get("name")})
            return {"provider": "ollama", "models": models}

        # OpenAI 兼容协议
        base = (body.base_url or config.openai_base_url or "https://api.openai.com/v1").rstrip("/")
        api_key = body.api_key or config.openai_api_key
        if not api_key:
            raise HTTPException(status_code=422, detail="缺少 API Key")
        headers = {"Authorization": f"Bearer {api_key}"}
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(f"{base}/models", headers=headers)
            resp.raise_for_status()
            data = resp.json()
        items = data.get("data", data if isinstance(data, list) else [])
        models = sorted({item.get("id", "") for item in items if item.get("id")})
        return {"provider": "openai", "models": models}
    except httpx.HTTPStatusError as exc:
        detail = f"服务返回 {exc.response.status_code}"
        raise HTTPException(status_code=502, detail=f"获取模型失败：{detail}") from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"无法连接模型服务：{exc}") from exc


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
    return state


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


@app.get("/api/operations")
def operations(limit: int = Query(100, ge=1, le=500)) -> list[Dict[str, Any]]:
    return [item.model_dump(mode="json") for item in db.recent_operations(limit)]


@app.post("/api/operations/{op_id}/rollback")
def rollback_operation(op_id: int) -> Dict[str, Any]:
    """回滚单条操作（move/rename/delete）：把文件从 dest 移回 target。"""
    op = db.get_operation(op_id)
    if op is None:
        raise HTTPException(status_code=404, detail="操作记录不存在")
    try:
        result = filesystem.restore_operation(op)
    except (SandboxViolation, FileNotFoundError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if result["status"] == "failed":
        raise HTTPException(status_code=409, detail=result["detail"])
    return {"op_id": op_id, **result}


@app.post("/api/threads/{thread_id}/rollback")
def rollback_thread(thread_id: str) -> Dict[str, Any]:
    """先预检全部目标，再按逆序回滚，返回可直接展示的分级汇总。"""
    ops = db.operations_for_thread(thread_id)
    reversible = [op for op in ops if op.action in ("move", "rename", "delete") and op.status == "ok" and op.dest]
    if not reversible:
        raise HTTPException(status_code=404, detail="没有可回滚的操作")
    return rest_api.rollback_summary(thread_id, reversible, filesystem.preflight_restore, filesystem.restore_operation)


@app.get("/api/preferences")
def preferences() -> list[Dict[str, str]]:
    return [item.model_dump() for item in db.all_preferences()]


@app.put("/api/preferences/{key}")
def update_preference(key: str, body: PreferenceUpdate) -> Dict[str, str]:
    try:
        normalized_key = db.validate_public_preference_key(key)
        db.set_preference(normalized_key, body.value)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"key": normalized_key, "value": body.value}


@app.get("/api/jobs")
def jobs() -> list[Dict[str, Any]]:
    return [item.model_dump() for item in db.list_jobs()]


@app.post("/api/jobs", status_code=status.HTTP_201_CREATED)
def create_job(body: JobCreate) -> Dict[str, Any]:
    try:
        directory_path = resolve_in_sandbox(body.directory, must_exist=True)
        directory = str(directory_path)
        scheduler.validate_cron(body.cron)
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
    # backend/app/main.py -> parents[2] 为仓库根目录
    candidate = Path(__file__).resolve().parents[2] / "frontend" / "dist"
    return candidate if candidate.is_dir() else None


_dist = _frontend_dist()
if _dist is not None:
    app.mount("/", StaticFiles(directory=str(_dist), html=True), name="frontend")
    logger.info("已挂载前端静态资源: %s", _dist)
else:
    logger.info("未发现前端构建产物，跳过静态托管（开发模式使用 Vite dev server）")
