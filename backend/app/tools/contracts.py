"""工具契约：参数模型、副作用与权限信息的唯一登记处。

设计目标：
- 工具名、参数形状和副作用集中登记，避免 PolicyEngine 与实际执行分叉。
- 参数在进入文件系统前由 Pydantic 确定性校验；未知字段明确拒绝。
- 副作用是后端安全事实，不来自提示词或模型自报。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator


SideEffect = Literal[
    "read",        # 只读，无副作用
    "create",      # 新建目录
    "write",       # 新建/写入摘要文件
    "move",        # 移动（含分类移动）
    "rename",      # 重命名
    "delete",      # 删除（进入回收站）
    "preference",  # 写入长期偏好
    "schedule",    # 创建定时任务
]

# 与「当前用户请求累计文件变更」有关的副作用；删除另行走强制审批。
CUMULATIVE_SIDE_EFFECTS = frozenset({"move", "rename"})


class _StrictModel(BaseModel):
    """所有工具参数都拒绝未知字段，避免拼写错误被静默忽略。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)


class ScanArgs(_StrictModel):
    directory: str = Field(min_length=1)
    recursive: bool = False


class ExtractArgs(_StrictModel):
    file_path: str = Field(min_length=1)


class ClassifyArgs(_StrictModel):
    file_path: str = Field(min_length=1)


class MakeDirArgs(_StrictModel):
    path: str = Field(min_length=1)


class MoveArgs(_StrictModel):
    src: str = Field(min_length=1)
    dest_dir: str = Field(min_length=1)
    new_name: Optional[str] = None


class BatchFilter(_StrictModel):
    extensions: list[str] = Field(default_factory=list)
    names: list[str] = Field(default_factory=list)
    name_contains: str = ""
    min_size: Optional[int] = Field(default=None, ge=0)
    max_size: Optional[int] = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _size_range(self) -> "BatchFilter":
        if (
            self.min_size is not None
            and self.max_size is not None
            and self.min_size > self.max_size
        ):
            raise ValueError("min_size 不能大于 max_size")
        return self


class BatchMoveArgs(_StrictModel):
    scan_id: str = Field(min_length=1)
    dest_dir: str = Field(min_length=1)
    filter: BatchFilter = Field(default_factory=BatchFilter)


class BatchRenameArgs(_StrictModel):
    scan_id: str = Field(min_length=1)
    filter: BatchFilter = Field(default_factory=BatchFilter)
    prefix: str = ""
    suffix: str = ""
    find: str = Field(default="", max_length=200)
    replace: str = Field(default="", max_length=200)
    case_sensitive: bool = False
    template: str = Field(default="", max_length=300)
    sequence_start: Optional[int] = Field(default=None, ge=0, le=999_999)
    sequence_step: int = Field(default=1, ge=1, le=1000)
    sequence_padding: int = Field(default=3, ge=1, le=10)
    sequence_position: Literal["prefix", "suffix"] = "suffix"
    date_source: Optional[Literal["modified", "created", "now", "content"]] = None
    date_format: str = Field(default="YYYYMMDD", max_length=40)

    @model_validator(mode="after")
    def _needs_affix(self) -> "BatchRenameArgs":
        # 前缀/后缀、查找替换、序号、日期、模板至少要有一项，否则是空操作。
        if not any([
            self.prefix, self.suffix, self.find, self.template,
            self.sequence_start is not None, self.date_source,
        ]):
            raise ValueError(
                "batch_rename 至少需要 prefix/suffix/find/template/sequence_start/date_source 之一"
            )
        return self

    @model_validator(mode="after")
    def _sequence_position(self) -> "BatchRenameArgs":
        if self.sequence_position not in {"prefix", "suffix"}:
            raise ValueError("sequence_position 仅支持 prefix 或 suffix")
        return self


class BatchClassifyArgs(_StrictModel):
    scan_id: str = Field(min_length=1)
    dest_root: str = Field(min_length=1)
    filter: BatchFilter = Field(default_factory=BatchFilter)
    # extension：按扩展名规则；content：按正文 + 模型（带缓存与预算回退）。
    mode: Literal["extension", "content"] = "extension"


class RenameArgs(_StrictModel):
    src: str = Field(min_length=1)
    new_name: str = Field(min_length=1)


class DeleteArgs(_StrictModel):
    path: str = Field(min_length=1)


class WriteSummaryArgs(_StrictModel):
    file_path: str = Field(min_length=1)
    output_dir: str = Field(min_length=1)
    output_name: Optional[str] = None


class SetPreferenceArgs(_StrictModel):
    key: str = Field(min_length=1, max_length=200)
    value: str = Field(max_length=10_000)


class DeletePreferenceArgs(_StrictModel):
    key: str = Field(min_length=1, max_length=200)


class CreateScheduleArgs(_StrictModel):
    directory: str = Field(min_length=1)
    instruction: str = Field(min_length=1, max_length=20_000)
    cron: str = Field(min_length=1, max_length=120)


class ClarifyArgs(_StrictModel):
    """信息不足时向用户提问；不产生任何文件副作用。"""

    question: str = Field(min_length=1, max_length=1_000)
    options: list[str] = Field(default_factory=list, max_length=10)
    reason: str = Field(default="", max_length=500)


