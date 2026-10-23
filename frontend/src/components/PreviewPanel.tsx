import { useCallback, useEffect, useMemo, useState } from 'react';
import { ApiError, type ApiClient } from '../api/client';
import {
  executionWarning,
  formatBytes,
  formatDateTime,
  mergeItems,
  overridesForSelection,
  previewOpLabels,
  previewSummary,
  selectedCount,
  togglePageSelection,
  toggleSelection,
} from '../features';
import type {
  PreviewConfirmResult,
  PreviewCreateInput,
  PreviewExecuteResult,
  PreviewItem,
  PreviewListItem,
  PreviewOp,
  PreviewPage,
} from '../types';

interface PreviewPanelProps {
  api: ApiClient;
  notify: (message: string, kind?: 'success' | 'info' | 'error') => void;
  onBack: () => void;
  /** 从对话/历史直接打开某个既有预览（面板同时支持从零新建）。 */
  initialPreviewId?: string | undefined;
}

const PAGE_SIZE = 100;

interface PendingConfirm {
  items: string[];
  message: string;
  count: number;
}

/**
 * P0：操作预览。
 *
 * 生成预览只调用只读接口；执行分成两步——“确认本次选择”与“执行”。确认会把
 * 「选中条目 + 每个条目的最终落点」一起绑定成一次性令牌：改选、或确认后改分类目标
 * 都必须重新确认；令牌只能用一次。面板本身也支持打开既有 preview_id（Agent 在对话里
 * 生成的预览），避免“只能新建、看不到刚生成的那份”。
 */
