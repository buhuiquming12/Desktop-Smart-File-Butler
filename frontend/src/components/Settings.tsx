import { useEffect, useRef, useState, type FormEvent } from 'react';
import type {
  LLMModelsRequest,
  LLMModelsResponse,
  LLMProvider,
  LLMSettings,
  LLMSettingsUpdate,
  OCRCapability,
  OperationLog,
  Preference,
  SandboxSettings,
  ScheduledJob,
  StructuredOutputMode,
  ToolSettings,
  ToolSettingsUpdate,
  WorkspaceSettings,
  WorkspaceSettingsUpdate,
} from '../types';

export type SettingsTab = 'general' | 'model' | 'preferences' | 'schedules' | 'logs';

interface SettingsProps {
  open: boolean;
  initialTab: SettingsTab;
  apiBase: string;
  wsBase: string;
  llmSettings: LLMSettings | null;
  sandboxSettings: SandboxSettings | null;
  workspaceSettings: WorkspaceSettings | null;
  toolSettings: ToolSettings | null;
  preferences: Preference[];
  jobs: ScheduledJob[];
  logs: OperationLog[];
  loading: boolean;
  error: string | null;
  onClose: () => void;
  onSaveEndpoints: (apiBase: string, wsBase: string) => void;
  onSaveLLM: (update: LLMSettingsUpdate) => Promise<void>;
  onSaveSandbox: (roots: string[]) => Promise<void>;
  onSaveWorkspace: (update: WorkspaceSettingsUpdate) => Promise<void>;
  onSaveTools: (update: ToolSettingsUpdate) => Promise<void>;
  onRefreshTools: () => Promise<void>;
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

/** OCR 必备语言包在界面上的展示名。 */
const requiredOcrLanguages: Array<{ code: string; label: string }> = [
  { code: 'eng', label: 'English' },
  { code: 'chi_sim', label: '简体中文' },
];

const ocrLanguageLabels: Record<string, string> = {
  eng: 'English',
  chi_sim: '简体中文',
  chi_sim_vert: '简体中文（竖排）',
  chi_tra: '繁体中文',
  osd: '方向检测',
};

function ocrLanguageLabel(code: string): string {
  return ocrLanguageLabels[code] ?? code;
}

/** 把能力检测结果翻成一句话 + 语气（✓ 可用 / △ 部分可用 / × 不可用）。 */
function ocrSummary(ocr: OCRCapability): { icon: string; tone: 'ok' | 'warn' | 'bad'; text: string } {
  switch (ocr.status) {
    case 'available':
      return { icon: '✓', tone: 'ok', text: `Tesseract ${ocr.version} 已就绪` };
    case 'language_pack_missing':
      return {
        icon: '△',
        tone: 'warn',
        text: `已找到 Tesseract ${ocr.version}，但缺少${ocr.missing_languages.map(ocrLanguageLabel).join('、')}语言包`,
      };
    case 'binary_not_found':
      return { icon: '×', tone: 'bad', text: '未找到 Tesseract 可执行文件，请在上方选择 tesseract.exe' };
    case 'invalid_tessdata_dir':
      return { icon: '×', tone: 'bad', text: '语言包目录不存在或不是目录' };
    case 'not_configured':
      return { icon: '×', tone: 'bad', text: '未安装 pytesseract，本机无法使用 OCR' };
    case 'error':
    default:
      return { icon: '×', tone: 'bad', text: 'OCR 检测失败' };
  }
}

const tabs: Array<{ id: SettingsTab; label: string }> = [
  { id: 'general', label: '常规' },
  { id: 'model', label: '模型' },
  { id: 'preferences', label: '偏好' },
  { id: 'schedules', label: '定时任务' },
  { id: 'logs', label: '操作日志' },
];

export function Settings(props: SettingsProps) {
  const drawerRef = useRef<HTMLElement>(null);
  const closeButtonRef = useRef<HTMLButtonElement>(null);
  const lastFocusedRef = useRef<Element | null>(null);
  const wasOpenRef = useRef(false);
  const onCloseRef = useRef(props.onClose);

  useEffect(() => {
    onCloseRef.current = props.onClose;
  }, [props.onClose]);

  // 打开时聚焦关闭按钮；真正关闭时把焦点还给触发元素。
  useEffect(() => {
    if (props.open) {
      if (!wasOpenRef.current) {
        lastFocusedRef.current = document.activeElement;
        window.setTimeout(() => closeButtonRef.current?.focus(), 0);
      }
      wasOpenRef.current = true;
    } else if (wasOpenRef.current) {
      wasOpenRef.current = false;
      const restoreTo = lastFocusedRef.current;
      if (restoreTo instanceof HTMLElement) restoreTo.focus();
    }
  }, [props.open]);

  // Esc 关闭；Tab/Shift+Tab 在抽屉内循环，焦点不逃逸到背景。
  useEffect(() => {
    if (!props.open) return;
    const handleKey = (event: KeyboardEvent): void => {
      if (event.key === 'Escape') {
        event.preventDefault();
        onCloseRef.current();
        return;
      }
      if (event.key !== 'Tab') return;
      const drawer = drawerRef.current;
      if (!drawer) return;
      const focusables = Array.from(drawer.querySelectorAll<HTMLElement>(
        'button, input, select, textarea, a[href], [tabindex]:not([tabindex="-1"])',
      )).filter((el) => !el.hasAttribute('disabled') && el.offsetParent !== null);
      if (focusables.length === 0) return;
      const first = focusables[0];
      const last = focusables[focusables.length - 1];
      if (!first || !last) return;
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    window.addEventListener('keydown', handleKey);
    return () => window.removeEventListener('keydown', handleKey);
  }, [props.open]);
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
  const [structuredOutputMode, setStructuredOutputMode] = useState<StructuredOutputMode>('auto');
  const [availableModels, setAvailableModels] = useState<string[]>([]);
  const [fetchingModels, setFetchingModels] = useState(false);
  const [modelStatus, setModelStatus] = useState<string | null>(null);
  const [savingLLM, setSavingLLM] = useState(false);
  const [rollingBack, setRollingBack] = useState<number | null>(null);

  // 沙箱根目录编辑态（权限变更，见 P1-3）
  const [sandboxRoots, setSandboxRoots] = useState<string[]>([]);
  const [savingSandbox, setSavingSandbox] = useState(false);
  const [manualRoot, setManualRoot] = useState('');

  // 默认管理目录（未指定路径时 Agent 的落脚点）
  const [managedRoot, setManagedRoot] = useState('');
  const [savingManagedRoot, setSavingManagedRoot] = useState(false);
  const [managedRootStatus, setManagedRootStatus] = useState<string | null>(null);

  // OCR 外部工具路径
  const [tesseractCmd, setTesseractCmd] = useState('');
  const [tessdataDir, setTessdataDir] = useState('');
  const [savingTools, setSavingTools] = useState(false);
  const [checkingTools, setCheckingTools] = useState(false);
  const [toolStatus, setToolStatus] = useState<string | null>(null);

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

  const pickManagedRoot = async (): Promise<void> => {
    const chosen = await window.desktop?.chooseDirectory();
    if (chosen) setManagedRoot(chosen);
  };

  const saveManagedRoot = async (): Promise<void> => {
    setSavingManagedRoot(true);
    setManagedRootStatus(null);
    const value = managedRoot.trim();
    try {
      await props.onSaveWorkspace({ default_managed_root: value });
      setManagedRootStatus(value ? '已保存：未指定路径时 Agent 会使用该目录' : '已清除默认管理目录');
    } catch (error) {
      setManagedRootStatus(error instanceof Error ? error.message : '保存失败');
    } finally {
      setSavingManagedRoot(false);
    }
  };

  const pickTesseractCmd = async (): Promise<void> => {
    const chosen = await window.desktop?.chooseFile();
    if (chosen) setTesseractCmd(chosen);
  };

  const pickTessdataDir = async (): Promise<void> => {
    const chosen = await window.desktop?.chooseDirectory();
    if (chosen) setTessdataDir(chosen);
  };

  const saveTools = async (): Promise<void> => {
    setSavingTools(true);
    setToolStatus(null);
    try {
      await props.onSaveTools({
        tesseract_cmd: tesseractCmd.trim(),
        tessdata_dir: tessdataDir.trim(),
      });
      setToolStatus('已保存，并用新配置重新检测');
    } catch (error) {
      setToolStatus(error instanceof Error ? error.message : '保存失败');
    } finally {
      setSavingTools(false);
    }
  };

  const recheckTools = async (): Promise<void> => {
    setCheckingTools(true);
    setToolStatus(null);
    try {
      await props.onRefreshTools();
      setToolStatus('已重新检测');
    } catch (error) {
      setToolStatus(error instanceof Error ? error.message : '检测失败');
    } finally {
      setCheckingTools(false);
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
    setStructuredOutputMode(s.structured_output_mode);
    setAvailableModels([]);
    setModelStatus(null);
  }, [props.llmSettings, props.open]);
  useEffect(() => {
    if (props.sandboxSettings) setSandboxRoots(props.sandboxSettings.roots);
  }, [props.sandboxSettings, props.open]);
  useEffect(() => {
    if (props.workspaceSettings) {
      setManagedRoot(props.workspaceSettings.default_managed_root);
    }
  }, [props.workspaceSettings, props.open]);
  useEffect(() => {
    const settings = props.toolSettings;
    if (!settings) return;
    setTesseractCmd(settings.tesseract_cmd);
    setTessdataDir(settings.tessdata_dir);
  }, [props.toolSettings, props.open]);

  if (!props.open) return null;

  const apiKeySet = props.llmSettings?.openai_api_key_set ?? false;
  const ocr = props.toolSettings?.ocr ?? null;
  const ocrStatus = ocr ? ocrSummary(ocr) : null;
  const toolSources = props.toolSettings?.sources;
  const workspace = props.workspaceSettings;

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
          ? {
              provider,
              ollama_base_url: ollamaBaseUrl.trim(),
              ollama_model: ollamaModel.trim(),
              structured_output_mode: structuredOutputMode,
            }
          : {
              provider,
              openai_base_url: openaiBaseUrl.trim(),
              openai_model: openaiModel.trim(),
              structured_output_mode: structuredOutputMode,
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
      <section ref={drawerRef} className="settings-drawer" role="dialog" aria-modal="true" aria-labelledby="settings-drawer-title">
        <header className="settings-header">
          <div><p className="eyebrow">工作台</p><h2 id="settings-drawer-title">设置与记录</h2></div>
          <button ref={closeButtonRef} className="icon-button" type="button" aria-label="关闭" onClick={props.onClose}>×</button>
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
              <div className="provider-card">
                <strong>OCR 文字识别（可选）</strong>
                <p>图片与扫描件的文字提取依赖本机 Tesseract。下方「文件与工具」里可直接选择 Tesseract 程序与语言包目录，不必再手改 <code>.env</code>。</p>
              </div>
              <label>本地 REST API 地址<input value={apiBase} onChange={(event) => setApiBase(event.target.value)} placeholder="http://127.0.0.1:8000" /></label>
              <label>本地 WebSocket 地址<input value={wsBase} onChange={(event) => setWsBase(event.target.value)} placeholder="ws://127.0.0.1:8000" /></label>
              <button className="primary-button" type="button" onClick={() => props.onSaveEndpoints(apiBase.trim(), wsBase.trim())}>保存并重连</button>
              {window.desktop && <p className="version-note">Electron {window.desktop.versions.electron} · {window.desktop.platform}</p>}

              <div className="section-divider" />
              <div><h3>文件管理</h3><p>文件管家只能访问授权目录及其子目录；没有明确指定路径时，则使用默认管理目录。</p></div>
              <p className="muted">
                授权目录决定文件管家能够访问哪些位置。默认管理目录用于没有明确指定路径时的文件整理任务。
              </p>
              <div className="inline-error" role="note">
                ⚠ 权限变更：新增授权目录会允许 Agent 移动、重命名、删除其中的文件。请只添加你信任 Agent 操作的目录。
              </div>
              {props.sandboxSettings?.source === 'env' && sandboxRoots.length > 0 && (
                <p className="muted">当前来自 .env 默认配置；保存后将改为界面配置覆盖。</p>
              )}
              <ul className="root-list">
                {sandboxRoots.length === 0 ? (
                  <li className="muted">未配置任何授权目录，所有文件操作都会被拒绝。</li>
                ) : sandboxRoots.map((root) => (
                  <li key={root} className="root-row">
                    <code title={root}>{root}</code>
                    <button className="icon-button" type="button" aria-label={`移除 ${root}`}
                      onClick={() => setSandboxRoots((current) => current.filter((r) => r !== root))}>×</button>
                  </li>
                ))}
              </ul>
              {window.desktop ? (
                <button className="secondary-button" type="button" onClick={() => void pickDirectory()}>添加授权目录…</button>
              ) : (
                <div className="root-add">
                  <input value={manualRoot} onChange={(event) => setManualRoot(event.target.value)} placeholder="输入目录的绝对路径" />
                  <button className="secondary-button" type="button" onClick={() => { addRoot(manualRoot); setManualRoot(''); }}>添加</button>
                </div>
              )}
              <button className="primary-button" type="button" disabled={savingSandbox} onClick={() => void saveSandbox()}>
                {savingSandbox ? '保存中…' : '保存授权目录'}
              </button>

              <div className="provider-card">
                <strong>默认管理目录</strong>
                <p>说“整理一下文件”“看看我的文件”而没有指定位置时，Agent 就在这个目录里工作。它必须位于上面的授权目录内。</p>
              </div>
              {workspace && !workspace.valid && workspace.message && (
                <p className="muted">△ {workspace.message}</p>
              )}
              <label>默认管理目录
                <input
                  value={managedRoot}
                  onChange={(event) => setManagedRoot(event.target.value)}
                  placeholder="例如 D:\\Downloads"
                />
              </label>
              <div className="button-row">
                {window.desktop && (
                  <button className="secondary-button" type="button" onClick={() => void pickManagedRoot()}>选择目录…</button>
                )}
                <button className="primary-button" type="button" disabled={savingManagedRoot} onClick={() => void saveManagedRoot()}>
                  {savingManagedRoot ? '保存中…' : '保存默认管理目录'}
                </button>
                {managedRoot.trim() !== '' && (
                  <button className="text-button" type="button" onClick={() => setManagedRoot('')}>清除</button>
                )}
              </div>
              {managedRootStatus && <p className="muted">{managedRootStatus}</p>}

              <div className="section-divider" />
              <div><h3>OCR 文字识别</h3><p>可选：指定本机 Tesseract 程序与语言包目录。保存后立即生效，无需重启。</p></div>
              <label>Tesseract 程序
                <input
                  value={tesseractCmd}
                  onChange={(event) => setTesseractCmd(event.target.value)}
                  placeholder="留空则使用系统 PATH 中的 tesseract"
                />
              </label>
              <div className="button-row">
                {window.desktop && (
                  <button className="secondary-button" type="button" onClick={() => void pickTesseractCmd()}>选择文件…</button>
                )}
                {toolSources?.tesseract_cmd === 'env' && tesseractCmd === '' && (
                  <span className="muted">当前走系统 PATH 查找</span>
                )}
              </div>
              <label>语言包目录
                <input
                  value={tessdataDir}
                  onChange={(event) => setTessdataDir(event.target.value)}
                  placeholder="例如 C:\\Program Files\\Tesseract-OCR\\tessdata"
                />
              </label>
              <div className="button-row">
                {window.desktop && (
                  <button className="secondary-button" type="button" onClick={() => void pickTessdataDir()}>选择目录…</button>
                )}
              </div>
              <button className="primary-button" type="button" disabled={savingTools} onClick={() => void saveTools()}>
                {savingTools ? '保存并检测中…' : '保存并检测'}
              </button>

              {ocrStatus && ocr && (
                <div className={`ocr-status ocr-status--${ocrStatus.tone}`} role="status">
                  <strong>{ocrStatus.icon} {ocrStatus.text}</strong>
                  <ul className="ocr-langs">
                    {requiredOcrLanguages.map((item) => {
                      const has = ocr.languages.includes(item.code);
                      return (
                        <li key={item.code} className={has ? 'ocr-lang ocr-lang--ok' : 'ocr-lang ocr-lang--missing'}>
                          {has ? '✓' : '×'} {item.label}
                        </li>
                      );
                    })}
                  </ul>
                  {ocr.languages.length > 0 && (
                    <p className="muted">检测到的语言包：{ocr.languages.map(ocrLanguageLabel).join('、')}</p>
                  )}
                  {ocr.message && <p className="muted">{ocr.message}</p>}
                  <div className="button-row">
                    <button className="secondary-button" type="button" disabled={checkingTools}
                      onClick={() => void recheckTools()}>
                      {checkingTools ? '检测中…' : '重新检测'}
                    </button>
                  </div>
                </div>
              )}
              {toolStatus && <p className="muted">{toolStatus}</p>}
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

              <div className="section-divider" />
              <label className="checkbox-row">
                <input
                  type="checkbox"
                  checked={structuredOutputMode === 'prompt'}
                  onChange={(event) => setStructuredOutputMode(event.target.checked ? 'prompt' : 'auto')}
                />
                手动降级：强制使用提示词 JSON
              </label>
              <p className="muted">
                默认「自动」——先让服务商的 function calling 生成结构化结果，服务商不支持（如只实现聊天补全、
                收到 <code>tools</code> 就返回 400）时自动改走提示词 JSON。若自动判断不适用，可勾选此项跳过原生
                路径，直接从第一次请求起就用提示词约束输出。保存后下一次对话生效。
              </p>

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
