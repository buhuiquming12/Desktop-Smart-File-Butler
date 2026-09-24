import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { AgentSocket, reconciliationEvents, rollbackFeedback, sendChatRest } from './api/client';
import { helpText, isCommandName, type SlashCommandName } from './commands';
import { ActivityCenter } from './components/ActivityCenter';
import { ApprovalModal } from './components/ApprovalModal';
import { ChatPanel } from './components/ChatPanel';
import { NewConversationModal } from './components/NewConversationModal';
import { Settings } from './components/Settings';
import { ToastStack, type ToastItem, type ToastKind } from './components/ToastStack';
import { useBackendConfig } from './hooks/useBackendConfig';
import { useSettingsController } from './hooks/useSettingsController';
import type {
  ActivityItem,
  ApprovalDecision,
  ChatMessage,
  ConnectionState,
  PendingApproval,
  TaskItem,
  TaskSummary,
  ThreadState,
  WSEvent,
} from './types';
import {
  BUSY_STALL_TICK_MS,
  STALLED_NOTICE,
  addToApprovalQueue,
  assistantMessageId,
  buildActivityItems,
  detachThreadToViews,
  isStalled,
  lastAssistantTurn,
  parseApproval,
  payloadText,
  parseSummary,
  reduceThreadEvent,
  resolveEventTarget,
  shouldPromptNewConversation,
  taskStatus,
  type NewConversationAction,
  type ThreadViewState,
} from './state';

