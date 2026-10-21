/**
 * 新能力（预览 / 重复文件 / 搜索 / 回收站）的纯函数与格式化。
 *
 * 抽成独立模块是为了能直接单测“选择变化如何影响执行数量”“格式化是否失真”这类
 * 状态逻辑，而不是把它们散落在组件里只能靠肉眼点界面验证。
 */
import type {
  DuplicateGroup,
  PreviewCounts,
  PreviewItem,
  PreviewOp,
  TrashEntry,
} from './types';

export function formatBytes(value: number | null | undefined): string {
  const bytes = typeof value === 'number' && Number.isFinite(value) ? Math.max(0, value) : 0;
  if (bytes < 1024) return `${bytes} B`;
  const units = ['KB', 'MB', 'GB', 'TB'];
  let size = bytes / 1024;
  let index = 0;
  while (size >= 1024 && index < units.length - 1) {
    size /= 1024;
    index += 1;
  }
  return `${size >= 100 ? size.toFixed(0) : size.toFixed(1)} ${units[index]}`;
}

export function formatDateTime(value: string): string {
  if (!value) return '—';
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  const pad = (input: number): string => String(input).padStart(2, '0');
  return `${parsed.getFullYear()}-${pad(parsed.getMonth() + 1)}-${pad(parsed.getDate())} ${pad(parsed.getHours())}:${pad(parsed.getMinutes())}`;
}

/** 勾选/取消勾选；返回新 Set，便于 React 状态比较。 */
export function toggleSelection<T>(selected: ReadonlySet<T>, value: T): Set<T> {
  const next = new Set<T>(selected);
  if (next.has(value)) next.delete(value);
  else next.add(value);
  return next;
}

/** 当前页全选/全不选：只影响本页可执行（ready）的条目。 */
export function togglePageSelection(
  selected: ReadonlySet<string>,
  items: PreviewItem[],
): Set<string> {
  const readyIds = items.filter((item) => item.status === 'ready').map((item) => item.item_id);
  const allSelected = readyIds.length > 0 && readyIds.every((id) => selected.has(id));
  const next = new Set(selected);
  for (const id of readyIds) {
    if (allSelected) next.delete(id);
    else next.add(id);
  }
  return next;
}

/** 执行按钮上的数量必须来自实际勾选，而不是“预览总数”。 */
export function selectedCount(selected: ReadonlySet<string>, items: PreviewItem[]): number {
  return items.filter((item) => item.status === 'ready' && selected.has(item.item_id)).length;
}

/**
 * 只保留“当前仍被勾选”的类别覆盖。
 *
 * 用户改了类别后又取消勾选，残留的 item_id 会让后端判定“覆盖不属于本次选择”
 * 而整批拒绝，表现为提交被卡住；提交前必须按勾选过滤，而不是原样发送整张表。
 */
export function overridesForSelection(
  overrides: Readonly<Record<string, string>>,
  selected: ReadonlySet<string>,
): Record<string, string> {
  const result: Record<string, string> = {};
  for (const [itemId, value] of Object.entries(overrides)) {
    if (!selected.has(itemId)) continue;
    const trimmed = (value ?? '').trim();
    if (trimmed) result[itemId] = trimmed;
  }
  return result;
}

/** 预览页脚的一句话统计：明确区分可执行、冲突与跳过。 */
export function previewSummary(counts: PreviewCounts, selected: number): string {
  const parts = [
    `共 ${counts.total} 项`,
    `可执行 ${counts.ready} 项`,
    `已选 ${selected} 项`,
  ];
  if (counts.conflict > 0) parts.push(`冲突 ${counts.conflict} 项`);
  if (counts.skipped > 0) parts.push(`跳过 ${counts.skipped} 项`);
  if (counts.fallback > 0) parts.push(`按内容失败回退 ${counts.fallback} 项`);
  return parts.join(' · ');
}

export const previewOpLabels: Record<PreviewOp, string> = {
  move: '移动',
  rename: '重命名',
  classify: '分类',
  delete: '删除',
};

/**
 * 执行前的本地提示：删除与超阈值都需要二次确认（后端同样会拒绝未确认的请求）。
 * 返回 null 表示无需额外确认。
 */
export function executionWarning(
  op: PreviewOp,
  selected: number,
  threshold: number,
): string | null {
  if (op === 'delete') {
    return `将把选中的 ${selected} 个文件移入项目回收站（可恢复），需要你的确认。`;
  }
  if (selected > threshold) {
    return `本次将变更 ${selected} 个文件，超过批量阈值 ${threshold}，需要你的确认。`;
  }
  return null;
}

/** 重复组里当前保留项；用户改选后以 overrides 为准。 */
export function keepPathFor(group: DuplicateGroup, overrides: Record<string, string>): string {
  const override = overrides[group.group_id];
  if (override && group.files.some((file) => file.path === override)) return override;
  if (group.files.some((file) => file.path === group.suggested_keep)) return group.suggested_keep;
  const fallback = group.files[0];
  return fallback ? fallback.path : '';
}

/** 组装清理请求的 keep_map；未改选的组用建议值，避免前端自己猜。 */
export function buildKeepMap(
  groups: DuplicateGroup[],
  overrides: Record<string, string>,
): Record<string, string> {
  const map: Record<string, string> = {};
  for (const group of groups) {
    const keep = keepPathFor(group, overrides);
    if (keep) map[group.group_id] = keep;
  }
  return map;
}

/** 每组将删除的数量（用于按钮文案）。 */
export function plannedDeletions(groups: DuplicateGroup[], overrides: Record<string, string>): number {
  return groups.reduce((total, group) => {
    const keep = keepPathFor(group, overrides);
    return total + group.files.filter((file) => file.path !== keep).length;
  }, 0);
}

/** 回收站条目能否恢复：回收站文件必须仍在。 */
export function isRestorable(entry: TrashEntry): boolean {
  return Boolean(entry.trash_path) && entry.size >= 0;
}

/** 恢复结果的一句话汇总（含冲突改名提示）。 */
export function restoreSummary(ok: number, skipped: number, failed: number): string {
  return `恢复完成：成功 ${ok}，跳过 ${skipped}，失败 ${failed}`;
}

/** 合并分页结果：按 item_id 去重，保证翻页不会重复渲染。 */
export function mergeItems(existing: PreviewItem[], incoming: PreviewItem[]): PreviewItem[] {
  const seen = new Set(existing.map((item) => item.item_id));
  return [...existing, ...incoming.filter((item) => !seen.has(item.item_id))];
}

/** 进度条百分比（0–100 整数），用于索引/重复扫描。 */
export function progressPercent(done: number, total: number): number {
  if (!Number.isFinite(done) || !Number.isFinite(total) || total <= 0) return 0;
  return Math.max(0, Math.min(100, Math.round((done / total) * 100)));
}
