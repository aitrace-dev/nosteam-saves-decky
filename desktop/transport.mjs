import http from 'node:http';
import path from 'node:path';
import os from 'node:os';
import { existsSync } from 'node:fs';
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));

export function stateHome(env = process.env) {
  return env.NOSTEAM_HOME || path.join(os.homedir(), '.local/share/nosteam-saves');
}

export function request(endpoint, data, home = stateHome()) {
  return new Promise((resolve, reject) => {
    const body = data === undefined ? undefined : JSON.stringify(data);
    const req = http.request({
      socketPath: path.join(home, 'companion.sock'), path: endpoint,
      method: body ? 'POST' : 'GET', timeout: 6000,
      headers: body ? { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(body) } : {},
    }, res => {
      const chunks = [];
      let length = 0;
      res.on('data', chunk => {
        length += chunk.length;
        if (length > 4 * 1024 * 1024) res.destroy(new Error('Local response is too large.'));
        else chunks.push(chunk);
      });
      res.on('error', reject);
      res.on('end', () => {
        try {
          const result = JSON.parse(Buffer.concat(chunks).toString());
          if (res.statusCode >= 400) reject(new Error(result.error || 'Local command failed.'));
          else resolve(result);
        } catch { reject(new Error('Could not read the background service response.')); }
      });
    });
    req.on('timeout', () => req.destroy(new Error('Background service did not respond.')));
    req.on('error', () => reject(new Error('Background service unavailable. Reopen NoSteam to retry.')));
    req.end(body);
  });
}

export function signInURL(value, server) {
  const url = new URL(value);
  const origin = new URL(server);
  if (url.protocol !== 'https:' || url.origin !== origin.origin || url.pathname !== '/pair'
      || url.username || url.password || url.hash
      || [...url.searchParams.keys()].some(key => key !== 'code')
      || url.searchParams.getAll('code').length !== 1
      || !/^[0-9A-HJKMNP-TV-Z]{8}$/.test(url.searchParams.get('code'))) {
    throw new Error('Invalid device sign-in destination.');
  }
  return url.href;
}

export async function ensureService() {
  const check = async () => {
    const result = await request('/v1/status');
    if (result.protocol !== 1) throw new Error('Update the NoSteam background service.');
    return result;
  };
  try { return await check(); } catch (error) {
    if (error.message.startsWith('Update')) throw error;
  }
  const candidates = [
    process.env.NOSTEAM_PYTHON,
    path.resolve(here, '../.venv/bin/python'),
    path.resolve(here, '../../.venv/bin/python'),
  ].filter(Boolean);
  const python = candidates.find(candidate => existsSync(candidate)) || 'python3';
  const child = spawn(python, ['-m', 'nosteam_client.companion'], {
    detached: true, stdio: 'ignore', env: process.env,
  });
  let failed = false;
  child.on('error', () => { failed = true; });
  child.unref();
  for (let attempt = 0; attempt < 40 && !failed; attempt++) {
    await new Promise(resolve => setTimeout(resolve, 200));
    try { return await check(); } catch (error) {
      if (error.message.startsWith('Update')) throw error;
    }
  }
  throw new Error('Install the NoSteam Python client, then reopen the app. See the companion README.');
}

export function steamGameURL(appid) {
  if (!Number.isInteger(appid) || appid < 0x80000000 || appid > 0xffffffff) throw new Error('This installation is not ready in Steam.');
  return `steam://rungameid/${(BigInt(appid) << 32n) | 0x02000000n}`;
}
