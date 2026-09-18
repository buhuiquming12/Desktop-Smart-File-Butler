import { contextBridge } from 'electron';

export interface DesktopBridge {
  platform: NodeJS.Platform;
  versions: Readonly<{
    electron: string;
    chrome: string;
    node: string;
  }>;
}

const desktopBridge: DesktopBridge = Object.freeze({
  platform: process.platform,
  versions: Object.freeze({
    electron: process.versions.electron,
    chrome: process.versions.chrome,
    node: process.versions.node,
  }),
});

contextBridge.exposeInMainWorld('desktop', desktopBridge);
