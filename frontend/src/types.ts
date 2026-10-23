export type ConnectionState = 'connecting' | 'connected' | 'disconnected' | 'error';
export type ApprovalDecision = 'approve' | 'reject';
export type WSEventType =
  | 'token'
  | 'node'
  | 'tool_call'
  | 'tool_result'
  | 'approval_required'
  | 'task'
  | 'done'
  | 'error';

export interface ChatRequest {
  message: string;
  thread_id?: string;
  client_id?: string;
}

export interface ChatResponse {
  thread_id: string;
  status: string;
}

export interface ThreadState {
  thread_id?: string;
  status: string;
  /** 目标完成度，独立于流程状态：success/partial/failed/rejected/cancelled/budget_exhausted/... */
  outcome?: string;
  stop_reason?: string;
  pending_approval?: Record<string, unknown> | null;
  observations?: Array<Record<string, unknown>>;
  final_response?: string;
  error?: string;
  summary?: TaskSummary;
  run_id?: string;
  usage?: TokenUsage;
}

export interface RollbackSummary {
  ok: number;
  skipped: number;
  failed: number;
  details?: Array<Record<string, unknown>>;
}

export interface ApprovalResponse {
  thread_id: string;
  approval_id: string;
  decision: ApprovalDecision;
}

export interface PendingApproval {
  approval_id: string;
  action: string;
  target: string;
  detail: string;
  created_at: string;
  thread_id: string;
}

export interface WSEvent {
  type: WSEventType;
  thread_id: string;
  payload: Record<string, unknown>;
}

export interface ChatMessage {
  id: string;
  role: 'user' | 'assistant' | 'system';
  content: string;
  timestamp: string;
  pending?: boolean;
  error?: boolean;
  /** 任务完成时由 done 事件携带的结构化摘要（可选，无则纯文本降级）。 */
  summary?: TaskSummary;
  /** 归属会话；有摘要时用于撤销/详情定位。 */
  threadId?: string;
}

export type TaskStatus = 'pending' | 'running' | 'success' | 'partial' | 'failed' | 'waiting';

export interface TaskItem {
  id: string;
  title: string;
  detail: string;
  status: TaskStatus;
  updatedAt: string;
  threadId?: string;
}

export interface Preference {
  key: string;
  value: string;
}

export interface ScheduledJob {
  job_id?: string;
  directory: string;
  instruction: string;
  cron: string;
  enabled: boolean;
}

export interface OperationLog {
  id?: number;
  ts: string;
  action: string;
  target: string;
  dest?: string | null;
  status: string;
  detail: string;
  thread_id?: string | null;
}

export interface BackendConfig {
  [key: string]: unknown;
}

export type LLMProvider = 'openai' | 'ollama';

/** auto = 原生结构化输出优先、失败自动降级；prompt = 手动强制降级为提示词 JSON。 */
export type StructuredOutputMode = 'auto' | 'prompt';

export interface LLMSettings {
  provider: LLMProvider;
  openai_base_url: string;
  openai_model: string;
  /** 兼容字段：与 api_key_set 同义，只表示“是否已配置”，永远不含明文。 */
  openai_api_key_set: boolean;
  /** 仅布尔状态：API Key 已保存到本地 SecretStore（DPAPI）。 */
  api_key_set?: boolean;
  /** 本地密钥存储元信息（后端类型/是否持久化/迁移诊断），绝不含密钥。 */
  api_key_storage?: ApiKeyStorage;
  ollama_base_url: string;
  ollama_model: string;
  structured_output_mode: StructuredOutputMode;
}

/** 本地密钥存储状态：只描述后端与迁移结果，绝不包含密钥本身。 */
export interface ApiKeyStorage {
  configured: boolean;
  source: 'secret_store' | 'env' | 'migration_failed' | 'unavailable' | 'unset' | string;
  backend: string;
  persistent: boolean;
  migration?: { status: string; message: string; attempted: boolean };
  diagnostic?: string;
}

export interface LLMSettingsUpdate {
  provider?: LLMProvider;
  openai_base_url?: string;
  openai_model?: string;
  openai_api_key?: string;
  ollama_base_url?: string;
  ollama_model?: string;
  structured_output_mode?: StructuredOutputMode;
}

export interface LLMModelsRequest {
  provider?: LLMProvider;
  base_url?: string;
  api_key?: string;
}

export interface LLMModelsResponse {
  provider: LLMProvider;
  models: string[];
}

