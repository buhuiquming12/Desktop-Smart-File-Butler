"""Pydantic 数据模型：API 请求/响应 与 领域对象。"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    """发起一次 Agent 会话。"""
    message: str = Field(min_length=1, max_length=20_000)
    thread_id: Optional[str] = None  # 为空则新建会话
    client_id: Optional[str] = None  # 可选：将事件推送给指定 WebSocket 客户端


class PreferenceUpdate(BaseModel):
    value: str = Field(max_length=10_000)


class JobCreate(BaseModel):
    directory: str
    instruction: str = Field(min_length=1, max_length=20_000)
    cron: str
    enabled: bool = True


class ApprovalDecision(str, Enum):
    approve = "approve"
    reject = "reject"


class ApprovalResponse(BaseModel):
    """前端对某次审批请求的回执。"""
    thread_id: str
    approval_id: str
    decision: ApprovalDecision


class PendingApproval(BaseModel):
    """需要用户确认的高危操作。"""
    approval_id: str
    action: str                       # delete / overwrite
    target: str                       # 目标路径
    detail: str = ""                  # 人类可读描述
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class FileMeta(BaseModel):
    name: str
    path: str
    ext: str
    size: int
    modified: datetime
    is_dir: bool = False


class OperationLog(BaseModel):
    id: Optional[int] = None
    ts: datetime
    action: str
    target: str
    dest: Optional[str] = None
    status: str                       # ok / failed / rejected
    detail: str = ""


class Preference(BaseModel):
    key: str
    value: str


class ScheduledJob(BaseModel):
    job_id: str
    directory: str
    instruction: str
    cron: str                         # e.g. "0 9 * * *"
    enabled: bool = True


class WSEventType(str, Enum):
    token = "token"           # LLM 流式 token
    node = "node"             # 进入某个图节点
    tool_call = "tool_call"   # 工具调用
    tool_result = "tool_result"
    approval_required = "approval_required"
    task = "task"             # 任务看板条目更新
    done = "done"
    error = "error"


class WSEvent(BaseModel):
    type: WSEventType
    thread_id: str
    payload: Dict[str, Any] = Field(default_factory=dict)
