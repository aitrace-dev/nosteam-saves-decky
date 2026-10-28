import { app, BrowserWindow, dialog, ipcMain, shell, session } from 'electron';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { ensureService, request, signInURL } from './transport.mjs';

const here = path.dirname(fileURLToPath(import.meta.url));
const entry = pathToFileURL(path.join(here, 'dist/index.html')).href;
let window;

function trusted(event) {
  const frameURL = new URL(event.senderFrame?.url || 'about:blank');
  frameURL.hash = '';
  if (!window || event.sender !== window.webContents || event.senderFrame !== window.webContents.mainFrame
      || frameURL.href !== entry) throw new Error('Untrusted app request.');
}

function createWindow() {
  window = new BrowserWindow({
    title: 'NoSteam', width: 1180, height: 780, minWidth: 360, minHeight: 480,
    backgroundColor: '#102139', autoHideMenuBar: true,
    webPreferences: {
      preload: path.join(here, 'preload.cjs'), contextIsolation: true,
      nodeIntegration: false, sandbox: true, webviewTag: false,
    },
  });
  window.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
  window.webContents.on('will-navigate', event => event.preventDefault());
  window.loadFile(path.join(here, 'dist/index.html'));
  window.on('closed', () => { window = undefined; });
}

if (!app.requestSingleInstanceLock()) app.quit();
else {
  app.on('second-instance', () => {
    if (window) { if (window.isMinimized()) window.restore(); window.show(); window.focus(); }
  });
  app.whenReady().then(async () => {
    session.defaultSession.setPermissionRequestHandler((_contents, _permission, callback) => callback(false));
    session.defaultSession.setPermissionCheckHandler(() => false);
    ipcMain.handle('nosteam:status', event => { trusted(event); return request('/v1/status'); });
    ipcMain.handle('nosteam:artwork', (event, key, kind) => {
      trusted(event);
      if (typeof key !== 'string' || !/^[a-f0-9]{64}$/.test(key) || !['portrait', 'landscape', 'hero'].includes(kind)) {
        throw new Error('Invalid game artwork request.');
      }
      return request(`/v1/artwork/${key}/${kind}`);
    });
    ipcMain.handle('nosteam:submit', (event, action, args) => {
      trusted(event); return request('/v1/tasks', { action, arguments: args });
    });
    ipcMain.handle('nosteam:task', (event, id) => {
      trusted(event);
      if (typeof id !== 'string' || !/^[0-9a-f-]{36}$/.test(id)) throw new Error('Invalid command.');
      return request(`/v1/tasks/${id}`);
    });
    ipcMain.handle('nosteam:sign-in', async event => {
      trusted(event);
      const state = await request('/v1/status');
      if (state.sign_in.status !== 'waiting') throw new Error('Start device sign-in first.');
      // The approved destination is projected by the service, never supplied by the renderer.
      const url = signInURL(state.sign_in.url, state.sign_in.server);
      await shell.openExternal(url);
    });
    try { await ensureService(); }
    catch (error) { dialog.showErrorBox('NoSteam background service', error.message); }
    createWindow();
  });
  // Quitting the interface deliberately does not stop its detached/user service.
  app.on('window-all-closed', () => app.quit());
}
