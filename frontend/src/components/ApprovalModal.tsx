import { useEffect, useRef } from 'react';
import { approvalOrigin, shortThreadId } from '../state';
import type { ApprovalDecision, PendingApproval } from '../types';

interface ApprovalModalProps {
  approval: PendingApproval | null;
  /** B3：队列中待审批总数；>1 时提示后面还排着多少项。 */
  queueSize?: number;
  submitting: boolean;
  onDecision: (decision: ApprovalDecision) => void;
}

export function ApprovalModal({ approval, queueSize = 1, submitting, onDecision }: ApprovalModalProps) {
  const rejectButtonRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (approval) rejectButtonRef.current?.focus();
  }, [approval]);

  useEffect(() => {
    if (!approval) return;
    const handleKey = (event: KeyboardEvent): void => {
      if (event.key === 'Escape' && !submitting) onDecision('reject');
    };
    window.addEventListener('keydown', handleKey);
    return () => window.removeEventListener('keydown', handleKey);
  }, [approval, onDecision, submitting]);

  if (!approval) return null;

  return (
    <div className="modal-backdrop" role="presentation">
      <section className="approval-modal" role="alertdialog" aria-modal="true" aria-labelledby="approval-title" aria-describedby="approval-detail">
        <div className="warning-icon" aria-hidden="true">!</div>
        <p className="eyebrow">需要你的确认</p>
        <h2 id="approval-title">即将执行高风险操作</h2>
        {/* B3：定时任务等后台会话的审批此前会被丢弃且无任何入口，这里明确标注来源与队列进度。 */}
        {queueSize > 1 && (
          <p className="approval-queue-note">
            第 1 / {queueSize} 项待审批 —— 处理后会自动弹出下一项。
          </p>
        )}
        <p id="approval-detail" className="approval-detail" style={{ whiteSpace: 'pre-line' }}>{approval.detail || '该操作可能改变或移除文件，请确认后继续。'}</p>

        <dl className="approval-summary">
          <div><dt>操作</dt><dd>{approval.action}</dd></div>
          <div><dt>目标</dt><dd title={approval.target}>{approval.target}</dd></div>
          <div><dt>来源</dt><dd title={approval.thread_id}>{approvalOrigin(approval.thread_id)} · {shortThreadId(approval.thread_id)}</dd></div>
        </dl>

        <p className="approval-note">拒绝后，当前任务将停止此操作，不会修改目标文件。</p>
        <div className="modal-actions">
          <button ref={rejectButtonRef} className="secondary-button" type="button" disabled={submitting} onClick={() => onDecision('reject')}>拒绝操作</button>
          <button className="danger-button" type="button" disabled={submitting} onClick={() => onDecision('approve')}>
            {submitting ? '正在提交…' : '确认并继续'}
          </button>
        </div>
      </section>
    </div>
  );
}
