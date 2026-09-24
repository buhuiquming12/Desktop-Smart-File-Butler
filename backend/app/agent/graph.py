"""LangGraph Agent 核心：感知 → 规划 → 工具调用 → 观察 → 反思。

删除操作在图内使用 ``interrupt`` 暂停；前端审批后用 ``Command(resume=...)``
恢复同一 thread。所有实际磁盘访问仍由工具层执行沙箱校验。
"""
from __future__ import annotations

import json
import uuid
from typing import Any, Dict, Iterable, Literal, Optional

import sqlite3
import threading

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from .. import db
from ..logging_conf import get_logger
from ..security import resolve_in_sandbox
from ..tools import manifests
from ..policy import PolicyEngine
from .checkpoints import (
    _UUID_EPOCH,
    build_checkpointer as _build_checkpointer,
    checkpoint_created_at as _checkpoint_created_at,
    cleanup_checkpoints,
)
from .llm import build_llm, build_structured_llm
from .prompts import PLANNER_SYSTEM_PROMPT, REFLECTION_SYSTEM_PROMPT
from .state import AgentState, PlanOutput, ReflectionOutput
from .tool_executor import SUMMARY_CHUNK_CHARS, SUMMARY_MAX_CHUNKS, ToolExecutor

logger = get_logger(__name__)
_MAX_REPLANS = 3
_MAX_PLAN_STEPS = 30           # 单个计划最多步数（提示词同步约束）
_SUPER_STEPS_PER_STEP = 2      # 移除 observe 后每步占用 act + reflecting 两个超级步
_MAX_SCAN_RESULTS = 500
_BATCH_TOOLS = ("move_file", "rename_file")  # 批量破坏性操作，超阈值需审批（P1-2）
_DEFAULT_BATCH_THRESHOLD = 20
_BATCH_PREVIEW_LIMIT = 10      # 审批弹窗最多预览的条目数


def _batch_threshold() -> int:
    """批量审批阈值，可用偏好 batch_approval_threshold 覆盖（默认 20）。"""
    raw = db.get_preference("batch_approval_threshold")
    try:
        value = int(raw) if raw is not None else _DEFAULT_BATCH_THRESHOLD
    except (TypeError, ValueError):
        return _DEFAULT_BATCH_THRESHOLD
    return value if value > 0 else _DEFAULT_BATCH_THRESHOLD


def _batch_diff_summary(steps: list) -> str:
    """构造批量操作的 diff 摘要：前 N 条 + 总数。"""
    lines = []
    for s in steps[:_BATCH_PREVIEW_LIMIT]:
        args = s.get("args") or {}
        if s.get("tool") == "move_file":
            dst = args.get("dest_dir", "")
            name = args.get("new_name")
            lines.append(f"移动 {args.get('src', '')} → {dst}" + (f"（改名 {name}）" if name else ""))
        else:  # rename_file
            lines.append(f"重命名 {args.get('src', '')} → {args.get('new_name', '')}")
    more = len(steps) - _BATCH_PREVIEW_LIMIT
    if more > 0:
        lines.append(f"…… 以及另外 {more} 项")
    return "\n".join(lines)


def _recursion_limit() -> int:
    """按计划上限与允许的重规划次数推导安全的 recursion_limit。

    每个规划周期 = 1(planning) + 30 步 × 2 超级步；共 1 + _MAX_REPLANS 个周期，
    再加 perceive(1) 与删除审批 / 边界余量。避免像固定 120 那样在 30 步 + 重规划时踩线。
    """
    per_cycle = 1 + _MAX_PLAN_STEPS * _SUPER_STEPS_PER_STEP
    cycles = 1 + _MAX_REPLANS
    margin = 20
    return 1 + cycles * per_cycle + margin
def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


_PLANNER_SAMPLE = 20        # 回灌给规划器的扫描样本条数
_PLANNER_TEXT_CAP = 1000    # 回灌给规划器的长文本（如 extract_text）上限字符数
_SUMMARY_CHUNK_CHARS = SUMMARY_CHUNK_CHARS  # 兼容既有调用与测试
_SUMMARY_MAX_CHUNKS = SUMMARY_MAX_CHUNKS


