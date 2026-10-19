"""LangGraph Agent 核心：感知 → 规划 → 工具调用 → 观察 → 反思。

删除操作在图内使用 ``interrupt`` 暂停；前端审批后用 ``Command(resume=...)``
恢复同一 thread。所有实际磁盘访问仍由工具层执行沙箱校验。
"""
from __future__ import annotations

import json
import time
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
from ..policy.engine import intent_effects, is_task_switch
from . import tracing
from .checkpoints import (
    _UUID_EPOCH,
    build_checkpointer as _build_checkpointer,
    checkpoint_created_at as _checkpoint_created_at,
    cleanup_checkpoints,
)
from .llm import build_llm, build_structured_llm
from .outcome import evaluate_completion, record_verification
from .prompts import PLANNER_SYSTEM_PROMPT, REFLECTION_SYSTEM_PROMPT
from .state import AgentState, PlanOutput, ReflectionOutput
from .tool_executor import SUMMARY_CHUNK_CHARS, SUMMARY_MAX_CHUNKS, ToolExecutor
from ..tools.contracts import validate_tool_args

logger = get_logger(__name__)
_MAX_REPLANS = 3
_MAX_PLAN_STEPS = 30           # 单个计划最多步数（提示词同步约束）
_SUPER_STEPS_PER_STEP = 2      # 移除 observe 后每步占用 act + reflecting 两个超级步
_MAX_SCAN_RESULTS = 500
# 会改变文件位置/名字的副作用；它们共用请求级累计审批。
_CUMULATIVE_TOOLS = (
    "move_file", "batch_move", "rename_file", "batch_rename", "batch_classify",
)
_DEFAULT_BATCH_THRESHOLD = 20


def _batch_threshold() -> int:
    """批量审批阈值，可用偏好 batch_approval_threshold 覆盖（默认 20）。"""
    raw = db.get_preference("batch_approval_threshold")
    try:
        value = int(raw) if raw is not None else _DEFAULT_BATCH_THRESHOLD
    except (TypeError, ValueError):
        return _DEFAULT_BATCH_THRESHOLD
    return value if value > 0 else _DEFAULT_BATCH_THRESHOLD


