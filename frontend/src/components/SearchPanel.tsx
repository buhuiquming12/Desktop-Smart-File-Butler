import { useCallback, useEffect, useRef, useState } from 'react';
import type { ApiClient } from '../api/client';
import { formatBytes, formatDateTime, progressPercent } from '../features';
import type { IndexJob, IndexStats, SearchHit, SearchResponse } from '../types';

interface SearchPanelProps {
  api: ApiClient;
  notify: (message: string, kind?: 'success' | 'info' | 'error') => void;
  onBack: () => void;
}

/**
 * P2：本地内容搜索。
 *
 * 索引与检索都在本机完成；结果会按当前授权目录再过滤一次，
 * 因此收缩授权后旧索引不会泄露文件内容。
 */
export function SearchPanel({ api, notify, onBack }: SearchPanelProps) {
  const [directory, setDirectory] = useState('');
  const [job, setJob] = useState<IndexJob | null>(null);
  const [stats, setStats] = useState<IndexStats | null>(null);
  const [query, setQuery] = useState('');
  const [response, setResponse] = useState<SearchResponse | null>(null);
  const [busy, setBusy] = useState(false);
  const timerRef = useRef<number | null>(null);

  const stopPolling = useCallback(() => {
    if (timerRef.current !== null) {
      window.clearTimeout(timerRef.current);
      timerRef.current = null;
    }
  }, []);

  useEffect(() => stopPolling, [stopPolling]);

  const loadStats = useCallback(async () => {
    try {
      setStats(await api.getIndexStats());
    } catch {
      // 统计失败不阻塞搜索
    }
  }, [api]);

  useEffect(() => {
    void loadStats();
  }, [loadStats]);

  const pollJob = useCallback((jobId: string) => {
    stopPolling();
    const tick = async () => {
      try {
        const state = await api.getIndexJob(jobId);
        setJob(state);
        if (state.status === 'running') {
          timerRef.current = window.setTimeout(() => void tick(), 600);
        } else {
          void loadStats();
        }
      } catch (exc) {
        notify(exc instanceof Error ? exc.message : '读取索引进度失败', 'error');
      }
    };
    void tick();
  }, [api, loadStats, notify, stopPolling]);

  const chooseDirectory = useCallback(async () => {
    const picked = await window.desktop?.chooseDirectory?.();
    if (picked) setDirectory(picked);
  }, []);

  const startIndex = useCallback(async () => {
    if (!directory.trim()) {
      notify('请先选择要建立索引的目录。', 'error');
      return;
    }
    setBusy(true);
    try {
      const created = await api.startIndex(directory.trim(), true);
      pollJob(created.job_id);
      notify('已开始建立内容索引；可继续使用其它功能。', 'info');
    } catch (exc) {
      notify(exc instanceof Error ? exc.message : '建立索引失败', 'error');
    } finally {
      setBusy(false);
    }
  }, [api, directory, notify, pollJob]);

  const cancelIndex = useCallback(async () => {
    if (!job) return;
    try {
      await api.cancelIndex(job.job_id);
      notify('已请求取消索引任务。', 'info');
    } catch (exc) {
      notify(exc instanceof Error ? exc.message : '取消失败', 'error');
    }
  }, [api, job, notify]);

  const runSearch = useCallback(async (offset = 0) => {
    if (!query.trim()) {
      notify('请输入要检索的关键词。', 'error');
      return;
    }
    setBusy(true);
    try {
      const result = await api.searchContent(query.trim(), 50, offset);
      setResponse((current) => (
        offset > 0 && current
          ? { ...result, results: [...current.results, ...result.results] }
          : result
      ));
      if (result.total === 0) {
        notify('没有匹配结果：可能尚未建立索引，或正文中没有该关键词。', 'info');
      }
    } catch (exc) {
      notify(exc instanceof Error ? exc.message : '搜索失败', 'error');
    } finally {
      setBusy(false);
    }
  }, [api, notify, query]);

  const reveal = useCallback(async (hit: SearchHit) => {
    const bridge = window.desktop;
    if (!bridge?.revealPath) {
      notify('当前环境不支持在资源管理器中定位。', 'info');
      return;
    }
    const ok = await bridge.revealPath(hit.path);
    if (!ok) notify('无法在资源管理器中定位该文件（可能已被移动或删除）。', 'error');
  }, [notify]);

  const running = job?.status === 'running';
  const indexedCount = stats?.by_status.indexed ?? 0;

  return (
    <section className="panel feature-panel" aria-label="内容搜索">
      <header className="panel-header">
        <div>
          <p className="eyebrow">记得内容也能找到文件</p>
          <h2>内容搜索</h2>
          <p className="panel-subtitle">复用 PDF、DOCX、文本与图片 OCR 提取能力，中文关键词可直接检索。</p>
        </div>
        <button className="ghost-button" type="button" onClick={onBack}>返回对话</button>
      </header>

      <div className="feature-body">
        <div className="section-block">
          <h3>建立索引</h3>
          <div className="form-grid">
            <label className="field">
              <span>目录</span>
              <div className="field-row">
                <input value={directory} onChange={(event) => setDirectory(event.target.value)} placeholder="例如 C:\Users\me\Documents" />
                <button className="ghost-button" type="button" onClick={() => void chooseDirectory()}>选择</button>
              </div>
            </label>
          </div>
          <div className="toolbar">
            <button className="primary-button" type="button" onClick={() => void startIndex()} disabled={busy || running}>建立/更新索引</button>
            {running && <button className="ghost-button" type="button" onClick={() => void cancelIndex()}>取消</button>}
            {stats && (
              <span className="muted">
                已索引 {indexedCount} 个文件 · {formatBytes(stats.indexed_bytes)} ·
                {stats.fts5 ? ' FTS5 全文索引可用' : ' 使用子串匹配（FTS5 不可用）'}
              </span>
            )}
          </div>

          {job && (
            <div className="progress-block">
              <div className="progress-meta">
                <span>阶段：{job.progress.phase}</span>
                <span>已索引 {job.progress.indexed}</span>
                <span>跳过 {job.progress.skipped}</span>
                <span>失败 {job.progress.failed}</span>
                <span>清理失效记录 {job.progress.removed}</span>
              </div>
              <div className="progress-track" aria-hidden="true">
                <div className="progress-fill" style={{ width: `${progressPercent(job.progress.scanned, Math.max(1, job.progress.total))}%` }} />
              </div>
              {job.progress.current && <p className="muted cell-path">当前：{job.progress.current}</p>}
              {job.progress.truncated && (
                <p className="notice notice--warn">
                  本次遍历未完成{job.progress.truncated_reason ? `（${job.progress.truncated_reason}）` : ''}，
                  因此不会清理任何已有索引记录：取消或中断不会让搜索结果凭空消失。
                </p>
              )}
              {job.progress.prune_skipped && !job.progress.truncated && (
                <p className="muted">本次未做增量清理：{job.progress.prune_skipped}</p>
              )}
              {job.error && <p className="inline-error">{job.error}</p>}
              {job.progress.errors.length > 0 && (
                <details className="error-details">
                  <summary>{job.progress.errors.length} 个文件提取失败</summary>
                  <ul className="result-list">
                    {job.progress.errors.map((entry) => (
                      <li key={entry.path}><span className="cell-path">{entry.path}</span><span className="muted">{entry.error}</span></li>
                    ))}
                  </ul>
                </details>
              )}
              <p className="muted">
                「跳过」包含格式不支持或没有可提取正文的文件；这些文件不会被伪装成“没有内容”。
              </p>
            </div>
          )}
        </div>

        <div className="section-block">
          <h3>搜索正文</h3>
          <form
            className="search-bar"
            onSubmit={(event) => {
              event.preventDefault();
              void runSearch(0);
            }}
          >
            <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="输入关键词，例如：采购合同 / quarterly report" />
            <button className="primary-button" type="submit" disabled={busy}>搜索</button>
          </form>

          {response && (
            <div className="search-results">
              <p className="muted">
                共 {response.total} 条结果 · 后端 {response.backend}
                {response.capabilities.semantic ? '（支持语义检索）' : '（关键词检索）'}
              </p>
              {response.results.length === 0 ? (
                <div className="empty-state">
                  <span className="empty-state__graphic" aria-hidden="true">∅</span>
                  <strong>没有找到匹配内容</strong>
                  <p>确认关键词拼写，或先对目录建立索引。</p>
                </div>
              ) : (
                <ul className="search-list">
                  {response.results.map((hit) => (
                    <li key={hit.path}>
                      <div className="search-hit">
                        <div>
                          <strong>{hit.name}</strong>
                          <div className="cell-path muted">{hit.path}</div>
                          <p className="search-snippet">{hit.snippet}</p>
                          <div className="muted">
                            {formatBytes(hit.size)} · 索引于 {formatDateTime(hit.indexed_at)}
                            {hit.truncated ? ' · 内容已截断（仅索引前 20 万字符）' : ''}
                          </div>
                        </div>
                        <button className="ghost-button" type="button" onClick={() => void reveal(hit)}>在资源管理器中定位</button>
                      </div>
                    </li>
                  ))}
                </ul>
              )}
              {response.total > response.results.length && (
                <button
                  className="ghost-button"
                  type="button"
                  onClick={() => void runSearch(response.results.length)}
                >
                  加载更多
                </button>
              )}
            </div>
          )}
        </div>
      </div>
    </section>
  );
}