export interface HealthResponse {
  status: string;
  [key: string]: unknown;
}

export interface SandboxSettings {
  roots: string[];
  source: 'database' | 'env';
  env_roots: string[];
}

/** 默认管理目录：Agent 在用户未指定路径时默认操作的位置（必须位于授权目录内）。 */
export interface WorkspaceSettings {
  /** 当前生效的默认管理目录；未配置或已失效时为空串。 */
  default_managed_root: string;
  /** 数据库里保存的值（可能已越界/已删除，仅用于提示“已失效”）。 */
  stored_default_managed_root: string;
  valid: boolean;
  /** 失效原因（人话，可为空）。 */
  message: string;
  allowed_roots: string[];
}

export interface WorkspaceSettingsUpdate {
  /** 空串表示清除默认管理目录。 */
  default_managed_root: string;
}

/** 某个外部工具配置项的生效来源：界面保存的覆盖项 / .env 默认值。 */
export type ToolConfigSource = 'database' | 'env';

export type OCRStatus =
  | 'available'
  | 'not_configured'
  | 'binary_not_found'
  | 'language_pack_missing'
  | 'invalid_tessdata_dir'
  | 'error';

/** OCR 能力检测结果（后端 ocr_capability()，键恒定存在）。 */
export interface OCRCapability {
  status: OCRStatus;
  available: boolean;
  version: string;
  languages: string[];
  missing_languages: string[];
  tesseract_cmd: string;
  tessdata_dir: string;
  message: string;
}

/** 外部工具（当前含 Tesseract）路径设置 + 实时能力检测。 */
export interface ToolSettings {
  tesseract_cmd: string;
  tessdata_dir: string;
  sources: Partial<Record<'tesseract_cmd' | 'tessdata_dir', ToolConfigSource>>;
  ocr: OCRCapability;
}

export interface ToolSettingsUpdate {
  /** 留空表示不修改；显式传空字符串表示清除覆盖、回退 .env。 */
  tesseract_cmd?: string;
  tessdata_dir?: string;
}

/** 任务完成结构化摘要（后端 done 事件 payload.summary）。 */
export interface TaskSummaryOperation {
  tool: string;
  tool_label: string;
  description: string;
  status: string;
  status_label: string;
  path: string;
  dest?: string | null;
}

/** 摘要里带回的预览引用（preview_batch 的结果）。 */
export interface TaskPreviewRef {
  preview_id: string;
  op: string;
  counts?: Partial<PreviewCounts>;
}

/** Token 用量：供应商未提供时 input/output 为 null，source=unknown。 */
export interface TokenUsage {
  input: number | null;
  output: number | null;
  calls?: number;
  source: string;
}

export type TaskOutcome =
  | 'success'
  | 'partial'
  | 'failed'
  | 'rejected'
  | 'cancelled'
  | 'budget_exhausted'
  | 'needs_clarification'
  | 'unknown';

export interface TaskSummary {
  /** 工具调用次数（不是文件数）。 */
  ok: number;
  failed: number;
  skipped: number;
  files: string[];
  operations: TaskSummaryOperation[];
  /** 本次任务里生成的操作预览：点一下可在预览面板打开并确认执行。 */
  previews?: TaskPreviewRef[];
  /** 以下字段由后端确定性校验产生；旧后端可能缺省。 */
  outcome?: TaskOutcome | string;
  outcome_label?: string;
  stop_reason?: string;
  verified?: boolean;
  /** 文件级计数，与工具调用次数分开统计。 */
  files_ok?: number;
  files_failed?: number;
  files_skipped?: number;
  matched?: number;
  tool_calls?: number;
  unexecuted_steps?: number;
  truncated_reasons?: string[];
  /** 用户要求但未发生的副作用（如只扫描就结束时的 move）。 */
  unmet_effects?: string[];
}

/** 首次使用检查结果：null 表示「未能确认」（如配置接口需要令牌而当前环境无法提供）。 */
export interface SetupHints {
  /** 是否已执行过检查（避免闪烁）。 */
  checked: boolean;
  backendOk: boolean;
  modelOk: boolean | null;
  sandboxOk: boolean | null;
  ocrOk: boolean | null;
}

/** 活动中心条目来源分组。 */
export type ActivityKind = 'current' | 'background' | 'scheduled' | 'approval';

