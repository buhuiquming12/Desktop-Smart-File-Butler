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

  await checkExternalToolEndpoints();
}

/** OCR / 默认管理目录接口：路径、动词与请求体都不能悄悄跑偏。 */
async function checkExternalToolEndpoints(): Promise<void> {
  const originalFetch = globalThis.fetch;
  const calls: Array<{ path: string; method: string; body: string }> = [];
  const ocr = {
    status: 'available', available: true, version: '5.3.0', languages: ['eng', 'chi_sim'],
    missing_languages: [], tesseract_cmd: '', tessdata_dir: '', message: '',
  };
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input));
    calls.push({
      path: url.pathname,
      method: init?.method ?? 'GET',
      body: typeof init?.body === 'string' ? init.body : '',
    });
    const payload = url.pathname.endsWith('/settings/tools')
      ? { tesseract_cmd: 'D:\\Tesseract-OCR\\tesseract.exe', tessdata_dir: '', sources: {}, ocr }
      : {
          default_managed_root: 'D:\\Downloads', stored_default_managed_root: 'D:\\Downloads',
          valid: true, message: '', allowed_roots: ['D:\\'],
        };
    return new Response(JSON.stringify(payload), {
      status: 200, headers: { 'Content-Type': 'application/json' },
    });
  }) as typeof fetch;

  try {
    const api = new ApiClient('http://127.0.0.1:8000');
    const tools = await api.getToolSettings();
    if (tools.ocr.status !== 'available') throw new Error('OCR 能力检测结果未透传');
    await api.updateToolSettings({ tesseract_cmd: '', tessdata_dir: 'D:\\Tess' });
    const workspace = await api.getWorkspaceSettings();
    if (!workspace.valid || workspace.allowed_roots[0] !== 'D:\\') {
      throw new Error('默认管理目录状态未透传');
    }
    await api.updateWorkspaceSettings({ default_managed_root: '' });
  } finally {
    globalThis.fetch = originalFetch;
  }

  const observed = calls.map((call) => `${call.method} ${call.path}`);
  const expected = [
    'GET /api/settings/tools',
    'PUT /api/settings/tools',
    'GET /api/settings/workspace',
    'PUT /api/settings/workspace',
  ];
  if (observed.join(' | ') !== expected.join(' | ')) {
    throw new Error(`外部工具接口调用序列不符：${observed.join(' | ')}`);
  }
  const toolBody = JSON.parse(calls[1]?.body ?? '{}') as Record<string, unknown>;
  // 空字符串是“清除覆盖、回退 .env”，必须原样发出，不能被前端过滤掉
  if (toolBody.tesseract_cmd !== '' || toolBody.tessdata_dir !== 'D:\\Tess') {
    throw new Error(`工具配置请求体不符：${calls[1]?.body ?? ''}`);
  }
  const workspaceBody = JSON.parse(calls[3]?.body ?? '{}') as Record<string, unknown>;
  if (workspaceBody.default_managed_root !== '') {
    throw new Error(`默认管理目录请求体不符：${calls[3]?.body ?? ''}`);
  }
}

void main().catch((error) => { globalThis.setTimeout(() => { throw error; }, 0); });