class PreviewBatchArgs(_StrictModel):
    """为批量操作生成逐文件预览；只读，不修改文件系统。"""

    op: Literal["move", "rename", "classify", "delete"]
    scan_id: str = Field(default="", max_length=64)
    directory: str = Field(default="", max_length=1000)
    filter: BatchFilter = Field(default_factory=BatchFilter)
    dest_dir: str = Field(default="", max_length=1000)
    dest_root: str = Field(default="", max_length=1000)
    mode: Literal["extension", "content"] = "extension"
    prefix: str = Field(default="", max_length=200)
    suffix: str = Field(default="", max_length=200)
    find: str = Field(default="", max_length=200)
    replace: str = Field(default="", max_length=200)
    template: str = Field(default="", max_length=300)
    sequence_start: Optional[int] = Field(default=None, ge=0, le=999_999)
    sequence_step: int = Field(default=1, ge=1, le=1000)
    sequence_padding: int = Field(default=3, ge=1, le=10)
    sequence_position: Literal["prefix", "suffix"] = "suffix"
    date_source: Optional[Literal["modified", "created", "now", "content"]] = None
    date_format: str = Field(default="YYYYMMDD", max_length=40)

    @model_validator(mode="after")
    def _needs_target(self) -> "PreviewBatchArgs":
        if not self.scan_id and not self.directory:
            raise ValueError("preview_batch 需要 scan_id 或 directory")
        if self.op == "move" and not self.dest_dir:
            raise ValueError("移动预览需要 dest_dir")
        if self.op == "classify" and not self.dest_root:
            raise ValueError("分类预览需要 dest_root")
        return self


class SearchContentArgs(_StrictModel):
    query: str = Field(min_length=1, max_length=500)
    limit: int = Field(default=20, ge=1, le=100)
    offset: int = Field(default=0, ge=0)


@dataclass(frozen=True)
class ToolContract:
    name: str
    args_model: type[BaseModel]
    side_effect: SideEffect
    description: str

    @property
    def is_mutation(self) -> bool:
        return self.side_effect in {"create", "write", "move", "rename", "delete"}

    @property
    def is_cumulative(self) -> bool:
        return self.side_effect in CUMULATIVE_SIDE_EFFECTS

    @property
    def always_requires_approval(self) -> bool:
        return self.side_effect == "delete"


TOOL_CONTRACTS: dict[str, ToolContract] = {
    "scan_directory": ToolContract(
        "scan_directory", ScanArgs, "read", "扫描目录并保存完整 manifest"
    ),
    "extract_text": ToolContract(
        "extract_text", ExtractArgs, "read", "提取文档或 OCR 文本"
    ),
    "classify_file": ToolContract(
        "classify_file", ClassifyArgs, "read", "按规则/模型给出文件类别"
    ),
    "make_dir": ToolContract("make_dir", MakeDirArgs, "create", "创建目录"),
    "move_file": ToolContract("move_file", MoveArgs, "move", "移动单个文件"),
    "batch_move": ToolContract(
        "batch_move", BatchMoveArgs, "move", "按 manifest 规则批量移动"
    ),
    "rename_file": ToolContract(
        "rename_file", RenameArgs, "rename", "重命名单个文件"
    ),
    "batch_rename": ToolContract(
        "batch_rename", BatchRenameArgs, "rename", "按 manifest 规则批量重命名"
    ),
    "batch_classify": ToolContract(
        "batch_classify", BatchClassifyArgs, "move", "按扩展名规则分类移动"
    ),
    "delete_file": ToolContract(
        "delete_file", DeleteArgs, "delete", "删除到项目回收站"
    ),
    "write_summary": ToolContract(
        "write_summary", WriteSummaryArgs, "write", "生成并写入摘要文件"
    ),
    "set_preference": ToolContract(
        "set_preference", SetPreferenceArgs, "preference", "保存长期偏好"
    ),
    "delete_preference": ToolContract(
        "delete_preference", DeletePreferenceArgs, "preference", "删除长期偏好"
    ),
    "create_schedule": ToolContract(
        "create_schedule", CreateScheduleArgs, "schedule", "创建定时任务"
    ),
    "request_clarification": ToolContract(
        "request_clarification", ClarifyArgs, "read", "暂停并向用户澄清目标或对象"
    ),
    "preview_batch": ToolContract(
        "preview_batch", PreviewBatchArgs, "read", "生成逐文件操作预览（只读，不修改文件）"
    ),
    "search_content": ToolContract(
        "search_content", SearchContentArgs, "read", "在已建立的内容索引中检索正文"
    ),
}

# 兼容既有导入：工具名 → 累计文件变更类别。
MUTATION_KIND: dict[str, str] = {
    name: contract.side_effect
    for name, contract in TOOL_CONTRACTS.items()
    if contract.is_cumulative or contract.side_effect == "delete"
}


def contract_for(tool: str) -> Optional[ToolContract]:
    return TOOL_CONTRACTS.get(tool)


def validate_tool_args(
    tool: str, args: Any
) -> tuple[Optional[dict[str, Any]], str]:
    """校验并规范化参数。

    返回 ``(normalized_args, error)``。未知工具、非对象参数和字段错误都明确失败，
    绝不返回“空参数 + 成功”让调用方误以为可以执行。
    """
    contract = TOOL_CONTRACTS.get(tool)
    if contract is None:
        return None, f"未知工具或缺少工具契约: {tool}"
    if args is None:
        args = {}
    if not isinstance(args, dict):
        return None, "工具参数必须是对象"
    try:
        model = contract.args_model.model_validate(args)
    except ValidationError as exc:
        problems = []
        for error in exc.errors():
            location = ".".join(str(part) for part in error.get("loc", ())) or "<root>"
            problems.append(f"{location}: {error.get('msg', '无效值')}")
        return None, "参数无效：" + "；".join(problems)
    return model.model_dump(), ""
