import { useEffect, useState, type FormEvent } from 'react';
import type {
  LLMModelsRequest,
  LLMModelsResponse,
  LLMProvider,
  LLMSettings,
  LLMSettingsUpdate,
  OperationLog,
  Preference,
  SandboxSettings,
  ScheduledJob,
} from '../types';

export type SettingsTab = 'general' | 'model' | 'preferences' | 'schedules' | 'logs';

interface SettingsProps {
  open: boolean;
  initialTab: SettingsTab;
  apiBase: string;
  wsBase: string;
  llmSettings: LLMSettings | null;
  sandboxSettings: SandboxSettings | null;
  preferences: Preference[];
  jobs: ScheduledJob[];
  logs: OperationLog[];
  loading: boolean;
  error: string | null;
  onClose: () => void;
  onSaveEndpoints: (apiBase: string, wsBase: string) => void;
  onSaveLLM: (update: LLMSettingsUpdate) => Promise<void>;
  onSaveSandbox: (roots: string[]) => Promise<void>;
  onFetchModels: (request: LLMModelsRequest) => Promise<LLMModelsResponse>;
  onSavePreference: (preference: Preference) => Promise<void>;
  onCreateJob: (job: Omit<ScheduledJob, 'job_id'>) => Promise<void>;
  onDeleteJob: (jobId: string) => Promise<void>;
  onRollbackOperation: (opId: number) => Promise<void>;
  onRefresh: () => void;
}

/** move/rename/delete 且成功的操作可撤销（见 P1-1）。 */
function isReversible(log: OperationLog): boolean {
  return (log.action === 'move' || log.action === 'rename' || log.action === 'delete') && log.status === 'ok';
}

const tabs: Array<{ id: SettingsTab; label: string }> = [
  { id: 'general', label: '常规' },
  { id: 'model', label: '模型' },
  { id: 'preferences', label: '偏好' },
  { id: 'schedules', label: '定时任务' },
  { id: 'logs', label: '操作日志' },
];