export function PreviewPanel({ api, notify, onBack, initialPreviewId }: PreviewPanelProps) {
  const [op, setOp] = useState<PreviewOp>('move');
  const [directory, setDirectory] = useState('');
  const [destDir, setDestDir] = useState('');
  const [destRoot, setDestRoot] = useState('');
  const [mode, setMode] = useState<'extension' | 'content'>('extension');
  const [extensions, setExtensions] = useState('');
  const [nameContains, setNameContains] = useState('');
  const [prefix, setPrefix] = useState('');
  const [suffix, setSuffix] = useState('');
  const [find, setFind] = useState('');
  const [replace, setReplace] = useState('');
  const [template, setTemplate] = useState('');
  const [useSequence, setUseSequence] = useState(false);
  const [sequenceStart, setSequenceStart] = useState(1);
  const [sequenceStep, setSequenceStep] = useState(1);
  const [sequencePadding, setSequencePadding] = useState(3);
  const [sequencePosition, setSequencePosition] = useState<'prefix' | 'suffix'>('suffix');
  const [useDate, setUseDate] = useState(false);
  const [dateSource, setDateSource] = useState<'modified' | 'created' | 'now' | 'content'>('modified');
  const [dateFormat, setDateFormat] = useState('YYYYMMDD');
  const [contentBudget, setContentBudget] = useState(30);

  const [page, setPage] = useState<PreviewPage | null>(null);
  const [items, setItems] = useState<PreviewItem[]>([]);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [overrides, setOverrides] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [pending, setPending] = useState<PendingConfirm | null>(null);
  const [result, setResult] = useState<PreviewExecuteResult | null>(null);
  const [error, setError] = useState('');

  // 打开既有预览：输入 preview_id 或从最近列表里点选。
  const [openId, setOpenId] = useState(initialPreviewId ?? '');
  const [recents, setRecents] = useState<PreviewListItem[]>([]);
  const [recentsLoading, setRecentsLoading] = useState(false);

  const resetSelection = useCallback(() => {
    setSelected(new Set());
    setOverrides({});
    setResult(null);
    setError('');
    setPending(null);
  }, []);

  const resetPreview = useCallback(() => {
    setPage(null);
    setItems([]);
    resetSelection();
  }, [resetSelection]);

  const refreshRecents = useCallback(async () => {
    setRecentsLoading(true);
    try {
      setRecents(await api.listPreviews(10));
    } catch {
      // 列表只是便捷入口，读取失败不影响新建/打开预览。
      setRecents([]);
    } finally {
      setRecentsLoading(false);
    }
  }, [api]);

  useEffect(() => {
    void refreshRecents();
  }, [refreshRecents]);

  const loadPreview = useCallback(async (previewId: string, hint = '') => {
    const id = previewId.trim();
    if (!id) {
      notify('请先填写要打开的预览编号。', 'error');
      return;
    }
    setBusy(true);
    setError('');
    try {
      const loaded = await api.getPreview(id, 0, PAGE_SIZE);
      setPage(loaded);
      setItems(loaded.items);
      setOpenId(loaded.preview_id);
      resetSelection();
      // 用预览里的规格回填表单，避免“打开的预览和左侧表单看起来是两回事”。
      const spec = loaded.spec as Partial<PreviewCreateInput>;
      setOp(loaded.op);
      if (spec.directory) setDirectory(String(spec.directory));
      if (spec.dest_dir) setDestDir(String(spec.dest_dir));
      if (spec.dest_root) setDestRoot(String(spec.dest_root));
      if (spec.mode === 'content' || spec.mode === 'extension') setMode(spec.mode);
      notify(hint || `已打开预览：${loaded.counts.total} 个文件（${loaded.status === 'executed' ? '已执行' : '待确认'}）。`, 'success');
    } catch (exc) {
      const message = exc instanceof Error ? exc.message : '打开预览失败';
      setError(message);
      notify(message, 'error');
    } finally {
      setBusy(false);
    }
  }, [api, notify, resetSelection]);

  // 从对话里点“打开预览”进来：立刻加载那一份，而不是停在空白表单。
  useEffect(() => {
    if (!initialPreviewId) return;
    setOpenId(initialPreviewId);
    void loadPreview(initialPreviewId, '已打开对话中生成的预览，确认后才会修改文件。');
  }, [initialPreviewId, loadPreview]);

  const chooseDirectory = useCallback(async (setter: (value: string) => void) => {
    const bridge = window.desktop;
    if (!bridge?.chooseDirectory) {
      notify('当前环境不支持原生目录选择，请直接粘贴路径。', 'info');
      return;
    }
    const picked = await bridge.chooseDirectory();
    if (picked) setter(picked);
  }, [notify]);

  const buildInput = useCallback((): PreviewCreateInput => {
    const input: PreviewCreateInput = { op, directory: directory.trim(), recursive: true };
    const filter: NonNullable<PreviewCreateInput['filter']> = {};
    const exts = extensions.split(/[,，\s]+/).map((value) => value.trim().replace(/^\./, '')).filter(Boolean);
    if (exts.length > 0) filter.extensions = exts;
    if (nameContains.trim()) filter.name_contains = nameContains.trim();
    if (Object.keys(filter).length > 0) input.filter = filter;
    if (op === 'move') input.dest_dir = destDir.trim();
    if (op === 'classify') {
      input.dest_root = destRoot.trim();
      input.mode = mode;
      input.content_budget = contentBudget;
    }
    if (op === 'rename') {
      input.prefix = prefix;
      input.suffix = suffix;
      input.find = find;
      input.replace = replace;
      input.template = template;
      // 序号参数完整开放：起始值、步长、补零位数、位置。
      input.use_sequence = useSequence || template.includes('{seq}');
      input.sequence_start = sequenceStart;
      input.sequence_step = sequenceStep;
      input.sequence_padding = sequencePadding;
      input.sequence_position = sequencePosition;
      input.use_date = useDate || template.includes('{date}');
      input.date_source = dateSource;
      input.date_format = dateFormat;
    }
    return input;
  }, [
    contentBudget, dateFormat, dateSource, destDir, destRoot, directory, extensions, find,
    mode, nameContains, op, prefix, replace, sequencePadding, sequencePosition, sequenceStart,
    sequenceStep, suffix, template, useDate, useSequence,
  ]);

  const createPreview = useCallback(async () => {
    if (!directory.trim()) {
      notify('请先选择要处理的目录。', 'error');
      return;
    }
    if (op === 'move' && !destDir.trim()) {
      notify('移动操作需要填写目标目录。', 'error');
      return;
    }
    if (op === 'classify' && !destRoot.trim()) {
      notify('分类操作需要填写目标根目录。', 'error');
      return;
    }
    setBusy(true);
    setError('');
    try {
      const created = await api.createPreview(buildInput());
      setPage(created);
      setItems(created.items);
      setOpenId(created.preview_id);
      resetSelection();
      notify(`已生成预览：${created.counts.total} 个文件，未修改任何文件。`, 'success');
      void refreshRecents();
    } catch (exc) {
      const message = exc instanceof Error ? exc.message : '生成预览失败';
      setError(message);
      notify(message, 'error');
    } finally {
      setBusy(false);
    }
  }, [api, buildInput, destDir, destRoot, directory, notify, op, refreshRecents, resetSelection]);

  const loadMore = useCallback(async () => {
    if (!page || !page.has_more) return;
    setBusy(true);
    try {
      const next = await api.getPreview(page.preview_id, items.length, PAGE_SIZE);
      setItems((current) => mergeItems(current, next.items));
      setPage({ ...next, items: [] });
    } catch (exc) {
      notify(exc instanceof Error ? exc.message : '加载更多失败', 'error');
    } finally {
      setBusy(false);
    }
  }, [api, items.length, notify, page]);

  const runExecute = useCallback(async (confirmed: boolean) => {
    if (!page) return;
    const ids = [...selected];
    if (ids.length === 0) {
      notify('请至少勾选一个文件。', 'error');
      return;
    }
    // 只提交“此刻仍被勾选”的类别覆盖：取消勾选后残留的条目会被后端判定为
    // “覆盖不属于本次选择”而整体拒绝，导致提交卡死。
    const appliedOverrides = overridesForSelection(overrides, selected);
    setBusy(true);
    setError('');
    try {
      // 类别覆盖在“确认”这一步提交：后端把落点一起绑定，执行时改不了。
      const approval: PreviewConfirmResult = await api.confirmPreview(
        page.preview_id, ids, confirmed, appliedOverrides,
      );
      // 确认已通过，确认框到这里才关；不能在 finally 里无条件清空，
      // 否则 approval_required 刚弹出的确认框会被立刻清掉（超阈值批量永远点不下去）。
      setPending(null);
      const execution = await api.executePreview(
        page.preview_id, ids, approval.confirm_token ?? approval.approval_id,
        approval.overrides ?? appliedOverrides,
      );
      setResult(execution);
      setSelected(new Set());
      notify(`执行完成：成功 ${execution.ok}，失败 ${execution.failed}，跳过 ${execution.skipped}`, execution.failed > 0 ? 'error' : 'success');
      const refreshed = await api.getPreview(page.preview_id, 0, PAGE_SIZE);
      setPage(refreshed);
      setItems(refreshed.items);
      void refreshRecents();
    } catch (exc) {
      const apiError = exc instanceof ApiError ? exc : null;
      const code = apiError ? String(apiError.body.code ?? '') : '';
      const needsApproval = apiError
        ? apiError.body.requires_approval === true || code === 'approval_required'
        : false;
      if (apiError && needsApproval && !confirmed) {
        const threshold = Number(apiError.body.threshold ?? 20);
        setPending({
          items: ids,
          message: executionWarning(page.op, ids.length, threshold) ?? apiError.message,
          count: ids.length,
        });
        return;
      }
      // 其他错误不该让确认框继续挂在界面上，改为行内错误提示。
      setPending(null);
      if (apiError) {
        if (code === 'stale_preview' || code === 'selection_changed'
          || code === 'confirm_required' || code === 'targets_changed' || code === 'not_ready') {
          notify(`${apiError.message} 请重新生成预览后再执行。`, 'error');
          setError(apiError.message);
          const refreshed = await api.getPreview(page.preview_id, 0, PAGE_SIZE).catch(() => null);
          if (refreshed) {
            setPage(refreshed);
            setItems(refreshed.items);
            setSelected(new Set());
          }
          return;
        }
      }
      notify(exc instanceof Error ? exc.message : '执行失败', 'error');
      setError(exc instanceof Error ? exc.message : '执行失败');
    } finally {
      setBusy(false);
    }
  }, [api, notify, overrides, page, refreshRecents, selected]);

  const threshold = 20;
  const count = useMemo(() => selectedCount(selected, items), [items, selected]);
  const executed = page?.status === 'executed' || page?.status === 'executing';
  const usedToken = Boolean(page?.approval?.used_at);

  return (
    <section className="panel feature-panel" aria-label="操作预览">
      <header className="panel-header">
        <div>
          <p className="eyebrow">执行前先看清变化</p>
          <h2>操作预览</h2>
          <p className="panel-subtitle">预览只读：生成计划不会移动、重命名或删除任何文件。</p>
        </div>
        <button className="ghost-button" type="button" onClick={onBack}>返回对话</button>
      </header>

      <div className="feature-body">
        <div className="section-block">
          <h3>打开已有预览</h3>
          <p className="form-hint">
            对话里生成的预览会带一个预览编号；粘贴编号即可在这里确认与执行，最近的预览也可直接点开。
          </p>
          <div className="form-grid">
            <label className="field field--wide">
              <span>预览编号（preview_id）</span>
              <div className="field-row">
                <input
                  value={openId}
                  onChange={(event) => setOpenId(event.target.value)}
                  placeholder="例如 3f2a9c…"
                />
                <button
                  className="ghost-button"
                  type="button"
                  disabled={busy || !openId.trim()}
                  onClick={() => void loadPreview(openId)}
                >
                  打开
                </button>
              </div>
            </label>
          </div>
          {recentsLoading ? (
            <p className="muted">正在读取最近的预览…</p>
          ) : recents.length === 0 ? (
            <p className="muted">还没有历史预览。</p>
          ) : (
            <ul className="result-list">
              {recents.map((entry) => (
                <li key={entry.preview_id}>
                  <span className={`badge ${entry.status === 'executed' ? 'badge--ok' : 'badge--warn'}`}>
                    {entry.status === 'executed' ? '已执行' : '待确认'}
                  </span>
                  <code>{entry.preview_id}</code>
                  <span className="muted">
                    {previewOpLabels[entry.op as PreviewOp] ?? entry.op} · {formatDateTime(entry.created_at)} · {entry.directory}
                  </span>
                  <button
                    className="text-button"
                    type="button"
                    disabled={busy}
                    onClick={() => void loadPreview(entry.preview_id)}
                  >
                    打开
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>

        <form className="feature-form" onSubmit={(event) => { event.preventDefault(); void createPreview(); }}>
          <h3>新建预览</h3>
          <div className="form-grid">
            <label className="field">
              <span>目录</span>
              <div className="field-row">
                <input value={directory} onChange={(event) => { setDirectory(event.target.value); resetPreview(); }} placeholder="例如 C:\Users\me\Downloads" />
                <button type="button" className="ghost-button" onClick={() => void chooseDirectory(setDirectory)}>选择</button>
              </div>
            </label>
            <label className="field">
              <span>操作</span>
              <select value={op} onChange={(event) => { setOp(event.target.value as PreviewOp); resetPreview(); }}>
                <option value="move">移动</option>
                <option value="rename">重命名</option>
                <option value="classify">分类</option>
                <option value="delete">删除（进回收站）</option>
              </select>
            </label>
            <label className="field">
              <span>扩展名过滤（逗号分隔，可留空）</span>
              <input value={extensions} onChange={(event) => { setExtensions(event.target.value); resetPreview(); }} placeholder="pdf, docx" />
            </label>
            <label className="field">
              <span>文件名包含（可留空）</span>
              <input value={nameContains} onChange={(event) => { setNameContains(event.target.value); resetPreview(); }} placeholder="report" />
            </label>
          </div>

          {op === 'move' && (
            <label className="field">
              <span>目标目录</span>
              <div className="field-row">
                <input value={destDir} onChange={(event) => { setDestDir(event.target.value); resetPreview(); }} placeholder="例如 C:\Users\me\Downloads\PDF" />
                <button type="button" className="ghost-button" onClick={() => void chooseDirectory(setDestDir)}>选择</button>
              </div>
            </label>
          )}

          {op === 'classify' && (
            <div className="form-grid">
              <label className="field">
                <span>目标根目录</span>
                <div className="field-row">
                  <input value={destRoot} onChange={(event) => { setDestRoot(event.target.value); resetPreview(); }} />
                  <button type="button" className="ghost-button" onClick={() => void chooseDirectory(setDestRoot)}>选择</button>
                </div>
              </label>
              <label className="field">
                <span>分类模式</span>
                <select value={mode} onChange={(event) => { setMode(event.target.value as 'extension' | 'content'); resetPreview(); }}>
                  <option value="extension">按类型（扩展名规则）</option>
                  <option value="content">按内容（读取正文，较慢）</option>
                </select>
              </label>
              {mode === 'content' && (
                <label className="field">
                  <span>本次模型调用预算</span>
                  <input type="number" min={0} max={500} value={contentBudget} onChange={(event) => setContentBudget(Number(event.target.value))} />
                </label>
              )}
            </div>
          )}

          {op === 'rename' && (
            <>
              <div className="form-grid">
                <label className="field"><span>前缀</span><input value={prefix} onChange={(event) => { setPrefix(event.target.value); resetPreview(); }} /></label>
                <label className="field"><span>后缀</span><input value={suffix} onChange={(event) => { setSuffix(event.target.value); resetPreview(); }} /></label>
                <label className="field"><span>查找</span><input value={find} onChange={(event) => { setFind(event.target.value); resetPreview(); }} /></label>
                <label className="field"><span>替换为</span><input value={replace} onChange={(event) => { setReplace(event.target.value); resetPreview(); }} /></label>
                <label className="field field--wide">
                  <span>模板（可选，支持 {'{name}'} {'{seq}'} {'{date}'} {'{ext}'}）</span>
                  <input value={template} onChange={(event) => { setTemplate(event.target.value); resetPreview(); }} placeholder="{name}_{date}_{seq}" />
                </label>
              </div>
              <div className="form-grid">
                <label className="field field--checkbox">
                  <input type="checkbox" checked={useSequence} onChange={(event) => { setUseSequence(event.target.checked); resetPreview(); }} />
                  <span>追加序号</span>
                </label>
                <label className="field">
                  <span>序号起始</span>
                  <input
                    type="number" min={0} max={999999} value={sequenceStart}
                    disabled={!useSequence && !template.includes('{seq}')}
                    onChange={(event) => { setSequenceStart(Math.max(0, Number(event.target.value))); resetPreview(); }}
                  />
                </label>
                <label className="field">
                  <span>序号步长</span>
                  <input
                    type="number" min={1} max={1000} value={sequenceStep}
                    disabled={!useSequence && !template.includes('{seq}')}
                    onChange={(event) => { setSequenceStep(Math.max(1, Number(event.target.value))); resetPreview(); }}
                  />
                </label>
                <label className="field">
                  <span>补零位数</span>
                  <input
                    type="number" min={1} max={10} value={sequencePadding}
                    disabled={!useSequence && !template.includes('{seq}')}
                    onChange={(event) => { setSequencePadding(Math.min(10, Math.max(1, Number(event.target.value)))); resetPreview(); }}
                  />
                </label>
                <label className="field">
                  <span>序号位置</span>
                  <select
                    value={sequencePosition}
                    disabled={!useSequence && !template.includes('{seq}')}
                    onChange={(event) => { setSequencePosition(event.target.value as 'prefix' | 'suffix'); resetPreview(); }}
                  >
                    <option value="suffix">放在名称后</option>
                    <option value="prefix">放在名称前</option>
                  </select>
                </label>
              </div>
              <div className="form-grid">
                <label className="field field--checkbox">
                  <input type="checkbox" checked={useDate} onChange={(event) => { setUseDate(event.target.checked); resetPreview(); }} />
                  <span>追加日期</span>
                </label>
                <label className="field">
                  <span>日期来源</span>
                  <select value={dateSource} onChange={(event) => { setDateSource(event.target.value as typeof dateSource); resetPreview(); }} disabled={!useDate && !template.includes('{date}')}>
                    <option value="modified">文件修改时间</option>
                    <option value="created">文件创建时间</option>
                    <option value="now">执行时的当前时间</option>
                    <option value="content">正文中的明确日期</option>
                  </select>
                </label>
                <label className="field">
                  <span>日期格式</span>
                  <input value={dateFormat} onChange={(event) => { setDateFormat(event.target.value); resetPreview(); }} placeholder="YYYYMMDD" />
                </label>
              </div>
              <p className="form-hint">
                日期取自文件时间或正文中明确写出的日期；取不到时会跳过该文件并说明原因，不会猜测。
              </p>
            </>
          )}

          <div className="toolbar">
            <button className="primary-button" type="submit" disabled={busy}>生成预览（只读）</button>
            {page && <button className="ghost-button" type="button" onClick={() => resetPreview()}>清空</button>}
          </div>
        </form>

        {error && <p className="inline-error" role="alert">{error}</p>}

        {page && (
          <div className="preview-result">
            <div className="preview-summary">
              <strong>{previewOpLabels[page.op]}预览</strong>
              <span>{previewSummary(page.counts, count)}</span>
              <span className="muted">编号 {page.preview_id}</span>
              <span className="muted">生成于 {formatDateTime(page.created_at)}</span>
            </div>
            {executed && (
              <p className="notice notice--warn">
                这份预览{page.status === 'executing' ? '正在执行' : '已经执行过'}，确认令牌已作废；
                需要再改文件请重新生成预览。
              </p>
            )}
            {!executed && usedToken && (
              <p className="notice notice--warn">这次确认已经用过了，重新执行需要再次确认。</p>
            )}
            {page.counts.fallback > 0 && (
              <p className="notice notice--warn">
                有 {page.counts.fallback} 个文件按内容分类失败并回退到扩展名规则，具体原因见每行“依据”。
              </p>
            )}
            <div className="table-wrap">
              <table className="data-table">
                <thead>
                  <tr>
                    <th className="col-check">
                      <input
                        type="checkbox"
                        aria-label="全选本页可执行项"
                        checked={items.filter((item) => item.status === 'ready').every((item) => selected.has(item.item_id)) && items.some((item) => item.status === 'ready')}
                        onChange={() => setSelected((current) => togglePageSelection(current, items))}
                      />
                    </th>
                    <th>文件名</th>
                    <th>原路径</th>
                    <th>目标</th>
                    <th>状态 / 依据</th>
                  </tr>
                </thead>
                <tbody>
                  {items.map((item) => (
                    <tr key={item.item_id} className={item.status !== 'ready' ? 'row-muted' : undefined}>
                      <td className="col-check">
                        <input
                          type="checkbox"
                          aria-label={`选择 ${item.name}`}
                          disabled={item.status !== 'ready' || executed}
                          checked={selected.has(item.item_id)}
                          onChange={() => setSelected((current) => toggleSelection(current, item.item_id))}
                        />
                      </td>
                      <td>
                        <div className="cell-strong">{item.name}</div>
                        <div className="muted">{formatBytes(item.size)} · {formatDateTime(item.modified)}</div>
                      </td>
                      <td className="cell-path" title={item.source}>{item.source}</td>
                      <td className="cell-path" title={item.target}>
                        {page.op === 'classify' && item.editable_category && !executed ? (
                          <input
                            className="category-input"
                            value={overrides[item.item_id] ?? item.category}
                            onChange={(event) => setOverrides((current) => ({ ...current, [item.item_id]: event.target.value }))}
                            aria-label={`修改 ${item.name} 的目标类别`}
                          />
                        ) : item.target}
                        {page.op === 'classify' && item.editable_category && !executed && (
                          <div className="muted">类别可修改；修改会在确认时一并锁定，执行时不能再变</div>
                        )}
                      </td>
                      <td>
                        {item.status === 'ready'
                          ? <span className="badge badge--ok">可执行</span>
                          : <span className="badge badge--warn">{item.status === 'conflict' ? '冲突' : '跳过'}</span>}
                        {item.reason && <div className="muted">{item.reason}</div>}
                        {item.basis && <div className="muted">{item.basis}</div>}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            <div className="toolbar toolbar--spread">
              <div>
                {page.has_more && <button className="ghost-button" type="button" onClick={() => void loadMore()} disabled={busy}>加载更多</button>}
                <span className="muted">执行数量以勾选为准：已选 {count} 项</span>
              </div>
              <button
                className="primary-button"
                type="button"
                disabled={busy || count === 0 || executed}
                onClick={() => {
                  const warning = page.op === 'delete'
                    ? executionWarning(page.op, count, threshold)
                    : null;
                  if (warning) setPending({ items: [...selected], message: warning, count });
                  else void runExecute(false);
                }}
              >
                {page.op === 'delete' ? `移入回收站（${count}）` : `执行选中 ${count} 项`}
              </button>
            </div>
          </div>
        )}

        {result && (
          <div className="execution-result">
            <h3>执行结果</h3>
            <p>成功 {result.ok} 项，失败 {result.failed} 项，跳过 {result.skipped} 项。</p>
            <p className="muted">可按会话撤销：该批次记录在 <code>{result.rollback_thread_id}</code> 下，可在「操作日志」中逐条撤销。</p>
            <ul className="result-list">
              {result.results.map((entry) => (
                <li key={entry.item_id}>
                  <span className={`badge ${entry.status === 'ok' ? 'badge--ok' : entry.status === 'failed' ? 'badge--bad' : 'badge--warn'}`}>
                    {entry.status === 'ok' ? '完成' : entry.status === 'failed' ? '失败' : '跳过'}
                  </span>
                  <span className="cell-path">{entry.source}</span>
                  <span className="muted">{entry.detail}</span>
                </li>
              ))}
            </ul>
          </div>
        )}
      </div>

      {pending && (
        <div className="modal-backdrop" role="dialog" aria-modal="true" aria-label="确认执行">
          <div className="modal-card">
            <h3>确认执行 {pending.count} 项</h3>
            <p>{pending.message}</p>
            <p className="muted">确认后后端会再次核验源文件与目标冲突；若预览已失效会拒绝执行并要求刷新。</p>
            <div className="toolbar toolbar--right">
              <button className="ghost-button" type="button" onClick={() => setPending(null)}>取消</button>
              <button className="primary-button" type="button" disabled={busy} onClick={() => void runExecute(true)}>确认执行</button>
            </div>
          </div>
        </div>
      )}
    </section>
  );
}
