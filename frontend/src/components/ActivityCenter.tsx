import { useState } from 'react';
import type { ActivityItem } from '../types';

interface ActivityCenterProps {
  items: ActivityItem[];
  onSwitch: (threadId: string) => void;
  onStop: (threadId: string) => void;
  onViewResult: (item: ActivityItem) => void;
  /** 复制结果的第一个路径（查看结果的具体动作）。 */
  onCopyResult: (item: ActivityItem) => void;
}

const groupTitles: Array<{ key: ActivityItem['kind']; title: string }> = [
  { key: 'current', title: '当前会话' },
  { key: 'background', title: '后台会话' },
  { key: 'scheduled', title: '定时任务实例' },
  { key: 'approval', title: '待审批任务' },
];

export function ActivityCenter({ items, onSwitch, onStop, onViewResult, onCopyResult }: ActivityCenterProps) {
  const [expandedId, setExpandedId] = useState<string | null>(null);

  return (
    <section className="panel task-board activity-center" aria-label="活动中心">
      <header className="panel-header panel-header--compact">
        <div>
          <p className="eyebrow">实时动态</p>
          <h2>活动中心</h2>
        </div>
        {items.length > 0 && <span className="counter" title="活动项数">{items.length}</span>}
      </header>

      <div className="activity-list">
        {items.length === 0 ? (
          <div className="empty-state">
            <span className="empty-state__graphic" aria-hidden="true">✓</span>
            <strong>暂无活动</strong>
            <p>发送整理指令后，会话与任务进度会显示在这里。</p>
          </div>
        ) : groupTitles.map((group) => {
          const groupItems = items.filter((item) => item.kind === group.key);
          if (groupItems.length === 0) return null;
          return (
            <div className="activity-group" key={group.key}>
              <h3 className="activity-group__title">{group.title}<span>{groupItems.length}</span></h3>
              {groupItems.map((item) => (
                <article className={`activity-card${item.active ? ' activity-card--active' : ''}`} key={item.id}>
                  <div className="activity-card__topline">
                    <span className={`badge badge--${item.status}`}>{item.statusLabel}</span>
                    <strong>{item.source}</strong>
                    <time title={item.updatedAt}>{new Date(item.updatedAt).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })}</time>
                  </div>
                  <p className="activity-card__summary">{item.summary}</p>

                  {expandedId === item.id && (
                    <div className="activity-card__detail">
                      {item.approvalId ? (
                        <dl className="approval-summary">
                          <div><dt>审批编号</dt><dd>{item.approvalId}</dd></div>
                          <div><dt>来源会话</dt><dd title={item.threadId}>{item.threadId}</dd></div>
                        </dl>
                      ) : (
                        <p className="muted">会话 {item.threadId}</p>
                      )}
                      {item.kind === 'approval' && <p className="activity-card__note">该操作正在等待确认，审批弹窗中可直接处理。</p>}
                    </div>
                  )}

                  <div className="activity-card__actions">
                    {item.hasDetail && (
                      <button type="button" className="text-button" onClick={() => setExpandedId(expandedId === item.id ? null : item.id)}>
                        {expandedId === item.id ? '收起详情' : '查看详情'}
                      </button>
                    )}
                    {item.switchable && (
                      <button type="button" className="text-button" onClick={() => onSwitch(item.threadId)}>切换到该会话</button>
                    )}
                    {item.running && (
                      <button type="button" className="text-button text-button--danger" onClick={() => onStop(item.threadId)}>停止任务</button>
                    )}
                    {(item.status === 'success' || item.status === 'failed') && (
                      <>
                        <button type="button" className="text-button" onClick={() => onViewResult(item)}>查看结果</button>
                        <button type="button" className="text-button" onClick={() => onCopyResult(item)}>复制结果</button>
                      </>
                    )}
                  </div>
                </article>
              ))}
            </div>
          );
        })}
      </div>
    </section>
  );
}