export function Settings(props: SettingsProps) {
  const [tab, setTab] = useState<SettingsTab>(props.initialTab);
  const [apiBase, setApiBase] = useState(props.apiBase);
  const [wsBase, setWsBase] = useState(props.wsBase);
  const [preferenceKey, setPreferenceKey] = useState('default_directory');
  const [preferenceValue, setPreferenceValue] = useState('');
  const [job, setJob] = useState({ directory: '', instruction: '', cron: '0 9 * * *' });
  const [saving, setSaving] = useState(false);

  // 模型配置本地状态
  const [provider, setProvider] = useState<LLMProvider>('openai');
  const [openaiBaseUrl, setOpenaiBaseUrl] = useState('');
  const [openaiModel, setOpenaiModel] = useState('');
  const [openaiApiKey, setOpenaiApiKey] = useState('');
  const [ollamaBaseUrl, setOllamaBaseUrl] = useState('');
  const [ollamaModel, setOllamaModel] = useState('');
  const [availableModels, setAvailableModels] = useState<string[]>([]);
  const [fetchingModels, setFetchingModels] = useState(false);
  const [modelStatus, setModelStatus] = useState<string | null>(null);
  const [savingLLM, setSavingLLM] = useState(false);
  const [rollingBack, setRollingBack] = useState<number | null>(null);

  // 沙箱根目录编辑态（权限变更，见 P1-3）
  const [sandboxRoots, setSandboxRoots] = useState<string[]>([]);
  const [savingSandbox, setSavingSandbox] = useState(false);
  const [manualRoot, setManualRoot] = useState('');

  const addRoot = (dir: string): void => {
    const value = dir.trim();
    if (!value) return;
    setSandboxRoots((current) => (current.includes(value) ? current : [...current, value]));
  };

  const pickDirectory = async (): Promise<void> => {
    const chosen = await window.desktop?.chooseDirectory();
    if (chosen) addRoot(chosen);
  };

  const saveSandbox = async (): Promise<void> => {
    setSavingSandbox(true);
    try {
      await props.onSaveSandbox(sandboxRoots);
    } finally {
      setSavingSandbox(false);
    }
  };

  const rollback = async (opId: number): Promise<void> => {
    setRollingBack(opId);
    try {
      await props.onRollbackOperation(opId);
    } finally {
      setRollingBack(null);
    }
  };

  useEffect(() => setTab(props.initialTab), [props.initialTab, props.open]);
  useEffect(() => {
    setApiBase(props.apiBase);
    setWsBase(props.wsBase);
  }, [props.apiBase, props.wsBase, props.open]);
  useEffect(() => {
    const s = props.llmSettings;
    if (!s) return;
    setProvider(s.provider);
    setOpenaiBaseUrl(s.openai_base_url);
    setOpenaiModel(s.openai_model);
    setOpenaiApiKey('');
    setOllamaBaseUrl(s.ollama_base_url);
    setOllamaModel(s.ollama_model);
    setAvailableModels([]);
    setModelStatus(null);
  }, [props.llmSettings, props.open]);
  useEffect(() => {
    if (props.sandboxSettings) setSandboxRoots(props.sandboxSettings.roots);
  }, [props.sandboxSettings, props.open]);

  if (!props.open) return null;

  const apiKeySet = props.llmSettings?.openai_api_key_set ?? false;

  const fetchModels = async (): Promise<void> => {
    setFetchingModels(true);
    setModelStatus(null);
    try {
      const request: LLMModelsRequest =
        provider === 'ollama'
          ? { provider, ...(ollamaBaseUrl.trim() ? { base_url: ollamaBaseUrl.trim() } : {}) }
          : {
              provider,
              ...(openaiBaseUrl.trim() ? { base_url: openaiBaseUrl.trim() } : {}),
              ...(openaiApiKey.trim() ? { api_key: openaiApiKey.trim() } : {}),
            };
      const result = await props.onFetchModels(request);
      setAvailableModels(result.models);
      setModelStatus(result.models.length ? `找到 ${result.models.length} 个模型` : '未返回任何模型');
    } catch (error) {
      setModelStatus(error instanceof Error ? error.message : '获取模型失败');
    } finally {
      setFetchingModels(false);
    }
  };

  const saveLLM = async (): Promise<void> => {
    setSavingLLM(true);
    setModelStatus(null);
    try {
      const update: LLMSettingsUpdate =
        provider === 'ollama'
          ? { provider, ollama_base_url: ollamaBaseUrl.trim(), ollama_model: ollamaModel.trim() }
          : {
              provider,
              openai_base_url: openaiBaseUrl.trim(),
              openai_model: openaiModel.trim(),
              // 仅在用户输入了新密钥时提交；留空表示保留已有密钥。
              ...(openaiApiKey.trim() ? { openai_api_key: openaiApiKey.trim() } : {}),
            };
      await props.onSaveLLM(update);
      setOpenaiApiKey('');
      setModelStatus('已保存，下一次对话生效');
    } catch (error) {
      setModelStatus(error instanceof Error ? error.message : '保存失败');
    } finally {
      setSavingLLM(false);
    }
  };

  const savePreference = async (event: FormEvent): Promise<void> => {
    event.preventDefault();
    if (!preferenceKey.trim()) return;
    setSaving(true);
    try {
      await props.onSavePreference({ key: preferenceKey.trim(), value: preferenceValue });
      setPreferenceValue('');
    } finally {
      setSaving(false);
    }
  };

  const createJob = async (event: FormEvent): Promise<void> => {
    event.preventDefault();
    if (!job.directory.trim() || !job.instruction.trim() || !job.cron.trim()) return;
    setSaving(true);
    try {
      await props.onCreateJob({ ...job, enabled: true });
      setJob({ directory: '', instruction: '', cron: '0 9 * * *' });
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="settings-layer">
      <button className="settings-scrim" type="button" aria-label="关闭设置" onClick={props.onClose} />
      <section className="settings-drawer" aria-label="设置">
        <header className="settings-header">
          <div><p className="eyebrow">工作台</p><h2>设置与记录</h2></div>
          <button className="icon-button" type="button" aria-label="关闭" onClick={props.onClose}>×</button>
        </header>

        <nav className="settings-tabs" aria-label="设置分类">
          {tabs.map((item) => (
            <button key={item.id} type="button" className={tab === item.id ? 'active' : ''} onClick={() => setTab(item.id)}>{item.label}</button>
          ))}
        </nav>

        <div className="settings-content">
          {props.error && <div className="inline-error">{props.error}</div>}
          {tab === 'general' && (
            <div className="settings-section">
              <div><h3>服务连接</h3><p>前端仅连接本地后端；保存后会立即重新连接。</p></div>
              <div className="provider-card">
                <strong>模型服务</strong>
                <p>Provider、模型名、Base URL 与 API Key 可在「模型配置」中修改。密钥仅保存在本地后端（<code>.env</code> 默认值或本地数据库覆盖项），保存后不会回传前端，也不会显示明文——界面只标记是否已配置。</p>
              </div>
              <label>本地 REST API 地址<input value={apiBase} onChange={(event) => setApiBase(event.target.value)} placeholder="http://127.0.0.1:8000" /></label>
              <label>本地 WebSocket 地址<input value={wsBase} onChange={(event) => setWsBase(event.target.value)} placeholder="ws://127.0.0.1:8000" /></label>
              <button className="primary-button" type="button" onClick={() => props.onSaveEndpoints(apiBase.trim(), wsBase.trim())}>保存并重连</button>
              {window.desktop && <p className="version-note">Electron {window.desktop.versions.electron} · {window.desktop.platform}</p>}

              <div className="section-divider" />
              <div><h3>可操作的文件夹（沙箱根目录）</h3><p>Agent 只能读写这些文件夹内的文件，越界一律拒绝。</p></div>
              <div className="inline-error" role="note">
                ⚠ 权限变更：新增目录会授权 Agent 移动、重命名、删除其中的文件。请只添加你信任 Agent 操作的目录。
              </div>
              {props.sandboxSettings?.source === 'env' && sandboxRoots.length > 0 && (
                <p className="muted">当前来自 .env 默认配置；保存后将改为界面配置覆盖。</p>
              )}
              <ul className="root-list">
                {sandboxRoots.length === 0 ? (
                  <li className="muted">未配置任何目录，所有文件操作都会被拒绝。</li>
                ) : sandboxRoots.map((root) => (
                  <li key={root} className="root-row">
                    <code title={root}>{root}</code>
                    <button className="icon-button" type="button" aria-label={`移除 ${root}`}
                      onClick={() => setSandboxRoots((current) => current.filter((r) => r !== root))}>×</button>
                  </li>
                ))}
              </ul>
              {window.desktop ? (
                <button className="secondary-button" type="button" onClick={() => void pickDirectory()}>选择目录…</button>
              ) : (
                <div className="root-add">
                  <input value={manualRoot} onChange={(event) => setManualRoot(event.target.value)} placeholder="输入目录的绝对路径" />
                  <button className="secondary-button" type="button" onClick={() => { addRoot(manualRoot); setManualRoot(''); }}>添加</button>
                </div>
              )}
              <button className="primary-button" type="button" disabled={savingSandbox} onClick={() => void saveSandbox()}>
                {savingSandbox ? '保存中…' : '保存沙箱目录'}
              </button>
            </div>
          )}

          {tab === 'model' && (
            <div className="settings-section">
              <div><h3>模型配置</h3><p>选择服务商，填写 API 地址与密钥，拉取可用模型后选择并保存。密钥仅保存在本地，不会显示。</p></div>

              <label>服务商
                <select value={provider} onChange={(event) => { setProvider(event.target.value as LLMProvider); setAvailableModels([]); setModelStatus(null); }}>
                  <option value="openai">OpenAI 兼容 API</option>
                  <option value="ollama">Ollama（本地）</option>
                </select>
              </label>

              {provider === 'openai' ? (
                <>
                  <label>API 地址 (Base URL)<input value={openaiBaseUrl} onChange={(event) => setOpenaiBaseUrl(event.target.value)} placeholder="https://api.openai.com/v1" /></label>
                  <label>API Key<input type="password" value={openaiApiKey} onChange={(event) => setOpenaiApiKey(event.target.value)} placeholder={apiKeySet ? '已配置（留空则保留）' : 'sk-...'} /></label>
                  <button className="secondary-button" type="button" disabled={fetchingModels} onClick={() => void fetchModels()}>{fetchingModels ? '获取中…' : '获取模型列表'}</button>
                  <label>模型
                    {availableModels.length > 0 ? (
                      <select value={openaiModel} onChange={(event) => setOpenaiModel(event.target.value)}>
                        {!availableModels.includes(openaiModel) && openaiModel && <option value={openaiModel}>{openaiModel}（当前）</option>}
                        {availableModels.map((name) => <option key={name} value={name}>{name}</option>)}
                      </select>
                    ) : (
                      <input value={openaiModel} onChange={(event) => setOpenaiModel(event.target.value)} placeholder="gpt-4o-mini" />
                    )}
                  </label>
                </>
              ) : (
                <>
                  <label>Ollama 地址<input value={ollamaBaseUrl} onChange={(event) => setOllamaBaseUrl(event.target.value)} placeholder="http://127.0.0.1:11434" /></label>
                  <button className="secondary-button" type="button" disabled={fetchingModels} onClick={() => void fetchModels()}>{fetchingModels ? '获取中…' : '获取模型列表'}</button>
                  <label>模型
                    {availableModels.length > 0 ? (
                      <select value={ollamaModel} onChange={(event) => setOllamaModel(event.target.value)}>
                        {!availableModels.includes(ollamaModel) && ollamaModel && <option value={ollamaModel}>{ollamaModel}（当前）</option>}
                        {availableModels.map((name) => <option key={name} value={name}>{name}</option>)}
                      </select>
                    ) : (
                      <input value={ollamaModel} onChange={(event) => setOllamaModel(event.target.value)} placeholder="qwen2.5" />
                    )}
                  </label>
                </>
              )}

              {modelStatus && <p className="muted">{modelStatus}</p>}
              <button className="primary-button" type="button" disabled={savingLLM} onClick={() => void saveLLM()}>{savingLLM ? '保存中…' : '保存模型配置'}</button>
            </div>
          )}

          {tab === 'preferences' && (
            <div className="settings-section">
              <div><h3>Agent 偏好</h3><p>这些键值会由后端保存，并在后续任务中使用。</p></div>
              <div className="data-list">
                {props.preferences.length === 0 ? <p className="muted">暂未保存偏好。</p> : props.preferences.map((item) => (
                  <div className="data-row" key={item.key}><strong>{item.key}</strong><span>{item.value || '—'}</span></div>
                ))}
              </div>
              <form className="stack-form" onSubmit={(event) => void savePreference(event)}>
                <label>偏好键<input value={preferenceKey} onChange={(event) => setPreferenceKey(event.target.value)} /></label>
                <label>偏好值<input value={preferenceValue} onChange={(event) => setPreferenceValue(event.target.value)} placeholder="输入新的偏好值" /></label>
                <button className="primary-button" disabled={saving} type="submit">保存偏好</button>
              </form>
            </div>
          )}

          {tab === 'schedules' && (
            <div className="settings-section">
              <div><h3>定时任务</h3><p>使用标准 5 段 Cron 表达式安排自动整理。</p></div>
              <div className="data-list">
                {props.jobs.length === 0 ? <p className="muted">暂无定时任务。</p> : props.jobs.map((item) => (
                  <div className="job-row" key={item.job_id}>
                    <div><strong>{item.instruction}</strong><span>{item.directory}</span><code>{item.cron}</code></div>
                    <button className="text-button text-button--danger" type="button" onClick={() => item.job_id && void props.onDeleteJob(item.job_id)}>删除</button>
                  </div>
                ))}
              </div>
              <form className="stack-form" onSubmit={(event) => void createJob(event)}>
                <label>目录<input value={job.directory} onChange={(event) => setJob((current) => ({ ...current, directory: event.target.value }))} placeholder="C:\\Users\\me\\Downloads" /></label>
                <label>整理指令<textarea rows={2} value={job.instruction} onChange={(event) => setJob((current) => ({ ...current, instruction: event.target.value }))} /></label>
                <label>Cron<input value={job.cron} onChange={(event) => setJob((current) => ({ ...current, cron: event.target.value }))} /></label>
                <button className="primary-button" disabled={saving} type="submit">添加定时任务</button>
              </form>
            </div>
          )}

          {tab === 'logs' && (
            <div className="settings-section settings-section--logs">
              <div className="section-heading"><div><h3>操作日志</h3><p>最近的文件操作及结果。</p></div><button className="secondary-button" type="button" onClick={props.onRefresh}>刷新</button></div>
              {props.loading ? <p className="muted">正在加载…</p> : (
                <div className="log-list">
                  {props.logs.length === 0 ? <p className="muted">暂无操作日志。</p> : props.logs.map((log, index) => (
                    <article className="log-row" key={log.id ?? `${log.ts}-${index}`}>
                      <span className={`log-status log-status--${log.status}`}>{log.status}</span>
                      <div><strong>{log.action}</strong><p>{log.target}{log.dest ? ` → ${log.dest}` : ''}</p><small>{log.detail || new Date(log.ts).toLocaleString('zh-CN')}</small></div>
                      {isReversible(log) && typeof log.id === 'number' && (
                        <button className="secondary-button" type="button" disabled={rollingBack !== null} onClick={() => void rollback(log.id as number)}>
                          {rollingBack === log.id ? '撤销中…' : '撤销'}
                        </button>
                      )}
                    </article>
                  ))}
                </div>
              )}
            </div>
          )}
        </div>
      </section>
    </div>
  );
}
