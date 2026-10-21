import { useCallback, useEffect, useState } from 'react';
import type { ApiClient } from '../api/client';
import { formatDateTime } from '../features';
import type { OrganizeRule } from '../types';

interface RulesPanelProps {
  api: ApiClient;
  notify: (message: string, kind?: 'success' | 'info' | 'error') => void;
  onBack: () => void;
}

/**
 * P5：目录监听与自动整理规则。
 *
 * 明确告知用户：整理由本应用的轮询线程执行，应用退出后不会再处理任何文件。
 */
export function RulesPanel({ api, notify, onBack }: RulesPanelProps) {
  const [rules, setRules] = useState<OrganizeRule[]>([]);
  const [running, setRunning] = useState(false);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [directory, setDirectory] = useState('');
  const [destRoot, setDestRoot] = useState('');
  const [mode, setMode] = useState<'extension' | 'content'>('extension');
  const [settle, setSettle] = useState(5);
  // 已有规则的编辑（此前只能启用/停用/删除，改目的地就必须删了重建）。
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editDirectory, setEditDirectory] = useState('');
  const [editDestRoot, setEditDestRoot] = useState('');
  const [editMode, setEditMode] = useState<'extension' | 'content'>('extension');
  const [editSettle, setEditSettle] = useState(5);

  const refresh = useCallback(async () => {
    try {
      const response = await api.listRules();
      setRules(response.rules);
      setRunning(response.running);
    } catch (exc) {
      notify(exc instanceof Error ? exc.message : '读取规则失败', 'error');
    } finally {
      setLoading(false);
    }
  }, [api, notify]);

  useEffect(() => {
    void refresh();
    const timer = window.setInterval(() => void refresh(), 4000);
    return () => window.clearInterval(timer);
  }, [refresh]);

  const pick = useCallback(async (setter: (value: string) => void) => {
    const picked = await window.desktop?.chooseDirectory?.();
    if (picked) setter(picked);
  }, []);

  const create = useCallback(async () => {
    if (!directory.trim() || !destRoot.trim()) {
      notify('请填写监听目录与目标根目录。', 'error');
      return;
    }
    setBusy(true);
    try {
      await api.createRule({
        directory: directory.trim(),
        dest_root: destRoot.trim(),
        mode,
        settle_seconds: settle,
      });
      setDirectory('');
      setDestRoot('');
      notify('规则已保存。新文件写入稳定后会自动整理；应用退出后不会继续执行。', 'success');
      await refresh();
    } catch (exc) {
      notify(exc instanceof Error ? exc.message : '保存规则失败', 'error');
    } finally {
      setBusy(false);
    }
  }, [api, destRoot, directory, mode, notify, refresh, settle]);

  const toggle = useCallback(async (rule: OrganizeRule) => {
    try {
      await api.updateRule(rule.rule_id, { enabled: !rule.enabled });
      await refresh();
    } catch (exc) {
      notify(exc instanceof Error ? exc.message : '更新规则失败', 'error');
    }
  }, [api, notify, refresh]);

  const remove = useCallback(async (rule: OrganizeRule) => {
    try {
      await api.deleteRule(rule.rule_id);
      notify('规则已删除（已整理的文件不受影响）。', 'success');
      await refresh();
    } catch (exc) {
      notify(exc instanceof Error ? exc.message : '删除规则失败', 'error');
    }
  }, [api, notify, refresh]);

  /** 进入编辑态：把当前规则的值填进草稿，避免改错对象。 */
  const startEdit = useCallback((rule: OrganizeRule) => {
    setEditingId(rule.rule_id);
    setEditDirectory(rule.directory);
    setEditDestRoot(rule.dest_root);
    setEditMode(rule.mode);
    setEditSettle(rule.settle_seconds);
  }, []);

  const saveEdit = useCallback(async (rule: OrganizeRule) => {
    if (!editDirectory.trim() || !editDestRoot.trim()) {
      notify('监听目录与目标根目录都不能为空。', 'error');
      return;
    }
    setBusy(true);
    try {
      await api.updateRule(rule.rule_id, {
        directory: editDirectory.trim(),
        dest_root: editDestRoot.trim(),
        mode: editMode,
        settle_seconds: Math.max(1, editSettle),
      });
      setEditingId(null);
      notify('规则已更新；已移动的文件不会回退。', 'success');
      await refresh();
    } catch (exc) {
      notify(exc instanceof Error ? exc.message : '更新规则失败', 'error');
    } finally {
      setBusy(false);
    }
  }, [api, editDestRoot, editDirectory, editMode, editSettle, notify, refresh]);

  const runNow = useCallback(async (rule: OrganizeRule) => {    try {
      const result = await api.runRule(rule.rule_id);
      notify(
        result.processed > 0
          ? `已处理 ${result.processed} 个文件。`
          : result.skipped > 0
            ? result.reason
            : '没有需要处理的新文件。',
        result.processed > 0 ? 'success' : 'info',
      );
      await refresh();
    } catch (exc) {
      notify(exc instanceof Error ? exc.message : '立即运行失败', 'error');
    }
  }, [api, notify, refresh]);

  return (
    <section className="panel feature-panel" aria-label="自动整理">
      <header className="panel-header">
        <div>
          <p className="eyebrow">新文件到达后自动归位</p>
          <h2>自动整理</h2>
          <p className="panel-subtitle">
            只在「桌面智能文件管家」运行期间生效：应用退出后不会再监听或整理任何文件。
          </p>
        </div>
        <button className="ghost-button" type="button" onClick={onBack}>返回对话</button>
      </header>

      <div className="feature-body">
        <p className={`notice ${running ? 'notice--ok' : 'notice--warn'}`}>
          {running
            ? '目录监听正在运行。文件写入稳定后才会被处理；应用关闭后监听停止。'
            : '目录监听未运行（后端未启动或已停止），此时不会自动整理任何文件。'}
        </p>

        <div className="section-block">
          <h3>新建规则</h3>
          <div className="form-grid">
            <label className="field">
              <span>监听目录</span>
              <div className="field-row">
                <input value={directory} onChange={(event) => setDirectory(event.target.value)} placeholder="新文件会落在这个目录" />
                <button className="ghost-button" type="button" onClick={() => void pick(setDirectory)}>选择</button>
              </div>
            </label>
            <label className="field">
              <span>整理到</span>
              <div className="field-row">
                <input value={destRoot} onChange={(event) => setDestRoot(event.target.value)} placeholder="按类别建立的子目录会放在这里" />
                <button className="ghost-button" type="button" onClick={() => void pick(setDestRoot)}>选择</button>
              </div>
            </label>
            <label className="field">
              <span>分类方式</span>
              <select value={mode} onChange={(event) => setMode(event.target.value as 'extension' | 'content')}>
                <option value="extension">按类型（扩展名规则）</option>
                <option value="content">按内容（读取正文，失败自动回退）</option>
              </select>
            </label>
            <label className="field">
              <span>写入稳定等待（秒）</span>
              <input type="number" min={1} max={3600} value={settle} onChange={(event) => setSettle(Math.max(1, Number(event.target.value)))} />
            </label>
          </div>
          <div className="toolbar">
            <button className="primary-button" type="button" onClick={() => void create()} disabled={busy}>保存规则</button>
            <span className="muted">规则只作用于该监听目录的直接子文件；超过批量审批阈值时不会自动执行。</span>
          </div>
        </div>

        <div className="section-block">
          <h3>已有规则</h3>
          {loading ? (
            <p className="muted">正在读取规则…</p>
          ) : rules.length === 0 ? (
            <div className="empty-state">
              <span className="empty-state__graphic" aria-hidden="true">◷</span>
              <strong>还没有自动整理规则</strong>
              <p>保存规则后，新文件写入稳定才会被移动到对应类别目录。</p>
            </div>
          ) : (
            <ul className="rule-list">
              {rules.map((rule) => (
                <li key={rule.rule_id} className={rule.enabled ? undefined : 'row-muted'}>
                  <div className="rule-head">
                    <div>
                      <strong>{rule.directory}</strong>
                      <span className="muted"> → {rule.dest_root}</span>
                    </div>
                    <div className="toolbar toolbar--tight">
                      <span className={`badge ${rule.enabled ? 'badge--ok' : 'badge--warn'}`}>{rule.enabled ? '已启用' : '已停用'}</span>
                      <button className="ghost-button" type="button" onClick={() => (editingId === rule.rule_id ? setEditingId(null) : startEdit(rule))}>
                        {editingId === rule.rule_id ? '收起编辑' : '编辑'}
                      </button>
                      <button className="ghost-button" type="button" onClick={() => void toggle(rule)}>{rule.enabled ? '停用' : '启用'}</button>
                      <button className="ghost-button" type="button" onClick={() => void runNow(rule)}>立即检查</button>
                      <button className="ghost-button" type="button" onClick={() => void remove(rule)}>删除</button>
                    </div>
                  </div>
                  {editingId === rule.rule_id && (
                    <div className="section-block">
                      <div className="form-grid">
                        <label className="field">
                          <span>监听目录</span>
                          <div className="field-row">
                            <input value={editDirectory} onChange={(event) => setEditDirectory(event.target.value)} />
                            <button className="ghost-button" type="button" onClick={() => void pick(setEditDirectory)}>选择</button>
                          </div>
                        </label>
                        <label className="field">
                          <span>整理到</span>
                          <div className="field-row">
                            <input value={editDestRoot} onChange={(event) => setEditDestRoot(event.target.value)} />
                            <button className="ghost-button" type="button" onClick={() => void pick(setEditDestRoot)}>选择</button>
                          </div>
                        </label>
                        <label className="field">
                          <span>分类方式</span>
                          <select value={editMode} onChange={(event) => setEditMode(event.target.value as 'extension' | 'content')}>
                            <option value="extension">按类型（扩展名规则）</option>
                            <option value="content">按内容（读取正文，失败自动回退）</option>
                          </select>
                        </label>
                        <label className="field">
                          <span>写入稳定等待（秒）</span>
                          <input type="number" min={1} max={3600} value={editSettle} onChange={(event) => setEditSettle(Math.max(1, Number(event.target.value)))} />
                        </label>
                      </div>
                      <div className="toolbar">
                        <button className="primary-button" type="button" disabled={busy} onClick={() => void saveEdit(rule)}>保存修改</button>
                        <button className="ghost-button" type="button" onClick={() => setEditingId(null)}>取消</button>
                        <span className="muted">改动只影响之后到达的新文件。</span>
                      </div>
                    </div>
                  )}
                  <div className="muted">
                    方式：{rule.mode === 'content' ? '按内容' : '按类型'} · 稳定等待 {rule.settle_seconds} 秒 ·
                    已处理 {rule.processed_count} 个文件
                  </div>
                  <div className="muted">
                    最近检查：{rule.last_run_at ? formatDateTime(rule.last_run_at) : '—'}
                    {rule.last_result ? ` · ${rule.last_result}` : ''}
                  </div>
                </li>
              ))}
            </ul>
          )}
        </div>
      </div>
    </section>
  );
}
