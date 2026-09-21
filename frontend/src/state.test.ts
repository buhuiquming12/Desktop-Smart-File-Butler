import { reconnectDelay } from './api/client';
import {
  SLASH_COMMANDS,
  commandQuery,
  exactCommand,
  helpText,
  isCommandName,
  matchCommands,
} from './commands';
import {
  BUSY_STALL_TIMEOUT_MS,
  addToApprovalQueue,
  approvalOrigin,
  buildActivityItems,
  detachThreadToViews,
  isBackgroundEvent,
  isNearBottom,
  isStalled,
  parseApproval,
  reduceThreadEvent,
  resolveEventTarget,
  shouldAutoScroll,
  shouldPromptNewConversation,
  shortThreadId,
  updateAssistant,
  type ThreadViewState,
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

// ---- B11：斜杠指令面板 ----

// 只是敲下 `/` 就该展开完整目录，因此过滤词为空串而不是 null。
if (commandQuery('/') !== '') throw new Error('B11: 单独的 / 未进入指令模式');
if (commandQuery('  /结束  ') !== '结束') throw new Error('B11: 指令词未 trim');
if (commandQuery('整理下载目录') !== null) throw new Error('B11: 普通消息被误判为指令');
if (commandQuery('/结束 顺便清一下下载目录') !== null) throw new Error('B11: 指令后带正文时不应拦截');
if (commandQuery('/新会话') !== '新会话') throw new Error('B11: 多字指令名解析错误');

// 空查询给全量目录：面板刚展开时不能是空的。
if (matchCommands('').length !== SLASH_COMMANDS.length) throw new Error('B11: 空查询未返回完整目录');
// 子串匹配：中文指令名没有词边界，按前缀匹配会让「会话」这类输入永远匹配不上。
if (!matchCommands('会话').some((command) => command.name === '新会话')) throw new Error('B11: 子串匹配失效');
if (matchCommands('').length === 0) throw new Error('B11: 指令目录为空');
if (matchCommands('这不可能匹配到任何指令').length !== 0) throw new Error('B11: 无匹配时应返回空列表');

// Enter 直接执行：只有输入与指令完全同名才算，半截输入交给高亮项。
if (exactCommand('/结束')?.name !== '结束') throw new Error('B11: 完整指令名未识别');
if (exactCommand('/结') !== null) throw new Error('B11: 半截输入不应直接执行');
if (exactCommand('结束') !== null) throw new Error('B11: 缺少前导斜杠不应识别为指令');
if (exactCommand('/结束 现在') !== null) throw new Error('B11: 带正文的输入不应识别为指令');

// 指令名收窄函数是 App 分发前的唯一门禁，必须挡住目录外的输入。
if (!isCommandName('结束')) throw new Error('B11: 合法指令被拒');
if (isCommandName('不存在的指令')) throw new Error('B11: 未知指令未被挡住');
if (isCommandName('')) throw new Error('B11: 空指令名未被挡住');

// /帮助 与面板同源：目录里每条指令都要能在帮助里找到，否则用户看的指令表是残的。
const help = helpText();
for (const command of SLASH_COMMANDS) {
  if (!help.includes(`/${command.name} `)) throw new Error(`B11: 帮助缺少指令 ${command.name}`);
}


// ---- 体验优化：新对话生命周期、后台隔离、迟到事件、任务切换、自动滚动 ----

// 新对话决策：只有任务在跑才需要弹窗。
if (!shouldPromptNewConversation(true)) throw new Error('running busy 未提示决策');
if (shouldPromptNewConversation(false)) throw new Error('空闲时不应提示决策');

// 「结束并新建 / 转入后台」都会先把当前会话快照放进后台视图表。
const detached = detachThreadToViews({}, 'old', { messages: [], tasks: [], busy: true });
if (!detached['old'] || !detached['old'].busy) throw new Error('detachThreadToViews 未快照为 busy');

// 新会话开始后 activeThreadId 为 undefined，旧会话迟到事件仍必须按后台路由，不污染新会话。
if (resolveEventTarget('old', undefined, new Set(['old'])) !== 'background') throw new Error('迟到事件未按后台路由');
if (resolveEventTarget('new', undefined, new Set(['old'])) !== 'foreground') throw new Error('新会话事件被误判为后台');
if (!isBackgroundEvent('old', undefined, new Set(['old']))) throw new Error('detached 集合未被 isBackgroundEvent 识别');

// 任务切换：切换到后台会话时，从 views 中取对应消息。
const threadViews: Record<string, ThreadViewState> = { 'bg-123': { messages: [{ id: 'm1', role: 'assistant', content: '后台结果', timestamp: '2026-01-01T00:00:00Z' }], tasks: [], busy: false } };
const items = buildActivityItems(threadViews, undefined, { messages: [], tasks: [], busy: false }, []);
if (!items.some((item) => item.threadId === 'bg-123' && item.kind === 'background')) throw new Error('活动中心未展示后台会话');
if (!items.some((item) => item.threadId === 'bg-123' && item.switchable)) throw new Error('后台会话未标记为可切换');
const switchedView = threadViews['bg-123'];
if (switchedView?.messages[0]?.content !== '后台结果') throw new Error('切换视图取回内容失败');

// 活动中心分类：定时任务 / 审批。
const scheduled: Record<string, ThreadViewState> = { 'scheduled-abc': { messages: [], tasks: [{ id: 't1', title: '整理', detail: '扫描', status: 'running', updatedAt: '2026-01-01T00:00:00Z', threadId: 'scheduled-abc' }], busy: true } };
const withApproval = [{ approval_id: 'ap-1', action: 'delete', target: 'a.txt', detail: '', created_at: '2026-01-01T00:00:00Z', thread_id: 'scheduled-abc' }];
const scheduledItems = buildActivityItems(scheduled, undefined, { messages: [], tasks: [], busy: false }, []);
if (!scheduledItems.some((item) => item.threadId === 'scheduled-abc' && item.kind === 'scheduled')) throw new Error('定时任务实例未进入活动中心');
if (!buildActivityItems({}, undefined, { messages: [], tasks: [], busy: false }, withApproval).some((item) => item.kind === 'approval' && item.approvalId === 'ap-1')) throw new Error('待审批任务未进入活动中心');

// 自动滚动判定：只在底部附近或本就在底部时才跟随。
if (isNearBottom(100, 500, 100)) throw new Error('距底过远误判为在底部');
if (!isNearBottom(400, 500, 100)) throw new Error('距底 0 未判定为在底部');
if (!shouldAutoScroll(true, 300, 500, 100)) throw new Error('此前在底部时不应停止跟随');
if (shouldAutoScroll(false, 100, 500, 100)) throw new Error('用户离开底部后不应自动滚动');

// 结构化摘要：done 事件携带 summary 时应写入会话视图，且挂在对应助手消息上。
const summaryEvent = event('s-1', 'done', {
  status: 'completed',
  message: '整理完成',
  summary: {
    ok: 3,
    failed: 1,
    skipped: 2,
    files: ['C:/a', 'C:/b'],
    operations: [
      { tool: 'move_file', tool_label: '移动文件', description: '移动 2 个文件', status: 'ok', status_label: '成功', path: 'C:/a', dest: 'C:/dst' },
    ],
  },
});
const summaryView = reduceThreadEvent({ messages: [{ id: 'assistant-s-1', role: 'assistant', content: '正在整理', timestamp: '2026-01-01T00:00:00Z', pending: true }], tasks: [], busy: true }, summaryEvent).view;
if (summaryView.summary?.ok !== 3 || summaryView.summary?.failed !== 1) throw new Error('done 摘要未写入视图');
if (summaryView.summary?.skipped !== 2) throw new Error('done 摘要 skipped 未解析');
if (summaryView.summary?.files?.[0] !== 'C:/a') throw new Error('done 摘要文件路径未解析');
if (summaryView.messages[0]?.summary?.operations?.[0]?.dest !== 'C:/dst') throw new Error('摘要未挂到助手消息');
if (summaryView.messages[0]?.threadId !== 's-1') throw new Error('摘要消息未记录来源会话');

// 缺 summary 的终态不产生摘要，纯文本降级路径不受影响。
const noSummaryView = reduceThreadEvent({ messages: [], tasks: [], busy: true }, event('s-2', 'done', { status: 'completed', message: '完成' })).view;
if (noSummaryView.summary !== undefined) throw new Error('无摘要时不应生成 summary 字段');
if (noSummaryView.messages[0]?.summary !== undefined) throw new Error('无摘要时消息不应带 summary');

// 非法形状的 summary 必须被拒，防止未经处理的 HTML/坏数据进入页面。
const badSummaryView = reduceThreadEvent({ messages: [], tasks: [], busy: true }, event('s-3', 'done', { status: 'completed', summary: { html: '<script>' } })).view;
if (badSummaryView.summary !== undefined) throw new Error('非法摘要形状未被拒绝');

// 活动中心展示结构化摘要的路径，供「复制结果 / 打开所在目录」使用。
const summaryItems = buildActivityItems({}, undefined, { messages: [{ id: 'm1', role: 'assistant', content: '完成', timestamp: '2026-01-01T00:00:00Z' }], tasks: [], busy: false, summary: { ok: 1, failed: 0, skipped: 0, files: ['C:/x.txt'], operations: [] } }, []);
if (summaryItems[0]?.resultPaths?.[0] !== 'C:/x.txt') throw new Error('当前会话 resultPaths 未透出摘要文件');
