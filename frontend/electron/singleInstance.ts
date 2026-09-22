export interface SingleInstanceHooks {
  quit: () => void;
  onSecondInstance: (focusExisting: () => void) => void;
  focusExisting: () => void;
  startPrimary: () => void;
}

/** The losing process exits before boot(), so it cannot remove session.json or spawn a backend. */
export function coordinateSingleInstance(acquired: boolean, hooks: SingleInstanceHooks): void {
  if (!acquired) {
    hooks.quit();
    return;
  }
  hooks.onSecondInstance(hooks.focusExisting);
  hooks.startPrimary();
}
