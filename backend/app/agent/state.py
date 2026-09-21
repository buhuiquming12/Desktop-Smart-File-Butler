"""LangGraph Agent 状态与结构化输出模型。"""
from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional, TypedDict

from pydantic import BaseModel, Field


ToolName = Literal[
    "scan_directory",
    "extract_text",
    "classify_file",
    "make_dir",
    "move_file",
    "rename_file",
    "delete_file",
    "write_summary",
    "set_preference",
    "create_schedule",
]


class PlanStep(BaseModel):
    """规划器生成的单个原子步骤。"""

    id: str
    description: str
    tool: ToolName
    args: Dict[str, Any] = Field(default_factory=dict)


class PlanOutput(BaseModel):
    """规划器的结构化响应。"""

    goal: str
    steps: List[PlanStep] = Field(default_factory=list)
    user_message: str = ""


class ReflectionOutput(BaseModel):
    """每次观察后的反思结果。"""

    decision: Literal["continue", "replan", "done"]
    reasoning: str
    final_response: str = ""


class AgentState(TypedDict, total=False):
    """图中传递并由 checkpointer 保存的状态。

    状态只保留 JSON 可序列化内容，避免将回调、模型对象或文件句柄写入检查点。
    """

    thread_id: str
    user_request: str
    perception: Dict[str, Any]
    plan: List[Dict[str, Any]]
    step_index: int
    current_step: Dict[str, Any]
    observations: List[Dict[str, Any]]
    reflection: Dict[str, Any]
    pending_approval: Optional[Dict[str, Any]]
    final_response: str
    status: Literal[
        "perceiving",
        "planning",
        "running",
        "waiting_approval",
        "completed",
        "failed",
        "cancelled",
    ]
    replan_count: int
    error: str
    batch_approved: bool          # 本计划的批量 move/rename 已获审批（P1-2）
    batch_rejected: bool          # 本计划的批量 move/rename 被拒绝，全部跳过