function createId(prefix: string): string {
  return `${prefix}-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
}

export function App() {
  const [connectionState, setConnectionState] = useState<ConnectionState>('connecting');
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [tasks, setTasks] = useState<TaskItem[]>([]);
  const [approvalQueue, setApprovalQueue] = useState<PendingApproval[]>([]);
  const [threadId, setThreadId] = useState<string>();
  const threadIdRef = useRef<string | undefined>(undefined);
  // 每个会话的助手回复按「回合」编号：追问时推进序号，让本次回答落在新气泡里。
  const assistantTurnRef = useRef<Record<string, number>>({});
  // P1: 后台定时会话与当前聊天各自维护事件状态，事件不会覆盖活动会话。
  const [threadViews, setThreadViews] = useState<Record<string, ThreadViewState>>({});
  // 新对话后进入后台/已结束的会话 id：它们的迟到事件永远按后台路由，绝不污染新会话。
  const detachedThreadIdsRef = useRef<Set<string>>(new Set());
  const [newChatModalOpen, setNewChatModalOpen] = useState(false);
  const [toasts, setToasts] = useState<ToastItem[]>([]);
  const toastIdRef = useRef(0);
  const [busy, setBusy] = useState(false);
  const [approvalSubmitting, setApprovalSubmitting] = useState(false);
  const socketRef = useRef<AgentSocket | null>(null);
  const threadViewsRef = useRef<Record<string, ThreadViewState>>({});
  // B2 心跳：最后一次收到后端事件的时刻，供 busy 看门狗判定是否已失联。
  const lastActivityRef = useRef<number>(Date.now());

  useEffect(() => { threadViewsRef.current = threadViews; }, [threadViews]);

  // 正在运行的后台/定时会话：它们的进度只存在 threadViews 里（活动中心逐一列出），
  // 因此用「结束」按钮上的计数把它们暴露出来，否则用户根本不知道还有东西在跑。
  const runningBackgroundIds = useMemo(
    () => Object.entries(threadViews).filter(([, view]) => view.busy).map(([id]) => id),
    [threadViews],
  );
  const runningCount = runningBackgroundIds.length + (busy ? 1 : 0);

  // 切换后台会话时会把快照搬回这组三元状态，因此前台只有一个权威状态源。
  const chatMessages = messages;
  const chatTasks = tasks;
  const chatBusy = busy;

  /** 统一的 Toast 反馈：保存、失败、撤销、任务结束等都用它，避免各写一套。 */
  const notify = useCallback((content: string, kind: ToastKind = 'info') => {
    const id = ++toastIdRef.current;
    setToasts((current) => [...current, { id, content, kind }]);
    window.setTimeout(() => setToasts((current) => current.filter((toast) => toast.id !== id)), 3500);
  }, []);

  const {
    api,
    apiBase,
    wsBase,
    clientId,
    setupHints,
    refreshSetupCheck,
    saveEndpoints,
  } = useBackendConfig(notify);

  const updateAssistant = useCallback((eventThreadId: string, content: string, append: boolean, error = false) => {
    setMessages((current) => {
      const id = assistantMessageId(eventThreadId, assistantTurnRef.current[eventThreadId] ?? 1);
      let index = -1;
      for (let cursor = current.length - 1; cursor >= 0; cursor -= 1) {
        const message = current[cursor];
        if (message?.role === 'assistant' && message.id === id) {
          index = cursor;
          break;
        }
      }
      if (index < 0) {
        return [...current, {
          id,
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
    // 事件按 thread_id 隔离：转入后台/已结束会话的迟到事件路由到对应后台视图，不污染当前会话。
    const target = resolveEventTarget(event.thread_id, threadIdRef.current, detachedThreadIdsRef.current);
    if (target === 'background') {
      // 后台/定时会话的进度不覆盖当前会话视图，但审批必须照常入队并弹窗（B3）。
      setThreadViews((current) => {
        const view = current[event.thread_id] ?? { messages: [], tasks: [], busy: true };
        const result = reduceThreadEvent(view, event);
        return { ...current, [event.thread_id]: result.view };
      });
      // 在 setState 更新函数之外计算，避免在 reducer 里做副作用（StrictMode 会重放）。
      const backgroundApproval = event.type === 'approval_required' ? parseApproval(event) : null;
      if (backgroundApproval) {
        setApprovalQueue((current) => addToApprovalQueue(current, backgroundApproval));
      }
      const node = event.type === 'node' ? payloadText(event.payload, 'node', 'name') : '';
      if (event.type === 'done' || node === 'approval') {
        setApprovalQueue((current) => current.filter((item) => item.thread_id !== event.thread_id));
      }
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
        if (!event.thread_id && node === 'connected') break;
        if (node === 'approval') {
          setApprovalQueue((current) => current.filter((item) => item.thread_id !== event.thread_id));
          setTasks((current) => current.filter((task) => !(task.threadId === event.thread_id && task.status === 'waiting')));
        }
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
          setApprovalQueue((current) => addToApprovalQueue(current, approval));
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
        const assistantId = assistantMessageId(event.thread_id, assistantTurnRef.current[event.thread_id] ?? 1);
        const summary = parseSummary(event.payload.summary);
        setMessages((current) => current.map((message) => {
          if (message.id !== assistantId) return message;
          const next = { ...message, pending: false };
          if (summary) return { ...next, summary, threadId: event.thread_id };
          return next;
        }));
        setTasks((current) => current.map((task) => task.threadId === event.thread_id && task.status === 'running' ? { ...task, status: failed ? 'failed' : 'success', updatedAt: now } : task));
        setBusy(false);
        setApprovalQueue((current) => current.filter((item) => item.thread_id !== event.thread_id));
        notify(failed ? '任务已结束（失败或已取消）。' : '任务已完成，结果见下方摘要。', failed ? 'error' : 'success');
        break;
      }
      case 'error': {
        const text = payloadText(event.payload, 'message', 'error', 'detail') || '任务执行失败，请查看后端日志。';
        setMessages((current) => [...current, {
          id: createId('system'), role: 'system', content: text,
          timestamp: now, error: true,
        }]);
        // error 是可恢复诊断（如审批过期），只有 done 才能结束 busy/清掉审批。
        notify('请求未被接受，请根据提示重试。', 'error');
        break;
      }
    }
  }, [notify, updateAssistant, updateTask]);

  const reconcileThread = useCallback(async (targetId: string) => {
    try {
      const state: ThreadState = await api.getThread(targetId);
      for (const event of reconciliationEvents(targetId, state)) handleSocketEvent(event);
    } catch {
      // A brand-new WS request may not have written its first checkpoint yet.
    }
  }, [api, handleSocketEvent]);

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
        const running = Object.entries(threadViewsRef.current)
          .filter(([, view]) => view.busy)
          .map(([id]) => id);
        const active = threadIdRef.current;
        for (const id of new Set([...(active ? [active] : []), ...running])) void reconcileThread(id);
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
  }, [clientId, handleSocketEvent, reconcileThread, wsBase]);

  /** 手动重连：连接断开时由界面上的「立即重试」触发。 */
  const reconnectSocket = useCallback(() => {
    socketRef.current?.connect();
    notify('正在重新连接本地服务…');
  }, [notify]);

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

  const {
    open: settingsOpen,
    tab: settingsTab,
    preferences,
    llmSettings,
    sandboxSettings,
    workspaceSettings,
    toolSettings,
    jobs,
    logs,
    loading: settingsLoading,
    error: settingsError,
    close: closeSettings,
    show: openSettings,
    load: loadSettingsData,
    saveLLM,
    fetchModels,
    saveSandbox,
    saveWorkspace,
    saveTools,
    refreshTools,
    savePreference,
    createJob,
    deleteJob,
    rollbackOperation,
  } = useSettingsController(api, refreshSetupCheck);

  const sendChat = useCallback((message: string) => {
    if (threadId) {
      // 追问同一会话时推进回合序号：本次回答进入新的气泡，不再往第一条回复里追加。
      assistantTurnRef.current[threadId] = (assistantTurnRef.current[threadId] ?? 1) + 1;
    }
    const userMessage: ChatMessage = { id: createId('user'), role: 'user', content: message, timestamp: new Date().toISOString() };
    setMessages((current) => [...current, userMessage]);
    // 心跳从“发出请求”开始计，断线时也就从这一刻起测算失联时长。
    lastActivityRef.current = Date.now();
    setBusy(true);
    const socketPayload = threadId ? { type: 'chat' as const, message, thread_id: threadId } : { type: 'chat' as const, message };
    if (socketRef.current?.send(socketPayload)) return;

    const request = threadId ? { message, thread_id: threadId, client_id: clientId } : { message, client_id: clientId };
    void sendChatRest(api, request, (acceptedId) => {
      threadIdRef.current = acceptedId;
      setThreadId(acceptedId);
    }).catch((error: unknown) => {
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

  /** 单线程取消：优先走 WS，发送失败时回落 REST。取消只在执行循环的步骤边界生效。 */
  const requestCancel = useCallback((targetId: string) => {
    if (socketRef.current?.send({ type: 'cancel', thread_id: targetId })) return;
    void api.cancelThread(targetId).catch(() => undefined);
  }, [api]);

  /**
   * 结束所有正在运行的工作流：当前会话与后台/定时会话一视同仁。
   *
   * 后台/定时会话此前没有任何停止入口（任务看板只显示当前会话的任务），定时任务一旦
   * 跑起来就只能等它自己结束——这是「没办法自己结束工作流」的根因。
   */
  const endWorkflows = useCallback(() => {
    const targets = busy && threadId ? [threadId, ...runningBackgroundIds] : [...runningBackgroundIds];
    if (targets.length === 0) return;
    targets.forEach(requestCancel);
    setMessages((current) => [...current, {
      id: createId('system'),
      role: 'system',
      content: `已请求结束 ${targets.length} 个正在运行的工作流。后端会在下一个步骤边界停止；停在等待审批的会话会在此后恢复时停止。`,
      timestamp: new Date().toISOString(),
    }]);
    notify(`已请求结束 ${targets.length} 个正在运行的工作流。`, 'info');
  }, [busy, notify, requestCancel, runningBackgroundIds, threadId]);

  /** 清空当前对话视图，下一条消息开启新会话；后端记录与操作日志不受影响。 */
  const resetForeground = useCallback(() => {
    setThreadId(undefined);
    threadIdRef.current = undefined;
    setTasks([]);
    setBusy(false);
    assistantTurnRef.current = {};
    setMessages([{
      id: createId('system'),
      role: 'system',
      content: '已开始新会话。此前会话的文件操作记录仍在「操作日志」中；若要撤销那批操作，请在开启新会话前使用 /撤销。',
      timestamp: new Date().toISOString(),
    }]);
  }, []);

  /**
   * 新对话统一入口（按钮与 /新会话 共用）：任务运行时不静默清空，先让用户选择去向；
   * 空闲时直接清空。
   */
  const requestNewConversation = useCallback(() => {
    if (shouldPromptNewConversation(chatBusy)) {
      setNewChatModalOpen(true);
      return;
    }
    resetForeground();
  }, [chatBusy, resetForeground]);

  /**
   * 新对话决策：结束 / 转入后台 都会先把当前会话快照放进后台视图表，并把它记入
   * detached 集合，旧会话的迟到事件从此只进后台视图，绝不污染新会话。
   */
  const handleNewChatChoice = useCallback((action: NewConversationAction) => {
    if (action === 'cancel') {
      setNewChatModalOpen(false);
      return;
    }
    const currentThreadId = threadIdRef.current;
    const hasWork = chatMessages.length > 0 || chatTasks.length > 0 || chatBusy;
    if (action === 'end' && chatBusy && currentThreadId) requestCancel(currentThreadId);
    if (currentThreadId && hasWork) {
      setThreadViews((current) => detachThreadToViews(current, currentThreadId, { messages: chatMessages, tasks: chatTasks, busy: chatBusy }));
      detachedThreadIdsRef.current.add(currentThreadId);
    }
    setNewChatModalOpen(false);
    resetForeground();
    notify(action === 'end' ? '已结束当前任务并新建对话。' : '当前任务已转入后台，可在活动中心查看。', 'success');
  }, [chatBusy, chatMessages, chatTasks, notify, requestCancel, resetForeground]);

  const showHelp = useCallback(() => {
    setMessages((current) => [...current, {
      id: createId('system'),
      role: 'system',
      content: helpText(),
      timestamp: new Date().toISOString(),
    }]);
  }, []);

  const decideApproval = useCallback(async (decision: ApprovalDecision) => {
    const approval = approvalQueue[0];
    if (!approval) return;
    setApprovalSubmitting(true);
    const response = { thread_id: approval.thread_id, approval_id: approval.approval_id, decision };
    try {
      const sentBySocket = socketRef.current?.send({ type: 'approval', ...response }) ?? false;
      if (!sentBySocket) {
        await api.respondToApproval(response);
        // REST 没有事件回推，只能在 202 验证通过后本地移除；WS 路径等待 approval 节点确认。
        setApprovalQueue((current) => current.slice(1));
      }
      updateTask({ id: `approval-${approval.approval_id}`, title: approval.action, detail: approval.target, status: decision === 'approve' ? 'running' : 'failed', updatedAt: new Date().toISOString(), threadId: approval.thread_id });
      notify(decision === 'approve' ? '已批准该操作，任务继续执行。' : '已拒绝该操作，任务已停止此步骤。', 'success');
    } catch (error) {
      notify(error instanceof Error ? `审批提交失败：${error.message}` : '审批提交失败', 'error');
    } finally {
      setApprovalSubmitting(false);
    }
  }, [api, approvalQueue, notify, updateTask]);

  /**
   * 撤销指定会话（默认当前会话）已完成的文件操作（复用 /api/threads/{id}/rollback）。
   * 由聊天输入、任务结果卡与活动中心共用。
   */
  const rollbackThread = useCallback(async (targetId?: string) => {
    const id = targetId ?? threadIdRef.current;
    if (!id) {
      notify('当前没有可撤销的会话。', 'error');
      return;
    }
    try {
      const result = await api.rollbackThread(id);
      const feedback = rollbackFeedback(result);
      notify(feedback.message, feedback.kind);
      void loadSettingsData();
    } catch (error) {
      notify(error instanceof Error ? `撤销失败：${error.message}` : '撤销失败', 'error');
    }
  }, [api, loadSettingsData, notify]);

  /** 从后台视图切换到前台会话：取回该会话的消息/任务/忙碌态，并从后台表中移除。 */
  const switchToThread = useCallback((targetId: string) => {
    const view = threadViews[targetId];
    if (!view) {
      notify('找不到该会话。', 'error');
      return;
    }
    setMessages(view.messages);
    setTasks(view.tasks);
    setBusy(view.busy);
    setThreadId(targetId);
    threadIdRef.current = targetId;
    // 从已有消息恢复最大回合号，下一次追问必须创建新气泡而非覆盖历史回复。
    assistantTurnRef.current[targetId] = lastAssistantTurn(view.messages, targetId);
    detachedThreadIdsRef.current.delete(targetId);
    setThreadViews((current) => {
      const next = { ...current };
      delete next[targetId];
      return next;
    });
    notify('已切换到该会话。');
  }, [notify, threadViews]);

  const viewActivityResult = useCallback((item: ActivityItem) => {
    if (item.kind === 'approval') {
      notify('该审批已在弹窗中，可直接处理。');
      return;
    }
    if (item.kind === 'current' || !item.threadId) {
      notify('当前会话的结果已在上方显示。');
      return;
    }
    switchToThread(item.threadId);
  }, [notify, switchToThread]);

  const copyActivityResult = useCallback((item: ActivityItem) => {
    const value = item.resultPaths?.[0] ?? item.summary;
    if (!value) {
      notify('暂无可复制的结果。', 'error');
      return;
    }
    if (typeof navigator.clipboard?.writeText !== 'function') {
      notify('当前环境不支持自动复制，请手动选择。', 'error');
      return;
    }
    void navigator.clipboard.writeText(value).then(
      () => notify('已复制路径。', 'success'),
      () => notify('复制失败，请手动选择。', 'error'),
    );
  }, [notify]);

  /**
   * 斜杠指令分发。
   *
   * handlers 用 `Record<SlashCommandName, ...>` 声明：目录里新增指令却没在这里接上，
   * tsc 会直接报缺键，不必等运行时才发现点了没反应。
   */
  const runCommand = useCallback((name: string): void => {
    if (!isCommandName(name)) {
      setMessages((current) => [...current, {
        id: createId('system'),
        role: 'system',
        content: `未知指令 /${name}，输入 /帮助 查看可用指令。`,
        timestamp: new Date().toISOString(),
        error: true,
      }]);
      return;
    }
    const handlers: Record<SlashCommandName, () => void> = {
      '结束': endWorkflows,
      '新会话': requestNewConversation,
      '撤销': () => void rollbackThread(),
      '日志': () => openSettings('logs'),
      '任务': () => openSettings('schedules'),
      '模型': () => openSettings('model'),
      '设置': () => openSettings('preferences'),
      '帮助': showHelp,
    };
    handlers[name]();
  }, [endWorkflows, openSettings, requestNewConversation, rollbackThread, showHelp]);

  // 当前会话最近一次任务的结构化摘要：从最后一条带摘要的助手消息取。
  const currentSummary = useMemo<TaskSummary | undefined>(() => {
    for (let index = messages.length - 1; index >= 0; index -= 1) {
      const message = messages[index];
      if (message?.summary) return message.summary;
    }
    return undefined;
  }, [messages]);

  const currentForegroundView = useMemo<ThreadViewState>(() => {
    const base: ThreadViewState = { messages, tasks, busy };
    return currentSummary ? { ...base, summary: currentSummary } : base;
  }, [busy, currentSummary, messages, tasks]);

  /** 右侧活动中心条目：当前会话 + 后台/定时会话（逐一列出，不折叠成数字）+ 待审批任务。 */
  const activityItems = useMemo(
    () => buildActivityItems(threadViews, undefined, currentForegroundView, approvalQueue),
    [approvalQueue, currentForegroundView, threadViews],
  );

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
        {/* B3：后台/定时任务的审批会在这里常驻计数，避免“有审批在等却看不见”。 */}
        <div className={`safety-card${approvalQueue.length > 0 ? ' safety-card--alert' : ''}`}>
          <span aria-hidden="true">{approvalQueue.length > 0 ? '!' : '⌾'}</span>
          <div>
            <strong>{approvalQueue.length > 0 ? `${approvalQueue.length} 项待审批` : '安全模式已启用'}</strong>
            <p>{approvalQueue.length > 0 ? '高风险操作等待你的确认' : '高风险操作需审批'}</p>
          </div>
        </div>
        <button className="profile-button" type="button" onClick={() => openSettings('general')}>
          <span className="profile-avatar">本</span><span><strong>本地工作区</strong><small>{connectionState === 'connected' ? '服务正常' : '检查连接'}</small></span><b aria-hidden="true">•••</b>
        </button>
      </aside>

      <main className="workspace">
        <ChatPanel
          messages={chatMessages}
          connectionState={connectionState}
          busy={chatBusy}
          runningCount={runningCount}
          setupHints={setupHints}
          onSend={sendChat}
          onEnd={endWorkflows}
          onCommand={runCommand}
          onNewConversation={requestNewConversation}
          onOpenSettings={openSettings}
          onRetryConnect={reconnectSocket}
          onRetrySetup={() => void refreshSetupCheck()}
          onNotify={notify}
          onRollbackThread={(threadId) => void rollbackThread(threadId)}
        />
        <ActivityCenter
          items={activityItems}
          onSwitch={switchToThread}
          onStop={(threadId) => requestCancel(threadId)}
          onViewResult={viewActivityResult}
          onCopyResult={(item) => void copyActivityResult(item)}
        />
      </main>

      <ApprovalModal approval={approvalQueue[0] ?? null} queueSize={approvalQueue.length} submitting={approvalSubmitting} onDecision={(decision) => void decideApproval(decision)} />
      <NewConversationModal open={newChatModalOpen} onChoose={handleNewChatChoice} onCancel={() => setNewChatModalOpen(false)} />
      <ToastStack toasts={toasts} />
      <Settings
        open={settingsOpen}
        initialTab={settingsTab}
        apiBase={apiBase}
        wsBase={wsBase}
        llmSettings={llmSettings}
        sandboxSettings={sandboxSettings}
        workspaceSettings={workspaceSettings}
        toolSettings={toolSettings}
        preferences={preferences}
        jobs={jobs}
        logs={logs}
        loading={settingsLoading}
        error={settingsError}
        onClose={closeSettings}
        onSaveEndpoints={saveEndpoints}
        onSaveLLM={saveLLM}
        onSaveSandbox={saveSandbox}
        onSaveWorkspace={saveWorkspace}
        onSaveTools={saveTools}
        onRefreshTools={refreshTools}
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