def _summarize_observation(obs: Dict[str, Any]) -> Dict[str, Any]:
    """把单条观察压缩后再喂给规划/反思，避免把 500 条扫描明细或整篇文本反复回灌（P1-5）。

    完整清单仍保留在 state 里供工具使用，这里只产出摘要视图。
    """
    slim: Dict[str, Any] = {
        "step_id": obs.get("step_id"),
        "tool": obs.get("tool"),
        "description": obs.get("description"),
        "status": obs.get("status"),
    }
    if obs.get("error"):
        slim["error"] = obs["error"]

    result = obs.get("result")
    if isinstance(result, dict) and isinstance(result.get("items", result.get("preview")), list):
        items = result.get("items", result.get("preview", []))
        distribution: Dict[str, int] = {}
        for item in items:
            ext = (item.get("ext") or "无扩展名") if isinstance(item, dict) else "未知"
            distribution[ext] = distribution.get(ext, 0) + 1
        slim["result"] = {
            "total": result.get("total", len(items)),
            "truncated": result.get("truncated", False),
            "extension_distribution": distribution,
            "sample": items[:_PLANNER_SAMPLE],
        }
    elif isinstance(result, str) and len(result) > _PLANNER_TEXT_CAP:
        slim["result"] = result[:_PLANNER_TEXT_CAP] + f"……（已截断，原文共 {len(result)} 字符）"
    else:
        slim["result"] = result
    return slim


def _summarize_observations(observations: list) -> list:
    return [_summarize_observation(o) for o in observations]


