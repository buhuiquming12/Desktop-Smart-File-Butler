import { contextBridge, ipcRenderer } from 'electron';

export interface DesktopBridge {
  platform: NodeJS.Platform;
  /** 后端注入的一次性会话令牌；跨源网页无法取得，用于 REST/WS 鉴权（P0-2）。 */
  sessionToken: string;
  /** 后端基址（含端口），由主进程从会话文件读取后注入。 */
  backendUrl: string;
  /** 打开原生目录选择器，返回所选目录绝对路径；取消则返回 null（P1-3）。 */
  chooseDirectory: () => Promise<string | null>;
  versions: Readonly<{
    electron: string;
    chrome: string;
    node: string;
  }>;
}

/** 从主进程注入的 additionalArguments 中读取 --butler-xxx=... 值。 */
function readArg(prefix: string): string {
  const hit = process.argv.find((arg) => arg.startsWith(prefix));
  return hit ? hit.slice(prefix.length) : '';
}

const desktopBridge: DesktopBridge = Object.freeze({
  platform: process.platform,
  sessionToken: readArg('--butler-token='),
  backendUrl: readArg('--butler-backend='),
  chooseDirectory: () => ipcRenderer.invoke('butler:choose-directory') as Promise<string | null>,
  versions: Object.freeze({
    electron: process.versions.electron,
    chrome: process.versions.chrome,
    node: process.versions.node,
  }),
});

contextBridge.exposeInMainWorld('desktop', desktopBridge);
