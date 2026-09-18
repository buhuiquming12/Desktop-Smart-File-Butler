import type { TaskItem, TaskStatus } from '../types';

interface TaskBoardProps {
  tasks: TaskItem[];
  onClear: () => void;
}

const statusText: Record<TaskStatus, string> = {
  pending: '等待',
  running: '执行中',
  success: '完成',
  failed: '失败',
  waiting: '待审批',
};

export function TaskBoard({ tasks, onClear }: TaskBoardProps) {
  const activeCount = tasks.filter((task) => task.status === 'running' || task.status === 'waiting').length;

  return (
    <section className="panel task-board">
      <header className="panel-header panel-header--compact">
        <div>
          <p className="eyebrow">任务动态</p>
          <h2>任务看板</h2>
        </div>
        <span className="counter" title="活跃任务数">{activeCount}</span>
      </header>

      <div className="task-list">
        {tasks.length === 0 ? (
          <div className="empty-state">
            <span className="empty-state__graphic" aria-hidden="true">✓</span>
            <strong>暂无任务</strong>
            <p>发送整理指令后，执行步骤会显示在这里。</p>
          </div>
        ) : tasks.map((task) => (
          <article className="task-card" key={task.id}>
            <div className={`task-status task-status--${task.status}`} aria-hidden="true" />
            <div className="task-card__content">
              <div className="task-card__topline">
                <strong>{task.title}</strong>
                <span className={`badge badge--${task.status}`}>{statusText[task.status]}</span>
              </div>
              <p>{task.detail}</p>
              <time>{new Date(task.updatedAt).toLocaleTimeString('zh-CN')}</time>
            </div>
          </article>
        ))}
      </div>

      {tasks.length > 0 && (
        <button type="button" className="text-button task-clear" onClick={onClear}>清除已结束任务</button>
      )}
    </section>
  );
}
