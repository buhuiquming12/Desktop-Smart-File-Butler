import type {
  ApprovalResponse,
  BackendConfig,
  ChatRequest,
  HealthResponse,
  LLMModelsRequest,
  LLMModelsResponse,
  LLMSettings,
  LLMSettingsUpdate,
  OperationLog,
  Preference,
  SandboxSettings,
  ScheduledJob,
  WSEvent,
} from '../types';

export const DEFAULT_API_BASE = 'http://127.0.0.1:8000';
export const DEFAULT_WS_BASE = 'ws://127.0.0.1:8000';

export class ApiError extends Error {
  readonly status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
  }
}

function trimTrailingSlash(value: string): string {
  return value.replace(/\/+$/, '');
}

/** 读取 preload 注入的会话令牌；浏览器直连（无 preload）时为空串。 */
function sessionToken(): string {
  return (typeof window !== 'undefined' && window.desktop?.sessionToken) || '';
}

function getErrorMessage(value: unknown): string {
  if (typeof value === 'string') return value;
  if (value && typeof value === 'object' && 'detail' in value) {
    const detail = (value as { detail?: unknown }).detail;
    if (typeof detail === 'string') return detail;
  }
  return '请求失败';
}

export class ApiClient {
  private readonly baseUrl: string;

  constructor(baseUrl = DEFAULT_API_BASE) {
    this.baseUrl = trimTrailingSlash(baseUrl);
  }

  private async request<T>(path: string, init?: RequestInit): Promise<T> {
    const token = sessionToken();
    const response = await fetch(`${this.baseUrl}${path}`, {
      ...init,
      headers: {
        Accept: 'application/json',
        ...(init?.body ? { 'Content-Type': 'application/json' } : {}),
        ...(token ? { 'X-Butler-Token': token } : {}),
        ...init?.headers,
      },
    });

    if (!response.ok) {
      let body: unknown;
      try {
        body = await response.json();
      } catch {
        body = await response.text();
      }
      throw new ApiError(getErrorMessage(body), response.status);
    }

    if (response.status === 204) return undefined as T;
    return (await response.json()) as T;
  }

  getHealth(): Promise<HealthResponse> {
    return this.request('/api/health');
  }

  getConfig(): Promise<BackendConfig> {
    return this.request('/api/config');
  }

  sendChat(request: ChatRequest): Promise<unknown> {
    return this.request('/api/chat', { method: 'POST', body: JSON.stringify(request) });
  }

  respondToApproval(response: ApprovalResponse): Promise<unknown> {
    return this.request('/api/approvals/respond', {
      method: 'POST',
      body: JSON.stringify(response),
    });
  }

  getPreferences(): Promise<Preference[]> {
    return this.request('/api/preferences');
  }

  updatePreference(preference: Preference): Promise<Preference> {
    return this.request(`/api/preferences/${encodeURIComponent(preference.key)}`, {
      method: 'PUT',
      body: JSON.stringify({ value: preference.value }),
    });
  }

  getOperations(): Promise<OperationLog[]> {
    return this.request('/api/operations');
  }

  rollbackOperation(opId: number): Promise<unknown> {
    return this.request(`/api/operations/${encodeURIComponent(String(opId))}/rollback`, { method: 'POST' });
  }

  rollbackThread(threadId: string): Promise<unknown> {
    return this.request(`/api/threads/${encodeURIComponent(threadId)}/rollback`, { method: 'POST' });
  }

  getJobs(): Promise<ScheduledJob[]> {
    return this.request('/api/jobs');
  }

  createJob(job: Omit<ScheduledJob, 'job_id'>): Promise<ScheduledJob> {
    return this.request('/api/jobs', { method: 'POST', body: JSON.stringify(job) });
  }

  deleteJob(jobId: string): Promise<{ job_id: string; status: string }> {
    return this.request(`/api/jobs/${encodeURIComponent(jobId)}`, { method: 'DELETE' });
  }

  getLLMSettings(): Promise<LLMSettings> {
    return this.request('/api/settings/llm');
  }

  updateLLMSettings(update: LLMSettingsUpdate): Promise<LLMSettings> {
    return this.request('/api/settings/llm', { method: 'PUT', body: JSON.stringify(update) });
  }

  listLLMModels(request: LLMModelsRequest): Promise<LLMModelsResponse> {
    return this.request('/api/settings/llm/models', { method: 'POST', body: JSON.stringify(request) });
  }

  getSandboxSettings(): Promise<SandboxSettings> {
    return this.request('/api/settings/sandbox');
  }

  updateSandboxSettings(roots: string[]): Promise<SandboxSettings> {
    return this.request('/api/settings/sandbox', { method: 'PUT', body: JSON.stringify({ roots }) });
  }
}

export type SocketMessage =
  | { type: 'chat'; message: string; thread_id?: string }
  | ({ type: 'approval' } & ApprovalResponse);

interface AgentSocketOptions {
  baseUrl?: string;
  clientId: string;
  onEvent: (event: WSEvent) => void;
  onOpen?: () => void;
  onClose?: () => void;
  onError?: () => void;
}

export class AgentSocket {
  private socket: WebSocket | null = null;
  private readonly url: string;
  private readonly options: AgentSocketOptions;

  constructor(options: AgentSocketOptions) {
    this.options = options;
    const base = trimTrailingSlash(options.baseUrl ?? DEFAULT_WS_BASE);
    // 浏览器 WebSocket 无法设置自定义请求头，令牌以查询参数携带（见 backend P0-2）。
    const token = sessionToken();
    const suffix = token ? `?token=${encodeURIComponent(token)}` : '';
    this.url = `${base}/ws/${encodeURIComponent(options.clientId)}${suffix}`;
  }

  connect(): void {
    if (this.socket?.readyState === WebSocket.OPEN || this.socket?.readyState === WebSocket.CONNECTING) return;
    this.socket = new WebSocket(this.url);
    this.socket.addEventListener('open', () => this.options.onOpen?.());
    this.socket.addEventListener('close', () => this.options.onClose?.());
    this.socket.addEventListener('error', () => this.options.onError?.());
    this.socket.addEventListener('message', (message) => {
      try {
        const value: unknown = JSON.parse(String(message.data));
        if (isWSEvent(value)) this.options.onEvent(value);
      } catch (error) {
        console.warn('忽略无法解析的 WebSocket 消息', error);
      }
    });
  }

  send(message: SocketMessage): boolean {
    if (this.socket?.readyState !== WebSocket.OPEN) return false;
    this.socket.send(JSON.stringify(message));
    return true;
  }

  disconnect(): void {
    this.socket?.close();
    this.socket = null;
  }
}

function isWSEvent(value: unknown): value is WSEvent {
  if (!value || typeof value !== 'object') return false;
  const candidate = value as Partial<WSEvent>;
  return typeof candidate.type === 'string' && typeof candidate.thread_id === 'string' && !!candidate.payload && typeof candidate.payload === 'object';
}
