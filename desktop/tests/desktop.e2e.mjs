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
  let electron;
  let pid;
  try {
    electron = await _electron.launch({ args: [desktop], env });
    const page = await electron.firstWindow();
    await page.waitForFunction(() => document.querySelector('#service-state')?.textContent === 'Background service connected');
    assert.equal(await page.locator('h1').textContent(), 'Games');
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
    await electron.close();
    electron = undefined;
    const after = await request('/v1/status', undefined, home);
    assert.equal(after.pid, pid);
    assert.equal(after.started, before.started);
    assert.equal(await readFile(path.join(saveRoot, 'slot.sav'), 'utf8'), 'preserve this progress');
    assert.deepEqual(JSON.parse(await readFile(path.join(home, 'config.json'))), cfg);
  } finally {
    if (electron) await electron.close();
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
