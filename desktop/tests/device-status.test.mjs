import assert from 'node:assert/strict';
import { test } from 'node:test';
import { liveState } from '../src/device-status.js';

test('a cached green state expires with the device heartbeat, even after a recent account refresh', () => {
  const shared = { checked_at: 1000, fresh_seconds: 90 };
  const device = { availability: 'online', seen: 915, expires: 10000 };
  const cell = { state: 'up_to_date' };
  assert.equal(liveState(shared, device, cell, 1000), 'up_to_date');
  assert.equal(liveState(shared, device, cell, 1005), 'last_confirmed');
  assert.equal(liveState(shared, { ...device, revoked: true }, cell, 1000), 'signed_out');
  assert.equal(liveState(shared, { ...device, availability: 'unknown' }, cell, 1000), 'not_reported');
  assert.equal(liveState(shared, device, cell, 900), 'last_confirmed');
});
