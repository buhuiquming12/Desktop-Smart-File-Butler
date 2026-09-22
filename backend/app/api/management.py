"""配置、审计、回滚和定时任务 REST 路由。"""
from __future__ import annotations

import uuid
from typing import Any, Callable, Dict

import httpx
from fastapi import APIRouter, HTTPException, Query, status

from .. import db, llm_config, sandbox_config
from ..config import get_settings
from ..models import (
    JobCreate,
    LLMModelsRequest,
    LLMSettingsUpdate,
    PreferenceUpdate,
    SandboxSettingsUpdate,
    ScheduledJob,
)
from ..security import SandboxViolation, resolve_in_sandbox
from ..tools import extract, filesystem, scheduler
from . import rest as rest_api


def build_management_router(reset_runtime: Callable[[], None]) -> APIRouter:
    """构造不依赖 Agent 流调度细节的管理 API。"""
    router = APIRouter(prefix="/api")

    @router.get("/health")
    def health() -> Dict[str, Any]:
        config = llm_config.get_effective_config()
        return {
            "status": "ok",
            "model_provider": config.provider,
            "sandbox_configured": bool(sandbox_config.effective_roots()),
        }

    @router.get("/config")
    def public_config() -> Dict[str, Any]:
        config = llm_config.get_effective_config()
        ocr = extract.ocr_capability()
        return {
            "model_provider": config.provider,
            "openai_model": config.openai_model,
            "openai_api_key_set": bool(config.openai_api_key),
            "ollama_model": config.ollama_model,
            "ollama_base_url": config.ollama_base_url,
            "sandbox_roots": [str(path) for path in sandbox_config.effective_roots()],
            "ocr": ocr,
            "ocr_enabled": bool(ocr.get("available")),
        }

    def sandbox_settings() -> Dict[str, Any]:
        settings = get_settings()
        override = db.get_preference(sandbox_config.ROOTS_KEY)
        return {
            "roots": [str(path) for path in sandbox_config.effective_roots()],
            "source": "database" if override else "env",
            "env_roots": [str(path) for path in settings.sandbox_root_paths],
        }

    @router.get("/settings/sandbox")
    def get_sandbox_settings() -> Dict[str, Any]:
        return sandbox_settings()

    @router.put("/settings/sandbox")
    def update_sandbox_settings(body: SandboxSettingsUpdate) -> Dict[str, Any]:
        try:
            sandbox_config.save_roots(body.roots)
        except sandbox_config.InvalidSandboxRoot as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return sandbox_settings()

    @router.get("/settings/llm")
    def get_llm_settings() -> Dict[str, Any]:
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

    @router.put("/settings/llm")
    def update_llm_settings(body: LLMSettingsUpdate) -> Dict[str, Any]:
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
                    status_code=422,
                    detail="structured_output_mode 仅支持 auto 或 prompt",
                )
            provided["structured_output_mode"] = mode
        llm_config.save_overrides({
            key: value if value is not None else "" for key, value in provided.items()
        })
        reset_runtime()
        return get_llm_settings()

    @router.post("/settings/llm/models")
    async def list_llm_models(body: LLMModelsRequest) -> Dict[str, Any]:
        config = llm_config.get_effective_config()
        provider = (body.provider or config.provider).lower()
        try:
            if provider == "ollama":
                base = (body.base_url or config.ollama_base_url or "").rstrip("/")
                if not base:
                    raise HTTPException(status_code=422, detail="缺少 Ollama Base URL")
                async with httpx.AsyncClient(timeout=15) as client:
                    response = await client.get(f"{base}/api/tags")
                    response.raise_for_status()
                    data = response.json()
                models = sorted({
                    item.get("name", "")
                    for item in data.get("models", [])
                    if item.get("name")
                })
                return {"provider": "ollama", "models": models}

            base = (
                body.base_url or config.openai_base_url or "https://api.openai.com/v1"
            ).rstrip("/")
            api_key = body.api_key or config.openai_api_key
            if not api_key:
                raise HTTPException(status_code=422, detail="缺少 API Key")
            headers = {"Authorization": f"Bearer {api_key}"}
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.get(f"{base}/models", headers=headers)
                response.raise_for_status()
                data = response.json()
            items = data.get("data", data if isinstance(data, list) else [])
            models = sorted({item.get("id", "") for item in items if item.get("id")})
            return {"provider": "openai", "models": models}
        except httpx.HTTPStatusError as exc:
            raise HTTPException(
                status_code=502,
                detail=f"获取模型失败：服务返回 {exc.response.status_code}",
            ) from exc
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=f"无法连接模型服务：{exc}") from exc

    @router.get("/operations")
    def operations(limit: int = Query(100, ge=1, le=500)) -> list[Dict[str, Any]]:
        return [item.model_dump(mode="json") for item in db.recent_operations(limit)]

    @router.post("/operations/{op_id}/rollback")
    def rollback_operation(op_id: int) -> Dict[str, Any]:
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

    @router.post("/threads/{thread_id}/rollback")
    def rollback_thread(thread_id: str) -> Dict[str, Any]:
        operations = db.operations_for_thread(thread_id)
        reversible = [
            item for item in operations
            if item.action in ("move", "rename", "delete")
            and item.status == "ok"
            and item.dest
        ]
        if not reversible:
            raise HTTPException(status_code=404, detail="没有可回滚的操作")
        return rest_api.rollback_summary(
            thread_id,
            reversible,
            filesystem.preflight_restore,
            filesystem.restore_operation,
        )

    @router.get("/preferences")
    def preferences() -> list[Dict[str, str]]:
        return [item.model_dump() for item in db.all_preferences()]

    @router.put("/preferences/{key}")
    def update_preference(key: str, body: PreferenceUpdate) -> Dict[str, str]:
        try:
            normalized_key = db.validate_public_preference_key(key)
            db.set_preference(normalized_key, body.value)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"key": normalized_key, "value": body.value}

    @router.get("/jobs")
    def jobs() -> list[Dict[str, Any]]:
        return [item.model_dump() for item in db.list_jobs()]

    @router.post("/jobs", status_code=status.HTTP_201_CREATED)
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

    @router.delete("/jobs/{job_id}")
    def remove_job(job_id: str) -> Dict[str, str]:
        if not any(item.job_id == job_id for item in db.list_jobs()):
            raise HTTPException(status_code=404, detail="定时任务不存在")
        scheduler.remove_job(job_id)
        return {"job_id": job_id, "status": "deleted"}

    return router
