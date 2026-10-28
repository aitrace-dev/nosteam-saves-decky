export const syncLabels = {
  up_to_date: 'Up to date', pending: 'Local changes pending', conflict: 'Choose progress',
  update_available: 'Save update available', last_confirmed: 'Last confirmed',
  not_checked: 'Needs a save check', not_reported: 'Not reported yet',
  not_configured: 'Save sync off', signed_out: 'Signed out', playing: 'Playing',
  busy: 'In use', checking: 'Checking saves', needs_attention: 'Needs attention',
};

export function liveState(shared, device, cell, now = Date.now() / 1000) {
  if (device.revoked || device.expires <= now) return 'signed_out';
  if (!cell || cell.state === 'not_reported' || device.availability === 'unknown') return 'not_reported';
  if (device.availability !== 'online' || !Number.isFinite(device.seen)
      || now - device.seen >= shared.fresh_seconds || now - shared.checked_at >= shared.fresh_seconds
      || shared.checked_at - now > 5) return 'last_confirmed';
  return cell.state;
}
