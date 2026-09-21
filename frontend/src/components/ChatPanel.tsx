import { useEffect, useMemo, useRef, useState, type FormEvent, type KeyboardEvent } from 'react';
import { commandQuery, exactCommand, matchCommands } from '../commands';
import { isNearBottom, shouldAutoScroll } from '../state';
import type { ChatMessage, ConnectionState, SetupHints } from '../types';
import { TaskSummaryCard } from './TaskSummaryCard';
import type { SettingsTab } from './Settings';
import type { ToastKind } from './ToastStack';

interface ChatPanelProps {
  messages: ChatMessage[];
  connectionState: ConnectionState;
  busy: boolean;
  /** 正在运行的工作流数量（当前会话 + 后台/定时会话），决定「结束」是否可用。 */
  runningCount: number;
  /** 首次使用检查结果；关键配置缺失时在聊天页顶部显示引导卡片。 */
  setupHints?: SetupHints;
  onSend: (message: string) => void;
  /** 结束所有正在运行的工作流。 */
  onEnd: () => void;
  /** 执行斜杠指令（名字不含前导斜杠）。 */
  onCommand: (name: string) => void;
  /** 开始新对话：任务运行时会先弹出去向选择，空闲时直接清空。 */
  onNewConversation: () => void;
  onOpenSettings?: (tab: SettingsTab) => void;
  onRetryConnect?: () => void;
  onRetrySetup?: () => void;
  onNotify?: (content: string, kind?: ToastKind) => void;
  /** 撤销指定会话的文件操作（由任务结果卡触发）。 */
  onRollbackThread?: (threadId: string) => void;
}

const suggestedPrompts = [
  '扫描下载目录并按类型给出整理建议',
  '找出最近 30 天的大文件',
  '把 PDF 按主题分类，但先不要移动',
];

/** 输入草稿持久化键：刷新/切换会话后保留未发送内容。 */
const DRAFT_KEY = 'file-butler-chat-draft';

function readDraft(): string {
  try {
    return localStorage.getItem(DRAFT_KEY) ?? '';
  } catch {
    return '';
  }
}

function writeDraft(value: string): void {
  try {
    if (value) localStorage.setItem(DRAFT_KEY, value);
    else localStorage.removeItem(DRAFT_KEY);
  } catch {
    // 存储不可用时静默降级：草稿不持久化，不影响正常聊天。
  }
}

function connectionLabel(state: ConnectionState): string {
  switch (state) {
    case 'connected': return '后端已连接';
    case 'connecting': return '正在连接';
    case 'error': return '连接异常';
    default: return '后端未连接';
  }
}

