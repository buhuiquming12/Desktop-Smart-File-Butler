"""LangGraph Agent 核心：感知 → 规划 → 工具调用 → 观察 → 反思。

删除操作在图内使用 ``interrupt`` 暂停；前端审批后用 ``Command(resume=...)``
恢复同一 thread。所有实际磁盘访问仍由工具层执行沙箱校验。
"""
from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, Literal, Optional

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from .. import db
from ..logging_conf import get_logger
from ..models import ScheduledJob
from ..security import resolve_in_sandbox
from ..tools import extract, filesystem, scheduler, vectorstore
from .llm import build_llm
from .prompts import PLANNER_SYSTEM_PROMPT, REFLECTION_SYSTEM_PROMPT
from .state import AgentState, PlanOutput, ReflectionOutput

logger = get_logger(__name__)
_MAX_REPLANS = 3
_MAX_SCAN_RESULTS = 500
_SUPPORTED_CONTENT_EXTS = {
    "pdf", "doc", "docx", "txt", "md", "csv", "log", "json",
    "png", "jpg", "jpeg", "bmp", "tiff", "webp",
}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _safe_summary_name(source: Path, output_name: Optional[str]) -> str:
    raw = output_name or f"{source.stem}_摘要.md"
    # 禁止把 output_name 当成路径逃逸；仅取文件名，并清除 Windows 非法字符。
    name = Path(raw).name
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    if not name:
        name = f"{source.stem}_摘要.md"
    if not Path(name).suffix:
        name += ".md"
    return name


