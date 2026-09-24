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
  pending_approval?: Record<string, unknown> | null;
  observations?: Array<Record<string, unknown>>;
  final_response?: string;
  error?: string;
  summary?: TaskSummary;
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

export type TaskStatus = 'pending' | 'running' | 'success' | 'failed' | 'waiting';

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
  openai_api_key_set: boolean;
  ollama_base_url: string;
  ollama_model: string;
  structured_output_mode: StructuredOutputMode;
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

export interface TaskSummary {
  ok: number;
  failed: number;
  skipped: number;
  files: string[];
  operations: TaskSummaryOperation[];
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
