import type { ActivityItem, ChatMessage, PendingApproval, TaskItem, TaskStatus, TaskSummary, TaskSummaryOperation, WSEvent } from './types';

export interface ThreadViewState {
  messages: ChatMessage[];
  tasks: TaskItem[];
  busy: boolean;
  /** 任务完成时的结构化摘要（done 事件字段；无则纯文本降级）。 */
  summary?: TaskSummary;
}

/** 前台回复气泡 id；回合号保证追问不会覆盖旧回答。 */
export function assistantMessageId(threadId: string, turn: number): string {
  return `assistant-${threadId}-${turn}`;
}

export function lastAssistantTurn(messages: ChatMessage[], threadId: string): number {
  const prefix = `assistant-${threadId}-`;
  return messages.reduce((maximum, message) => {
    if (!message.id.startsWith(prefix)) return maximum;
    const turn = Number(message.id.slice(prefix.length));
    return Number.isFinite(turn) ? Math.max(maximum, turn) : maximum;
  }, 0);
}

export function payloadText(payload: Record<string, unknown>, ...keys: string[]): string {
  for (const key of keys) {
    const value = payload[key];
    if (typeof value === 'string') return value;
  }
  return '';
}

export function taskStatus(value: unknown): TaskStatus {
  return value === 'pending' || value === 'running' || value === 'success' || value === 'failed' || value === 'waiting'
    ? value
    : 'running';
}

