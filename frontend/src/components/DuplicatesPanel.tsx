import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { ApiError, rollbackFeedback, type ApiClient } from '../api/client';
import {
  buildKeepMap,
  formatBytes,
  formatDateTime,
  plannedDeletions,
  progressPercent,
} from '../features';
import type { DuplicateCleanupResult, DuplicateScan } from '../types';

interface DuplicatesPanelProps {
  api: ApiClient;
  notify: (message: string, kind?: 'success' | 'info' | 'error') => void;
  onBack: () => void;
}

/**
 * P1：重复文件检测。
 *
 * 检测本身只读；清理必须显式确认，后端会重新计算内容哈希后才软删除，
 * 因此“扫描后文件被改过”不会被误删。
 */
export function DuplicatesPanel({ api, notify, onBack }: DuplicatesPanelProps) {
  const [directory, setDirectory] = useState('');
  const [minSize, setMinSize] = useState(1);
  const [scan, setScan] = useState<DuplicateScan | null>(null);
  const [overrides, setOverrides] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [result, setResult] = useState<DuplicateCleanupResult | null>(null);
  const [rollbackConfirming, setRollbackConfirming] = useState(false);
  const timerRef = useRef<number | null>(null);

  const stopPolling = useCallback(() => {
    if (timerRef.current !== null) {
      window.clearTimeout(timerRef.current);
      timerRef.current = null;
    }
  }, []);

  useEffect(() => stopPolling, [stopPolling]);

  const poll = useCallback((scanId: string) => {
    stopPolling();
    const tick = async () => {
      try {
        const state = await api.getDuplicateScan(scanId);
        setScan(state);
        if (state.status === 'running') {
          timerRef.current = window.setTimeout(() => void tick(), 700);
        }
      } catch (exc) {
        notify(exc instanceof Error ? exc.message : '读取扫描进度失败', 'error');
      }
    };
    void tick();
  }, [api, notify, stopPolling]);

  const chooseDirectory = useCallback(async () => {
    const picked = await window.desktop?.chooseDirectory?.();
    if (picked) setDirectory(picked);
  }, []);

  const start = useCallback(async () => {
    if (!directory.trim()) {
      notify('请先选择要扫描的目录。', 'error');
      return;
    }
    setBusy(true);
    setResult(null);
    setOverrides({});
    try {
      const created = await api.scanDuplicates([directory.trim()], minSize);
      poll(created.scan_id);
      notify('已开始扫描重复文件，检测过程不会删除任何文件。', 'info');
    } catch (exc) {
      notify(exc instanceof Error ? exc.message : '启动扫描失败', 'error');
    } finally {
      setBusy(false);
    }
  }, [api, directory, minSize, notify, poll]);

  const cancel = useCallback(async () => {
    if (!scan) return;
    try {
      await api.cancelDuplicateScan(scan.scan_id);
      notify('已请求取消扫描，正在停止…', 'info');
    } catch (exc) {
      notify(exc instanceof Error ? exc.message : '取消失败', 'error');
    }
  }, [api, notify, scan]);

  const groups = scan?.groups ?? [];
  const deleting = useMemo(() => plannedDeletions(groups, overrides), [groups, overrides]);

  const cleanup = useCallback(async () => {
    if (!scan) return;
    setBusy(true);
    setConfirmOpen(false);
    try {
      const response = await api.cleanupDuplicates(scan.scan_id, buildKeepMap(groups, overrides));
      setResult(response);
      notify(`清理完成：删除 ${response.deleted} 个，跳过 ${response.skipped} 个`, response.failed > 0 ? 'error' : 'success');
      poll(scan.scan_id);
    } catch (exc) {
      if (exc instanceof ApiError && exc.status === 409) {
        notify(`${exc.message}`, 'error');
      } else {
        notify(exc instanceof Error ? exc.message : '清理失败', 'error');
      }
    } finally {
      setBusy(false);
    }
  }, [api, groups, notify, overrides, poll, scan]);

  /** 整批撤销本次清理：按 cleanup 返回的批次编号回滚（与操作日志同一链路）。 */
  const rollbackBatch = useCallback(async () => {
    if (!result) return;
    if (!rollbackConfirming) {
      setRollbackConfirming(true);
      window.setTimeout(() => setRollbackConfirming(false), 4000);
      return;
    }
    setRollbackConfirming(false);
    setBusy(true);
    try {
      const summary = await api.rollbackThread(result.thread_id);
      const feedback = rollbackFeedback(summary);
      notify(feedback.message, feedback.kind);
      if (scan) poll(scan.scan_id);
    } catch (exc) {
      notify(exc instanceof Error ? `撤销失败：${exc.message}` : '撤销失败', 'error');
    } finally {
      setBusy(false);
    }
  }, [api, notify, poll, result, rollbackConfirming, scan]);

  const scanning = scan?.status === 'running';
  return (
    <section className="panel feature-panel" aria-label="重复文件">
      <header className="panel-header">
        <div>
          <p className="eyebrow">内容完全相同才算重复</p>
          <h2>重复文件</h2>
          <p className="panel-subtitle">先按大小分组，再对候选计算内容哈希；检测过程不会删除任何文件。</p>
        </div>
        <button className="ghost-button" type="button" onClick={onBack}>返回对话</button>
      </header>

      <div className="feature-body">
        <div className="form-grid">
          <label className="field">
            <span>扫描目录</span>
            <div className="field-row">
              <input value={directory} onChange={(event) => setDirectory(event.target.value)} placeholder="例如 C:\Users\me\Downloads" />
              <button className="ghost-button" type="button" onClick={() => void chooseDirectory()}>选择</button>
            </div>
          </label>
          <label className="field">
            <span>最小文件大小（字节，0 表示包含空文件）</span>
            <input type="number" min={0} value={minSize} onChange={(event) => setMinSize(Math.max(0, Number(event.target.value)))} />
          </label>
        </div>

        <div className="toolbar">
          <button className="primary-button" type="button" onClick={() => void start()} disabled={busy || scanning}>开始扫描</button>
          {scanning && <button className="ghost-button" type="button" onClick={() => void cancel()}>取消扫描</button>}
          {scan && <span className="muted">扫描编号 {scan.scan_id.slice(0, 8)} · 状态 {scan.status}</span>}
        </div>

        {scan && (
          <div className="progress-block">
            <div className="progress-meta">
              <span>已扫描 {scan.progress.scanned} 个文件</span>
              <span>已计算内容 {scan.progress.hashed} 个</span>
              <span>读取 {formatBytes(scan.progress.bytes_read)}</span>
            </div>
            <div className="progress-track" aria-hidden="true">
              <div
                className="progress-fill"
                style={{ width: `${progressPercent(scan.progress.hashed, Math.max(1, scan.progress.total_candidates))}%` }}
              />
            </div>
            {scan.progress.current && <p className="muted cell-path">当前：{scan.progress.current}</p>}
            {scan.error && <p className="inline-error">{scan.error}</p>}
            {scan.summary.stale && (
              <p className="notice notice--warn">
                有 {scan.summary.changed_files} 个文件在扫描期间发生变化，已排除或重新计算；结果可能不完整，建议重新扫描。
              </p>
            )}
            {scan.status === 'completed' && (
              <p className="muted">
                共 {scan.summary.scanned_files ?? 0} 个文件，发现 {scan.summary.duplicate_groups ?? 0} 组重复，
                预计可回收 {formatBytes(scan.summary.reclaimable_bytes ?? 0)}。
              </p>
            )}
          </div>
        )}

        {groups.length > 0 && (
          <div className="dup-groups">
            {groups.map((group) => {
              const keep = overrides[group.group_id] ?? group.suggested_keep;
              return (
                <article className="dup-group" key={group.group_id}>
                  <header>
                    <div>
                      <strong>{group.count} 份相同内容</strong>
                      <span className="muted"> · 每个 {formatBytes(group.size)} · 预计可回收 {formatBytes(group.reclaimable)}</span>
                    </div>
                    <span className="badge badge--info">{group.distinct_inodes} 个独立文件对象</span>
                  </header>
                  <p className="muted">保留建议：{group.keep_reason}</p>
                  <ul className="dup-files">
                    {group.files.map((file) => (
                      <li key={file.path}>
                        <label className="dup-file">
                          <input
                            type="radio"
                            name={`keep-${group.group_id}`}
                            checked={keep === file.path}
                            onChange={() => setOverrides((current) => ({ ...current, [group.group_id]: file.path }))}
                          />
                          <span className="dup-file__body">
                            <span className="cell-strong">{file.name}</span>
                            <span className="cell-path muted">{file.path}</span>
                            <span className="muted">
                              {formatBytes(file.size)} · 修改于 {formatDateTime(file.modified)}
                              {file.hardlink_of ? ' · 硬链接（与另一份共享同一数据，删除不释放空间）' : ''}
                            </span>
                          </span>
                          {keep === file.path && <span className="badge badge--ok">保留</span>}
                        </label>
                      </li>
                    ))}
                  </ul>
                </article>
              );
            })}
            <div className="toolbar toolbar--spread">
              <span className="muted">清理只会把未保留的副本移入项目回收站，可按批次撤销。</span>
              <button className="danger-button" type="button" disabled={busy || deleting === 0} onClick={() => setConfirmOpen(true)}>
                清理 {deleting} 个副本
              </button>
            </div>
          </div>
        )}

        {scan?.status === 'completed' && groups.length === 0 && (
          <div className="empty-state">
            <span className="empty-state__graphic" aria-hidden="true">✓</span>
            <strong>没有发现内容相同的文件</strong>
            <p>同名不同内容的文件不会被当作重复。</p>
          </div>
        )}

        {result && (
          <div className="execution-result">
            <h3>清理结果</h3>
            <p>删除 {result.deleted} 个，跳过 {result.skipped} 个，失败 {result.failed} 个，释放 {formatBytes(result.freed_bytes)}。</p>
            <p className="muted">批次编号 <code>{result.thread_id}</code>，可在「回收站」或「操作日志」中恢复/撤销。</p>
            {result.deleted > 0 && (
              <div className="toolbar">
                <button
                  className={rollbackConfirming ? 'danger-button' : 'secondary-button'}
                  type="button"
                  disabled={busy}
                  onClick={() => void rollbackBatch()}
                >
                  {rollbackConfirming ? '再次点击确认撤销（放回原位）' : '撤销本次清理'}
                </button>
                <span className="muted">把这一批删除副本从回收站返回到原目录；保留项不受影响。</span>
              </div>
            )}
            <ul className="result-list">
              {result.results.map((entry) => (
                <li key={`${entry.group_id}-${entry.path}`}>
                  <span className={`badge ${entry.status === 'ok' ? 'badge--ok' : entry.status === 'failed' ? 'badge--bad' : 'badge--warn'}`}>
                    {entry.status === 'ok' ? '已删除' : entry.status === 'failed' ? '失败' : '跳过'}
                  </span>
                  <span className="cell-path">{entry.path}</span>
                  <span className="muted">{entry.detail}</span>
                </li>
              ))}
            </ul>
          </div>
        )}
      </div>

      {confirmOpen && scan && (
        <div className="modal-backdrop" role="dialog" aria-modal="true" aria-label="确认清理重复文件">
          <div className="modal-card">
            <h3>确认清理 {deleting} 个副本</h3>
            <p>未保留的副本会移入项目回收站（可恢复），保留项不会被修改。</p>
            <p className="muted">清理前会重新计算内容哈希；如果文件在扫描后发生变化，该文件会被跳过而不是删除。</p>
            <div className="toolbar toolbar--right">
              <button className="ghost-button" type="button" onClick={() => setConfirmOpen(false)}>取消</button>
              <button className="danger-button" type="button" disabled={busy} onClick={() => void cleanup()}>确认清理</button>
            </div>
          </div>
        </div>
      )}
    </section>
  );
}
