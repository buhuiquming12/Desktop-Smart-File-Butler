/** 新能力的纯函数测试：选择语义、保留项映射、格式化与进度。 */
import {
  buildKeepMap,
  executionWarning,
  formatBytes,
  formatDateTime,
  keepPathFor,
  mergeItems,
  overridesForSelection,
  plannedDeletions,
  previewSummary,
  progressPercent,
  selectedCount,
  togglePageSelection,
  toggleSelection,
} from './features.js';
import type { DuplicateGroup, PreviewItem } from './types';

function equal(actual: unknown, expected: unknown, message: string): void {
  if (actual !== expected) {
    throw new Error(`${message}（期望 ${String(expected)}，实际 ${String(actual)}）`);
  }
}

function deepEqual(actual: unknown, expected: unknown, message: string): void {
  if (JSON.stringify(actual) !== JSON.stringify(expected)) {
    throw new Error(`${message}（期望 ${JSON.stringify(expected)}，实际 ${JSON.stringify(actual)}）`);
  }
}

function ok(value: boolean, message: string): void {
  if (!value) throw new Error(message);
}

function previewItem(overrides: Partial<PreviewItem> = {}): PreviewItem {
  return {
    item_id: overrides.item_id ?? 'id-1',
    source: overrides.source ?? 'C:/inbox/a.txt',
    name: overrides.name ?? 'a.txt',
    op: overrides.op ?? 'move',
    size: overrides.size ?? 10,
    modified: overrides.modified ?? '2024-01-01T00:00:00',
    target: overrides.target ?? 'C:/out/a.txt',
    target_name: overrides.target_name ?? 'a.txt',
    status: overrides.status ?? 'ready',
    reason: overrides.reason ?? '',
    category: overrides.category ?? '',
    basis: overrides.basis ?? '',
    fallback: overrides.fallback ?? false,
    ...overrides,
  };
}

function group(overrides: Partial<DuplicateGroup> = {}): DuplicateGroup {
  return {
    group_id: overrides.group_id ?? 'g1',
    hash: 'hash',
    size: 100,
    count: 2,
    distinct_inodes: 2,
    reclaimable: 100,
    suggested_keep: overrides.suggested_keep ?? 'C:/a.txt',
    keep_reason: '修改时间最新',
    files: overrides.files ?? [
      { path: 'C:/a.txt', name: 'a.txt', size: 100, modified: '', hardlink_of: '', keep: true, delete_by_default: false },
      { path: 'C:/b.txt', name: 'b.txt', size: 100, modified: '', hardlink_of: '', keep: false, delete_by_default: true },
    ],
    ...overrides,
  };
}

// ---- 选择 ----

const selected = toggleSelection(new Set<string>(), 'id-1');
equal(selected.has('id-1'), true, '勾选后应包含该条目');
equal(toggleSelection(selected, 'id-1').has('id-1'), false, '再次点击应取消勾选');
equal(toggleSelection(selected, 'id-1').size, 0, '取消后集合应为空');
equal(selected.size, 1, 'toggleSelection 不应修改入参');

const items = [
  previewItem({ item_id: 'a', status: 'ready' }),
  previewItem({ item_id: 'b', status: 'ready' }),
  previewItem({ item_id: 'c', status: 'conflict' }),
];
equal(selectedCount(new Set(['a', 'c']), items), 1, '冲突项不计入可执行数量');
equal(selectedCount(new Set(['a', 'b']), items), 2, '可执行项计数');

const all = togglePageSelection(new Set<string>(), items);
deepEqual([...all].sort(), ['a', 'b'], '全选只应选中 ready 项');
equal(togglePageSelection(all, items).size, 0, '再次全选应清空本页 ready 项');
const keepOther = togglePageSelection(new Set(['z']), items);
equal(keepOther.has('z'), true, '全选不应清掉其他页的选择');

// 取消勾选后残留的类别覆盖不能再提交：后端会判定“覆盖不属于本次选择”并整批拒绝。
deepEqual(
  overridesForSelection({ a: '合同', b: '发票' }, new Set(['a'])),
  { a: '合同' },
  '只提交仍被勾选的类别覆盖',
);
deepEqual(
  overridesForSelection({ a: '  ', b: '发票' }, new Set(['a', 'b'])),
  { b: '发票' },
  '空类别不应提交',
);

// ---- 预览统计与确认提示 ----

equal(
  previewSummary({ total: 10, ready: 8, conflict: 1, skipped: 1, fallback: 2 }, 5),
  '共 10 项 · 可执行 8 项 · 已选 5 项 · 冲突 1 项 · 跳过 1 项 · 按内容失败回退 2 项',
  '统计文案',
);
equal(executionWarning('move', 3, 20), null, '阈值内移动无需额外确认');
ok(executionWarning('move', 25, 20)?.includes('超过批量阈值 20') === true, '超阈值必须提示');
ok(executionWarning('delete', 1, 20)?.includes('回收站') === true, '删除必须提示回收站语义');

// ---- 重复组保留项 ----

const first = group();
equal(keepPathFor(first, {}), 'C:/a.txt', '默认使用保留建议');
equal(keepPathFor(first, { g1: 'C:/b.txt' }), 'C:/b.txt', '用户改选后应生效');
equal(keepPathFor(first, { g1: 'C:/not-in-group' }), 'C:/a.txt', '非法改选回退到建议值');
deepEqual(buildKeepMap([first], {}), { g1: 'C:/a.txt' }, 'keep_map 组装');
equal(plannedDeletions([first], {}), 1, '默认删除 1 份');
equal(plannedDeletions([first], { g1: 'C:/b.txt' }), 1, '换保留项后删除数量仍为 1');
const single = group({
  files: [
    { path: 'C:/a.txt', name: 'a.txt', size: 100, modified: '', hardlink_of: '', keep: true, delete_by_default: false },
  ],
});
equal(plannedDeletions([single], {}), 0, '只有一份时不删除');

// ---- 格式化 ----

equal(formatBytes(0), '0 B', '0 字节');
equal(formatBytes(1023), '1023 B', '1023 字节');
equal(formatBytes(1024), '1.0 KB', '1 KB');
equal(formatBytes(1024 * 1024 * 3), '3.0 MB', '3 MB');
equal(formatBytes(Number.NaN), '0 B', '非法数值不应显示 NaN');
equal(formatBytes(-5), '0 B', '负数按 0 处理');
equal(formatDateTime(''), '—', '空时间');
equal(formatDateTime('not-a-date'), 'not-a-date', '非法时间原样展示');

// ---- 合并与进度 ----

const merged = mergeItems([previewItem({ item_id: 'a' })], [
  previewItem({ item_id: 'a', name: 'dup.txt' }),
  previewItem({ item_id: 'b' }),
]);
equal(merged.length, 2, '分页合并按 item_id 去重');
equal(merged[0]?.name, 'a.txt', '已存在条目不被覆盖');
equal(progressPercent(0, 0), 0, '未知总量时进度为 0');
equal(progressPercent(5, 10), 50, '一半进度');
equal(progressPercent(50, 10), 100, '进度上限 100');
