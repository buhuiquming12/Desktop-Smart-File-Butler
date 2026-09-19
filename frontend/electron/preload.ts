import { contextBridge, ipcRenderer } from 'electron';

export interface DesktopBridge {
  platform: NodeJS.Platform;
  /** 后端注入的一次性会话令牌；跨源网页无法取得，用于 REST/WS 鉴权（P0-2）。 */
  sessionToken: string;
  /** 后端基址（含端口），由主进程注入。 */
  backendUrl: string;
  /** 打开原生目录选择器，返回所选目录绝对路径；取消则返回 null（P1-3）。 */
  chooseDirectory: () => Promise<string | null>;
  versions: Readonly<{
    electron: string;
    chrome: string;
    node: string;
  }>;
}

// 通过同步 IPC 取会话信息：主进程可能先加载 loading 页、待后端就绪后再 reload 本页，
// reload 会重跑 preload，从而拿到最新令牌（sandbox 预加载无法读文件，故用 IPC）。
let session: { token?: string; backendUrl?: string } = {};
try {
  session = (ipcRenderer.sendSync('butler:session') as { token?: string; backendUrl?: string }) || {};
} catch {
  session = {};
}

const desktopBridge: DesktopBridge = Object.freeze({
  platform: process.platform,
  sessionToken: session.token ?? '',
  backendUrl: session.backendUrl ?? '',
  chooseDirectory: () => ipcRenderer.invoke('butler:choose-directory') as Promise<string | null>,
  versions: Object.freeze({
    electron: process.versions.electron,
    chrome: process.versions.chrome,
    node: process.versions.node,
  }),
});

contextBridge.exposeInMainWorld('desktop', desktopBridge);
