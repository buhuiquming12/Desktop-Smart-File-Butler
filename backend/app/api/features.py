"""P0–P6 的 REST 路由：预览、重复文件、内容索引与搜索、自动整理规则、回收站。

这些能力都由确定性后端逻辑驱动，前端只负责选择与展示；所有写操作仍然经过
``resolve_in_sandbox``、目录锁、审计与回滚，和 Agent 工具走同一条链路。
"""
from __future__ import annotations

from typing import Any, Callable, Dict

from fastapi import APIRouter, HTTPException, Query, status

from .. import db
from ..models import (
    DuplicateCleanupRequest,
    DuplicateScanRequest,
    IndexRequest,
    PreviewConfirm,
    PreviewCreate,
    PreviewExecute,
    RuleCreate,
    RuleUpdate,
    SearchRequest,
    TrashRestoreRequest,
)
from ..security import SandboxViolation
from ..tools import duplicates, indexer, preview, trash, watcher
from ..tools.watcher import DEFAULT_SETTLE_SECONDS

# PreviewError.code → HTTP 状态码
_PREVIEW_STATUS = {
    "not_found": 404,
    "approval_required": 409,
    "confirm_required": 409,
    "selection_changed": 409,
    "targets_changed": 409,
    "stale_preview": 409,
    "not_ready": 409,
    "invalid": 422,
    "empty_selection": 422,
    "unknown_items": 422,
    "duplicate_items": 422,
}


def _preview_error(exc: preview.PreviewError) -> HTTPException:
    return HTTPException(
        status_code=_PREVIEW_STATUS.get(exc.code, 422),
        detail={"message": exc.message, "code": exc.code, **exc.detail},
    )


