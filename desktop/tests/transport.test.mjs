import assert from 'node:assert/strict';
import { test } from 'node:test';
import http from 'node:http';
import { mkdtemp, rm } from 'node:fs/promises';
import path from 'node:path';
import { request, signInURL, steamGameURL } from '../transport.mjs';

test('only the approved pairing destination can open outside the app', () => {
  const server = 'https://nosteam.example.test';
  assert.equal(signInURL(`${server}/pair?code=1234ABCD`, server), `${server}/pair?code=1234ABCD`);
  for (const value of [
    'javascript:alert(1)', 'file:///etc/passwd', 'https://evil.test/pair?code=1234ABCD',
    `${server}/other?code=1234ABCD`, `${server}/pair?code=1234ABCD&code=1234ABCD`,
    `${server}/pair?code=1234ABCD&next=https://evil.test`, `${server}/pair?code=1234ABCU`,
  ]) assert.throws(() => signInURL(value, server));
});

test('local transport uses a Unix socket and preserves queued command data', async () => {
  const directory = await mkdtemp('/tmp/ns-node-');
  const socket = path.join(directory, 'companion.sock');
  let received;
  const server = http.createServer(async (req, res) => {
    const chunks = [];
    for await (const chunk of req) chunks.push(chunk);
    received = { method: req.method, path: req.url, body: JSON.parse(Buffer.concat(chunks)) };
    res.writeHead(202, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ id: 'queued' }));
  });
  await new Promise(resolve => server.listen(socket, resolve));
  try {
    assert.deepEqual(await request('/v1/tasks', { action: 'sync', arguments: { game: 'celeste' } }, directory), { id: 'queued' });
    assert.deepEqual(received, { method: 'POST', path: '/v1/tasks', body: { action: 'sync', arguments: { game: 'celeste' } } });
  } finally {
    await new Promise(resolve => server.close(resolve));
    await rm(directory, { recursive: true });
  }
});

test('only a registered non-Steam app ID can produce a Steam launch URL', () => {
  assert.equal(steamGameURL(2147484000), 'steam://rungameid/9223373548716818432');
  for (const value of [null, '2147484000', 570, 2 ** 32, -1, 1.5]) assert.throws(() => steamGameURL(value));
});
