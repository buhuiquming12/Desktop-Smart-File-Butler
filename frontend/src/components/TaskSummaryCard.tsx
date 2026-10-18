import { useEffect, useRef, useState } from 'react';
import { outcomeLabel, shortThreadId } from '../state';
import type { TaskOutcome, TaskSummary, TaskSummaryOperation } from '../types';
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

const OUTCOME_TONE: Record<string, string> = {
  success: 'summary-card--success',
  partial: 'summary-card--partial',
  failed: 'summary-card--failed',
  rejected: 'summary-card--failed',
  cancelled: 'summary-card--failed',
  budget_exhausted: 'summary-card--partial',
  needs_clarification: 'summary-card--partial',
};

/** 复制/打开目录只处理后端摘要里已校验过的路径；不使用 dangerouslySetInnerHTML。 */
export function TaskSummaryCard({ summary, threadId, onCopy, onReveal, onRollback }: TaskSummaryCardProps) {
  const [expanded, setExpanded] = useState(false);
  const [confirmingRollback, setConfirmingRollback] = useState(false);
  const confirmTimerRef = useRef<number | null>(null);

  const outcome = (summary.outcome || 'unknown') as TaskOutcome;
  const toneClass = OUTCOME_TONE[outcome] ?? '';
  const label = summary.outcome_label || outcomeLabel(outcome);
  const filesOk = summary.files_ok ?? 0;
  const filesFailed = summary.files_failed ?? 0;
  const filesSkipped = summary.files_skipped ?? 0;

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
    <div className={`summary-card ${toneClass}`.trim()}>
      <header className="summary-card__header">
        <strong>任务结果 · {label}</strong>
        <span>会话 {shortThreadId(threadId)}</span>
      </header>

      {summary.stop_reason && summary.stop_reason !== 'goal_verified' && (
        <p className="summary-card__stop-reason">
          停止原因：{summary.stop_reason}
          {summary.unexecuted_steps ? `；仍有 ${summary.unexecuted_steps} 个已规划步骤未执行` : ''}
        </p>
      )}

      {summary.truncated_reasons && summary.truncated_reasons.length > 0 && (
        <p className="summary-card__stop-reason">
          扫描不完整：{summary.truncated_reasons.join('、')}（未列举完整目录）
        </p>
      )}

      {(filesOk + filesFailed + filesSkipped > 0 || summary.matched) ? (
        <div className="summary-stats" aria-label="文件统计">
          <span className="summary-stat summary-stat--ok">文件成功 <b>{filesOk}</b></span>
          <span className="summary-stat summary-stat--failed">文件失败 <b>{filesFailed}</b></span>
          <span className="summary-stat summary-stat--skipped">文件跳过 <b>{filesSkipped}</b></span>
          {summary.matched ? <span className="summary-stat">匹配 <b>{summary.matched}</b></span> : null}
        </div>
      ) : (
        <div className="summary-stats" aria-label="工具调用统计">
          <span className="summary-stat summary-stat--ok">工具成功 <b>{summary.ok}</b></span>
          <span className="summary-stat summary-stat--failed">工具失败 <b>{summary.failed}</b></span>
          <span className="summary-stat summary-stat--skipped">工具跳过 <b>{summary.skipped}</b></span>
        </div>
      )}

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