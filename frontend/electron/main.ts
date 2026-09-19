import { app, BrowserWindow, dialog, ipcMain, shell } from 'electron';
import { spawn, type ChildProcess } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { readFile, rm } from 'node:fs/promises';
import { existsSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';

const currentDirectory = path.dirname(fileURLToPath(import.meta.url));
const developmentUrl = process.env.VITE_DEV_SERVER_URL;
// 生产模式下前端由后端 StaticFiles 同源托管（P0-1），Electron loadURL 到后端，
// 使渲染进程 origin 与 /api、/ws 一致，摆脱 CORS 与 opaque(null) origin。
const fallbackBackendUrl = (process.env.BUTLER_BACKEND_URL ?? 'http://127.0.0.1:8000').replace(/\/+$/, '');

// 供 preload 同步读取的会话信息；后端就绪后填充。
let currentSession: { token: string; backendUrl: string } = { token: '', backendUrl: fallbackBackendUrl };
let mainWindow: BrowserWindow | null = null;
let allowedOrigin = new URL(developmentUrl ?? fallbackBackendUrl).origin;

function sessionFilePath(): string {
  return process.env.BUTLER_SESSION_FILE ?? path.join(os.homedir(), '.desktop-smart-file-butler', 'session.json');
}

// ---- 启动 / 错误占位页，避免后端就绪前一片白屏 ----
function htmlPage(body: string): string {
  const doc = `<!doctype html><html lang="zh"><head><meta charset="utf-8">
<style>html,body{height:100%;margin:0}body{display:flex;align-items:center;justify-content:center;
background:#f5f7fb;color:#334;font-family:system-ui,"Microsoft YaHei",sans-serif}
.box{text-align:center;max-width:520px;padding:24px}.sp{width:38px;height:38px;margin:0 auto 18px;
border:4px solid #d7deea;border-top-color:#4b6bfb;border-radius:50%;animation:spin 1s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}h1{font-size:18px;margin:.4em 0}p{color:#6b7280;font-size:14px;line-height:1.6}
code{background:#eef;padding:1px 5px;border-radius:4px}</style></head><body><div class="box">${body}</div></body></html>`;
  return `data:text/html;charset=utf-8,${encodeURIComponent(doc)}`;
}
const LOADING_PAGE = htmlPage('<div class="sp"></div><h1>正在启动本地服务…</h1><p>首次启动需加载模型与索引组件，可能需要几秒。</p>');
function errorPage(reason: string): string {
  return htmlPage(
    `<h1>无法连接本地后端</h1><p>${reason}</p>` +
    `<p>请确认后端可启动；也可手动运行后端后重开应用：<br><code>cd backend &amp;&amp; python -m uvicorn app.main:app --port 8000</code></p>` +
    `<p>如已自行启动后端，可设置环境变量 <code>BUTLER_NO_SPAWN=1</code> 让应用不再自行拉起。</p>`,
  );
}

// ---- Python 后端 sidecar（P2 打包链路）----
let backendProcess: ChildProcess | null = null;

/** 从多个根向上查找含 app/main.py 的 backend 目录，兼容打包后的多层嵌套布局。 */
function findBackendDir(): string | null {
  if (process.env.BUTLER_BACKEND_DIR) {
    return existsSync(path.join(process.env.BUTLER_BACKEND_DIR, 'app', 'main.py'))
      ? process.env.BUTLER_BACKEND_DIR
      : null;
  }
  const starts = [currentDirectory, app.getAppPath(), process.resourcesPath, process.cwd()].filter(Boolean);
  for (const start of starts) {
    let dir = start as string;
    for (let i = 0; i < 8; i += 1) {
      if (existsSync(path.join(dir, 'app', 'main.py'))) return dir;
      const candidate = path.join(dir, 'backend');
      if (existsSync(path.join(candidate, 'app', 'main.py'))) return candidate;
      const parent = path.dirname(dir);
      if (parent === dir) break;
      dir = parent;
    }
  }
  return null;
}

function resolvePython(dir: string): string {
  if (process.env.BUTLER_PYTHON) return process.env.BUTLER_PYTHON;
  const venv = process.platform === 'win32'
    ? path.join(dir, '.venv', 'Scripts', 'python.exe')
    : path.join(dir, '.venv', 'bin', 'python');
  if (existsSync(venv)) return venv;
  return process.platform === 'win32' ? 'python' : 'python3';
}

function startBackend(): boolean {
  if (process.env.BUTLER_NO_SPAWN) return true;  // 用户自行启动后端
  const dir = findBackendDir();
  if (!dir) {
    console.warn('未找到后端目录（backend/app/main.py），跳过 sidecar 启动');
    return false;
  }
  const url = new URL(fallbackBackendUrl);
  const port = url.port || '8000';
  let command: string;
  let args: string[];
  if (process.env.BUTLER_BACKEND_CMD) {
    const parts = process.env.BUTLER_BACKEND_CMD.split(' ').filter(Boolean);
    command = parts[0] ?? 'python';
    args = parts.slice(1);
  } else {
    command = resolvePython(dir);
    args = ['-m', 'uvicorn', 'app.main:app', '--host', url.hostname || '127.0.0.1', '--port', port];
  }
  try {
    console.log('启动后端 sidecar：', command, args.join(' '), '于', dir);
    backendProcess = spawn(command, args, { cwd: dir, stdio: 'inherit', env: process.env });
    backendProcess.on('error', (err) => console.error('后端 sidecar 启动失败：', err));
    backendProcess.on('exit', (code) => { console.log('后端 sidecar 退出，code=', code); backendProcess = null; });
    return true;
  } catch (err) {
    console.error('无法拉起后端 sidecar：', err);
    return false;
  }
}

function stopBackend(): void {
  if (backendProcess) {
    backendProcess.kill();
    backendProcess = null;
  }
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

async function waitForSession(timeoutMs = 30_000): Promise<SessionInfo | null> {
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

function createWindow(): BrowserWindow {
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
    if (isTrustedExternalUrl(url)) void shell.openExternal(url);
    return { action: 'deny' };
  });
  window.webContents.on('will-navigate', (event, url) => {
    // 仅允许在应用自身 origin 内导航；data: 占位页与跨源导航按需放行/拦截。
    if (url.startsWith('data:')) return;
    try {
      if (new URL(url).origin !== allowedOrigin) event.preventDefault();
    } catch {
      event.preventDefault();
    }
  });

  void window.loadURL(LOADING_PAGE);  // 先展示启动页，避免白屏
  return window;
}

// 令牌经同步 IPC 提供给 preload；页面 reload 时会重新取到最新值。
ipcMain.on('butler:session', (event) => {
  event.returnValue = currentSession;
});

// 原生目录选择器（P1-3）。
ipcMain.handle('butler:choose-directory', async () => {
  const result = await dialog.showOpenDialog({ properties: ['openDirectory', 'createDirectory'] });
  if (result.canceled || result.filePaths.length === 0) return null;
  return result.filePaths[0];
});

async function boot(): Promise<void> {
  mainWindow = createWindow();  // 立即出窗，显示启动页

  if (!process.env.BUTLER_NO_SPAWN) {
    // 删除旧会话文件，确保读到本次新后端写入的新令牌。
    await rm(sessionFilePath(), { force: true }).catch(() => undefined);
  }
  const spawned = startBackend();
  const session = await waitForSession();

  if (!mainWindow || mainWindow.isDestroyed()) return;

  if (developmentUrl) {
    // 开发模式：UI 走 Vite，后端提供 API；令牌来自会话文件。
    currentSession = { token: session?.token ?? '', backendUrl: session?.backendUrl ?? fallbackBackendUrl };
    allowedOrigin = new URL(developmentUrl).origin;
    void mainWindow.loadURL(developmentUrl);
    return;
  }

  if (session) {
    currentSession = { token: session.token, backendUrl: session.backendUrl };
    allowedOrigin = new URL(session.backendUrl).origin;
    void mainWindow.loadURL(session.backendUrl);
  } else {
    const reason = spawned
      ? '后端进程已启动，但在 30 秒内未就绪（首次可能在下载/加载组件）。'
      : '未能自动启动后端（未找到 backend 目录或 Python 环境）。';
    void mainWindow.loadURL(errorPage(reason));
  }
}

app.whenReady().then(async () => {
  await boot();
  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) void boot();
  });
});

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') app.quit();
});

// 应用退出时结束后端 sidecar，避免残留进程占用端口。
app.on('will-quit', stopBackend);
process.on('exit', stopBackend);
