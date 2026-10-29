import { app, BrowserWindow, dialog, ipcMain, shell, session } from 'electron';
import path from 'node:path';
import { execFileSync } from 'node:child_process';
import { gamingSession, consoleZoom } from './window-layout.mjs';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { ensureService, request, signInURL, steamGameURL } from './transport.mjs';

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
  let gamescopeProperty = '';
  if (process.platform === 'linux') {
    try { gamescopeProperty = execFileSync('xprop', ['-root', 'GAMESCOPE_PID'], { encoding: 'utf8', timeout: 1000, stdio: ['ignore', 'pipe', 'ignore'] }); } catch {}
  }
  const consoleMode = process.platform === 'linux' && gamingSession(process.env, gamescopeProperty);
  window = new BrowserWindow({
    title: 'NoSteam', width: 1180, height: 780, minWidth: 640, minHeight: 480,
    fullscreen: consoleMode, frame: !consoleMode,
    backgroundColor: '#102139', autoHideMenuBar: true,
    webPreferences: {
      preload: path.join(here, 'preload.cjs'), contextIsolation: true,
      nodeIntegration: false, sandbox: true, webviewTag: false,
    },
  });
  window.removeMenu();
  const scaleConsole = () => { if (consoleMode) { const [width, height] = window.getContentSize(); window.webContents.setZoomFactor(consoleZoom(width, height)); } };
  window.on('resize', scaleConsole);
  window.webContents.on('did-finish-load', scaleConsole);
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
    ipcMain.handle('nosteam:close', event => { trusted(event); window.close(); });
    ipcMain.handle('nosteam:status', event => { trusted(event); return request('/v1/status'); });
    ipcMain.handle('nosteam:artwork', (event, key, kind) => {
      trusted(event);
      if (typeof key !== 'string' || !/^[a-f0-9]{64}$/.test(key) || !['portrait', 'landscape', 'hero'].includes(kind)) {
        throw new Error('Invalid game artwork request.');
      }
      return request(`/v1/artwork/${key}/${kind}`);
    });
    ipcMain.handle('nosteam:submit', (event, action, args) => {
      trusted(event);
      if (!['sign-in', 'poll-sign-in', 'sync', 'history', 'resolve', 'refresh-cloud', 'games-enable',
        'recover-save-setup', 'refresh-games', 'pause-transfer', 'resume-transfer', 'unshare-game'].includes(action)) throw new Error('Use the app’s folder picker for file operations.');
      return request('/v1/tasks', { action, arguments: args });
    });
    ipcMain.handle('nosteam:setup-saves', async (event, key) => {
      trusted(event);
      if (typeof key !== 'string' || !/^[a-f0-9]{64}$/.test(key)) throw new Error('Invalid game.');
      if ((await request('/v1/steam/status')).running) throw new Error('Close Steam in Desktop Mode before setting up saves.');
      const result = await dialog.showOpenDialog(window, { title: 'Choose this game’s save folder', properties: ['openDirectory'] });
      if (result.canceled) return null;
      return request('/v1/tasks', { action: 'setup-saves', arguments: { key, root: result.filePaths[0] } });
    });
    ipcMain.handle('nosteam:share-game', async (event, key) => {
      trusted(event);
      if (typeof key !== 'string' || !/^[a-f0-9]{64}$/.test(key)) throw new Error('Invalid game.');
      const result = await dialog.showOpenDialog(window, { title: 'Choose the installed game folder to share', properties: ['openDirectory'] });
      if (result.canceled) return null;
      return request('/v1/tasks', { action: 'share-game', arguments: { key, root: result.filePaths[0] } });
    });
    ipcMain.handle('nosteam:download-game', async (event, game, version) => {
      trusted(event);
      if (typeof game !== 'string' || !/^[a-z0-9][a-z0-9_-]{0,99}$/.test(game)
        || typeof version !== 'string' || !/^[a-f0-9]{64}$/.test(version)) throw new Error('Invalid game version.');
      const result = await dialog.showOpenDialog(window, { title: 'Choose a library for this download', properties: ['openDirectory', 'createDirectory'] });
      if (result.canceled) return null;
      return request('/v1/tasks', { action: 'download-game', arguments: { game, version, root: result.filePaths[0] } });
    });
    ipcMain.handle('nosteam:play-installation', async (event, id) => {
      trusted(event);
      if (typeof id !== 'string' || !/^[0-9a-f-]{36}$/.test(id)) throw new Error('Invalid installation.');
      const result = await request(`/v1/installations/${id}`);
      await shell.openExternal(steamGameURL(result.appid));
    });
    ipcMain.handle('nosteam:open-installation', async (event, id) => {
      trusted(event);
      if (typeof id !== 'string' || !/^[0-9a-f-]{36}$/.test(id)) throw new Error('Invalid installation.');
      const result = await request(`/v1/installations/${id}`);
      const error = await shell.openPath(result.path);
      if (error) throw new Error('Could not open the installed game folder.');
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