def build_feature_router(get_llm: Callable[[], Any]) -> APIRouter:
    router = APIRouter(prefix="/api")

    # ---------------- P0：操作预览 ----------------

    @router.post("/previews", status_code=status.HTTP_201_CREATED)
    def create_preview(body: PreviewCreate) -> Dict[str, Any]:
        spec = body.model_dump()
        filter_values = spec.pop("filter")
        spec["filter"] = filter_values
        try:
            return preview.create(
                spec,
                llm=get_llm(),
                content_budget=int(spec.get("content_budget") or 0),
            )
        except preview.PreviewError as exc:
            raise _preview_error(exc) from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 - 沙箱越界等参数类错误统一 422
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.get("/previews")
    def list_previews(limit: int = Query(20, ge=1, le=100)) -> list[Dict[str, Any]]:
        return db.list_previews(limit)

    @router.get("/previews/{preview_id}")
    def read_preview(
        preview_id: str,
        offset: int = Query(0, ge=0),
        limit: int = Query(100, ge=1, le=500),
    ) -> Dict[str, Any]:
        try:
            return preview.page(preview_id, offset=offset, limit=limit)
        except preview.PreviewError as exc:
            raise _preview_error(exc) from exc

    @router.post("/previews/{preview_id}/confirm")
    def confirm_preview(preview_id: str, body: PreviewConfirm) -> Dict[str, Any]:
        """把当前选中的条目与最终落点绑定成一次性确认；不修改文件系统。"""
        try:
            return preview.confirm(
                preview_id,
                body.item_ids,
                approve=body.approve,
                category_overrides=body.category_overrides,
            )
        except preview.PreviewError as exc:
            raise _preview_error(exc) from exc

    @router.post("/previews/{preview_id}/execute")
    def execute_preview(preview_id: str, body: PreviewExecute) -> Dict[str, Any]:
        try:
            return preview.execute(
                preview_id,
                body.item_ids,
                confirm_token=body.confirm_token,
                category_overrides=body.category_overrides,
            )
        except preview.PreviewError as exc:
            raise _preview_error(exc) from exc

    # ---------------- P1：重复文件 ----------------

    @router.post("/duplicates/scan", status_code=status.HTTP_202_ACCEPTED)
    def start_duplicate_scan(body: DuplicateScanRequest) -> Dict[str, Any]:
        try:
            return duplicates.start_scan(body.directories, min_size=body.min_size)
        except (duplicates.DuplicateError, SandboxViolation, FileNotFoundError, NotADirectoryError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.get("/duplicates/{scan_id}")
    def read_duplicate_scan(scan_id: str) -> Dict[str, Any]:
        try:
            return duplicates.get(scan_id)
        except duplicates.DuplicateError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.post("/duplicates/{scan_id}/cancel")
    def cancel_duplicate_scan(scan_id: str) -> Dict[str, Any]:
        try:
            return duplicates.cancel(scan_id)
        except duplicates.DuplicateError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.post("/duplicates/{scan_id}/cleanup")
    def cleanup_duplicates(scan_id: str, body: DuplicateCleanupRequest) -> Dict[str, Any]:
        if not body.confirm:
            raise HTTPException(
                status_code=409,
                detail={"message": "清理会删除重复文件（进入回收站），需要显式确认", "code": "confirm_required"},
            )
        try:
            return duplicates.cleanup(scan_id, keep_map=body.keep_map, group_ids=body.group_ids)
        except duplicates.DuplicateError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    # ---------------- P2：内容索引与搜索 ----------------

    @router.post("/index/scan", status_code=status.HTTP_202_ACCEPTED)
    def start_index(body: IndexRequest) -> Dict[str, Any]:
        try:
            return indexer.start_index(body.directory, recursive=body.recursive)
        except (indexer.IndexingError, SandboxViolation, FileNotFoundError, NotADirectoryError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.get("/index/jobs/{job_id}")
    def index_job(job_id: str) -> Dict[str, Any]:
        try:
            return indexer.job_status(job_id)
        except indexer.IndexingError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.post("/index/jobs/{job_id}/cancel")
    def cancel_index(job_id: str) -> Dict[str, Any]:
        try:
            return indexer.cancel_index(job_id)
        except indexer.IndexingError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.get("/index/stats")
    def index_stats() -> Dict[str, Any]:
        return indexer.index_stats()

    @router.post("/search")
    def search_content(body: SearchRequest) -> Dict[str, Any]:
        try:
            return indexer.search(
                body.query, limit=body.limit, offset=body.offset, backend_name=body.backend
            )
        except indexer.IndexingError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    # ---------------- P5：自动整理规则 ----------------

    @router.get("/rules")
    def list_rules() -> Dict[str, Any]:
        return {"rules": watcher.list_rules(), "running": watcher.watcher_running()}

    @router.post("/rules", status_code=status.HTTP_201_CREATED)
    def create_rule(body: RuleCreate) -> Dict[str, Any]:
        try:
            return watcher.create_rule(
                directory=body.directory,
                dest_root=body.dest_root,
                mode=body.mode,
                instruction=body.instruction,
                enabled=body.enabled,
                settle_seconds=body.settle_seconds or DEFAULT_SETTLE_SECONDS,
            )
        except (SandboxViolation, FileNotFoundError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.put("/rules/{rule_id}")
    def update_rule(rule_id: str, body: RuleUpdate) -> Dict[str, Any]:
        changes = body.model_dump(exclude_unset=True)
        try:
            return watcher.update_rule(rule_id, **changes)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.delete("/rules/{rule_id}")
    def delete_rule(rule_id: str) -> Dict[str, Any]:
        if not watcher.delete_rule(rule_id):
            raise HTTPException(status_code=404, detail="整理规则不存在")
        return {"rule_id": rule_id, "status": "deleted"}

    @router.post("/rules/{rule_id}/run")
    def run_rule(rule_id: str) -> Dict[str, Any]:
        try:
            return watcher.run_rule_once(rule_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    # ---------------- P6：回收站 ----------------

    @router.get("/trash")
    def list_trash(
        query: str = Query("", max_length=200),
        limit: int = Query(200, ge=1, le=500),
        offset: int = Query(0, ge=0),
    ) -> Dict[str, Any]:
        return trash.list_entries(query=query, limit=limit, offset=offset)

    @router.post("/trash/restore")
    def restore_trash(body: TrashRestoreRequest) -> Dict[str, Any]:
        return trash.restore(body.op_ids)

    return router
