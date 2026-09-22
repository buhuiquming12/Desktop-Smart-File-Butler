import { coordinateSingleInstance } from './singleInstance.js';

const calls = { quit: 0, start: 0, registered: 0 };
function assertEqual(actual: number, expected: number, message: string): void {
  if (actual !== expected) throw new Error(message);
}
coordinateSingleInstance(false, {
  quit: () => { calls.quit += 1; },
  onSecondInstance: () => { calls.registered += 1; },
  focusExisting: () => undefined,
  startPrimary: () => { calls.start += 1; },
});
assertEqual(calls.quit, 1, '第二实例没有退出');
assertEqual(calls.start, 0, '第二实例仍触发了 primary boot/backend spawn');
assertEqual(calls.registered, 0, '第二实例注册了不应存在的 handler');

coordinateSingleInstance(true, {
  quit: () => { calls.quit += 1; },
  onSecondInstance: (focus) => { calls.registered += 1; focus(); },
  focusExisting: () => undefined,
  startPrimary: () => { calls.start += 1; },
});
assertEqual(calls.start, 1, '主实例未启动');
assertEqual(calls.registered, 1, '主实例未注册聚焦处理器');