def _is_clarification_answer(message: str) -> bool:
    """判断这轮消息是否只是对上一条澄清问题的补充。

    判据是确定性的：出现改口/换任务标记，或自带新的操作动词，都视为“新请求”，
    不能继承上一轮授权文本。
    """
    text = message.strip()
    if not text:
        return False
    if is_task_switch(text):
        return False
    return not intent_effects(text)


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
    """把单条观察压缩后再喂给规划/反思，避免把 500 条扫描明细或整篇文本反复回灌。

    压缩必须按工具结果类型进行，不能把“前 20 条样本的统计”冒充完整统计：
    - scan_directory：保留 scan_id、total、scanned_count、truncated、reason、
      manifest 的完整扩展名统计，以及有限样本。
    - batch_*：保留 matched/success/failed/skipped/truncated/reason 与有限明细，
      供后续重规划和用户汇报使用。
    - 长文本：保留截断标记和原始长度。
    完整清单仍保留在 state / manifest 中供工具使用，这里只产出摘要视图。
    """
    slim: Dict[str, Any] = {
        "step_id": obs.get("step_id"),
        "tool": obs.get("tool"),
        "description": obs.get("description"),
        "status": obs.get("status"),
    }
    if obs.get("error"):
        slim["error"] = obs["error"]
    # 参数通常很短，能帮助规划器复用 scan_id / 目标目录；过长时只保留工具名。
    args = obs.get("args")
    if isinstance(args, dict) and len(_json(args)) <= 800:
        slim["args"] = args

    result = obs.get("result")
    tool = str(obs.get("tool") or "")
    if tool == "scan_directory" and isinstance(result, dict):
        preview = result.get("preview")
        if not isinstance(preview, list):
            legacy_items = result.get("items")
            preview = legacy_items if isinstance(legacy_items, list) else []
        total = int(result.get("total", len(preview)) or 0)
        scanned_count = int(result.get("scanned_count", total) or 0)
        statistics = result.get("summary")
        statistics_source = "manifest"
        if not isinstance(statistics, dict):
            # 兼容旧结果/测试桩：只有样本文本时，明确标记为样本统计。
            statistics = {}
            for item in preview:
                ext = (item.get("ext") or "无扩展名") if isinstance(item, dict) else "未知"
                statistics[ext] = statistics.get(ext, 0) + 1
            statistics_source = "sample" if total > len(preview) else "manifest"
        slim["result"] = {
            "scan_id": result.get("scan_id"),
            "total": total,
            "scanned_count": scanned_count,
            "truncated": bool(result.get("truncated", False)),
            "reason": str(result.get("reason") or "none"),
            "extension_distribution": statistics,
            "statistics_source": statistics_source,
            "sample_size": len(preview[:_PLANNER_SAMPLE]),
            "sample": preview[:_PLANNER_SAMPLE],
        }
        return slim

    if tool in {"batch_move", "batch_rename", "batch_classify"} and isinstance(result, dict):
        slim["result"] = {
            key: result.get(key)
            for key in (
                "matched",
                "success",
                "failed",
                "skipped",
                "truncated",
                "reason",
                "preview",
                "failures",
                "skips",
            )
            if key in result
        }
        for key in ("preview", "failures", "skips"):
            values = slim["result"].get(key)
            if isinstance(values, list):
                slim["result"][key] = values[:_PLANNER_SAMPLE]
        return slim

    if isinstance(result, str) and len(result) > _PLANNER_TEXT_CAP:
        slim["result"] = result[:_PLANNER_TEXT_CAP] + f"……（已截断，原文共 {len(result)} 字符）"
        return slim

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
        workflow.add_node("perceive", self._node("perceive", self._perceive))
        workflow.add_node("planning", self._node("planning", self._plan))
        workflow.add_node("act", self._node("act", self._act))
        # 审批节点内部使用 interrupt 暂停；等待时间不计入执行预算。
        workflow.add_node("approval", self._approval)
        workflow.add_node("reflecting", self._node("reflecting", self._reflect))

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

    def _node(self, name: str, handler):
        """把节点包成带计时与预算检查的可调用对象（LangGraph 只接受单参数函数）。"""

        def runner(state: AgentState) -> Dict[str, Any]:
            return self._timed_node(name, handler, state)

        runner.__name__ = f"node_{name}"
        return runner

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
        last_scan = state.get("last_scan") or self._last_scan(state.get("observations", []))
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
            "outcome": "",
            "stop_reason": "",
            "completion": {},
            "batch_approved": False,
            "batch_rejected": False,
            # 授权文本由 start_stream 决定（澄清续问会合并上一轮意图），perceive
            # 不能再把它重置成“本轮最后一条消息”，否则澄清后的授权会被抹掉。
            "trusted_user_intent": state.get("trusted_user_intent") or state.get("user_request", ""),
            "untrusted_file_data": [],
            "tool_observations": [],
            "mutations": state.get("mutations") or {"move": 0, "rename": 0, "delete": 0},
            "approved_mutation_limit": int(state.get("approved_mutation_limit") or 0),
            "last_scan": last_scan,
        }

    @staticmethod
    def _validate_scan_reference(args: Dict[str, Any]) -> str:
        """执行批量步骤前核验 scan_id 与扫描目录仍然有效。

        引用来自上一轮会话或旧扫描时，manifest 可能已随数据库清理消失，目录也可能
        已被移动/删除。这里只做只读检查，并在失败时给出可执行的补救提示。
        """
        scan_id = str(args.get("scan_id") or "").strip()
        if not scan_id:
            return "缺少 scan_id，无法确定要处理的文件集合"
        try:
            manifest = manifests.load(scan_id)
        except Exception as exc:  # noqa: BLE001
            return f"扫描引用已失效：{exc}；请重新扫描目录"
        directory = str(manifest.get("directory") or "")
        if directory:
            try:
                from pathlib import Path

                if not Path(directory).is_dir():
                    return f"扫描目录已不存在：{directory}；请重新扫描"
            except OSError as exc:
                return f"无法核验扫描目录：{exc}"
        return ""

    # ---------------- 运行预算与追踪（P1-5） ----------------

    @staticmethod
    def _budget_limits() -> Dict[str, int]:
        defaults = {
            # 30 步计划 + 重规划时，反思本身就会产生几十次模型调用；上限必须留出余量，
            # 否则一次正常的大计划会被误判为预算耗尽。
            "agent_max_model_calls": 120,
            "agent_max_tool_calls": 100,
            "agent_execution_budget_seconds": 600,
        }
        limits: Dict[str, int] = {}
        for key, default in defaults.items():
            raw = db.get_preference(key)
            try:
                value = int(raw) if raw is not None else default
            except (TypeError, ValueError):
                value = default
            limits[key] = value if value > 0 else default
        return limits

    def _trace_update(
        self,
        state: AgentState,
        *,
        model_calls: int = 0,
        tool_calls: int = 0,
    ) -> Dict[str, Any]:
        trace = dict(state.get("run_trace") or {})
        trace["run_id"] = trace.get("run_id") or state.get("run_id", "")
        trace["model_calls"] = int(trace.get("model_calls", 0)) + model_calls
        trace["tool_calls"] = int(trace.get("tool_calls", 0)) + tool_calls
        trace.setdefault("node_timings", [])
        trace.setdefault("execution_ms", 0)
        # 供应商未提供 usage 时保持 unknown，而不是伪造为 0。
        trace.setdefault(
            "token_usage", {"input": None, "output": None, "source": "unknown"}
        )
        # 供应商真的给了 usage 才覆盖 unknown；否则保留未知，不伪造 0。
        usage = tracing.usage_for(str(state.get("thread_id") or ""))
        if usage.get("source") == "provider":
            trace["token_usage"] = {
                "input": usage.get("input"),
                "output": usage.get("output"),
                "calls": usage.get("calls", 0),
                "source": "provider",
            }
        return trace

    def _budget_exceeded(self, state: AgentState) -> str:
        trace = state.get("run_trace") or {}
        limits = self._budget_limits()
        if int(trace.get("model_calls", 0)) >= limits["agent_max_model_calls"]:
            return (
                f"模型调用达到上限 {limits['agent_max_model_calls']} 次"
            )
        if int(trace.get("tool_calls", 0)) >= limits["agent_max_tool_calls"]:
            return f"工具调用达到上限 {limits['agent_max_tool_calls']} 次"
        budget_ms = limits["agent_execution_budget_seconds"] * 1000
        if int(trace.get("execution_ms", 0)) >= budget_ms:
            return (
                f"执行时间达到预算 {limits['agent_execution_budget_seconds']} 秒"
                "（等待人工审批的时间不计入）"
            )
        return ""

    def _remaining_model_calls(self, state: AgentState) -> int:
        """本次运行还剩多少次模型调用额度，交给工具内部自查。"""
        limits = self._budget_limits()
        trace = state.get("run_trace") or {}
        used = int(trace.get("model_calls", 0))
        return max(0, limits["agent_max_model_calls"] - used)

    def _budget_stop(self, state: AgentState, reason: str) -> Dict[str, Any]:
        completion = evaluate_completion(
            state, stop_reason="budget_exhausted", forced_outcome="budget_exhausted"
        )
        trace = self._trace_update(state)
        trace["stop_reason"] = "budget_exhausted"
        return {
            "status": "completed",
            "outcome": "budget_exhausted",
            "stop_reason": "budget_exhausted",
            "completion": completion,
            "run_trace": trace,
            "pending_approval": None,
            "current_step": {},
            "final_response": f"{completion['message']}停止原因：{reason}。",
        }

    def _timed_node(self, name: str, handler, state: AgentState) -> Dict[str, Any]:
        """记录节点耗时，并在节点边界执行预算检查。

        预算用“节点执行时间之和”而不是墙钟时间：等待审批/用户补充期间不运行节点，
        因此不会误耗执行预算。
        """
        if name in {"planning", "act", "reflecting"}:
            reason = self._budget_exceeded(state)
            if reason:
                logger.info("运行预算耗尽 thread=%s reason=%s", state.get("thread_id"), reason)
                return self._budget_stop(state, reason)
        started = time.monotonic()
        result = handler(state)
        elapsed_ms = int((time.monotonic() - started) * 1000)
        # 以节点自己返回的 trace 为准（可能已累加模型/工具调用），再补本节点耗时。
        # 早先这里用 setdefault，节点返回的 trace 会让耗时记录永远只剩第一个节点。
        trace = dict(result.get("run_trace") or self._trace_update(state)) if isinstance(result, dict) else self._trace_update(state)
        timings = list(trace.get("node_timings") or [])
        timings.append({"node": name, "ms": elapsed_ms})
        trace["node_timings"] = timings[-50:]
        trace["execution_ms"] = int(trace.get("execution_ms", 0)) + elapsed_ms
        if isinstance(result, dict):
            result["run_trace"] = trace
            result.setdefault("run_id", state.get("run_id", ""))
            # 终态必须能解释停止原因；未显式给出时按状态推导，避免出现“完成但无原因”。
            final_status = result.get("status")
            if final_status in {"completed", "failed", "cancelled"}:
                result.setdefault(
                    "stop_reason",
                    "tool_failed" if final_status == "failed" else "model_done",
                )
        return result

    @staticmethod
    def _last_scan(observations: list) -> Dict[str, Any]:
        """从既有观察中提取最近一次可用扫描，供“刚才那些 PDF”这类追问引用。"""
        for obs in reversed(observations or []):
            if not isinstance(obs, dict) or obs.get("tool") != "scan_directory":
                continue
            result = obs.get("result")
            if not isinstance(result, dict) or not result.get("scan_id"):
                continue
            args = obs.get("args") if isinstance(obs.get("args"), dict) else {}
            summary = result.get("summary")
            if not isinstance(summary, dict):
                summary = {}
            return {
                "scan_id": result.get("scan_id"),
                "directory": args.get("directory", ""),
                "total": int(result.get("total", 0) or 0),
                "scanned_count": int(result.get("scanned_count", result.get("total", 0)) or 0),
                "truncated": bool(result.get("truncated", False)),
                "reason": str(result.get("reason") or "none"),
                "summary": summary,
            }
        return {}

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
            # 多轮上下文：仅用于理解指代，不是新的授权来源。
            "conversation": (state.get("conversation") or [])[-10:],
            "last_scan": state.get("last_scan", {}),
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
            trace = self._trace_update(state, model_calls=1)
            if not steps:
                # 空计划没有执行任何工具，也就没有任何可验证的后置条件：
                # 既不能宣称“目标完成”，也不能声称失败，只能是 unknown。
                completion = evaluate_completion(
                    {
                        **state,
                        "plan": [],
                        "step_index": 0,
                    },
                    stop_reason="no_operations",
                    forced_outcome="unknown" if not state.get("observations") else None,
                )
                # 模型措辞只有在校验通过（success）时才能作为正文；否则“全部完成！”
                # 会盖住“未执行任何文件操作，无法验证目标”的确定性结论。
                response = (
                    result.user_message
                    if completion["outcome"] == "success"
                    else completion["message"]
                )
                return {
                    "plan": [],
                    "step_index": 0,
                    "status": "failed" if completion["outcome"] == "failed" else "completed",
                    "outcome": completion["outcome"],
                    "stop_reason": completion["stop_reason"],
                    "completion": completion,
                    "run_trace": trace,
                    "final_response": response,
                    "current_step": {},
                }
            return {
                "plan": steps,
                "step_index": 0,
                "current_step": {},
                "status": "planning",
                "replan_count": replan_count + (1 if state.get("observations") else 0),
                "final_response": result.user_message,
                "run_trace": trace,
                # 累计审批状态属于整个 user request，重规划不得重置。
            }
        except Exception as exc:  # noqa: BLE001
            logger.exception("规划失败")
            completion = evaluate_completion(
                {**state, "plan": [], "step_index": 0},
                stop_reason="planning_error",
                forced_outcome="failed",
            )
            return {
                "status": "failed",
                "error": str(exc),
                "outcome": "failed",
                "stop_reason": "planning_error",
                "completion": completion,
                "run_trace": self._trace_update(state, model_calls=1),
                "final_response": f"规划失败：{exc}" + (f"。{completion['message']}" if completion.get("message") else ""),
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

        # 澄清不产生副作用：暂停执行，等待用户补充信息（P1-4）。
        if tool == "request_clarification":
            normalized, argument_error = validate_tool_args(tool, args)
            if argument_error:
                observation = self._observation(step, "failed", error=argument_error)
                return {
                    "current_step": current,
                    "observations": [*state.get("observations", []), observation],
                    "step_index": index + 1,
                    "status": "running",
                }
            question = str((normalized or {}).get("question", ""))
            completion = evaluate_completion(
                {**state, "plan": [], "step_index": 0},
                stop_reason="clarification_requested",
                forced_outcome="needs_clarification",
            )
            return {
                "current_step": current,
                "plan": [],
                "step_index": 0,
                "status": "needs_clarification",
                "pending_approval": None,
                "outcome": "needs_clarification",
                "stop_reason": "clarification_requested",
                "completion": completion,
                "final_response": question or "需要补充信息后才能继续。",
            }

        proposed = self._proposed_mutation_count(tool, args, plan[index:])
        # 授权先于一切：意图不成立时，连扫描清单都不必查询（也避免用错误信息泄露
        # “某个 scan_id 是否存在”）。参数校验同样在这里完成。
        decision = self.policy.evaluate(
            tool, state=state, proposed_count=proposed, args=args
        )
        if not decision.allowed:
            observation = self._observation(step, "failed", error=decision.reason)
            return {"current_step": current, "observations": [*state.get("observations", []), observation],
                    "step_index": index + 1, "status": "running"}

        # 用户拒绝过本请求的累计审批后，后续同类的移动/重命名/分类步骤一律跳过。
        # 这个判断必须早于“请求审批”：拒绝后累计变更数没有获批，policy 仍会返回
        # requires_approval，先弹窗就会对同一请求反复要审批。
        if tool in _CUMULATIVE_TOOLS and state.get("batch_rejected"):
            observation = self._observation(
                step, "rejected", result="用户拒绝了批量操作，未移动该文件"
            )
            db.log_operation(
                tool, str(args.get("src") or args.get("scan_id") or ""), "rejected",
                detail="用户拒绝批量操作", thread_id=state.get("thread_id"),
            )
            return {
                "current_step": current,
                "observations": [*state.get("observations", []), observation],
                "step_index": index + 1,
                "status": "running",
            }

        # 引用旧扫描时必须在使用前核验：manifest 不存在、目录已消失都明确失败，
        # 不能拿过期清单继续移动文件。
        if tool in {"batch_move", "batch_rename", "batch_classify"}:
            reference_error = self._validate_scan_reference(args)
            if reference_error:
                observation = self._observation(step, "failed", error=reference_error)
                return {
                    "current_step": current,
                    "observations": [*state.get("observations", []), observation],
                    "step_index": index + 1,
                    "status": "running",
                }

        if tool != "delete_file" and decision.requires_approval:
            cumulative = self._cumulative_mutations(state)
            approval = {
                "approval_id": uuid.uuid4().hex,
                # 审批标题必须反映实际工具：batch_classify 也是移动，但它不是 batch_move。
                "action": tool if tool.startswith("batch_") else f"batch_{decision.mutation_kind}",
                "target": f"累计 {cumulative + proposed} 项文件变更",
                "detail": decision.reason,
                "count": proposed,
                "approved_limit": cumulative + proposed,
                "batch": True,
                "tool": tool,
                "args": args,
            }
            return {"current_step": current, "pending_approval": approval, "status": "waiting_approval"}

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
            executor = self._tools()
            observation = record_verification(
                executor.execute(step, model_call_limit=self._remaining_model_calls(state))
            )
            # 摘要/分类工具内部也会调用模型，必须计入本次运行预算。
            inner_model_calls = executor.consume_model_calls()
        update = {
            "current_step": current,
            "observations": [*state.get("observations", []), observation],
            "tool_observations": [*state.get("tool_observations", []), _summarize_observation(observation)],
            "step_index": index + 1,
            "status": "running",
            "run_trace": self._trace_update(
                state, tool_calls=1, model_calls=inner_model_calls
            ),
        }
        if tool in {"scan_directory", "extract_text", "classify_file"}:
            update["untrusted_file_data"] = [
                *state.get("untrusted_file_data", []), _summarize_observation(observation)
            ]
        if tool == "scan_directory":
            latest_scan = self._last_scan([observation])
            if latest_scan:
                update["last_scan"] = latest_scan
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
        inner_model_calls = 0
        if approved:
            with db.operation_thread(thread_id):
                executor = self._tools()
                observation = record_verification(
                    executor.execute(
                        step, model_call_limit=self._remaining_model_calls(state)
                    )
                )
                inner_model_calls = executor.consume_model_calls()
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
            "run_trace": self._trace_update(
                state,
                tool_calls=1 if approved else 0,
                model_calls=inner_model_calls,
            ),
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
        # 任何终态（含预算在 act 边界触发的 completed）都不再反思，
        # 否则模型可能把已经确定的 outcome 覆盖掉。
        if state.get("status") in {
            "failed", "needs_clarification", "completed", "cancelled"
        }:
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
            replan_limit_reached = False
            if decision == "replan" and state.get("replan_count", 0) >= _MAX_REPLANS:
                decision = "done"
                replan_limit_reached = True
            if decision == "continue" and index >= len(plan):
                decision = "done"
            if decision == "done":
                completion = evaluate_completion(
                    state,
                    stop_reason="replan_limit" if replan_limit_reached else "model_done",
                    replan_limit_reached=replan_limit_reached,
                )
                outcome = completion["outcome"]
                # 模型措辞只在确定性校验通过时作为正文；任何部分完成/失败/拒绝/
                # 预算耗尽都用后端生成的计数与原因，避免“全部成功”的幻觉。
                if outcome == "success" and result.final_response:
                    response = result.final_response
                else:
                    response = completion["message"]
                next_status = "failed" if outcome == "failed" else "completed"
                conversation = list(state.get("conversation") or [])
                conversation.append({
                    "role": "assistant",
                    "content": response[:2000],
                    "run_id": state.get("run_id", ""),
                    "outcome": outcome,
                })
                return {
                    "reflection": result.model_dump(),
                    "status": next_status,
                    "outcome": outcome,
                    "stop_reason": completion["stop_reason"],
                    "completion": completion,
                    "conversation": conversation[-20:],
                    "run_trace": self._trace_update(state, model_calls=1),
                    "final_response": response,
                    "current_step": {},
                }
            return {
                "reflection": {**result.model_dump(), "decision": decision},
                "status": "running",
                "current_step": {},
                "run_trace": self._trace_update(state, model_calls=1),
            }
        except Exception as exc:  # noqa: BLE001
            logger.exception("反思失败")
            # 工具已执行，反思失败不能回滚；安全终止并如实报告。
            completion = evaluate_completion(
                state,
                stop_reason="reflection_error",
                forced_outcome="failed",
            )
            return {
                "status": "failed",
                "error": str(exc),
                "outcome": "failed",
                "stop_reason": "reflection_error",
                "completion": completion,
                "run_trace": self._trace_update(state, model_calls=1),
                "final_response": f"文件操作已停止：反思阶段失败（{exc}）。"
                + completion.get("message", "")
                + "请查看任务记录确认已完成部分。",
            }

    # ---------------- 路由 ----------------

    @staticmethod
    def _after_plan(state: AgentState) -> Literal["act", "done"]:
        if state.get("status") in {"completed", "failed", "needs_clarification"} or not state.get("plan"):
            return "done"
        return "act"

    @staticmethod
    def _after_act(state: AgentState) -> Literal["approval", "reflecting"]:
        return "approval" if state.get("pending_approval") else "reflecting"

    @staticmethod
    def _after_reflect(state: AgentState) -> Literal["act", "plan", "done"]:
        if state.get("status") in {"completed", "failed", "cancelled", "needs_clarification"}:
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
    def _cumulative_mutations(state: AgentState) -> int:
        mutations = state.get("mutations") or {}
        return sum(int(mutations.get(name, 0)) for name in ("move", "rename"))

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
    def _batch_result(matched: int, successes: list[str], failed: list[dict], skipped: list[dict], *, truncated: bool = False, reason: str = "none", sources: Optional[list[str]] = None) -> Dict[str, Any]:
        return ToolExecutor.batch_result(
            matched, successes, failed, skipped,
            truncated=truncated, reason=reason, sources=sources,
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
        # 同一 thread 的追问保留对话与最近扫描；每次运行重新开始审批/变更计数。
        # 因此“刚才那些 PDF”可复用 scan_id，但历史审批绝不会为新请求授权。
        try:
            previous = self.state(thread_id)
        except Exception:  # noqa: BLE001 - 新线程尚无 checkpoint
            previous = {}
        if previous.get("pending_approval"):
            # 等待审批时不允许用新消息隐式“恢复”旧计划：LangGraph 会把新输入当作
            # interrupt 的返回值，可能被误读为拒绝/批准。必须先显式处理审批。
            raise RuntimeError("当前会话仍在等待审批，请先批准或拒绝该操作后再发送新消息。")
        run_id = uuid.uuid4().hex
        # Token 用量是“本次运行”的统计，不能把上一轮的用量累加进新 run。
        tracing.clear(thread_id)
        # 澄清是同一目标的延续：用户这轮往往只补充“哪个目录”，不会重复操作动词。
        # 只有上一轮确实停在 needs_clarification、且这轮既不是改口也不是新任务时
        # 才沿用原授权；已完成/失败/取消的请求不延伸授权。
        prior_intent = ""
        if previous.get("status") == "needs_clarification":
            prior_intent = str(previous.get("trusted_user_intent") or "").strip()
        if prior_intent and not _is_clarification_answer(message):
            prior_intent = ""
        trusted_intent = (
            f"{prior_intent}\n{message}".strip()
            if prior_intent and prior_intent != message.strip()
            else message
        )
        conversation = list(previous.get("conversation") or [])
        conversation.append({"role": "user", "content": message, "run_id": run_id})
        conversation = conversation[-20:]
        last_scan = previous.get("last_scan") or self._last_scan(
            previous.get("observations", [])
        )
        initial: AgentState = {
            "thread_id": thread_id,
            "run_id": run_id,
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
            "outcome": "",
            "stop_reason": "",
            "completion": {},
            "perception": {},
            "reflection": {},
            "batch_approved": False,
            "batch_rejected": False,
            "trusted_user_intent": trusted_intent,
            "untrusted_file_data": [],
            "tool_observations": [],
            "mutations": {"move": 0, "rename": 0, "delete": 0},
            "approved_mutation_limit": 0,
            "conversation": conversation,
            "last_scan": last_scan,
            "run_trace": {
                "run_id": run_id,
                "node_timings": [],
                "model_calls": 0,
                "token_usage": {"input": None, "output": None, "source": "unknown"},
            },
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
        try:
            state = self.state(thread_id)
        except Exception:  # noqa: BLE001
            state = {}
        completion = evaluate_completion(
            state, stop_reason="cancelled", forced_outcome="cancelled"
        )
        self.graph.update_state(
            self.config(thread_id),
            {
                "status": "cancelled",
                "pending_approval": None,
                "outcome": "cancelled",
                "stop_reason": "cancelled",
                "completion": completion,
                "final_response": completion["message"],
            },
        )