export interface ActivityItem {
  id: string;
  kind: ActivityKind;
  source: string;
  /** 状态文本（用于徽标与 aria-label）。 */
  status: TaskStatus;
  statusLabel: string;
  updatedAt: string;
  summary: string;
  threadId: string;
  approvalId?: string;
  /** 是否正在运行（可停止、可结束计数）。 */
  running: boolean;
  /** 是否为当前正在查看的会话。 */
  active: boolean;
  /** 是否有可展开的操作明细/详情。 */
  hasDetail: boolean;
  /** 是否可以切换到该会话（有消息或任务历史可查看）。 */
  switchable: boolean;
  /** 结果涉及的主要文件/目录路径（来自结构化摘要），供复制/打开所在目录。 */
  resultPaths?: string[];
}

/** preload 注入的桌面桥接对象（见 electron/preload.cts）。 */
export interface DesktopBridge {
  platform: string;
  sessionToken: string;
  backendUrl: string;
  chooseDirectory: () => Promise<string | null>;
  /** 打开原生文件选择器（OCR 可执行文件等），取消返回 null。 */
  chooseFile: () => Promise<string | null>;
  /** 在系统文件管理器中显示指定路径（仅本地绝对路径，经主进程参数校验后执行）。 */
  revealPath: (filePath: string) => Promise<boolean>;
  versions: { electron: string; chrome: string; node: string };
}

declare global {
  interface Window {
    desktop?: DesktopBridge;
  }
}

// ---------------- P0：操作预览 ----------------

export type PreviewOp = 'move' | 'rename' | 'classify' | 'delete';

export interface PreviewFilterInput {
  extensions?: string[];
  name_contains?: string;
  min_size?: number;
  max_size?: number;
}

export interface PreviewItem {
  item_id: string;
  source: string;
  name: string;
  op: PreviewOp;
  size: number;
  modified: string;
  target: string;
  target_name: string;
  status: 'ready' | 'conflict' | 'skipped';
  reason: string;
  category: string;
  basis: string;
  fallback: boolean;
  fallback_reason?: string;
  editable_category?: boolean;
}

export interface PreviewCounts {
  total: number;
  ready: number;
  conflict: number;
  skipped: number;
  fallback: number;
  model_calls?: number;
}

export interface PreviewPage {
  preview_id: string;
  created_at: string;
  op: PreviewOp;
  directory: string;
  digest: string;
  status: string;
  spec: Record<string, unknown>;
  counts: PreviewCounts;
  items: PreviewItem[];
  offset: number;
  limit: number;
  has_more: boolean;
  approval?: {
    approval_id: string;
    count: number;
    /** 确认绑定后的最终落点：item_id → 目标路径。 */
    targets?: Record<string, string>;
    overrides?: Record<string, string>;
    /** 非空表示这次确认已经用掉，不能再执行。 */
    used_at?: string;
  } | null;
}

/** 预览列表项：用于“打开已有预览”（对话里生成的预览也能在这里打开）。 */
export interface PreviewListItem {
  preview_id: string;
  created_at: string;
  op: PreviewOp | string;
  directory: string;
  digest: string;
  status: string;
}

export interface PreviewCreateInput {
  op: PreviewOp;
  directory?: string;
  scan_id?: string;
  recursive?: boolean;
  filter?: PreviewFilterInput;
  dest_dir?: string;
  dest_root?: string;
  mode?: 'extension' | 'content';
  prefix?: string;
  suffix?: string;
  find?: string;
  replace?: string;
  case_sensitive?: boolean;
  template?: string;
  sequence_start?: number;
  sequence_step?: number;
  sequence_padding?: number;
  sequence_position?: 'prefix' | 'suffix';
  date_source?: 'modified' | 'created' | 'now' | 'content';
  date_format?: string;
  use_sequence?: boolean;
  use_date?: boolean;
  content_budget?: number;
}

export interface PreviewConfirmResult {
  preview_id: string;
  approval_id: string;
  /** 一次性确认 token（与 approval_id 同权限，执行时必须原样回传）。 */
  confirm_token?: string;
  count: number;
  requires_approval: boolean;
  threshold: number;
  selection_digest: string;
  /** 审批绑定：请求指纹、授权范围指纹、选中条目指纹与过期时间。 */
  request_digest?: string;
  authorization_scope_digest?: string;
  selected_item_digest?: string;
  expires_at?: number;
  /** 确认时绑定的最终落点（item_id → 目标路径）。 */
  targets?: Record<string, string>;
  /** 确认时提交并绑定的类别覆盖；执行时必须原样沿用。 */
  overrides?: Record<string, string>;
}

