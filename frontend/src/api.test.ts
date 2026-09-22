import { ApiClient, reconciliationEvents, rollbackFeedback, sendChatRest } from './api/client.js';

async function main(): Promise<void> {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = (async () => new Response(JSON.stringify({ thread_id: 'rest-created', status: 'accepted' }), {
    status: 202, headers: { 'Content-Type': 'application/json' },
  })) as typeof fetch;
  try {
    let saved = '';
    await sendChatRest(new ApiClient('http://127.0.0.1:8000'), { message: 'hello' }, (id) => { saved = id; });
    if (saved !== 'rest-created') throw new Error('REST fallback 未保存新 thread_id');
  } finally {
    globalThis.fetch = originalFetch;
  }

  const events = reconciliationEvents('thread-1', {
    status: 'completed', final_response: 'done',
    observations: [{ tool: 'move_file', status: 'ok', description: 'moved' }],
    summary: { ok: 1, failed: 0, skipped: 0, files: [], operations: [] },
  });
  if (!events.some((event) => event.type === 'tool_result')) throw new Error('重连未恢复 observations');
  if (!events.some((event) => event.type === 'done')) throw new Error('重连未恢复终态');

  const rollback = rollbackFeedback({ ok: 7, skipped: 2, failed: 1, details: [] });
  if (rollback.kind !== 'error' || !rollback.message.includes('失败 1')) throw new Error('部分回滚失败被显示为全成功');
}

void main().catch((error) => { globalThis.setTimeout(() => { throw error; }, 0); });
