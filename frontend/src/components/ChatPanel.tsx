import { useEffect, useRef, useState, type FormEvent, type KeyboardEvent } from 'react';
import type { ChatMessage, ConnectionState } from '../types';

interface ChatPanelProps {
  messages: ChatMessage[];
  connectionState: ConnectionState;
  busy: boolean;
  onSend: (message: string) => void;
  onStop: () => void;
}

const suggestedPrompts = [
  '扫描下载目录并按类型给出整理建议',
  '找出最近 30 天的大文件',
  '把 PDF 按主题分类，但先不要移动',
];

function connectionLabel(state: ConnectionState): string {
  switch (state) {
    case 'connected': return '后端已连接';
    case 'connecting': return '正在连接';
    case 'error': return '连接异常';
    default: return '后端未连接';
  }
}

export function ChatPanel({ messages, connectionState, busy, onSend, onStop }: ChatPanelProps) {
  const [draft, setDraft] = useState('');
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages]);

  const submit = (event?: FormEvent): void => {
    event?.preventDefault();
    const message = draft.trim();
    if (!message || busy) return;
    setDraft('');
    onSend(message);
  };

  const handleKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>): void => {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      submit();
    }
  };

  return (
    <section className="panel chat-panel" aria-label="智能对话">
      <header className="panel-header">
        <div>
          <p className="eyebrow">AI 文件助手</p>
          <h1>今天想整理什么？</h1>
        </div>
        <span className={`connection connection--${connectionState}`}>
          <i aria-hidden="true" />{connectionLabel(connectionState)}
        </span>
      </header>

      <div className="message-list" aria-live="polite">
        {messages.length === 0 ? (
          <div className="empty-chat">
            <div className="empty-chat__icon" aria-hidden="true">✦</div>
            <h2>用一句话安排文件工作</h2>
            <p>我会先分析和规划，删除或覆盖等操作一定会请求你的确认。</p>
            <div className="suggestions">
              {suggestedPrompts.map((prompt) => (
                <button key={prompt} type="button" onClick={() => setDraft(prompt)}>{prompt}</button>
              ))}
            </div>
          </div>
        ) : messages.map((message) => (
          <article key={message.id} className={`message message--${message.role}${message.error ? ' message--error' : ''}`}>
            <div className="message__avatar" aria-hidden="true">{message.role === 'user' ? '你' : 'AI'}</div>
            <div className="message__body">
              <div className="message__meta">
                <strong>{message.role === 'user' ? '你' : '文件管家'}</strong>
                <time>{new Date(message.timestamp).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })}</time>
              </div>
              <p>{message.content || (message.pending ? '正在思考…' : '')}</p>
              {message.pending && <span className="typing"><i /><i /><i /></span>}
            </div>
          </article>
        ))}
        <div ref={endRef} />
      </div>

      <form className="composer" onSubmit={submit}>
        <textarea
          aria-label="输入文件整理指令"
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={handleKeyDown}
          placeholder="例如：整理下载目录，把图片按月份归档…"
          rows={2}
        />
        <div className="composer__footer">
          <span>Enter 发送 · Shift + Enter 换行</span>
          {busy ? (
            <button className="secondary-button send-button" type="button" onClick={onStop}>
              停止 <span aria-hidden="true">■</span>
            </button>
          ) : (
            <button className="primary-button send-button" type="submit" disabled={!draft.trim()}>
              发送 <span aria-hidden="true">→</span>
            </button>
          )}
        </div>
      </form>
    </section>
  );
}