export interface PreviewExecuteItemResult {
  item_id: string;
  source: string;
  op: PreviewOp;
  target: string;
  status: 'ok' | 'failed' | 'skipped';
  detail: string;
}

export interface PreviewExecuteResult {
  preview_id: string;
  op: PreviewOp;
  selected: number;
  ok: number;
  failed: number;
  skipped: number;
  results: PreviewExecuteItemResult[];
  rollback_thread_id: string;
}

// ---------------- P1：重复文件 ----------------

export interface DuplicateFile {
  path: string;
  name: string;
  size: number;
  modified: string;
  hardlink_of: string;
  keep: boolean;
  delete_by_default: boolean;
}

export interface DuplicateGroup {
  group_id: string;
  hash: string;
  size: number;
  count: number;
  distinct_inodes: number;
  reclaimable: number;
  suggested_keep: string;
  keep_reason: string;
  files: DuplicateFile[];
}

export interface DuplicateSummary {
  scanned_files: number;
  duplicate_groups: number;
  duplicate_files: number;
  reclaimable_bytes: number;
  changed_files: number;
  stale: boolean;
  errors: number;
}

export interface DuplicateProgress {
  phase: string;
  scanned: number;
  hashed: number;
  total_files: number;
  total_candidates: number;
  groups_done: number;
  bytes_read: number;
  current: string;
  errors: Array<{ path: string; error: string }>;
  finished_at: string;
}

export interface DuplicateScan {
  scan_id: string;
  status: 'running' | 'completed' | 'cancelled' | 'failed';
  error: string;
  directories: string[];
  groups: DuplicateGroup[];
  changed: string[];
  summary: Partial<DuplicateSummary>;
  progress: DuplicateProgress;
}

export interface DuplicateCleanupResult {
  scan_id: string;
  thread_id: string;
  deleted: number;
  skipped: number;
  failed: number;
  freed_bytes: number;
  results: Array<{ path: string; group_id: string; status: string; detail: string; trash_path?: string }>;
}

// ---------------- P2：内容索引与搜索 ----------------

export interface IndexProgress {
  phase: string;
  scanned: number;
  indexed: number;
  skipped: number;
  failed: number;
  unchanged: number;
  removed: number;
  total: number;
  current: string;
  errors: Array<{ path: string; error: string }>;
  /** 遍历是否被取消或触到文件数上限：此时不会清理任何索引记录。 */
  truncated?: boolean;
  truncated_reason?: string;
  /** 非空表示本次跳过了清理（遍历不完整或非递归）。 */
  prune_skipped?: string;
}

export interface IndexJob {
  job_id: string;
  root: string;
  status: 'running' | 'completed' | 'cancelled' | 'failed';
  error: string;
  progress: IndexProgress;
}

export interface IndexStats {
  documents: number;
  by_status: Record<string, number>;
  indexed_bytes: number;
  fts5: boolean;
  jobs: IndexJob[];
}

export interface SearchHit {
  path: string;
  name: string;
  size: number;
  ext: string;
  status: string;
  truncated: boolean;
  indexed_at: string;
  snippet: string;
}

export interface SearchResponse {
  query: string;
  backend: string;
  total: number;
  offset: number;
  limit: number;
  results: SearchHit[];
  capabilities: { fts5: boolean; semantic: boolean };
}

// ---------------- P5：自动整理规则 ----------------

export interface OrganizeRule {
  rule_id: string;
  directory: string;
  dest_root: string;
  mode: 'extension' | 'content';
  instruction: string;
  enabled: boolean;
  settle_seconds: number;
  created_at: string;
  updated_at: string;
  last_run_at: string;
  last_result: string;
  processed_count: number;
}

export interface RuleListResponse {
  rules: OrganizeRule[];
  running: boolean;
}

// ---------------- P6：回收站 ----------------

export interface TrashEntry {
  op_id: number;
  original_path: string;
  name: string;
  trash_path: string;
  deleted_at: string;
  size: number;
  thread_id: string;
  task: string;
  detail: string;
  conflict: boolean;
}

export interface TrashList {
  total: number;
  offset: number;
  limit: number;
  entries: TrashEntry[];
}

export interface TrashRestoreResult {
  ok: number;
  skipped: number;
  failed: number;
  results: Array<{
    op_id: number;
    original_path?: string;
    status: string;
    detail: string;
    restored_to?: string;
    conflict?: boolean;
  }>;
}
