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
from pathlib import Path
from urllib.parse import urlsplit

from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse

import httpx

from . import db, llm_config, sandbox_config
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
    _write_session_file()
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
    """回滚某会话的所有可逆操作（按发生顺序倒序还原）。"""
    ops = db.operations_for_thread(thread_id)
    reversible = [
        op for op in ops
        if op.action in ("move", "rename", "delete") and op.status == "ok" and op.dest
    ]
    if not reversible:
        raise HTTPException(status_code=404, detail="该会话没有可回滚的操作")

    results = []
    for op in reversed(reversible):  # 后发生的先撤销，避免路径互相依赖
        try:
            results.append({"op_id": op.id, **filesystem.restore_operation(op)})
        except (SandboxViolation, FileNotFoundError) as exc:
            results.append({"op_id": op.id, "status": "failed", "detail": str(exc)})

    summary = {
        "thread_id": thread_id,
        "total": len(results),
        "ok": sum(1 for r in results if r["status"] == "ok"),
        "skipped": sum(1 for r in results if r["status"] == "skipped"),
        "failed": sum(1 for r in results if r["status"] == "failed"),
        "results": results,
    }
    return summary


@app.get("/api/preferences")
def preferences() -> list[Dict[str, str]]:
    return [item.model_dump() for item in db.all_preferences()]


@app.put("/api/preferences/{key}")
def update_preference(key: str, body: PreferenceUpdate) -> Dict[str, str]:
    normalized_key = key.strip()
    if not normalized_key or len(normalized_key) > 200:
        raise HTTPException(status_code=422, detail="偏好键不能为空且最多 200 字符")
    if normalized_key.startswith("__"):
        # __ 前缀为内部保留键（如沙箱根目录），不允许经普通偏好接口绕过校验写入。
        raise HTTPException(status_code=422, detail="保留键不可通过偏好接口修改")
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
