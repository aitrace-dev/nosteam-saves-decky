import assert from 'node:assert/strict';
import { test } from 'node:test';
import { _electron } from 'playwright';
import { mkdtemp, mkdir, writeFile, readFile, rm } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import { request } from '../transport.mjs';

const desktop = fileURLToPath(new URL('..', import.meta.url));

test('real desktop app reuses existing saves and leaves its service running after window close', { timeout: 45000 }, async () => {
  const directory = await mkdtemp('/tmp/ns-desktop-');
  const home = path.join(directory, 'state');
  await mkdir(home);
  const saveRoot = path.join(directory, 'saves');
  await mkdir(saveRoot);
  await writeFile(path.join(saveRoot, 'slot.sav'), 'preserve this progress');
  const cfg = { games: { celeste: {
    title: 'Celeste', source: 'steam-shortcut', shortcut_id: '2147484000',
    registry: '/fixture/shortcuts.vdf', roots: [saveRoot], original_options: '%command%',
  } } };
  await writeFile(path.join(home, 'config.json'), JSON.stringify(cfg), { mode: 0o600 });
  const env = { ...process.env, NOSTEAM_HOME: home };
  delete env.ELECTRON_RUN_AS_NODE;
  let electron;
  let electronProcess;
  let pid;
  try {
    electron = await _electron.launch({
      executablePath: process.env.NOSTEAM_ELECTRON,
      args: [...(process.env.NOSTEAM_ELECTRON ? [] : [desktop]), '--user-data-dir=' + path.join(directory, 'profile')], env,
    });
    electronProcess = electron.process();
    const page = await electron.firstWindow();
    page.setDefaultTimeout(8000);
    page.setDefaultNavigationTimeout(8000);
    await page.waitForFunction(() => document.querySelector('#service-state')?.textContent === 'Background service connected');
    assert.equal(await page.locator('h1').textContent(), 'Games');
    if (process.env.NOSTEAM_SCREENSHOT) await page.screenshot({ path: process.env.NOSTEAM_SCREENSHOT });
    await page.getByRole('link', { name: 'Saves', exact: true }).click();
    await page.getByRole('heading', { name: 'Celeste', exact: true }).waitFor();
    // IPC must remain valid after navigation changes the renderer's hash.
    await page.getByRole('button', { name: 'Refresh', exact: true }).click();
    assert.equal(await page.locator('#notice').textContent(), '');
    await page.getByRole('button', { name: 'Check saves' }).click();
    await page.waitForFunction(() => document.querySelector('#notice').textContent.includes('Local saves are preserved'));
    await page.getByRole('link', { name: 'Settings', exact: true }).click();
    await page.getByRole('heading', { name: 'Background service', exact: true }).waitFor();
    const before = await request('/v1/status', undefined, home);
    pid = before.pid;
    const preferences = await electron.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].webContents.getLastWebPreferences());
    assert.equal(preferences.contextIsolation, true);
    assert.equal(preferences.sandbox, true);
    assert.equal(preferences.nodeIntegration, false);
    // Drive the actual renderer gamepad loop, including edge-triggered shoulders and B.
    await page.evaluate(() => {
      window.testPad = { connected: true, axes: [0, 0], buttons: Array.from({ length: 16 }, () => ({ pressed: false })) };
      Object.defineProperty(navigator, 'getGamepads', { configurable: true, value: () => [window.testPad] });
      window.testPad.buttons[4].pressed = true;
    });
    await page.getByRole('heading', { name: 'Devices', exact: true }).waitFor();
    await page.evaluate(() => { window.testPad.buttons[4].pressed = false; window.testPad.buttons[5].pressed = true; });
    await page.getByRole('heading', { name: 'Settings', exact: true }).waitFor();
    await page.evaluate(() => { window.testPad.buttons[5].pressed = false; window.testPad.buttons[1].pressed = true; });
    await page.waitForFunction(() => document.activeElement.matches('nav a[aria-current]'));
    await page.evaluate(() => { window.testPad.buttons[1].pressed = false; });
    const cdp = await page.context().newCDPSession(page);
    // Playwright otherwise forces document.hasFocus() true even behind native dialogs.
    await cdp.send('Emulation.setFocusEmulationEnabled', { enabled: false });
    await electron.evaluate(({ BrowserWindow }) => {
      globalThis.focusProbe = new BrowserWindow({ width: 150, height: 100 });
      globalThis.focusProbe.loadURL('about:blank');
    });
    await page.waitForFunction(() => !document.hasFocus());
    await page.evaluate(() => { window.testPad.buttons[5].pressed = true; });
    await page.waitForTimeout(200);
    assert.equal(await page.locator('h1').textContent(), 'Settings');
    await electron.evaluate(({ BrowserWindow }) => {
      globalThis.focusProbe.destroy();
      BrowserWindow.getAllWindows()[0].focus();
    });
    await cdp.send('Emulation.setFocusEmulationEnabled', { enabled: true });
    await cdp.detach();
    await page.evaluate(() => { window.testPad.buttons[5].pressed = false; });
    await page.keyboard.press('PageUp');
    await page.getByRole('heading', { name: 'Devices', exact: true }).waitFor();
    assert.equal(await page.evaluate(() => document.documentElement.scrollHeight > innerHeight), false);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
    const exited = new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error('Closing the window did not exit the app')), 8000);
      electronProcess.once('exit', () => { clearTimeout(timer); resolve(); });
    });
    await page.getByRole('link', { name: 'Settings', exact: true }).click();
    try { await page.getByRole('button', { name: 'Close app', exact: true }).click(); }
    catch (error) { if (!error.message.includes('Target page, context or browser has been closed')) throw error; }
    await exited;
    electron = undefined;
    const after = await request('/v1/status', undefined, home);
    assert.equal(after.pid, pid);
    assert.equal(after.started, before.started);
    assert.equal(await readFile(path.join(saveRoot, 'slot.sav'), 'utf8'), 'preserve this progress');
    assert.deepEqual(JSON.parse(await readFile(path.join(home, 'config.json'))), cfg);
  } finally {
    if (electron) {
      let timer;
      try {
        await Promise.race([electron.close(), new Promise(resolve => {
          timer = setTimeout(() => { electronProcess.kill('SIGKILL'); resolve(); }, 3000);
        })]);
      } finally { clearTimeout(timer); }
    }
    if (!pid) { try { pid = (await request('/v1/status', undefined, home)).pid; } catch {} }
    if (pid) {
      try { process.kill(pid, 'SIGTERM'); } catch {}
      for (let i = 0; i < 50; i++) {
        try { await request('/v1/status', undefined, home); } catch { break; }
        await new Promise(resolve => setTimeout(resolve, 100));
      }
    }
    await rm(directory, { recursive: true, force: true });
  }
});
