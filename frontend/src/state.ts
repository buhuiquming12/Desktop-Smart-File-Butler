import type { ChatMessage, PendingApproval, TaskItem, TaskStatus, WSEvent } from './types';

export interface ThreadViewState {
  messages: ChatMessage[];
  tasks: TaskItem[];
  busy: boolean;
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

export function reduceThreadEvent(view: ThreadViewState, event: WSEvent, now = new Date().toISOString()): { view: ThreadViewState; approval?: PendingApproval } {
  let next: ThreadViewState = { ...view, messages: [...view.messages], tasks: [...view.tasks] };
  switch (event.type) {
    case 'token':
      next.messages = updateAssistant(next.messages, event.thread_id, payloadText(event.payload, 'token', 'content', 'text'), true);
      break;
    case 'node': {
      const node = payloadText(event.payload, 'node', 'name') || '处理中';
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
    case 'done':
    case 'error': {
      // B1：后端终态统一为 done(status=failed|completed)，error 类型仅用于非终态诊断。
      const failed = event.type === 'error' || event.payload.status === 'failed' || event.payload.status === 'cancelled';
      const text = payloadText(event.payload, 'message', 'error', 'detail', 'content', 'result');
      if (text) next.messages = updateAssistant(next.messages, event.thread_id, text, false, failed);
      next.messages = next.messages.map((message) => message.id === `assistant-${event.thread_id}` ? { ...message, pending: false } : message);
      next.tasks = next.tasks.map((task) => task.threadId === event.thread_id && task.status === 'running' ? { ...task, status: failed ? 'failed' : 'success', updatedAt: now } : task);
      next.busy = false;
      break;
    }
  }
  return { view: next };
}
