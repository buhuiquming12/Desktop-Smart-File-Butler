import { app, BrowserWindow, shell } from 'electron';
import { fileURLToPath } from 'node:url';
import { readFile } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';

const currentDirectory = path.dirname(fileURLToPath(import.meta.url));
const developmentUrl = process.env.VITE_DEV_SERVER_URL;
// 生产模式下前端由后端 StaticFiles 同源托管（见 P0-1），Electron 直接加载后端 URL，
// 使渲染进程 origin 与 /api、/ws 一致，彻底摆脱 CORS 与 opaque(null) origin。
const fallbackBackendUrl = (process.env.BUTLER_BACKEND_URL ?? 'http://127.0.0.1:8000').replace(/\/+$/, '');

// 后端启动时把 { token, host, port } 写入该会话文件（见 backend P0-2）。
// 主进程读取后经 preload 注入渲染进程，供 REST/WS 鉴权。两端约定同一路径。
function sessionFilePath(): string {
  return process.env.BUTLER_SESSION_FILE ?? path.join(os.homedir(), '.desktop-smart-file-butler', 'session.json');
}

interface SessionInfo {
  token: string;
  backendUrl: string;
}

async function readSession(): Promise<SessionInfo | null> {
  try {
    const raw = await readFile(sessionFilePath(), 'utf-8');
    const parsed = JSON.parse(raw) as { token?: string; host?: string; port?: number };
    if (!parsed.token) return null;
    const host = parsed.host && parsed.host !== '0.0.0.0' ? parsed.host : '127.0.0.1';
    const backendUrl = parsed.port ? `http://${host}:${parsed.port}` : fallbackBackendUrl;
    return { token: parsed.token, backendUrl };
  } catch {
    return null;
  }
}

// 后端与 Electron 进程解耦（见 P2 打包链路），启动顺序不确定；轮询等待会话文件出现。
async function waitForSession(timeoutMs = 15_000): Promise<SessionInfo | null> {
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    const session = await readSession();
    if (session) return session;
    if (Date.now() >= deadline) return null;
    await new Promise((resolve) => setTimeout(resolve, 300));
  }
}

function isTrustedExternalUrl(rawUrl: string): boolean {
  try {
    const url = new URL(rawUrl);
    return url.protocol === 'https:' || url.protocol === 'http:';
  } catch {
    return false;
  }
}

function createWindow(appUrl: string, token: string, backendUrl: string): void {
  const window = new BrowserWindow({
    width: 1240,
    height: 820,
    minWidth: 980,
    minHeight: 680,
    backgroundColor: '#f5f7fb',
    title: '桌面智能文件管家',
    show: false,
    autoHideMenuBar: true,
    webPreferences: {
      preload: path.join(currentDirectory, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      webSecurity: true,
      // 令牌与后端地址经 argv 注入 preload；不走 IPC，避免渲染进程主动索取。
      additionalArguments: [`--butler-token=${token}`, `--butler-backend=${backendUrl}`],
    },
  });

  window.once('ready-to-show', () => window.show());
  window.webContents.setWindowOpenHandler(({ url }) => {
    if (isTrustedExternalUrl(url)) {
      void shell.openExternal(url);
    }
    return { action: 'deny' };
  });
  window.webContents.on('will-navigate', (event, url) => {
    // 仅允许在应用自身 origin 内导航；跨源导航一律拦截（外链走 openExternal）。
    try {
      const target = new URL(url);
      const allowed = new URL(appUrl);
      if (target.origin !== allowed.origin) {
        event.preventDefault();
      }
    } catch {
      event.preventDefault();
    }
  });

  void window.loadURL(appUrl);
}

app.whenReady().then(async () => {
  const session = await waitForSession();
  const backendUrl = session?.backendUrl ?? fallbackBackendUrl;
  // 应用实际加载的地址：开发用 Vite dev server，生产用同源后端。
  const appUrl = developmentUrl ?? backendUrl;
  const token = session?.token ?? '';

  createWindow(appUrl, token, backendUrl);
  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow(appUrl, token, backendUrl);
  });
});

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') app.quit();
});
