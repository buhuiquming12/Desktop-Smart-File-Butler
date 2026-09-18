import { useEffect, useState, type FormEvent } from 'react';
import type { OperationLog, Preference, ScheduledJob } from '../types';

export type SettingsTab = 'general' | 'preferences' | 'schedules' | 'logs';

interface SettingsProps {
  open: boolean;
  initialTab: SettingsTab;
  apiBase: string;
  wsBase: string;
  preferences: Preference[];
  jobs: ScheduledJob[];
  logs: OperationLog[];
  loading: boolean;
  error: string | null;
  onClose: () => void;
  onSaveEndpoints: (apiBase: string, wsBase: string) => void;
  onSavePreference: (preference: Preference) => Promise<void>;
  onCreateJob: (job: Omit<ScheduledJob, 'job_id'>) => Promise<void>;
  onDeleteJob: (jobId: string) => Promise<void>;
  onRefresh: () => void;
}

const tabs: Array<{ id: SettingsTab; label: string }> = [
  { id: 'general', label: '常规' },
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

  useEffect(() => setTab(props.initialTab), [props.initialTab, props.open]);
  useEffect(() => {
    setApiBase(props.apiBase);
    setWsBase(props.wsBase);
  }, [props.apiBase, props.wsBase, props.open]);

  if (!props.open) return null;

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
                <strong>OpenAI 兼容 API</strong>
                <p>模型服务的 Provider、模型名和 Base URL 等非敏感信息由后端配置提供。API Key 仅在后端 <code>.env</code> 中配置，前端不会读取、显示或保存密钥。</p>
              </div>
              <label>本地 REST API 地址<input value={apiBase} onChange={(event) => setApiBase(event.target.value)} placeholder="http://127.0.0.1:8000" /></label>
              <label>本地 WebSocket 地址<input value={wsBase} onChange={(event) => setWsBase(event.target.value)} placeholder="ws://127.0.0.1:8000" /></label>
              <button className="primary-button" type="button" onClick={() => props.onSaveEndpoints(apiBase.trim(), wsBase.trim())}>保存并重连</button>
              {window.desktop && <p className="version-note">Electron {window.desktop.versions.electron} · {window.desktop.platform}</p>}
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
