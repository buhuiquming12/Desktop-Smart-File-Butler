import type {
  ApprovalResponse,
  BackendConfig,
  ChatRequest,
  ChatResponse,
  HealthResponse,
  LLMModelsRequest,
  LLMModelsResponse,
  LLMSettings,
  LLMSettingsUpdate,
  OperationLog,
  Preference,
  SandboxSettings,
  RollbackSummary,
  ScheduledJob,
  WSEvent,
  ThreadState,
} from '../types';

export const DEFAULT_API_BASE = 'http://127.0.0.1:8000';
export const DEFAULT_WS_BASE = 'ws://127.0.0.1:8000';

const LOOPBACK_HOSTS = new Set(['localhost', '127.0.0.1', '::1', '[::1]']);

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

export function normalizeApiBase(value: string): string {
  const url = new URL(value.trim());
  if (url.protocol !== 'http:' && url.protocol !== 'https:') throw new Error('REST 地址必须使用 http:// 或 https://');
  if (!LOOPBACK_HOSTS.has(url.hostname.toLowerCase())) throw new Error('本地后端地址仅允许 localhost、127.0.0.1 或 ::1');
  return trimTrailingSlash(url.toString());
}

export function normalizeWsBase(value: string): string {
  const url = new URL(value.trim());
  if (url.protocol !== 'ws:' && url.protocol !== 'wss:') throw new Error('WebSocket 地址必须使用 ws:// 或 wss://');
  if (!LOOPBACK_HOSTS.has(url.hostname.toLowerCase())) throw new Error('本地 WebSocket 地址仅允许 loopback 主机');
  return trimTrailingSlash(url.toString());
}

export function wsBaseFromApi(apiBase: string): string {
  const url = new URL(normalizeApiBase(apiBase));
  url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
  return trimTrailingSlash(url.toString());
}