class AgentRuntime:
    """封装编译后的 LangGraph，并提供启动/恢复/读取状态接口。"""

    def __init__(self) -> None:
        self._lifecycle_lock = threading.Lock()
        self.active_stream_count = 0
        self.retired = False
        self.closed = False
        self.llm = build_llm(temperature=0.1)
        # 不直接用 llm.with_structured_output：默认走 function_calling，请求体带 tools，
        # 只实现聊天补全的 OpenAI 兼容服务会直接 400（见 build_structured_llm）。
        self.planner = build_structured_llm(self.llm, PlanOutput)
        self.reflector = build_structured_llm(self.llm, ReflectionOutput)
        self.tool_executor = ToolExecutor(self.llm)
        self.checkpointer = _build_checkpointer()
        self.policy = PolicyEngine(_batch_threshold())
        self.graph = self._build_graph()
        cleanup_checkpoints(self.checkpointer, self.state)

    def retain_stream(self) -> None:
        with self._lifecycle_lock:
            if self.closed:
                raise RuntimeError("AgentRuntime 已关闭")
            self.active_stream_count += 1

    def release_stream(self) -> None:
        with self._lifecycle_lock:
            self.active_stream_count = max(0, self.active_stream_count - 1)
            should_close = self.retired and self.active_stream_count == 0
        if should_close:
            self.close()

    def retire(self) -> None:
        with self._lifecycle_lock:
            self.retired = True
            should_close = self.active_stream_count == 0
        if should_close:
            self.close()

    def close(self) -> None:
        with self._lifecycle_lock:
            if self.closed or self.active_stream_count:
                return
            self.closed = True
        conn = getattr(self.checkpointer, "conn", None) or getattr(self.checkpointer, "connection", None)
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                logger.exception("关闭 AgentRuntime checkpointer 失败")

    def _build_graph(self):
        workflow = StateGraph(AgentState)
        workflow.add_node("perceive", self._perceive)
        workflow.add_node("planning", self._plan)
        workflow.add_node("act", self._act)
        workflow.add_node("approval", self._approval)
        workflow.add_node("reflecting", self._reflect)

        workflow.add_edge(START, "perceive")
        workflow.add_edge("perceive", "planning")
        workflow.add_conditional_edges(
            "planning", self._after_plan, {"act": "act", "done": END}
        )
        # 移除只返回 {"status": "running"} 的空 observe 节点：观察已由 act/approval
        # 写入状态，act 直连 reflecting，每步从 3 个超级步降到 2 个（见 P0-3）。
        workflow.add_conditional_edges(
            "act",
            self._after_act,
            {"approval": "approval", "reflecting": "reflecting"},
        )
        workflow.add_edge("approval", "reflecting")
        workflow.add_conditional_edges(
            "reflecting",
            self._after_reflect,
            {"act": "act", "plan": "planning", "done": END},
        )
        return workflow.compile(checkpointer=self.checkpointer)

    # ---------------- 图节点 ----------------

    def _perceive(self, state: AgentState) -> Dict[str, Any]:
        from ..sandbox_config import effective_roots
        from ..workspace_config import effective_default_root

        roots = [str(p) for p in effective_roots()]
        # 默认管理目录只是“用户没给路径时用哪个目录”，仍必须落在授权目录内；
        # 越界时 effective_default_root() 返回 None（见 workspace_config）。
        default_root = effective_default_root()
        preferences = [p.model_dump() for p in db.all_preferences()]
        history = [
            op.model_dump(mode="json") for op in db.recent_operations(limit=20)
        ]
        return {
            "perception": {
                "allowed_roots": roots,
                "default_managed_root": str(default_root) if default_root else "",
                "preferences": preferences,
                "recent_operations": history,
            },
            "observations": [],
            "plan": [],
            "step_index": 0,
            "current_step": {},
            "pending_approval": None,
            "final_response": "",
            "status": "perceiving",
            "error": "",
            "batch_approved": False,
            "batch_rejected": False,
            "trusted_user_intent": state.get("user_request", ""),
            "untrusted_file_data": [],
            "tool_observations": [],
            "mutations": state.get("mutations") or {"move": 0, "rename": 0, "delete": 0},
            "approved_mutation_limit": int(state.get("approved_mutation_limit") or 0),
        }

    def _plan(self, state: AgentState) -> Dict[str, Any]:
        replan_count = state.get("replan_count", 0)
        context = {
            "original_user_request": state.get("trusted_user_intent") or state.get("user_request", ""),
            "环境感知": state.get("perception", {}),
            "既有计划": state.get("plan", []),
            # 只喂摘要（总数 / 扩展名分布 / 前 20 条），完整清单留在 state 供工具用（P1-5）。
            "untrusted_file_data": state.get("untrusted_file_data", []),
            "tool_observations": _summarize_observations(state.get("observations", [])),
            # 兼容既有 planner/context 测试与第三方提示模板；值仍是同一份受限工具观察。
            "已获得观察": _summarize_observations(state.get("observations", [])),
            "要求": (
                "这是重规划。不要重复已经成功或被拒绝的步骤；请根据扫描/提取结果，"
                "生成尚未完成的具体步骤。"
                if state.get("observations")
                else "生成安全的初始计划。批量任务应先扫描再根据结果重规划。"
            ),
        }
        try:
            result = self.planner.invoke(
                [
                    SystemMessage(content=PLANNER_SYSTEM_PROMPT),
                    HumanMessage(content=_json(context)),
                ]
            )
            for item in result.steps[:_MAX_PLAN_STEPS]:
                if item.tool == "set_preference" and "key" in item.args:
                    # 与 REST 共用同一条数据边界校验，禁止 Agent 修改沙箱保留键。
                    db.validate_public_preference_key(str(item.args["key"]))
            steps = [step.model_dump() for step in result.steps[:_MAX_PLAN_STEPS]]
            if not steps:
                return {
                    "plan": [],
                    "step_index": 0,
                    "status": "completed",
                    "final_response": result.user_message or "没有需要执行的文件操作。",
                    "current_step": {},
                }
            return {
                "plan": steps,
                "step_index": 0,
                "current_step": {},
                "status": "planning",
                "replan_count": replan_count + (1 if state.get("observations") else 0),
                "final_response": result.user_message,
                # 累计审批状态属于整个 user request，重规划不得重置。
            }
        except Exception as exc:  # noqa: BLE001
            logger.exception("规划失败")
            return {
                "status": "failed",
                "error": str(exc),
                "final_response": f"规划失败：{exc}",
                "plan": [],
            }

    def _act(self, state: AgentState) -> Dict[str, Any]:
        plan = state.get("plan", [])
        index = state.get("step_index", 0)
        if index >= len(plan):
            return {
                "reflection": {"decision": "done", "reasoning": "没有剩余步骤", "final_response": ""},
                "current_step": {},
                "status": "running",
            }

        step = plan[index]
        tool = str(step.get("tool", ""))
        args = dict(step.get("args") or {})
        current = {**step, "args": args}

        proposed = self._proposed_mutation_count(tool, args, plan[index:])
        decision = self.policy.evaluate(tool, state=state, proposed_count=proposed)
        if not decision.allowed:
            observation = self._observation(step, "failed", error=decision.reason)
            return {"current_step": current, "observations": [*state.get("observations", []), observation],
                    "step_index": index + 1, "status": "running"}
        if tool != "delete_file" and decision.requires_approval:
            approval = {
                "approval_id": uuid.uuid4().hex,
                "action": f"batch_{decision.mutation_kind}",
                "target": f"累计 {sum((state.get('mutations') or {}).values()) + proposed} 项文件变更",
                "detail": decision.reason,
                "count": proposed,
                "approved_limit": sum((state.get("mutations") or {}).values()) + proposed,
                "batch": True,
                "tool": tool,
                "args": args,
            }
            return {"current_step": current, "pending_approval": approval, "status": "waiting_approval"}

        # 批量破坏性操作预演审批（P1-2）：计划中 move/rename 步数超阈值时，
        # 在执行第一个前统一审批一次。拒绝后所有批量步骤都跳过，文件一个都不动。
        if tool in _BATCH_TOOLS:
            if state.get("batch_rejected"):
                observation = self._observation(
                    step, "rejected", result="用户拒绝了批量操作，未移动该文件"
                )
                db.log_operation(
                    tool, str(args.get("src", "")), "rejected",
                    detail="用户拒绝批量操作", thread_id=state.get("thread_id"),
                )
                return {
                    "current_step": current,
                    "observations": [*state.get("observations", []), observation],
                    "step_index": index + 1,
                    "status": "running",
                }
            if not state.get("batch_approved"):
                batch_steps = [s for s in plan if str(s.get("tool", "")) in _BATCH_TOOLS]
                if len(batch_steps) > _batch_threshold():
                    approval = {
                        "approval_id": uuid.uuid4().hex,
                        "action": "batch_move",
                        "target": f"{len(batch_steps)} 项文件移动/重命名",
                        "detail": _batch_diff_summary(batch_steps),
                        "count": len(batch_steps),
                        "batch": True,
                        "tool": tool,
                        "args": args,
                    }
                    return {
                        "current_step": current,
                        "pending_approval": approval,
                        "status": "waiting_approval",
                    }

        if tool == "delete_file":
            target = str(args.get("path", ""))
            # 在弹窗出现前先校验路径，避免用审批 UI 暴露/操作沙箱外路径。
            try:
                validated = str(resolve_in_sandbox(target, must_exist=True))
            except Exception as exc:  # noqa: BLE001
                observation = self._observation(step, "failed", error=str(exc))
                return {
                    "current_step": current,
                    "observations": [*state.get("observations", []), observation],
                    "step_index": index + 1,
                    "status": "running",
                }

            delete_decision = self.policy.evaluate(tool, state=state, proposed_count=1)
            if not delete_decision.allowed:
                observation = self._observation(step, "failed", error=delete_decision.reason)
                return {
                    "current_step": current,
                    "observations": [*state.get("observations", []), observation],
                    "step_index": index + 1,
                    "status": "running",
                }

            approval = {
                "approval_id": uuid.uuid4().hex,
                "action": "delete",
                "target": validated,
                "detail": step.get("description", "删除文件或目录"),
                "tool": tool,
                "args": {**args, "path": validated},
            }
            return {
                "current_step": current,
                "pending_approval": approval,
                "status": "waiting_approval",
            }

        with db.operation_thread(state.get("thread_id")):
            observation = self._execute_step(step)
        update = {
            "current_step": current,
            "observations": [*state.get("observations", []), observation],
            "tool_observations": [*state.get("tool_observations", []), _summarize_observation(observation)],
            "step_index": index + 1,
            "status": "running",
        }
        if tool in {"scan_directory", "extract_text", "classify_file"}:
            update["untrusted_file_data"] = [
                *state.get("untrusted_file_data", []), _summarize_observation(observation)
            ]
        self._apply_mutation_count(update, state, tool, observation)
        return update

    def _approval(self, state: AgentState) -> Dict[str, Any]:
        pending = state.get("pending_approval")
        if not pending:
            return {}

        decision = interrupt(
            {
                "approval_id": pending["approval_id"],
                "action": pending["action"],
                "target": pending["target"],
                "detail": pending.get("detail", ""),
            }
        )
        approved = (
            decision is True
            or decision == "approve"
            or (isinstance(decision, dict) and decision.get("decision") == "approve")
        )
        step = {
            "id": state.get("current_step", {}).get("id", "approval"),
            "description": pending.get("detail", ""),
            "tool": pending["tool"],
            "args": pending["args"],
        }
        thread_id = state.get("thread_id")
        if approved:
            with db.operation_thread(thread_id):
                observation = self._execute_step(step)
        else:
            db.log_operation(
                pending["action"], pending["target"], "rejected",
                detail="用户拒绝审批", thread_id=thread_id,
            )
            observation = self._observation(
                step, "rejected", result="用户拒绝了危险操作，未修改文件"
            )

        result: Dict[str, Any] = {
            "pending_approval": None,
            "observations": [*state.get("observations", []), observation],
            "step_index": state.get("step_index", 0) + 1,
            "status": "running",
        }
        # 批量审批：记住决定，使同一计划里后续 move/rename 步骤不再逐条弹窗（P1-2）。
        if pending.get("batch"):
            if approved:
                result["batch_approved"] = True
                result["approved_mutation_limit"] = max(
                    int(state.get("approved_mutation_limit") or 0),
                    int(pending.get("approved_limit") or 0),
                )
            else:
                result["batch_rejected"] = True
        self._apply_mutation_count(result, state, str(step.get("tool", "")), observation)
        return result

    def _reflect(self, state: AgentState) -> Dict[str, Any]:
        if state.get("status") == "failed":
            return {}

        plan = state.get("plan", [])
        index = state.get("step_index", 0)
        latest = _summarize_observations(state.get("observations", [])[-1:])
        context = {
            "original_user_request": state.get("trusted_user_intent") or state.get("user_request", ""),
            "当前计划": plan,
            "下一步骤索引": index,
            "刚完成步骤": state.get("current_step", {}),
            "最新观察": latest,
            "是否还有步骤": index < len(plan),
            "已重规划次数": state.get("replan_count", 0),
        }
        try:
            result = self.reflector.invoke(
                [
                    SystemMessage(content=REFLECTION_SYSTEM_PROMPT),
                    HumanMessage(content=_json(context)),
                ]
            )
            decision = result.decision
            if decision == "replan" and state.get("replan_count", 0) >= _MAX_REPLANS:
                decision = "done"
                result.final_response = (
                    "已达到最大重规划次数。已完成的操作已保留，请检查任务记录后再继续。"
                )
            if decision == "continue" and index >= len(plan):
                decision = "done"
            if decision == "done":
                response = result.final_response or self._fallback_summary(state)
                return {
                    "reflection": result.model_dump(),
                    "status": "completed",
                    "final_response": response,
                    "current_step": {},
                }
            return {
                "reflection": {**result.model_dump(), "decision": decision},
                "status": "running",
                "current_step": {},
            }
        except Exception as exc:  # noqa: BLE001
            logger.exception("反思失败")
            # 工具已执行，反思失败不能回滚；安全终止并如实报告。
            return {
                "status": "failed",
                "error": str(exc),
                "final_response": f"文件操作已停止：反思阶段失败（{exc}）。请查看任务记录确认已完成部分。",
            }

    # ---------------- 路由 ----------------

    @staticmethod
    def _after_plan(state: AgentState) -> Literal["act", "done"]:
        if state.get("status") in {"completed", "failed"} or not state.get("plan"):
            return "done"
        return "act"

    @staticmethod
    def _after_act(state: AgentState) -> Literal["approval", "reflecting"]:
        return "approval" if state.get("pending_approval") else "reflecting"

    @staticmethod
    def _after_reflect(state: AgentState) -> Literal["act", "plan", "done"]:
        if state.get("status") in {"completed", "failed"}:
            return "done"
        decision = state.get("reflection", {}).get("decision", "done")
        if decision == "replan":
            return "plan"
        if decision == "continue":
            return "act"
        return "done"

    # ---------------- 工具执行 ----------------

    def _tools(self) -> ToolExecutor:
        """兼容绕过 ``__init__`` 的轻量测试，同时集中工具实现。"""
        executor = getattr(self, "tool_executor", None)
        if executor is None:
            executor = ToolExecutor(getattr(self, "llm", None))
            self.tool_executor = executor
        return executor

    @staticmethod
    def _proposed_mutation_count(tool: str, args: Dict[str, Any], remaining: list) -> int:
        if tool in {"batch_move", "batch_rename", "batch_classify"}:
            try:
                return len(manifests.match(str(args["scan_id"]), dict(args.get("filter") or {})))
            except Exception:
                return 1
        if tool in {"move_file", "rename_file"}:
            return sum(1 for step in remaining if str(step.get("tool")) in {"move_file", "rename_file"})
        return 1

    @staticmethod
    def _apply_mutation_count(update: Dict[str, Any], state: AgentState, tool: str, observation: Dict[str, Any]) -> None:
        if observation.get("status") != "ok":
            return
        kind = {"move_file": "move", "batch_move": "move", "batch_classify": "move",
                "rename_file": "rename", "batch_rename": "rename", "delete_file": "delete"}.get(tool)
        if not kind:
            return
        result = observation.get("result")
        count = int(result.get("success", 0)) if isinstance(result, dict) and tool.startswith("batch_") else 1
        mutations = dict(state.get("mutations") or {"move": 0, "rename": 0, "delete": 0})
        mutations[kind] = int(mutations.get(kind, 0)) + count
        update["mutations"] = mutations

    def _execute_step(self, step: Dict[str, Any]) -> Dict[str, Any]:
        return self._tools().execute(step)

    @staticmethod
    def _batch_result(matched: int, successes: list[str], failed: list[dict], skipped: list[dict], *, truncated: bool = False, reason: str = "none") -> Dict[str, Any]:
        return ToolExecutor.batch_result(
            matched, successes, failed, skipped, truncated=truncated, reason=reason
        )

    def _batch_move(self, args: Dict[str, Any]) -> Dict[str, Any]:
        return self._tools().batch_move(args)

    def _batch_rename(self, args: Dict[str, Any]) -> Dict[str, Any]:
        return self._tools().batch_rename(args)

    def _batch_classify(self, args: Dict[str, Any]) -> Dict[str, Any]:
        return self._tools().batch_classify(args)

    def _llm_classify(self, name: str, rule: str, text: str) -> Optional[str]:
        return self._tools().llm_classify(name, rule, text)

    def _classify_file(self, file_path: str) -> Dict[str, Any]:
        return self._tools().classify_file(file_path)

    def _summarize_once(self, title: str, text: str, *, is_segment: bool = False) -> str:
        return self._tools().summarize_once(title, text, is_segment=is_segment)

    def _summarize_text(self, name: str, text: str) -> str:
        return self._tools().summarize_text(name, text)

    def _write_summary(
        self, file_path: str, output_dir: str, output_name: Optional[str]
    ) -> str:
        return self._tools().write_summary(file_path, output_dir, output_name)

    @staticmethod
    def _create_schedule(args: Dict[str, Any]) -> Dict[str, Any]:
        return ToolExecutor.create_schedule(args)

    @staticmethod
    def _observation(
        step: Dict[str, Any], status: str, *, result: Any = None, error: str = ""
    ) -> Dict[str, Any]:
        return ToolExecutor.observation(step, status, result=result, error=error)

    @staticmethod
    def _fallback_summary(state: AgentState) -> str:
        return ToolExecutor.fallback_summary(state)

    # ---------------- 对外接口 ----------------

    @staticmethod
    def config(thread_id: str) -> Dict[str, Any]:
        return {"configurable": {"thread_id": thread_id}, "recursion_limit": _recursion_limit()}

    def start_stream(self, message: str, thread_id: str) -> Iterable[Dict[str, Any]]:
        initial: AgentState = {
            "thread_id": thread_id,
            "user_request": message,
            "status": "perceiving",
            "replan_count": 0,
            "observations": [],
            "plan": [],
            "step_index": 0,
            "current_step": {},
            "pending_approval": None,
            "final_response": "",
            "error": "",
            "perception": {},
            "reflection": {},
            "batch_approved": False,
            "batch_rejected": False,
            "trusted_user_intent": message,
            "untrusted_file_data": [],
            "tool_observations": [],
            "mutations": {"move": 0, "rename": 0, "delete": 0},
            "approved_mutation_limit": 0,
        }
        # updates：逐节点状态增量；messages：LLM token 流（点亮 P1-4 的 token 事件）。
        return self.graph.stream(
            initial, self.config(thread_id), stream_mode=["updates", "messages"]
        )

    def resume_stream(
        self, thread_id: str, decision: Literal["approve", "reject"]
    ) -> Iterable[Dict[str, Any]]:
        return self.graph.stream(
            Command(resume={"decision": decision}),
            self.config(thread_id),
            stream_mode=["updates", "messages"],
        )

    def state(self, thread_id: str) -> Dict[str, Any]:
        snapshot = self.graph.get_state(self.config(thread_id))
        return dict(snapshot.values) if snapshot and snapshot.values else {}

    def cancel(self, thread_id: str) -> None:
        """把取消写入 checkpoint，避免重启后会话又显示为 running/waiting。"""
        self.graph.update_state(
            self.config(thread_id),
            {
                "status": "cancelled",
                "pending_approval": None,
                "final_response": "任务已停止。已完成的文件操作仍可在操作日志中撤销。",
            },
        )