export function ChatPanel({
  messages,
  connectionState,
  busy,
  runningCount,
  setupHints,
  onSend,
  onEnd,
  onCommand,
  onNewConversation,
  onOpenSettings,
  onRetryConnect,
  onRetrySetup,
  onNotify,
  onRollbackThread,
}: ChatPanelProps) {
  const [draft, setDraft] = useState(readDraft);
  // 指令面板：输入以 / 开头即展开；Esc 只关闭本次，继续输入会重新展开。
  const [highlight, setHighlight] = useState(0);
  const [dismissed, setDismissed] = useState(false);
  const endRef = useRef<HTMLDivElement>(null);
  const listRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  // 用户是否位于消息底部附近：是才自动跟随流式输出。
  const nearBottomRef = useRef(true);
  const [showScrollToBottom, setShowScrollToBottom] = useState(false);

  // 自动滚动：仅当用户位于底部附近才跟随；正在阅读历史时改为显示「有新内容」按钮。
  useEffect(() => {
    const el = listRef.current;
    if (!el) return;
    if (shouldAutoScroll(nearBottomRef.current, el.scrollTop, el.scrollHeight, el.clientHeight)) {
      el.scrollTop = el.scrollHeight;
      nearBottomRef.current = true;
      setShowScrollToBottom(false);
    } else {
      setShowScrollToBottom(true);
    }
  }, [messages]);

  // 输入框自动增高：内容变化时重算高度，最高 160px 后出现内部滚动。
  useEffect(() => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = 'auto';
    el.style.height = `${Math.min(el.scrollHeight, 160)}px`;
  }, [draft]);

  const handleScroll = (): void => {
    const el = listRef.current;
    if (!el) return;
    const near = isNearBottom(el.scrollTop, el.scrollHeight, el.clientHeight);
    nearBottomRef.current = near;
    setShowScrollToBottom(!near);
  };

  const scrollToBottom = (): void => {
    const el = listRef.current;
    if (!el) return;
    el.scrollTop = el.scrollHeight;
    nearBottomRef.current = true;
    setShowScrollToBottom(false);
  };

  const query = commandQuery(draft);
  const matches = useMemo(() => (query === null ? [] : matchCommands(query)), [query]);
  const menuOpen = query !== null && !dismissed;
  const activeIndex = matches.length === 0 ? -1 : Math.min(highlight, matches.length - 1);

  const clearDraft = (): void => {
    setDraft('');
    writeDraft('');
    setDismissed(false);
    setHighlight(0);
  };

  const submit = (event?: FormEvent): void => {
    event?.preventDefault();
    const message = draft.trim();
    if (!message || busy) return;
    clearDraft();
    onSend(message);
  };

  const runCommand = (name: string): void => {
    clearDraft();
    onCommand(name);
  };

  const changeDraft = (value: string): void => {
    setDraft(value);
    writeDraft(value);
    setDismissed(false);
    setHighlight(0);
  };

  const copyValue = (value: string): void => {
    if (typeof navigator.clipboard?.writeText !== 'function') {
      onNotify?.('当前环境不支持自动复制，请手动选择。', 'error');
      return;
    }
    void navigator.clipboard.writeText(value).then(
      () => onNotify?.('已复制路径。', 'success'),
      () => onNotify?.('复制失败，请手动选择。', 'error'),
    );
  };

  const revealPath = async (path: string): Promise<boolean> => {
    if (!window.desktop?.revealPath) {
      onNotify?.('当前环境无法打开所在目录。', 'error');
      return false;
    }
    const ok = await window.desktop.revealPath(path);
    if (!ok) onNotify?.('无法打开该目录：路径不存在或不受支持。', 'error');
    return ok;
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

  // 首次使用检查：任一关键项明确缺失（false）时显示引导卡片；null 视为“尚未确认”，不打扰。
  const setupMissing = setupHints?.checked === true
    && (setupHints.backendOk === false || setupHints.modelOk === false || setupHints.sandboxOk === false || setupHints.ocrOk === false);
  const connectionBroken = connectionState === 'disconnected' || connectionState === 'error';
  return (
    <section className="panel chat-panel" aria-label="智能对话">
      <header className="panel-header">
        <div>
          <p className="eyebrow">AI 文件助手</p>
          <h1>今天想整理什么？</h1>
        </div>
        <div className="panel-header__actions">
          <span className={`connection connection--${connectionState}`}>
            <i aria-hidden="true" />{connectionLabel(connectionState)}
          </span>
          <button
            className="secondary-button new-chat-button"
            type="button"
            onClick={onNewConversation}
            title={busy ? '任务运行中：可选择结束任务或转入后台后新建对话' : '开始新对话，下一条消息将开启全新会话'}
          >
            <span aria-hidden="true">＋</span>新对话
          </button>
        </div>
      </header>

      {setupMissing && (
        <div className="setup-card" role="region" aria-label="需要完成的设置">
          <div className="setup-card__title"><strong>还差几步，即可开始整理</strong><span>完成这些设置后，我就能安全地处理文件。</span></div>
          <div className="setup-card__items">
            {setupHints.backendOk === false && (
              <div className="setup-card__item">
                <span>本地服务未连接</span>
                <div className="setup-card__actions">
                  <button className="text-button" type="button" onClick={() => onRetryConnect?.()}>重新连接</button>
                  <button className="text-button" type="button" onClick={() => onOpenSettings?.('general')}>连接设置</button>
                </div>
              </div>
            )}
            {setupHints.modelOk === false && (
              <div className="setup-card__item">
                <span>模型尚未配置</span>
                <button className="text-button" type="button" onClick={() => onOpenSettings?.('model')}>去配置</button>
              </div>
            )}
            {setupHints.sandboxOk === false && (
              <div className="setup-card__item">
                <span>尚未授权可操作的文件夹</span>
                <button className="text-button" type="button" onClick={() => onOpenSettings?.('general')}>去授权</button>
              </div>
            )}
            {setupHints.ocrOk === false && (
              <div className="setup-card__item">
                <span>OCR 组件未就绪</span>
                <button className="text-button" type="button" onClick={() => onOpenSettings?.('general')}>查看说明</button>
              </div>
            )}
          </div>
          <button className="setup-card__retry" type="button" onClick={() => onRetrySetup?.()}>重新检查</button>
        </div>
      )}

      {connectionBroken && (
        <div className="connection-banner" role="status" aria-live="polite">
          <span>
            {connectionState === 'error'
              ? '与本地服务的连接出错，应用会自动重试。'
              : '与本地服务的连接已断开，正在自动重试。'}
          </span>
          <div className="connection-banner__actions">
            <button className="text-button" type="button" onClick={() => onRetryConnect?.()}>立即重试</button>
            <button className="text-button" type="button" onClick={() => onOpenSettings?.('general')}>连接设置</button>
          </div>
        </div>
      )}

      <div className="message-list" ref={listRef} onScroll={handleScroll} aria-live="polite">
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
              {message.summary && message.role === 'assistant' && !message.pending && (
                <TaskSummaryCard
                  summary={message.summary}
                  threadId={message.threadId ?? ''}
                  onCopy={copyValue}
                  onReveal={revealPath}
                  onRollback={message.threadId ? () => { onRollbackThread?.(message.threadId ?? ''); } : undefined}
                  onNotify={onNotify}
                />
              )}
            </div>
          </article>
        ))}
        <div ref={endRef} />
      </div>
      {showScrollToBottom && (
        <button className="chat-scroll-to-bottom" type="button" onClick={scrollToBottom}>
          有新内容 <span aria-hidden="true">↓</span>
        </button>
      )}

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
          ref={textareaRef}
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
