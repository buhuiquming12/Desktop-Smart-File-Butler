import { useEffect, useMemo, useRef, useState, type FormEvent, type KeyboardEvent } from 'react';
import { commandQuery, exactCommand, matchCommands } from '../commands';
import type { ChatMessage, ConnectionState } from '../types';

interface ChatPanelProps {
  messages: ChatMessage[];
  connectionState: ConnectionState;
  busy: boolean;
  /** 正在运行的工作流数量（当前会话 + 后台/定时会话），决定「结束」是否可用。 */
  runningCount: number;
  onSend: (message: string) => void;
  /** 结束所有正在运行的工作流。 */
  onEnd: () => void;
  /** 执行斜杠指令（名字不含前导斜杠）。 */
  onCommand: (name: string) => void;
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

export function ChatPanel({ messages, connectionState, busy, runningCount, onSend, onEnd, onCommand }: ChatPanelProps) {
  const [draft, setDraft] = useState('');
  // 指令面板：输入以 / 开头即展开；Esc 只关闭本次，继续输入会重新展开。
  const [highlight, setHighlight] = useState(0);
  const [dismissed, setDismissed] = useState(false);
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages]);

  const query = commandQuery(draft);
  const matches = useMemo(() => (query === null ? [] : matchCommands(query)), [query]);
  const menuOpen = query !== null && !dismissed;
  const activeIndex = matches.length === 0 ? -1 : Math.min(highlight, matches.length - 1);

  const submit = (event?: FormEvent): void => {
    event?.preventDefault();
    const message = draft.trim();
    if (!message || busy) return;
    setDraft('');
    setDismissed(false);
    onSend(message);
  };

  const runCommand = (name: string): void => {
    setDraft('');
    setDismissed(false);
    setHighlight(0);
    onCommand(name);
  };

  const changeDraft = (value: string): void => {
    setDraft(value);
    setDismissed(false);
    setHighlight(0);
  };

  const handleKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>): void => {
    if (menuOpen && matches.length > 0) {
      if (event.key === 'ArrowDown') {
        event.preventDefault();
        setHighlight((current) => (current + 1) % matches.length);
        return;
      }
      if (event.key === 'ArrowUp') {
        event.preventDefault();
        setHighlight((current) => (current - 1 + matches.length) % matches.length);
        return;
      }
      if (event.key === 'Tab') {
        event.preventDefault();
        const picked = matches[activeIndex];
        if (picked) runCommand(picked.name);
        return;
      }
    }
    if (event.key === 'Escape') {
      if (menuOpen) {
        event.preventDefault();
        setDismissed(true);
      }
      return;
    }
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      if (menuOpen && matches.length > 0) {
        // 输入已是完整指令名就直接执行，否则执行当前高亮项。
        const picked = exactCommand(draft) ?? matches[activeIndex] ?? matches[0];
        if (picked) {
          runCommand(picked.name);
          return;
        }
      }
      // 没有任何匹配时不拦截：用户可能就是想发一条以 / 开头的普通消息。
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
            <p>我会先分析和规划，删除或覆盖等操作一定会请求你的确认。输入 / 可查看全部指令。</p>
            <div className="suggestions">
              {suggestedPrompts.map((prompt) => (
                <button key={prompt} type="button" onClick={() => changeDraft(prompt)}>{prompt}</button>
              ))}
            </div>
          </div>
        ) : messages.map((message) => (
          <article key={message.id} className={`message message--${message.role}${message.error ? ' message--error' : ''}`}>
            <div className="message__avatar" aria-hidden="true">{message.role === 'user' ? '你' : message.role === 'system' ? '·' : 'AI'}</div>
            <div className="message__body">
              <div className="message__meta">
                <strong>{message.role === 'user' ? '你' : message.role === 'system' ? '系统' : '文件管家'}</strong>
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
        {menuOpen ? (
          <div className="command-menu" role="listbox" aria-label="可用指令">
            {matches.length === 0 ? (
              <div className="command-menu__empty">没有匹配的指令，直接发送将作为普通消息处理。</div>
            ) : matches.map((command, index) => (
              <div
                key={command.name}
                role="option"
                aria-selected={index === activeIndex}
                className={`command-menu__item${index === activeIndex ? ' command-menu__item--active' : ''}`}
                onMouseEnter={() => setHighlight(index)}
                // 用 mousedown + preventDefault：既比 click 早，又不会把焦点从输入框抢走。
                onMouseDown={(event) => {
                  event.preventDefault();
                  runCommand(command.name);
                }}
              >
                <code>/{command.name}</code>
                <span>{command.description}</span>
              </div>
            ))}
          </div>
        ) : null}
        <textarea
          aria-label="输入文件整理指令"
          value={draft}
          onChange={(event) => changeDraft(event.target.value)}
          onKeyDown={handleKeyDown}
          placeholder="例如：整理下载目录，把图片按月份归档… 输入 / 查看指令"
          rows={2}
        />
        <div className="composer__footer">
          <span>Enter 发送 · Shift + Enter 换行 · / 指令</span>
          <div className="composer__actions">
            <button
              className="secondary-button send-button"
              type="button"
              onClick={onEnd}
              disabled={runningCount === 0}
              title={runningCount === 0 ? '当前没有正在运行的工作流' : '结束所有正在运行的工作流'}
            >
              结束{runningCount > 1 ? ` (${runningCount})` : ''} <span aria-hidden="true">■</span>
            </button>
            <button className="primary-button send-button" type="submit" disabled={!draft.trim() || busy}>
              发送 <span aria-hidden="true">→</span>
            </button>
          </div>
        </div>
      </form>
    </section>
  );
}
