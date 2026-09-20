import { reconnectDelay } from './api/client';
import {
  BUSY_STALL_TIMEOUT_MS,
  addToApprovalQueue,
  approvalOrigin,
  isBackgroundEvent,
  isStalled,
  parseApproval,
  reduceThreadEvent,
  shortThreadId,
  updateAssistant,
} from './state';
import type { PendingApproval, WSEvent } from './types';

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

// ---- B3：后台/定时任务审批不得丢弃 ----

const scheduledEvent = event('scheduled-abc12345', 'approval_required', { approval_id: 'ap-1', action: 'delete', target: 'a.txt' });
const backgroundResult = reduceThreadEvent({ messages: [], tasks: [], busy: true }, scheduledEvent);
if (!backgroundResult.approval) throw new Error('B3: 后台审批事件未产出 approval');
if (backgroundResult.approval.approval_id !== 'ap-1') throw new Error('B3: approval_id 解析错误');
if (backgroundResult.approval.thread_id !== 'scheduled-abc12345') throw new Error('B3: 审批未带上来源会话');

// 后台判定：仅当前会话之外、且确实有 thread_id 的事件算后台。
if (!isBackgroundEvent('scheduled-abc12345', 'chat-1')) throw new Error('B3: 未识别出后台事件');
if (isBackgroundEvent('chat-1', 'chat-1')) throw new Error('B3: 当前会话事件被误判为后台');
if (isBackgroundEvent('chat-1', undefined)) throw new Error('B3: 无活动会话时不应判为后台');
if (isBackgroundEvent('', 'chat-1')) throw new Error('B3: 空 thread_id 不应判为后台');

const approvalOf = (id: string): PendingApproval => ({
  approval_id: id,
  thread_id: 'scheduled-abc12345',
  action: 'delete',
  target: 'a.txt',
  detail: '',
  created_at: '2026-01-01T00:00:00Z',
});

// 入队去重：同一条审批重复送达（重连重放）不得在队列里堆积。
const queued = addToApprovalQueue(addToApprovalQueue([], approvalOf('ap-1')), approvalOf('ap-1'));
if (queued.length !== 1) throw new Error(`B3: 审批重复入队，长度为 ${queued.length}`);
if (addToApprovalQueue(queued, approvalOf('ap-2')).length !== 2) throw new Error('B3: 不同审批未入队');

// 来源标签：定时任务会话 id 形如 scheduled-xxxx（见后端 _scheduled_runner）。
if (approvalOrigin('scheduled-abc12345') !== '定时任务') throw new Error('B3: 定时任务来源标注错误');
if (approvalOrigin('chat-1') !== '对话会话') throw new Error('B3: 对话会话来源标注错误');
// 取尾部：定时任务 id 共享 scheduled- 前缀，取头部会全部撞成同一串。
if (shortThreadId('scheduled-abc12345') !== '…abc12345') throw new Error('B3: 会话 id 截断错误');
if (shortThreadId('scheduled-ffffffff') === shortThreadId('scheduled-abc12345')) {
  throw new Error('B3: 不同定时任务会话无法区分');
}
if (shortThreadId('short') !== 'short') throw new Error('B3: 短会话 id 不应截断');
