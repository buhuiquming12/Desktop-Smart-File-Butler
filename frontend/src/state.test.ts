import { reconnectDelay } from './api/client';
import {
  BUSY_STALL_TIMEOUT_MS,
  isStalled,
  parseApproval,
  reduceThreadEvent,
  updateAssistant,
} from './state';
import type { WSEvent } from './types';

const event = (thread_id: string, type: WSEvent['type'], payload: Record<string, unknown>): WSEvent => ({ thread_id, type, payload });

const first = reduceThreadEvent({ messages: [], tasks: [], busy: true }, event('a', 'task', { task_id: 'same', title: 'A', status: 'running' })).view;
const second = reduceThreadEvent({ messages: [], tasks: [], busy: true }, event('b', 'task', { task_id: 'same', title: 'B', status: 'success' })).view;
if (first.tasks[0]?.title !== 'A' || second.tasks[0]?.title !== 'B') throw new Error('thread state leaked');
if (parseApproval(event('a', 'approval_required', { approval_id: 'x', action: 'delete', target: 'a.txt' }))?.thread_id !== 'a') throw new Error('approval thread missing');

// ---- B1：终态统一为 done(status=failed)，前端必须按失败呈现 ----

const failedView = reduceThreadEvent(
  { messages: [], tasks: [{ id: 't1', title: '移动', detail: '', status: 'running', updatedAt: '', threadId: 'x' }], busy: true },
  event('x', 'done', { status: 'failed', message: '无法启动会话：未配置 API Key' }),
).view;
if (failedView.busy) throw new Error('B1: done(status=failed) 未能解除 busy');
if (failedView.tasks[0]?.status !== 'failed') throw new Error('B1: 失败终态未把任务标记为 failed');
if (failedView.messages[0]?.error !== true) throw new Error('B1: 失败终态未把消息标记为错误');

const completedView = reduceThreadEvent(
  { messages: [], tasks: [], busy: true },
  event('y', 'done', { status: 'completed', message: '整理完成' }),
).view;
if (completedView.busy) throw new Error('done 未解除 busy');
if (completedView.messages[0]?.error === true) throw new Error('成功终态不应标记为错误');

// ---- B2：断线重连退避与 busy 兜底超时 ----

// 退避必须有上界，否则长时间离线会把重连间隔推到无意义的量级。
for (let attempt = 0; attempt < 40; attempt += 1) {
  const delay = reconnectDelay(attempt);
  if (!Number.isFinite(delay) || delay < 0 || delay > 15_000) throw new Error(`reconnectDelay 越界: ${delay}`);
}
// 确实在退避：早期尝试的间隔远小于后期尝试。
if (!(reconnectDelay(0) < reconnectDelay(8))) throw new Error('reconnectDelay 未退避');

// 看门狗：阈值前不算失联，达到阈值才算，避免误伤流式长任务。
if (isStalled(1_000, 1_000 + BUSY_STALL_TIMEOUT_MS - 1)) throw new Error('看门狗过早判定失联');
if (!isStalled(1_000, 1_000 + BUSY_STALL_TIMEOUT_MS)) throw new Error('看门狗未检出失联');

// ---- 辅助函数回归 ----

if (updateAssistant([], 'z', 'hi', true)[0]?.content !== 'hi') throw new Error('updateAssistant 未新建消息');
