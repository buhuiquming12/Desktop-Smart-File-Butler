import { useEffect, useRef } from 'react';
import type { ApprovalDecision, PendingApproval } from '../types';

interface ApprovalModalProps {
  approval: PendingApproval | null;
  submitting: boolean;
  onDecision: (decision: ApprovalDecision) => void;
}

export function ApprovalModal({ approval, submitting, onDecision }: ApprovalModalProps) {
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
        <p id="approval-detail" className="approval-detail">{approval.detail || '该操作可能改变或移除文件，请确认后继续。'}</p>

        <dl className="approval-summary">
          <div><dt>操作</dt><dd>{approval.action}</dd></div>
          <div><dt>目标</dt><dd title={approval.target}>{approval.target}</dd></div>
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
