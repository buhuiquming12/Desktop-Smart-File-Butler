import { useEffect, useRef, useState } from 'react';
import { shortThreadId } from '../state';
import type { TaskSummary, TaskSummaryOperation } from '../types';
import type { ToastKind } from './ToastStack';

interface TaskSummaryCardProps {
  summary: TaskSummary;
  threadId: string;
  onCopy: (value: string) => void;
  onReveal: (path: string) => Promise<boolean | void>;
  onRollback?: (() => void) | undefined;
  onNotify?: ((content: string, kind?: ToastKind) => void) | undefined;
}

function operationStatusClass(status: string): string {
  if (status === 'ok') return 'summary-op__status--ok';
  if (status === 'failed') return 'summary-op__status--failed';
  return 'summary-op__status--skipped';
}

function operationLabel(op: TaskSummaryOperation): string {
  return op.tool_label || op.tool || '文件操作';
}

function operationStatusLabel(op: TaskSummaryOperation): string {
  return op.status_label || op.status || '未知';
}

/** 复制/打开目录只处理后端摘要里已校验过的路径；不使用 dangerouslySetInnerHTML。 */
export function TaskSummaryCard({ summary, threadId, onCopy, onReveal, onRollback }: TaskSummaryCardProps) {
  const [expanded, setExpanded] = useState(false);
  const [confirmingRollback, setConfirmingRollback] = useState(false);
  const confirmTimerRef = useRef<number | null>(null);

  useEffect(() => () => {
    if (confirmTimerRef.current !== null) window.clearTimeout(confirmTimerRef.current);
  }, []);

  const handleRollback = (): void => {
    if (!onRollback) return;
    if (!confirmingRollback) {
      setConfirmingRollback(true);
      confirmTimerRef.current = window.setTimeout(() => setConfirmingRollback(false), 4000);
      return;
    }
    if (confirmTimerRef.current !== null) window.clearTimeout(confirmTimerRef.current);
    setConfirmingRollback(false);
    // 撤销结果由 App 的统一反馈（Toast/操作日志）呈现，避免重复提示。
    onRollback();
  };

  return (
    <div className="summary-card">
      <header className="summary-card__header">
        <strong>任务完成摘要</strong>
        <span>会话 {shortThreadId(threadId)}</span>
      </header>

      <div className="summary-stats" aria-label="统计">
        <span className="summary-stat summary-stat--ok">成功 <b>{summary.ok}</b></span>
        <span className="summary-stat summary-stat--failed">失败 <b>{summary.failed}</b></span>
        <span className="summary-stat summary-stat--skipped">跳过 <b>{summary.skipped}</b></span>
      </div>

      {summary.files.length > 0 && (
        <div className="summary-files">
          <p className="summary-files__title">涉及的主要文件 / 目录</p>
          <ul className="summary-files__list">
            {summary.files.map((file) => (
              <li key={file}>
                <code title={file}>{file}</code>
                <span className="summary-files__actions">
                  <button type="button" className="text-button" onClick={() => onCopy(file)}>复制路径</button>
                  <button type="button" className="text-button" onClick={() => void onReveal(file)}>打开所在目录</button>
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {summary.operations.length > 0 && (
        <div className="summary-operations">
          <button
            type="button"
            className="text-button summary-operations__toggle"
            aria-expanded={expanded}
            onClick={() => setExpanded((current) => !current)}
          >
            {expanded ? '收起' : '展开'}操作明细（{summary.operations.length} 条）
          </button>
          {expanded && (
            <ul className="summary-operations__list">
              {summary.operations.map((op, index) => (
                <li key={`${op.tool}-${index}`}>
                  <span className="summary-op__label">{operationLabel(op)}</span>
                  <span className={`summary-op__status ${operationStatusClass(op.status)}`}>{operationStatusLabel(op)}</span>
                  <p>{op.description}</p>
                  <code title={op.path}>{op.path}{op.dest ? ` → ${op.dest}` : ''}</code>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}

      {onRollback && (
        <footer className="summary-card__footer">
          <button
            type="button"
            className={confirmingRollback ? 'danger-button' : 'secondary-button'}
            onClick={handleRollback}
          >
            {confirmingRollback ? '再次点击确认撤销' : '撤销本次操作'}
          </button>
          <span>仅回滚本次会话中可逆的文件操作</span>
        </footer>
      )}
    </div>
  );
}