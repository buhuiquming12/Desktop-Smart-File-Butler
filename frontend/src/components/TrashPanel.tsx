import { useCallback, useEffect, useMemo, useState } from 'react';
import type { ApiClient } from '../api/client';
import { formatBytes, formatDateTime, isRestorable, restoreSummary, toggleSelection } from '../features';
import type { TrashEntry, TrashRestoreResult } from '../types';

interface TrashPanelProps {
  api: ApiClient;
  notify: (message: string, kind?: 'success' | 'info' | 'error') => void;
  onBack: () => void;
}

/**
 * P6：回收站查看与恢复。
 *
 * 只做“查看 + 恢复”闭环：恢复复用回滚链路，原位置被占用时自动改名，绝不覆盖。
 * 永久删除不在本版范围内（界面也不提供入口）。
 */
export function TrashPanel({ api, notify, onBack }: TrashPanelProps) {
  const [entries, setEntries] = useState<TrashEntry[]>([]);
  const [total, setTotal] = useState(0);
  const [query, setQuery] = useState('');
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<TrashRestoreResult | null>(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async (search = query) => {
    setLoading(true);
    try {
      const response = await api.listTrash(search.trim(), 200, 0);
      setEntries(response.entries);
      setTotal(response.total);
      setSelected(new Set());
    } catch (exc) {
      notify(exc instanceof Error ? exc.message : '读取回收站失败', 'error');
    } finally {
      setLoading(false);
    }
  }, [api, notify, query]);

  useEffect(() => {
    void load('');
    // 只在首次挂载时加载；后续由搜索按钮显式触发。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const visibleIds = useMemo(() => entries.filter(isRestorable).map((entry) => entry.op_id), [entries]);
  const selectedCount = visibleIds.filter((id) => selected.has(id)).length;
  const totalBytes = entries.reduce((sum, entry) => sum + entry.size, 0);

  const restore = useCallback(async () => {
    const ids = visibleIds.filter((id) => selected.has(id));
    if (ids.length === 0) {
      notify('请先勾选要恢复的文件。', 'error');
      return;
    }
    setBusy(true);
    try {
      const response = await api.restoreTrash(ids);
      setResult(response);
      notify(restoreSummary(response.ok, response.skipped, response.failed), response.failed > 0 ? 'error' : 'success');
      await load(query);
      setResult(response);
    } catch (exc) {
      notify(exc instanceof Error ? exc.message : '恢复失败', 'error');
    } finally {
      setBusy(false);
    }
  }, [api, load, notify, query, selected, visibleIds]);

  return (
    <section className="panel feature-panel" aria-label="回收站">
      <header className="panel-header">
        <div>
          <p className="eyebrow">删除可恢复</p>
          <h2>回收站</h2>
          <p className="panel-subtitle">
            删除的文件先进入项目回收站；恢复时原位置被占用会自动改名，不会覆盖现有文件。
          </p>
        </div>
        <button className="ghost-button" type="button" onClick={onBack}>返回对话</button>
      </header>

      <div className="feature-body">
        <form
          className="search-bar"
          onSubmit={(event) => {
            event.preventDefault();
            void load(query);
          }}
        >
          <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="按文件名或原路径筛选" />
          <button className="primary-button" type="submit" disabled={loading}>筛选</button>
        </form>

        <div className="toolbar toolbar--spread">
          <span className="muted">
            共 {total} 项 · 占用 {formatBytes(totalBytes)}
            {selectedCount > 0 ? ` · 已选 ${selectedCount} 项` : ''}
          </span>
          <div className="toolbar toolbar--tight">
            <button
              className="ghost-button"
              type="button"
              onClick={() => setSelected(selectedCount === visibleIds.length ? new Set() : new Set(visibleIds))}
              disabled={visibleIds.length === 0}
            >
              {selectedCount === visibleIds.length && visibleIds.length > 0 ? '取消全选' : '全选'}
            </button>
            <button className="primary-button" type="button" disabled={busy || selectedCount === 0} onClick={() => void restore()}>
              恢复选中 {selectedCount} 项
            </button>
          </div>
        </div>

        {loading ? (
          <p className="muted">正在读取回收站…</p>
        ) : entries.length === 0 ? (
          <div className="empty-state">
            <span className="empty-state__graphic" aria-hidden="true">✓</span>
            <strong>回收站是空的</strong>
            <p>通过对话或操作预览删除的文件会出现在这里。</p>
          </div>
        ) : (
          <div className="table-wrap">
            <table className="data-table">
              <thead>
                <tr>
                  <th className="col-check" />
                  <th>文件名</th>
                  <th>原路径</th>
                  <th>删除时间</th>
                  <th>大小</th>
                  <th>所属任务</th>
                </tr>
              </thead>
              <tbody>
                {entries.map((entry) => (
                  <tr key={entry.op_id}>
                    <td className="col-check">
                      <input
                        type="checkbox"
                        aria-label={`选择 ${entry.name}`}
                        checked={selected.has(entry.op_id)}
                        onChange={() => setSelected((current) => toggleSelection(current, entry.op_id))}
                      />
                    </td>
                    <td>
                      <div className="cell-strong">{entry.name}</div>
                      {entry.conflict && <span className="badge badge--warn">原位置已存在同名文件</span>}
                    </td>
                    <td className="cell-path" title={entry.original_path}>{entry.original_path}</td>
                    <td>{formatDateTime(entry.deleted_at)}</td>
                    <td>{formatBytes(entry.size)}</td>
                    <td>{entry.task}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        {result && (
          <div className="execution-result">
            <h3>恢复结果</h3>
            <p>{restoreSummary(result.ok, result.skipped, result.failed)}</p>
            <ul className="result-list">
              {result.results.map((item) => (
                <li key={item.op_id}>
                  <span className={`badge ${item.status === 'ok' ? 'badge--ok' : item.status === 'failed' ? 'badge--bad' : 'badge--warn'}`}>
                    {item.status === 'ok' ? '已恢复' : item.status === 'failed' ? '失败' : '跳过'}
                  </span>
                  <span className="cell-path">{item.restored_to || item.original_path || `#${item.op_id}`}</span>
                  <span className="muted">{item.detail}</span>
                </li>
              ))}
            </ul>
          </div>
        )}
      </div>
    </section>
  );
}
