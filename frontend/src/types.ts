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

export interface LLMSettings {
  provider: LLMProvider;
  openai_base_url: string;
  openai_model: string;
  openai_api_key_set: boolean;
  ollama_base_url: string;
  ollama_model: string;
}

export interface LLMSettingsUpdate {
  provider?: LLMProvider;
  openai_base_url?: string;
  openai_model?: string;
  openai_api_key?: string;
  ollama_base_url?: string;
  ollama_model?: string;
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
