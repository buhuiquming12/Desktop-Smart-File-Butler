import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { AgentSocket, ApiClient, DEFAULT_API_BASE, DEFAULT_WS_BASE } from './api/client';
import { ApprovalModal } from './components/ApprovalModal';
import { ChatPanel } from './components/ChatPanel';
import { Settings, type SettingsTab } from './components/Settings';
import { TaskBoard } from './components/TaskBoard';
import type {
  ApprovalDecision,
  ChatMessage,
  ConnectionState,
  LLMModelsRequest,
  LLMModelsResponse,
  LLMSettings,
  LLMSettingsUpdate,
  OperationLog,
  PendingApproval,
  Preference,
  SandboxSettings,
  ScheduledJob,
  TaskItem,
  TaskStatus,
  WSEvent,
} from './types';
import {
  BUSY_STALL_TICK_MS,
  STALLED_NOTICE,
  isStalled,
  reduceThreadEvent,
  type ThreadViewState,
} from './state';

function createId(prefix: string): string {
  return `${prefix}-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
}

function readStored(key: string, fallback: string): string {
  return localStorage.getItem(key) ?? fallback;
}

function ensureClientId(): string {
  const current = localStorage.getItem('file-butler-client-id');
  if (current) return current;
  const value = typeof crypto.randomUUID === 'function' ? crypto.randomUUID() : createId('client');
  localStorage.setItem('file-butler-client-id', value);
  return value;
}

function payloadText(payload: Record<string, unknown>, ...keys: string[]): string {
  for (const key of keys) {
    const value = payload[key];
    if (typeof value === 'string') return value;
  }
  return '';
}

function taskStatus(value: unknown): TaskStatus {
  return value === 'pending' || value === 'running' || value === 'success' || value === 'failed' || value === 'waiting'
    ? value
    : 'running';
}

function parseApproval(event: WSEvent): PendingApproval | null {
  const nested = event.payload.approval;
  const source = nested && typeof nested === 'object' ? nested as Record<string, unknown> : event.payload;
  const approvalId = payloadText(source, 'approval_id', 'id');
  if (!approvalId) return null;
  return {
    approval_id: approvalId,
    thread_id: event.thread_id,
    action: payloadText(source, 'action') || 'unknown',
    target: payloadText(source, 'target', 'path') || '未提供目标',
    detail: payloadText(source, 'detail', 'message'),
    created_at: payloadText(source, 'created_at') || new Date().toISOString(),
  };
}

export function App() {
  const clientId = useMemo(ensureClientId, []);
  const [apiBase, setApiBase] = useState(() => readStored('file-butler-api-base', DEFAULT_API_BASE));
  const [wsBase, setWsBase] = useState(() => readStored('file-butler-ws-base', DEFAULT_WS_BASE));
  const [connectionState, setConnectionState] = useState<ConnectionState>('connecting');
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [tasks, setTasks] = useState<TaskItem[]>([]);
  const [approvalQueue, setApprovalQueue] = useState<PendingApproval[]>([]);
  const [threadId, setThreadId] = useState<string>();
  const threadIdRef = useRef<string | undefined>(undefined);
  const [activeThreadId, setActiveThreadId] = useState<string>();
  // P1: 后台定时会话与当前聊天各自维护事件状态，事件不会覆盖活动会话。
  const [threadViews, setThreadViews] = useState<Record<string, ThreadViewState>>({});
  const [busy, setBusy] = useState(false);
  const [approvalSubmitting, setApprovalSubmitting] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [settingsTab, setSettingsTab] = useState<SettingsTab>('general');
  const [preferences, setPreferences] = useState<Preference[]>([]);
  const [llmSettings, setLLMSettings] = useState<LLMSettings | null>(null);
  const [sandboxSettings, setSandboxSettings] = useState<SandboxSettings | null>(null);
  const [jobs, setJobs] = useState<ScheduledJob[]>([]);
  const [logs, setLogs] = useState<OperationLog[]>([]);
  const [settingsLoading, setSettingsLoading] = useState(false);
  const [settingsError, setSettingsError] = useState<string | null>(null);
  const socketRef = useRef<AgentSocket | null>(null);
  // B2 心跳：最后一次收到后端事件的时刻，供 busy 看门狗判定是否已失联。
  const lastActivityRef = useRef<number>(Date.now());
  const api = useMemo(() => new ApiClient(apiBase), [apiBase]);

  const updateAssistant = useCallback((eventThreadId: string, content: string, append: boolean, error = false) => {
    setMessages((current) => {
      let index = -1;
      for (let cursor = current.length - 1; cursor >= 0; cursor -= 1) {
        const message = current[cursor];
        if (message?.role === 'assistant' && message.id === `assistant-${eventThreadId}`) {
          index = cursor;
          break;
        }
      }
      if (index < 0) {
        return [...current, {
          id: `assistant-${eventThreadId}`,
          role: 'assistant',
          content,
          timestamp: new Date().toISOString(),
          pending: !error,
          error,
        }];
      }
      const next = [...current];
      const previous = next[index];
      if (!previous) return current;
      next[index] = { ...previous, content: append ? `${previous.content}${content}` : content, pending: !error, error };
      return next;
    });
  }, []);

  const updateTask = useCallback((task: TaskItem) => {
    setTasks((current) => {
      const existing = current.findIndex((item) => item.id === task.id);
      if (existing < 0) return [task, ...current];
      const next = [...current];
      next[existing] = { ...next[existing], ...task };
      return next;
    });
  }, []);

  const handleSocketEvent = useCallback((event: WSEvent) => {
    lastActivityRef.current = Date.now();  // 任何事件都算心跳，避免看门狗误判长任务
    if (event.thread_id && threadIdRef.current && event.thread_id !== threadIdRef.current) {
      setThreadViews((current) => {
        const view = current[event.thread_id] ?? { messages: [], tasks: [], busy: true };
        const result = reduceThreadEvent(view, event);
        return { ...current, [event.thread_id]: result.view };
      });
      return;
    }
    if (event.thread_id) {
      threadIdRef.current = event.thread_id;
      setThreadId(event.thread_id);
    }
    const now = new Date().toISOString();

    switch (event.type) {
      case 'token':
        updateAssistant(event.thread_id, payloadText(event.payload, 'token', 'content', 'text'), true);
        break;
      case 'node': {
        const node = payloadText(event.payload, 'node', 'name') || '处理中';
        updateTask({ id: `${event.thread_id}-node`, title: 'Agent 工作流', detail: `正在执行：${node}`, status: 'running', updatedAt: now, threadId: event.thread_id });
        break;
      }
      case 'tool_call': {
        const name = payloadText(event.payload, 'tool', 'name', 'action') || '文件工具';
        updateTask({ id: payloadText(event.payload, 'task_id', 'call_id') || `${event.thread_id}-${name}`, title: name, detail: payloadText(event.payload, 'detail', 'target', 'message') || '正在调用工具', status: 'running', updatedAt: now, threadId: event.thread_id });
        break;
      }
      case 'tool_result': {
        const name = payloadText(event.payload, 'tool', 'name', 'action') || '工具执行';
        updateTask({ id: payloadText(event.payload, 'task_id', 'call_id') || `${event.thread_id}-${name}`, title: name, detail: payloadText(event.payload, 'detail', 'result', 'message') || '执行完成', status: event.payload.success === false ? 'failed' : 'success', updatedAt: now, threadId: event.thread_id });
        break;
      }
      case 'task': {
        const id = payloadText(event.payload, 'task_id', 'id') || `${event.thread_id}-task`;
        updateTask({ id, title: payloadText(event.payload, 'title', 'name') || '文件任务', detail: payloadText(event.payload, 'detail', 'message', 'description'), status: taskStatus(event.payload.status), updatedAt: now, threadId: event.thread_id });
        break;
      }
      case 'approval_required': {
        const approval = parseApproval(event);
        if (approval) {
          setApprovalQueue((current) => current.some((item) => item.approval_id === approval.approval_id) ? current : [...current, approval]);
          updateTask({ id: `approval-${approval.approval_id}`, title: approval.action, detail: approval.target, status: 'waiting', updatedAt: now, threadId: event.thread_id });
        }
        break;
      }
      case 'done': {
        // B1：后端已把失败统一收敛为 done(status=failed)，此处必须按失败呈现，
        // 否则用户只看到一句普通回复，无从得知任务其实失败了。
        const failed = event.payload.status === 'failed' || event.payload.status === 'cancelled';
        const finalText = payloadText(event.payload, 'message', 'content', 'result');
        if (finalText) updateAssistant(event.thread_id, finalText, false, failed);
        setMessages((current) => current.map((message) => message.id === `assistant-${event.thread_id}` ? { ...message, pending: false } : message));
        setTasks((current) => current.map((task) => task.threadId === event.thread_id && task.status === 'running' ? { ...task, status: failed ? 'failed' : 'success', updatedAt: now } : task));
        setBusy(false);
        break;
      }
      case 'error': {
        const text = payloadText(event.payload, 'message', 'error', 'detail') || '任务执行失败，请查看后端日志。';
        updateAssistant(event.thread_id, text, false, true);
        setTasks((current) => current.map((task) => task.threadId === event.thread_id && task.status === 'running' ? { ...task, status: 'failed', updatedAt: now } : task));
        setBusy(false);
        break;
      }
    }
  }, [updateAssistant, updateTask]);

  useEffect(() => {
    setConnectionState('connecting');
    const socket = new AgentSocket({
      baseUrl: wsBase,
      clientId,
      onEvent: handleSocketEvent,
      // 重连握手成功本身就是后端可达的证据，据此刷新心跳，不让看门狗误伤。
      onOpen: () => {
        lastActivityRef.current = Date.now();
        setConnectionState('connected');
      },
      onClose: () => setConnectionState('disconnected'),
      onError: () => setConnectionState('error'),
    });
    socketRef.current = socket;
    socket.connect();
    return () => {
      socket.disconnect();
      if (socketRef.current === socket) socketRef.current = null;
    };
  }, [clientId, handleSocketEvent, wsBase]);

  // B2 兜底：WS 断线时后端事件无处可送，前端只认终态事件来解除 busy，于是永久卡死。
  // 以“最后一次收到事件的时刻”为心跳，失联超时即强制解除并明确告知用户。
  useEffect(() => {
    if (!busy) return;
    const timer = setInterval(() => {
      if (!isStalled(lastActivityRef.current, Date.now())) return;
      setBusy(false);
      setMessages((current) => [...current, {
        id: createId('system'),
        role: 'system',
        content: STALLED_NOTICE,
        timestamp: new Date().toISOString(),
        error: true,
      }]);
    }, BUSY_STALL_TICK_MS);
    return () => clearInterval(timer);
  }, [busy]);

  const sendChat = useCallback((message: string) => {
    if (threadId) setActiveThreadId(threadId);
    const userMessage: ChatMessage = { id: createId('user'), role: 'user', content: message, timestamp: new Date().toISOString() };
    setMessages((current) => [...current, userMessage]);
    // 心跳从“发出请求”开始计，断线时也就从这一刻起测算失联时长。
    lastActivityRef.current = Date.now();
    setBusy(true);
    const socketPayload = threadId ? { type: 'chat' as const, message, thread_id: threadId } : { type: 'chat' as const, message };
    if (socketRef.current?.send(socketPayload)) return;

    const request = threadId ? { message, thread_id: threadId, client_id: clientId } : { message, client_id: clientId };
    void api.sendChat(request).catch((error: unknown) => {
      setBusy(false);
      setMessages((current) => [...current, {
        id: createId('system'),
        role: 'system',
        content: error instanceof Error ? `发送失败：${error.message}` : '发送失败：无法连接后端',
        timestamp: new Date().toISOString(),
        error: true,
      }]);
    });
  }, [api, clientId, threadId]);

  const stopChat = useCallback(() => {
    if (!threadId) {
      setBusy(false);
      return;
    }
    if (!socketRef.current?.send({ type: 'cancel', thread_id: threadId })) {
      void api.cancelThread(threadId).catch(() => undefined);
    }
  }, [api, threadId]);

  const decideApproval = useCallback(async (decision: ApprovalDecision) => {
    const approval = approvalQueue[0];
    if (!approval) return;
    setApprovalSubmitting(true);
    const response = { thread_id: approval.thread_id, approval_id: approval.approval_id, decision };
    try {
      if (!socketRef.current?.send({ type: 'approval', ...response })) await api.respondToApproval(response);
      setApprovalQueue((current) => current.slice(1));
      updateTask({ id: `approval-${approval.approval_id}`, title: approval.action, detail: approval.target, status: decision === 'approve' ? 'running' : 'failed', updatedAt: new Date().toISOString(), threadId: approval.thread_id });
    } catch (error) {
      setMessages((current) => [...current, { id: createId('system'), role: 'system', content: error instanceof Error ? `审批提交失败：${error.message}` : '审批提交失败', timestamp: new Date().toISOString(), error: true }]);
    } finally {
      setApprovalSubmitting(false);
    }
  }, [api, approvalQueue, updateTask]);

  const loadSettingsData = useCallback(async () => {
    setSettingsLoading(true);
    setSettingsError(null);
    const results = await Promise.allSettled([api.getPreferences(), api.getJobs(), api.getOperations(), api.getLLMSettings(), api.getSandboxSettings()]);
    const [preferenceResult, jobResult, logResult, llmResult, sandboxResult] = results;
    if (preferenceResult.status === 'fulfilled') setPreferences(preferenceResult.value);
    if (jobResult.status === 'fulfilled') setJobs(jobResult.value);
    if (logResult.status === 'fulfilled') setLogs(logResult.value);
    if (llmResult.status === 'fulfilled') setLLMSettings(llmResult.value);
    if (sandboxResult.status === 'fulfilled') setSandboxSettings(sandboxResult.value);
    const rejected = results.find((result) => result.status === 'rejected');
    if (rejected?.status === 'rejected') setSettingsError(rejected.reason instanceof Error ? rejected.reason.message : '部分数据加载失败');
    setSettingsLoading(false);
  }, [api]);

  useEffect(() => {
    if (settingsOpen) void loadSettingsData();
  }, [loadSettingsData, settingsOpen]);

  const openSettings = (tab: SettingsTab): void => {
    setSettingsTab(tab);
    setSettingsOpen(true);
  };

  const saveEndpoints = (newApiBase: string, newWsBase: string): void => {
    if (!newApiBase || !newWsBase) return;
    localStorage.setItem('file-butler-api-base', newApiBase);
    localStorage.setItem('file-butler-ws-base', newWsBase);
    setApiBase(newApiBase);
    setWsBase(newWsBase);
  };

  const saveLLM = async (update: LLMSettingsUpdate): Promise<void> => {
    setSettingsError(null);
    try {
      const saved = await api.updateLLMSettings(update);
      setLLMSettings(saved);
    } catch (error) {
      setSettingsError(error instanceof Error ? error.message : '保存模型配置失败');
      throw error;
    }
  };

  const fetchModels = (request: LLMModelsRequest): Promise<LLMModelsResponse> => api.listLLMModels(request);

  const saveSandbox = async (roots: string[]): Promise<void> => {
    setSettingsError(null);
    try {
      const saved = await api.updateSandboxSettings(roots);
      setSandboxSettings(saved);
    } catch (error) {
      setSettingsError(error instanceof Error ? error.message : '保存沙箱目录失败');
      throw error;
    }
  };

  const savePreference = async (preference: Preference): Promise<void> => {
    setSettingsError(null);
    try {
      const saved = await api.updatePreference(preference);
      setPreferences((current) => [...current.filter((item) => item.key !== saved.key), saved]);
    } catch (error) {
      setSettingsError(error instanceof Error ? error.message : '保存偏好失败');
      throw error;
    }
  };

  const createJob = async (job: Omit<ScheduledJob, 'job_id'>): Promise<void> => {
    setSettingsError(null);
    try {
      const saved = await api.createJob(job);
      setJobs((current) => [...current, saved]);
    } catch (error) {
      setSettingsError(error instanceof Error ? error.message : '创建定时任务失败');
      throw error;
    }
  };

  const deleteJob = async (jobId: string): Promise<void> => {
    setSettingsError(null);
    try {
      await api.deleteJob(jobId);
      setJobs((current) => current.filter((item) => item.job_id !== jobId));
    } catch (error) {
      setSettingsError(error instanceof Error ? error.message : '删除定时任务失败');
    }
  };

  const rollbackOperation = async (opId: number): Promise<void> => {
    setSettingsError(null);
    try {
      await api.rollbackOperation(opId);
      await loadSettingsData();
    } catch (error) {
      setSettingsError(error instanceof Error ? error.message : '撤销失败');
      throw error;
    }
  };

  const clearCompleted = (): void => {
    setTasks((current) => current.filter((task) => task.status === 'running' || task.status === 'waiting' || task.status === 'pending'));
  };

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand"><span className="brand-mark" aria-hidden="true">F</span><div><strong>文件管家</strong><small>Smart Butler</small></div></div>
        <nav className="main-nav" aria-label="主导航">
          <button className="active" type="button"><span aria-hidden="true">⌁</span>智能对话</button>
          <button type="button" onClick={() => openSettings('schedules')}><span aria-hidden="true">◷</span>定时任务</button>
          <button type="button" onClick={() => openSettings('logs')}><span aria-hidden="true">≡</span>操作日志</button>
          <button type="button" onClick={() => openSettings('preferences')}><span aria-hidden="true">◇</span>偏好设置</button>
          <button type="button" onClick={() => openSettings('model')}><span aria-hidden="true">✦</span>模型配置</button>
        </nav>
        <div className="sidebar-spacer" />
        <div className="safety-card"><span aria-hidden="true">⌾</span><div><strong>安全模式已启用</strong><p>高风险操作需审批</p></div></div>
        <button className="profile-button" type="button" onClick={() => openSettings('general')}>
          <span className="profile-avatar">本</span><span><strong>本地工作区</strong><small>{connectionState === 'connected' ? '服务正常' : '检查连接'}</small></span><b aria-hidden="true">•••</b>
        </button>
      </aside>

      <main className="workspace">
        <ChatPanel messages={activeThreadId && threadViews[activeThreadId] ? threadViews[activeThreadId].messages : messages} connectionState={connectionState} busy={activeThreadId && threadViews[activeThreadId] ? threadViews[activeThreadId].busy : busy} onSend={sendChat} onStop={stopChat} />
        <TaskBoard tasks={activeThreadId && threadViews[activeThreadId] ? threadViews[activeThreadId].tasks : tasks} onClear={clearCompleted} />
      </main>

      <ApprovalModal approval={approvalQueue[0] ?? null} submitting={approvalSubmitting} onDecision={(decision) => void decideApproval(decision)} />
      <Settings
        open={settingsOpen}
        initialTab={settingsTab}
        apiBase={apiBase}
        wsBase={wsBase}
        llmSettings={llmSettings}
        sandboxSettings={sandboxSettings}
        preferences={preferences}
        jobs={jobs}
        logs={logs}
        loading={settingsLoading}
        error={settingsError}
        onClose={() => setSettingsOpen(false)}
        onSaveEndpoints={saveEndpoints}
        onSaveLLM={saveLLM}
        onSaveSandbox={saveSandbox}
        onFetchModels={fetchModels}
        onSavePreference={savePreference}
        onCreateJob={createJob}
        onDeleteJob={deleteJob}
        onRollbackOperation={rollbackOperation}
        onRefresh={() => void loadSettingsData()}
      />
    </div>
  );
}
