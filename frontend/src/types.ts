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

/** preload 注入的桌面桥接对象（见 electron/preload.cts）。 */
export interface DesktopBridge {
  platform: string;
  sessionToken: string;
  backendUrl: string;
  chooseDirectory: () => Promise<string | null>;
  versions: { electron: string; chrome: string; node: string };
}

declare global {
  interface Window {
    desktop?: DesktopBridge;
  }
}
