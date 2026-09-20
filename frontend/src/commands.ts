/**
 * 斜杠指令目录。
 *
 * 这里只负责「目录 + 匹配」，具体动作由 App 的 handlers 提供。两条原则：
 *   1. 面板里出现的每条指令都必须对应界面上真实可用的能力，不摆看得见却点不动的选项；
 *   2. 目录是唯一事实来源——App 用 `Record<SlashCommandName, () => void>` 实现分发，
 *      因此「加了指令却忘了接 handler」会在 tsc 阶段直接报错，而不是等用户点到才发现。
 */
export const SLASH_COMMANDS = [
  { name: '结束', description: '结束正在运行的工作流（当前会话与后台/定时会话）' },
  { name: '新会话', description: '清空当前对话，下一条消息开启新会话' },
  { name: '撤销', description: '撤销本次会话已完成的文件操作' },
  { name: '日志', description: '打开操作日志' },
  { name: '任务', description: '打开定时任务' },
  { name: '模型', description: '打开模型配置' },
  { name: '设置', description: '打开偏好设置' },
  { name: '帮助', description: '显示可用指令' },
] as const;

export type SlashCommand = (typeof SLASH_COMMANDS)[number];

/** 指令名的字面量联合，供 App 侧做穷尽分发。 */
export type SlashCommandName = SlashCommand['name'];

/**
 * 取出输入框里的指令过滤词；不是指令输入时返回 null。
 *
 * 以 `/` 开头即进入指令模式；正文里出现空白说明用户已经在写自然语言（如
 * "/结束 顺便把下载目录也清一下"），此时不再拦截，按普通消息发送。
 */
export function commandQuery(draft: string): string | null {
  const trimmed = draft.trim();
  if (!trimmed.startsWith('/')) return null;
  const body = trimmed.slice(1);
  return /\s/.test(body) ? null : body;
}

/** 按名称子串匹配指令；空查询返回全部（刚敲下 `/` 时展示完整目录）。 */
export function matchCommands(query: string): SlashCommand[] {
  const needle = query.trim().toLowerCase();
  if (!needle) return [...SLASH_COMMANDS];
  return SLASH_COMMANDS.filter((command) => command.name.toLowerCase().includes(needle));
}

/** 输入与某条指令完全同名时返回该指令，供 Enter 直接执行（不必先高亮）。 */
export function exactCommand(draft: string): SlashCommand | null {
  const query = commandQuery(draft);
  if (!query) return null;
  return SLASH_COMMANDS.find((command) => command.name === query) ?? null;
}

/** 指令名收窄，供 App 在分发前挡住未知输入。 */
export function isCommandName(candidate: string): candidate is SlashCommandName {
  return SLASH_COMMANDS.some((command) => command.name === candidate);
}

/** /帮助 的正文：与面板同源，避免两处各写一份。 */
export function helpText(): string {
  const lines = SLASH_COMMANDS.map((command) => `/${command.name} — ${command.description}`);
  return ['可用指令：', ...lines, '', '输入 / 可随时唤出指令面板，↑↓ 选择、Enter 执行、Esc 关闭。'].join('\n');
}