export function parseApproval(event: WSEvent): PendingApproval | null {
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

/**
 * 从终态事件 payload 中解析结构化任务摘要；缺字段或形状不对时返回 null（纯文本降级）。
 *
 * 只收可信的标量/数组字段，其余一律丢弃，避免把未经处理的 HTML 注入页面。
 */
export function parseSummary(value: unknown): TaskSummary | null {
  if (!value || typeof value !== 'object') return null;
  const candidate = value as Partial<TaskSummary>;
  if (typeof candidate.ok !== 'number' && typeof candidate.failed !== 'number' && typeof candidate.skipped !== 'number') return null;
  return {
    ok: typeof candidate.ok === 'number' ? candidate.ok : 0,
    failed: typeof candidate.failed === 'number' ? candidate.failed : 0,
    skipped: typeof candidate.skipped === 'number' ? candidate.skipped : 0,
    files: Array.isArray(candidate.files) ? candidate.files.filter((file): file is string => typeof file === 'string') : [],
    operations: Array.isArray(candidate.operations)
      ? candidate.operations.filter((op): op is TaskSummaryOperation => Boolean(op) && typeof op === 'object')
      : [],
  };
}

export function updateAssistant(messages: ChatMessage[], threadId: string, content: string, append: boolean, error = false): ChatMessage[] {
  const id = `assistant-${threadId}`;
  const index = messages.findIndex((message) => message.id === id);
  if (index < 0) {
    return [...messages, { id, role: 'assistant', content, timestamp: new Date().toISOString(), pending: !error, error }];
  }
  const next = [...messages];
  const previous = next[index];
  if (!previous) return messages;
  next[index] = { ...previous, content: append ? `${previous.content}${content}` : content, pending: !error, error };
  return next;
}

export function updateTask(tasks: TaskItem[], task: TaskItem): TaskItem[] {
  const index = tasks.findIndex((item) => item.id === task.id);
  if (index < 0) return [task, ...tasks];
  const next = [...tasks];
  next[index] = { ...next[index], ...task };
  return next;
}

/** B2 兜底超时：WS 断线后事件无人送达，busy 会永久卡住。 */
export const BUSY_STALL_TIMEOUT_MS = 60_000;

/** 看门狗轮询间隔。 */
export const BUSY_STALL_TICK_MS = 5_000;

/** 距最后一次收到后端事件超过阈值即判定为失联。 */
export function isStalled(lastActivityAt: number, now: number, timeoutMs = BUSY_STALL_TIMEOUT_MS): boolean {
  return now - lastActivityAt >= timeoutMs;
}

export const STALLED_NOTICE =
  `已超过 ${BUSY_STALL_TIMEOUT_MS / 1000} 秒未收到后端任何响应，已停止等待。` +
  '任务可能仍在后台执行：可在「操作日志」中核对已完成的步骤，必要时撤销。';

// ---- B3：审批队列 ----

/** 审批入队，按 approval_id 去重（同一项可能因重连或重复推送被多次送达）。 */
export function addToApprovalQueue(queue: PendingApproval[], approval: PendingApproval): PendingApproval[] {
  if (queue.some((item) => item.approval_id === approval.approval_id)) return queue;
  return [...queue, approval];
}

/**
 * 判断事件是否属于后台会话。
 *
 * 后台/定时会话的进度事件不应覆盖当前会话视图，但它们的审批必须照常入队（B3）：
 * 早先直接 return 把 reduceThreadEvent 给出的 approval 丢了，导致定时任务一旦
 * 需要危险操作审批就永远停在 waiting_approval，且界面上没有任何恢复入口。
 */
export function isBackgroundEvent(
  eventThreadId: string,
  activeThreadId: string | undefined,
  detachedThreadIds: ReadonlySet<string> = new Set(),
): boolean {
  if (!eventThreadId) return false;
  // 定时任务永远属于活动中心，即使当前没有聊天会话也不能劫持前台。
  if (eventThreadId.startsWith('scheduled-')) return true;
  // 已转入后台（或已结束但仍有迟到事件）的会话，永远按后台路由，绝不污染新会话。
  if (detachedThreadIds.has(eventThreadId)) return true;
  return Boolean(activeThreadId && eventThreadId !== activeThreadId);
}

/** 审批来源标签。定时任务会话 id 形如 scheduled-xxxx（见后端 _scheduled_runner）。 */
export function approvalOrigin(threadId: string): string {
  return threadId.startsWith('scheduled-') ? '定时任务' : '对话会话';
}

/**
 * 会话 id 较长时取尾部 8 位展示。
 *
 * 取尾部而非头部：定时任务 id 一律以 scheduled- 开头，取头部会全部显示成
 * "schedule…" 而无法区分是哪一次任务。是否定时任务由 approvalOrigin 单独标注。
 */
export function shortThreadId(threadId: string): string {
  return threadId.length > 8 ? `…${threadId.slice(-8)}` : threadId;
}

export function reduceThreadEvent(view: ThreadViewState, event: WSEvent, now = new Date().toISOString()): { view: ThreadViewState; approval?: PendingApproval } {
  let next: ThreadViewState = { ...view, messages: [...view.messages], tasks: [...view.tasks] };
  switch (event.type) {
    case 'token':
      next.messages = updateAssistant(next.messages, event.thread_id, payloadText(event.payload, 'token', 'content', 'text'), true);
      break;
    case 'node': {
      const node = payloadText(event.payload, 'node', 'name') || '处理中';
      if (!event.thread_id && node === 'connected') break;
      if (node === 'approval') {
        next.tasks = next.tasks.filter((task) => !(task.threadId === event.thread_id && task.status === 'waiting'));
      }
      next.tasks = updateTask(next.tasks, { id: `${event.thread_id}-node`, title: 'Agent 工作流', detail: `正在执行：${node}`, status: 'running', updatedAt: now, threadId: event.thread_id });
      break;
    }
    case 'tool_call': {
      const name = payloadText(event.payload, 'tool', 'name', 'action') || '文件工具';
      next.tasks = updateTask(next.tasks, { id: payloadText(event.payload, 'task_id', 'call_id') || `${event.thread_id}-${name}`, title: name, detail: payloadText(event.payload, 'detail', 'target', 'message') || '正在调用工具', status: 'running', updatedAt: now, threadId: event.thread_id });
      break;
    }
    case 'tool_result': {
      const name = payloadText(event.payload, 'tool', 'name', 'action') || '工具执行';
      next.tasks = updateTask(next.tasks, { id: payloadText(event.payload, 'task_id', 'call_id') || `${event.thread_id}-${name}`, title: name, detail: payloadText(event.payload, 'detail', 'result', 'message') || '执行完成', status: event.payload.success === false ? 'failed' : 'success', updatedAt: now, threadId: event.thread_id });
      break;
    }
    case 'task': {
      const id = payloadText(event.payload, 'task_id', 'id') || `${event.thread_id}-task`;
      next.tasks = updateTask(next.tasks, { id, title: payloadText(event.payload, 'title', 'name') || '文件任务', detail: payloadText(event.payload, 'detail', 'message', 'description'), status: taskStatus(event.payload.status), updatedAt: now, threadId: event.thread_id });
      break;
    }
    case 'approval_required': {
      const approval = parseApproval(event);
      if (approval) {
        next.tasks = updateTask(next.tasks, { id: `approval-${approval.approval_id}`, title: approval.action, detail: approval.target, status: 'waiting', updatedAt: now, threadId: event.thread_id });
        return { view: next, approval };
      }
      break;
    }
    case 'done': {
      const failed = event.payload.status === 'failed' || event.payload.status === 'cancelled';
      const text = payloadText(event.payload, 'message', 'error', 'detail', 'content', 'result');
      if (text) next.messages = updateAssistant(next.messages, event.thread_id, text, false, failed);
      next.messages = next.messages.map((message) => message.id === `assistant-${event.thread_id}` ? { ...message, pending: false } : message);
      next.tasks = next.tasks
        .filter((task) => !(task.threadId === event.thread_id && task.status === 'waiting'))
        .map((task) => task.threadId === event.thread_id && task.status === 'running' ? { ...task, status: failed ? 'failed' : 'success', updatedAt: now } : task);
      next.busy = false;
      const summary = parseSummary(event.payload.summary);
      if (summary) {
        next.summary = summary;
        next.messages = next.messages.map((message) =>
          message.id === `assistant-${event.thread_id}` ? { ...message, summary, threadId: event.thread_id } : message,
        );
      }
      break;
    }
    case 'error': {
      // error 是非终态诊断（例如审批已过期）；保留 busy 与审批入口，等待用户重试。
      const text = payloadText(event.payload, 'message', 'error', 'detail') || '请求未被接受';
      next.messages.push({
        id: `diagnostic-${event.thread_id}-${now}`,
        role: 'system',
        content: text,
        timestamp: now,
        error: true,
      });
      break;
    }
  }
  return { view: next };
}


// ---------------- 新对话生命周期（体验优化） ----------------

/** 新对话的三个去向：新建对话前的用户决策。 */
export type NewConversationAction = 'end' | 'background' | 'cancel';

/**
 * 是否需要弹出决策弹窗：仅当当前会话确实有任务在跑时才需要问用户
 * （否则直接清空即可，避免打扰）。
 */
export function shouldPromptNewConversation(busy: boolean): boolean {
  return busy;
}

/** 把某个会话快照放入后台视图表（供「结束并新建 / 转入后台」复用）。 */
export function detachThreadToViews(
  views: Record<string, ThreadViewState>,
  threadId: string,
  view: ThreadViewState,
): Record<string, ThreadViewState> {
  return { ...views, [threadId]: view };
}

/**
 * 任务/事件路由判定：事件应落到前台还是后台视图。
 * detached 集合里的（含已结束会话的迟到事件）一律视为后台。
 */
export function resolveEventTarget(
  eventThreadId: string,
  activeThreadId: string | undefined,
  detachedThreadIds: ReadonlySet<string>,
): 'foreground' | 'background' {
  return isBackgroundEvent(eventThreadId, activeThreadId, detachedThreadIds) ? 'background' : 'foreground';
}

// ---------------- 活动中心（体验优化） ----------------

/** 取一个会话视图的最新状态、时间与摘要。 */
export function describeThreadView(
  view: ThreadViewState,
  now = new Date().toISOString(),
): { status: TaskStatus; statusLabel: string; updatedAt: string; summary: string } {
  const latest = view.tasks[0];
  let status: TaskStatus = 'pending';
  let updatedAt = now;
  let summary = '';
  if (view.busy) {
    status = 'running';
  } else if (latest) {
    status = latest.status;
    updatedAt = latest.updatedAt || now;
    summary = latest.detail || latest.title || '';
  }
  if (!summary) {
    const lastMessage = view.messages[view.messages.length - 1];
    if (lastMessage) {
      summary = (lastMessage.content || '').slice(0, 120);
      if (lastMessage.timestamp) updatedAt = lastMessage.timestamp;
    }
  }
  if (view.tasks.some((task) => task.status === 'waiting')) {
    status = 'waiting';
  }
  return {
    status,
    statusLabel: statusText(status),
    updatedAt,
    summary: summary || (status === 'running' ? '任务正在执行' : '暂无动态'),
  };
}

const statusTextMap: Record<TaskStatus, string> = {
  pending: '等待',
  running: '执行中',
  success: '完成',
  failed: '失败',
  waiting: '待审批',
};

export function statusText(status: TaskStatus): string {
  return statusTextMap[status] ?? '未知';
}

/** 组装右侧活动中心条目：当前会话 + 后台/定时会话 + 待审批任务。 */
export function buildActivityItems(
  views: Record<string, ThreadViewState>,
  activeThreadId: string | undefined,
  currentView: ThreadViewState,
  approvalQueue: PendingApproval[],
  now = new Date().toISOString(),
): ActivityItem[] {
  const items: ActivityItem[] = [];

  // 当前会话：有内容或正在运行时才展示。
  const hasCurrent = currentView.messages.length > 0 || currentView.tasks.length > 0 || currentView.busy;
  if (hasCurrent) {
    const described = describeThreadView(currentView, now);
    items.push({
      id: 'current',
      kind: 'current',
      source: '当前会话',
      status: described.status,
      statusLabel: described.statusLabel,
      updatedAt: described.updatedAt,
      summary: described.summary,
      threadId: activeThreadId ?? '',
      resultPaths: currentView.summary?.files ?? [],
      running: currentView.busy,
      active: true,
      hasDetail: currentView.tasks.length > 0 || currentView.messages.length > 0,
      switchable: false,
    });
  }

  // 后台/定时会话：按插入顺序稳定展示，不折叠成数字。
  for (const [threadId, view] of Object.entries(views)) {
    const described = describeThreadView(view, now);
    const scheduled = threadId.startsWith('scheduled-');
    items.push({
      id: `thread-${threadId}`,
      kind: scheduled ? 'scheduled' : 'background',
      source: scheduled ? '定时任务' : '后台会话',
      status: described.status,
      statusLabel: described.statusLabel,
      updatedAt: described.updatedAt,
      summary: described.summary,
      threadId,
      resultPaths: view.summary?.files ?? [],
      running: view.busy || view.tasks.some((task) => task.status === 'running' || task.status === 'waiting'),
      active: activeThreadId === threadId,
      hasDetail: view.tasks.length > 0 || view.messages.length > 0,
      switchable: activeThreadId !== threadId && (view.tasks.length > 0 || view.messages.length > 0),
    });
  }

  // 待审批任务：审批入口由 ApprovalModal 承担，这里展示常驻卡片。
  for (const approval of approvalQueue) {
    items.push({
      id: `approval-${approval.approval_id}`,
      kind: 'approval',
      source: approvalOrigin(approval.thread_id),
      status: 'waiting',
      statusLabel: '待审批',
      updatedAt: approval.created_at || now,
      summary: `${approval.action} → ${approval.target}`,
      threadId: approval.thread_id,
      approvalId: approval.approval_id,
      running: false,
      active: false,
      hasDetail: Boolean(approval.detail),
      switchable: false,
    });
  }

  return items;
}

// ---------------- 自动滚动判定（体验优化） ----------------

/** 默认判定阈值：距底部 120px 内视为“位于底部附近”。 */
export const AUTO_SCROLL_THRESHOLD_PX = 120;

/** 是否位于滚动容器底部附近：是才自动跟随最新内容。 */
export function isNearBottom(scrollTop: number, scrollHeight: number, clientHeight: number, threshold = AUTO_SCROLL_THRESHOLD_PX): boolean {
  return scrollHeight - scrollTop - clientHeight < threshold;
}

/** 制作消息后应否自动滚动：仅当用户本就位于底部（或容器尚无高度）。 */
export function shouldAutoScroll(
  previousNearBottom: boolean,
  scrollTop: number,
  scrollHeight: number,
  clientHeight: number,
  threshold = AUTO_SCROLL_THRESHOLD_PX,
): boolean {
  return previousNearBottom || isNearBottom(scrollTop, scrollHeight, clientHeight, threshold) || scrollHeight <= clientHeight;
}
