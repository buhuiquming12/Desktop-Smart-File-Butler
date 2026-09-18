import { app, BrowserWindow, shell } from 'electron';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const currentDirectory = path.dirname(fileURLToPath(import.meta.url));
const developmentUrl = process.env.VITE_DEV_SERVER_URL;
// 生产模式下前端由后端 StaticFiles 同源托管（见 P0-1），Electron 直接加载后端 URL，
// 使渲染进程 origin 与 /api、/ws 一致，彻底摆脱 CORS 与 opaque(null) origin。
const backendUrl = (process.env.BUTLER_BACKEND_URL ?? 'http://127.0.0.1:8000').replace(/\/+$/, '');
// 应用实际加载的地址：开发用 Vite dev server，生产用同源后端。
const appUrl = developmentUrl ?? backendUrl;

function isTrustedExternalUrl(rawUrl: string): boolean {
  try {
    const url = new URL(rawUrl);
    return url.protocol === 'https:' || url.protocol === 'http:';
  } catch {
    return false;
  }
}

function createWindow(): void {
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

app.whenReady().then(() => {
  createWindow();
  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') app.quit();
});
