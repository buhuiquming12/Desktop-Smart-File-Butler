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


class LLMSettingsUpdate(BaseModel):
    """前端保存模型配置。字段留空表示不修改；显式传空字符串表示清除覆盖、回退 .env。"""
    provider: Optional[str] = None
    openai_base_url: Optional[str] = Field(default=None, max_length=500)
    openai_model: Optional[str] = Field(default=None, max_length=200)
    openai_api_key: Optional[str] = Field(default=None, max_length=500)
    ollama_base_url: Optional[str] = Field(default=None, max_length=500)
    ollama_model: Optional[str] = Field(default=None, max_length=200)
    structured_output_mode: Optional[str] = Field(default=None, max_length=20)


class SandboxSettingsUpdate(BaseModel):
    """前端保存沙箱根目录覆盖项（权限变更）。空列表表示清除覆盖、回退 .env。"""
    roots: List[str] = Field(default_factory=list, max_length=50)


class ToolSettingsUpdate(BaseModel):
    """前端保存外部工具路径（OCR 等）。字段留空表示不修改；显式传空字符串表示清除覆盖、回退 .env。"""
    tesseract_cmd: Optional[str] = Field(default=None, max_length=500)
    tessdata_dir: Optional[str] = Field(default=None, max_length=500)


class WorkspaceSettingsUpdate(BaseModel):
    """前端保存默认管理目录（Agent 在未指定路径时默认操作的位置）。空串表示清除。"""
    default_managed_root: str = Field(default="", max_length=500)


class LLMModelsRequest(BaseModel):
    """探测某个 OpenAI 兼容 / Ollama 服务的可用模型列表。

    留空则使用当前已保存的配置；密钥留空则使用已保存的密钥。
    """
    provider: Optional[str] = None
    base_url: Optional[str] = Field(default=None, max_length=500)
    api_key: Optional[str] = Field(default=None, max_length=500)


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
    status: str                       # ok / failed / rejected / rollback
    detail: str = ""
    thread_id: Optional[str] = None


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
