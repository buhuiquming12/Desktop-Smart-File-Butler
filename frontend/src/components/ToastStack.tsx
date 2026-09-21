import type { CSSProperties } from 'react';

export type ToastKind = 'success' | 'error' | 'info';

export interface ToastItem {
  id: number;
  content: string;
  kind: ToastKind;
}

interface ToastStackProps {
  toasts: ToastItem[];
}

const kindLabel: Record<ToastKind, string> = {
  success: '完成',
  error: '出错了',
  info: '提示',
};

export function ToastStack({ toasts }: ToastStackProps) {
  if (toasts.length === 0) return null;
  return (
    <div className="toast-stack" role="status" aria-live="polite">
      {toasts.map((toast) => (
        <div key={toast.id} className={`toast toast--${toast.kind}`} style={{ '--toast-index': toasts.indexOf(toast) } as CSSProperties}>
          <strong>{kindLabel[toast.kind]}</strong>
          <span>{toast.content}</span>
        </div>
      ))}
    </div>
  );
}
