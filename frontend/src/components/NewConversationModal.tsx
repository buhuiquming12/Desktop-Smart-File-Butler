import { useEffect, useRef } from 'react';
import type { NewConversationAction } from '../state';

interface NewConversationModalProps {
  open: boolean;
  onChoose: (action: NewConversationAction) => void;
  onCancel: () => void;
}

/** 新对话决策：任务运行时不允许静默清空，必须由用户选择去向。 */
export function NewConversationModal({ open, onChoose, onCancel }: NewConversationModalProps) {
  const modalRef = useRef<HTMLElement>(null);
  const endButtonRef = useRef<HTMLButtonElement>(null);
  const lastFocusedRef = useRef<Element | null>(null);
  const wasOpenRef = useRef(false);
  const onCancelRef = useRef(onCancel);

  useEffect(() => {
    onCancelRef.current = onCancel;
  }, [onCancel]);

  // 打开时聚焦第一个选项；关闭后把焦点还给触发元素。
  useEffect(() => {
    if (open) {
      if (!wasOpenRef.current) {
        lastFocusedRef.current = document.activeElement;
        window.setTimeout(() => endButtonRef.current?.focus(), 0);
      }
      wasOpenRef.current = true;
    } else if (wasOpenRef.current) {
      wasOpenRef.current = false;
      const restoreTo = lastFocusedRef.current;
      if (restoreTo instanceof HTMLElement) restoreTo.focus();
    }
  }, [open]);

  // Esc 取消；Tab/Shift+Tab 在弹窗内循环，焦点不逃逸到背景。
  useEffect(() => {
    if (!open) return;
    const handleKey = (event: KeyboardEvent): void => {
      if (event.key === 'Escape') {
        event.preventDefault();
        onCancelRef.current();
        return;
      }
      if (event.key !== 'Tab') return;
      const modal = modalRef.current;
      if (!modal) return;
      const focusables = Array.from(modal.querySelectorAll<HTMLElement>(
        'button, input, select, textarea, a[href], [tabindex]:not([tabindex="-1"])',
      )).filter((el) => !el.hasAttribute('disabled') && el.offsetParent !== null);
      if (focusables.length === 0) return;
      const first = focusables[0];
      const last = focusables[focusables.length - 1];
      if (!first || !last) return;
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    window.addEventListener('keydown', handleKey);
    return () => window.removeEventListener('keydown', handleKey);
  }, [open]);

  if (!open) return null;

  return (
    <div className="modal-backdrop" role="presentation">
      <section ref={modalRef} className="new-chat-modal" role="dialog" aria-modal="true" aria-labelledby="new-chat-title" aria-describedby="new-chat-desc">
        <p className="eyebrow">开始新对话</p>
        <h2 id="new-chat-title">当前任务仍在运行</h2>
        <p id="new-chat-desc">直接清空会丢失正在执行任务的进度。请选择如何处理当前任务：</p>

        <div className="new-chat-options">
          <button ref={endButtonRef} className="new-chat-option new-chat-option--end" type="button" onClick={() => onChoose('end')}>
            <strong>结束当前任务并新建对话</strong>
            <span>停止正在运行的任务，已完成的文件操作会保留在操作日志中。</span>
          </button>
          <button className="new-chat-option new-chat-option--background" type="button" onClick={() => onChoose('background')}>
            <strong>转入后台并新建对话</strong>
            <span>任务继续在后台运行，进度可在右侧活动中心查看，随时可以再切回。</span>
          </button>
        </div>

        <div className="modal-actions">
          <button className="secondary-button" type="button" onClick={onCancel}>取消</button>
        </div>
      </section>
    </div>
  );
}