class AgentRuntime:
    """封装编译后的 LangGraph，并提供启动/恢复/读取状态接口。"""

    def __init__(self) -> None:
        self.llm = build_llm(temperature=0.1)
        self.planner = self.llm.with_structured_output(PlanOutput)
        self.reflector = self.llm.with_structured_output(ReflectionOutput)
        self.checkpointer = MemorySaver()
        self.graph = self._build_graph()

    def _build_graph(self):
        workflow = StateGraph(AgentState)
        workflow.add_node("perceive", self._perceive)
        workflow.add_node("planning", self._plan)
        workflow.add_node("act", self._act)
        workflow.add_node("approval", self._approval)
        workflow.add_node("observe", self._observe)
        workflow.add_node("reflecting", self._reflect)

        workflow.add_edge(START, "perceive")
        workflow.add_edge("perceive", "planning")
        workflow.add_conditional_edges(
            "planning", self._after_plan, {"act": "act", "done": END}
        )
        workflow.add_conditional_edges(
            "act",
            self._after_act,
            {"approval": "approval", "observe": "observe"},
        )
        workflow.add_edge("approval", "observe")
        workflow.add_edge("observe", "reflecting")
        workflow.add_conditional_edges(
            "reflecting",
            self._after_reflect,
            {"act": "act", "plan": "planning", "done": END},
        )
        return workflow.compile(checkpointer=self.checkpointer)

    # ---------------- 图节点 ----------------

    def _perceive(self, state: AgentState) -> Dict[str, Any]:
        from ..config import get_settings

        roots = [str(p) for p in get_settings().sandbox_root_paths]
        preferences = [p.model_dump() for p in db.all_preferences()]
        history = [
            op.model_dump(mode="json") for op in db.recent_operations(limit=20)
        ]
        return {
            "perception": {
                "allowed_roots": roots,
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
        }

    def _plan(self, state: AgentState) -> Dict[str, Any]:
        replan_count = state.get("replan_count", 0)
        context = {
            "用户目标": state.get("user_request", ""),
            "环境感知": state.get("perception", {}),
            "既有计划": state.get("plan", []),
            "已获得观察": state.get("observations", []),
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
            for item in result.steps[:30]:
                if item.tool == "set_preference" and "key" in item.args:
                    key = str(item.args["key"]).lower()
                    if any(
                        secret in key
                        for secret in ("api_key", "token", "secret", "password")
                    ):
                        raise ValueError("拒绝把密钥、令牌或密码保存为用户偏好")
            steps = [step.model_dump() for step in result.steps[:30]]
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

        observation = self._execute_step(step)
        return {
            "current_step": current,
            "observations": [*state.get("observations", []), observation],
            "step_index": index + 1,
            "status": "running",
        }

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
        if approved:
            observation = self._execute_step(step)
        else:
            db.log_operation(
                pending["action"], pending["target"], "rejected", detail="用户拒绝审批"
            )
            observation = self._observation(
                step, "rejected", result="用户拒绝了危险操作，未修改文件"
            )

        return {
            "pending_approval": None,
            "observations": [*state.get("observations", []), observation],
            "step_index": state.get("step_index", 0) + 1,
            "status": "running",
        }

    def _observe(self, state: AgentState) -> Dict[str, Any]:
        # 工具结果已由 act/approval 以结构化观察写入状态；该节点明确保留循环语义。
        return {"status": "running"}

    def _reflect(self, state: AgentState) -> Dict[str, Any]:
        if state.get("status") == "failed":
            return {}

        plan = state.get("plan", [])
        index = state.get("step_index", 0)
        latest = state.get("observations", [])[-1:] or []
        context = {
            "目标": state.get("user_request", ""),
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
    def _after_act(state: AgentState) -> Literal["approval", "observe"]:
        return "approval" if state.get("pending_approval") else "observe"

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

    def _execute_step(self, step: Dict[str, Any]) -> Dict[str, Any]:
        tool = str(step.get("tool", ""))
        args = dict(step.get("args") or {})
        try:
            if tool == "scan_directory":
                files = filesystem.scan_directory(
                    str(args["directory"]), bool(args.get("recursive", False))
                )
                result: Any = {
                    "items": [f.model_dump(mode="json") for f in files[:_MAX_SCAN_RESULTS]],
                    "total": len(files),
                    "truncated": len(files) > _MAX_SCAN_RESULTS,
                }
            elif tool == "extract_text":
                result = extract.extract_text(str(args["file_path"]))
            elif tool == "classify_file":
                result = self._classify_file(str(args["file_path"]))
            elif tool == "make_dir":
                result = filesystem.make_dir(str(args["path"]))
            elif tool == "move_file":
                result = filesystem.move_file(
                    str(args["src"]),
                    str(args["dest_dir"]),
                    str(args["new_name"]) if args.get("new_name") else None,
                )
            elif tool == "rename_file":
                result = filesystem.rename_file(str(args["src"]), str(args["new_name"]))
            elif tool == "delete_file":
                result = filesystem.delete_file(str(args["path"]))
            elif tool == "write_summary":
                result = self._write_summary(
                    str(args["file_path"]),
                    str(args["output_dir"]),
                    str(args["output_name"]) if args.get("output_name") else None,
                )
            elif tool == "set_preference":
                db.set_preference(str(args["key"]), str(args["value"]))
                result = {"key": str(args["key"]), "saved": True}
            elif tool == "create_schedule":
                result = self._create_schedule(args)
            else:
                raise ValueError(f"不支持的工具: {tool}")
            return self._observation(step, "ok", result=result)
        except Exception as exc:  # noqa: BLE001
            logger.exception("工具执行失败 tool=%s args=%s", tool, args)
            return self._observation(step, "failed", error=str(exc))

    def _classify_file(self, file_path: str) -> Dict[str, Any]:
        path = resolve_in_sandbox(file_path, must_exist=True)
        ext = path.suffix.lower().lstrip(".")
        rule = vectorstore.rule_category(ext)
        text = ""
        if ext in _SUPPORTED_CONTENT_EXTS:
            text = extract.extract_text(str(path))
        semantic = vectorstore.suggest_category(text) if text else None
        category = semantic or rule
        vectorstore.index_file(
            file_id=str(path),
            text=text,
            category=category,
            metadata={"path": str(path), "extension": ext},
        )
        return {"category": category, "rule_category": rule, "semantic_category": semantic}

    def _write_summary(
        self, file_path: str, output_dir: str, output_name: Optional[str]
    ) -> str:
        source = resolve_in_sandbox(file_path, must_exist=True)
        destination_dir = resolve_in_sandbox(output_dir)
        destination_dir.mkdir(parents=True, exist_ok=True)
        text = extract.extract_text(str(source))
        if not text or text.startswith("["):
            raise ValueError(f"无法从 {source.name} 提取可摘要内容: {text}")

        response = self.llm.invoke(
            [
                SystemMessage(
                    content=(
                        "请用中文总结文档，保留主题、关键事实、日期、行动项。"
                        "使用 Markdown，避免补充原文没有的信息。"
                    )
                ),
                HumanMessage(content=f"文件名：{source.name}\n\n{text}"),
            ]
        )
        summary = response.content
        if not isinstance(summary, str):
            summary = _json(summary)

        target = destination_dir / _safe_summary_name(source, output_name)
        # 摘要也是写操作；默认不覆盖，自动编号。
        if target.exists():
            stem, suffix = target.stem, target.suffix
            index = 1
            while target.exists():
                target = destination_dir / f"{stem} ({index}){suffix}"
                index += 1
        target.write_text(summary, encoding="utf-8")
        db.log_operation("write_summary", str(source), "ok", dest=str(target))
        return str(target)

    @staticmethod
    def _create_schedule(args: Dict[str, Any]) -> Dict[str, Any]:
        directory = str(resolve_in_sandbox(str(args["directory"]), must_exist=True))
        job = ScheduledJob(
            job_id=uuid.uuid4().hex,
            directory=directory,
            instruction=str(args["instruction"]),
            cron=str(args["cron"]),
            enabled=True,
        )
        scheduler.add_job(job)
        return job.model_dump()

    @staticmethod
    def _observation(
        step: Dict[str, Any], status: str, *, result: Any = None, error: str = ""
    ) -> Dict[str, Any]:
        return {
            "step_id": step.get("id", ""),
            "description": step.get("description", ""),
            "tool": step.get("tool", ""),
            "args": step.get("args", {}),
            "status": status,
            "result": result,
            "error": error,
        }

    @staticmethod
    def _fallback_summary(state: AgentState) -> str:
        observations = state.get("observations", [])
        ok = sum(1 for item in observations if item.get("status") == "ok")
        failed = sum(1 for item in observations if item.get("status") == "failed")
        rejected = sum(1 for item in observations if item.get("status") == "rejected")
        return f"任务结束：成功 {ok} 项，失败 {failed} 项，已拒绝 {rejected} 项。"

    # ---------------- 对外接口 ----------------

    @staticmethod
    def config(thread_id: str) -> Dict[str, Any]:
        return {"configurable": {"thread_id": thread_id}, "recursion_limit": 120}

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
        }
        return self.graph.stream(initial, self.config(thread_id), stream_mode="updates")

    def resume_stream(
        self, thread_id: str, decision: Literal["approve", "reject"]
    ) -> Iterable[Dict[str, Any]]:
        return self.graph.stream(
            Command(resume={"decision": decision}),
            self.config(thread_id),
            stream_mode="updates",
        )

    def state(self, thread_id: str) -> Dict[str, Any]:
        snapshot = self.graph.get_state(self.config(thread_id))
        return dict(snapshot.values) if snapshot and snapshot.values else {}