export function defaultApiBase(): string {
  const injected = typeof window !== 'undefined' ? window.desktop?.backendUrl : '';
  try {
    return normalizeApiBase(injected || DEFAULT_API_BASE);
  } catch {
    return DEFAULT_API_BASE;
  }
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

  sendChat(request: ChatRequest): Promise<ChatResponse> {
    return this.request('/api/chat', { method: 'POST', body: JSON.stringify(request) });
  }

  getThread(threadId: string): Promise<ThreadState> {
    return this.request(`/api/threads/${encodeURIComponent(threadId)}`);
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

  cancelThread(threadId: string): Promise<unknown> {
    return this.request(`/api/threads/${encodeURIComponent(threadId)}/cancel`, { method: 'POST' });
  }

  rollbackOperation(opId: number): Promise<unknown> {
    return this.request(`/api/operations/${encodeURIComponent(String(opId))}/rollback`, { method: 'POST' });
  }

  rollbackThread(threadId: string): Promise<RollbackSummary> {
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

export async function sendChatRest(
  api: ApiClient,
  request: ChatRequest,
  onThread: (threadId: string) => void,
): Promise<ChatResponse> {
  const response = await api.sendChat(request);
  onThread(response.thread_id);
  return response;
}

export function reconciliationEvents(threadId: string, state: ThreadState): WSEvent[] {
  const events: WSEvent[] = (state.observations ?? []).map((observation) => ({
    type: 'tool_result', thread_id: threadId, payload: {
      tool: observation.tool,
      detail: typeof observation.error === 'string' && observation.error
        ? observation.error
        : typeof observation.description === 'string' ? observation.description : '已从后端恢复执行结果',
      success: observation.status === 'ok',
    },
  }));
  if (state.pending_approval) events.push({ type: 'approval_required', thread_id: threadId, payload: state.pending_approval });
  if (['completed', 'failed', 'cancelled'].includes(state.status)) {
    events.push({ type: 'done', thread_id: threadId, payload: {
      status: state.status, message: state.final_response ?? '', error: state.error ?? '',
      observations: state.observations ?? [], summary: state.summary,
    } });
  }
  return events;
}

export function rollbackFeedback(result: RollbackSummary): { message: string; kind: 'success' | 'info' | 'error' } {
  return {
    message: `撤销完成：成功 ${result.ok}，跳过 ${result.skipped}，失败 ${result.failed}`,
    kind: result.failed > 0 ? 'error' : result.skipped > 0 ? 'info' : 'success',
  };
}

export type SocketMessage =
  | { type: 'chat'; message: string; thread_id?: string }
  | { type: 'cancel'; thread_id: string }
  | ({ type: 'approval' } & ApprovalResponse);

interface AgentSocketOptions {
  baseUrl?: string;
  clientId: string;
  onEvent: (event: WSEvent) => void;
  onOpen?: () => void;
  onClose?: () => void;
  onError?: () => void;
  /** 覆盖重连等待时长（毫秒）；主要供测试注入确定值。 */
  backoff?: (attempt: number) => number;
}

const RECONNECT_BASE_MS = 500;
const RECONNECT_MAX_MS = 15_000;

/**
 * 指数退避重连间隔（毫秒），带 0.8~1.0 抖动。
 *
 * 抖动是为了错开后端重启时所有客户端的重连时刻，避免惊群；上界保证不会无限增长。
 */
export function reconnectDelay(
  attempt: number,
  baseMs = RECONNECT_BASE_MS,
  maxMs = RECONNECT_MAX_MS,
): number {
  const exponential = Math.min(maxMs, baseMs * 2 ** Math.max(0, attempt));
  return Math.min(maxMs, Math.round(exponential * (0.8 + Math.random() * 0.2)));
}

export class AgentSocket {
  private socket: WebSocket | null = null;
  private readonly url: string;
  private readonly options: AgentSocketOptions;
  private attempt = 0;
  private retryTimer: ReturnType<typeof setTimeout> | null = null;
  private stopped = false;

  constructor(options: AgentSocketOptions) {
    this.options = options;
    const base = trimTrailingSlash(options.baseUrl ?? DEFAULT_WS_BASE);
    // 浏览器 WebSocket 无法设置自定义请求头，令牌以查询参数携带（见 backend P0-2）。
    const token = sessionToken();
    const suffix = token ? `?token=${encodeURIComponent(token)}` : '';
    this.url = `${base}/ws/${encodeURIComponent(options.clientId)}${suffix}`;
  }

  connect(): void {
    this.stopped = false;
    this.open();
  }

  private open(): void {
    if (this.socket && (this.socket.readyState === WebSocket.OPEN || this.socket.readyState === WebSocket.CONNECTING)) return;
    let socket: WebSocket;
    try {
      socket = new WebSocket(this.url);
    } catch {
      this.options.onError?.();
      this.scheduleReconnect();
      return;
    }
    this.socket = socket;
    // 所有监听器都先确认自己仍是在用连接：disconnect() 或重连替换后，旧 socket 的
    // 迟到事件既不能污染状态，也不能再触发一次重连。
    socket.addEventListener('open', () => {
      if (this.socket !== socket) return;
      this.attempt = 0;  // 连上即重置退避，避免长连接掉线后仍按最大间隔等待
      this.options.onOpen?.();
    });
    socket.addEventListener('close', () => {
      if (this.socket !== socket) return;
      this.options.onClose?.();
      this.scheduleReconnect();
    });
    socket.addEventListener('error', () => {
      if (this.socket !== socket) return;
      this.options.onError?.();
    });
    socket.addEventListener('message', (message) => {
      if (this.socket !== socket) return;
      try {
        const value: unknown = JSON.parse(String(message.data));
        if (isWSEvent(value)) this.options.onEvent(value);
      } catch (error) {
        console.warn('忽略无法解析的 WebSocket 消息', error);
      }
    });
  }

  /**
   * 断线后按指数退避自动重连（B2）。
   *
   * 后端按 client_id 路由事件，重连成功后后续事件即恢复送达；断线期间漏掉的事件
   * 不补发，由 App 的兜底超时负责兜底告知用户。
   */
  private scheduleReconnect(): void {
    if (this.stopped || this.retryTimer !== null) return;
    const delay = this.options.backoff?.(this.attempt) ?? reconnectDelay(this.attempt);
    this.attempt += 1;
    this.retryTimer = setTimeout(() => {
      this.retryTimer = null;
      if (!this.stopped) this.open();
    }, delay);
  }

  send(message: SocketMessage): boolean {
    if (this.socket?.readyState !== WebSocket.OPEN) return false;
    this.socket.send(JSON.stringify(message));
    return true;
  }

  disconnect(): void {
    this.stopped = true;
    if (this.retryTimer !== null) {
      clearTimeout(this.retryTimer);
      this.retryTimer = null;
    }
    this.socket?.close();
    this.socket = null;
  }
}

function isWSEvent(value: unknown): value is WSEvent {
  if (!value || typeof value !== 'object') return false;
  const candidate = value as Partial<WSEvent>;
  return typeof candidate.type === 'string' && typeof candidate.thread_id === 'string' && !!candidate.payload && typeof candidate.payload === 'object';
}
