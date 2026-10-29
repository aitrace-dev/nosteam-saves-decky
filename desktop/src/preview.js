// Only preview.html imports this file; the production build has one index.html entry.
const empty = new URLSearchParams(location.search).has('empty');
const tasks = new Map();
const now = Date.now() / 1000;
const games = empty ? [] : [
  { id: 'celeste', key: 'celeste-shortcut', title: 'Celeste', status: 'synced', revision: 'd0dc62e1-97bd-4e5c-9a5f-0c0644b80985', synced_at: now - 3600, pending: false },
  { id: 'hades', key: 'hades-shortcut', title: 'Hades', status: 'conflict', revision: '89bf57a1-9365-4b8f-a5aa-adc18e32af14', synced_at: now - 86400, pending: true },
  { id: 'hollow-knight', key: 'hollow-knight-shortcut', title: 'Hollow Knight', status: 'pending', synced_at: now - 7200, pending: true },
];
const snapshot = {
  protocol: 1, device: { name: 'Bazzite handheld', connected: !empty }, sign_in: { status: 'signed_out' },
  games, shortcuts: games.map(game => ({ key: game.key, title: game.title })),
  workers: { saves: { state: empty ? 'signed_out' : 'idle', checked_at: now }, reporting: { state: empty ? 'signed_out' : 'idle', checked_at: now }, games: { state: 'unavailable', enabled: false } }, tasks: [],
};
snapshot.sync = empty ? null : {
  self_device_id: 'handheld', checked_at: now, fresh_seconds: 90,
  devices: [
    { id: 'desktop', name: 'Bazzite desktop', expires: now + 86400, availability: 'online', seen: now },
    { id: 'handheld', name: 'Bazzite handheld', expires: now + 86400, availability: 'online', seen: now },
    { id: 'travel', name: 'Travel handheld', expires: now + 86400, availability: 'offline', seen: now - 86400 },
  ],
  games: games.map(game => ({
    game: game.id, title: game.title, revision: game.revision || '946b2cf9-8118-48e9-9bd0-332f39a14632',
    devices: [
      { device_id: 'desktop', state: 'up_to_date', revision: game.revision || '946b2cf9-8118-48e9-9bd0-332f39a14632', reported_at: now },
      { device_id: 'handheld', state: game.status === 'synced' ? 'up_to_date' : game.status, revision: game.revision, reported_at: now },
      { device_id: 'travel', state: 'last_confirmed', revision: game.revision, reported_at: now - 86400 },
    ],
  })),
};
snapshot.game_sync = {
  enabled: !empty, state: empty ? 'off' : 'active', sharing: empty ? 0 : 1,
  self_device_id: 'handheld',
  offers: empty ? [] : [
    { game: 'celeste', title: 'Celeste', version: 'a'.repeat(64), size: 1280 * 1024 ** 2, files: 120,
      device_id: 'desktop', name: 'Bazzite desktop', available: true, seen: now },
    { game: 'hades', title: 'Hades', version: 'b'.repeat(64), size: 12 * 1024 ** 3, files: 860,
      device_id: 'travel', name: 'Travel handheld', available: false, seen: now - 86400 },
  ],
  jobs: empty ? [] : [
    { id: 'download-hollow-knight', kind: 'download', game: 'hollow-knight', title: 'Hollow Knight', version: 'c'.repeat(64),
      state: 'completed', ready: true, proton: 'GE-Proton11-11', total: 8 * 1024 ** 3, verified: 8 * 1024 ** 3, created: now - 300 },
    { id: 'download-ori', kind: 'download', game: 'ori', title: 'Ori and the Will of the Wisps', version: 'd'.repeat(64),
      state: 'awaiting_steam', total: 15 * 1024 ** 3, verified: 15 * 1024 ** 3, created: now - 120,
      error: 'Close Steam to add the game and its Proton configuration' },
    { id: 'download-celeste', kind: 'download', game: 'celeste', title: 'Celeste', version: 'a'.repeat(64),
      state: 'paused', total: 1280 * 1024 ** 2, verified: 640 * 1024 ** 2, created: now - 90 },
    { id: 'download-hades', kind: 'download', game: 'hades', title: 'Hades', version: 'b'.repeat(64),
      state: 'waiting_for_source', total: 12 * 1024 ** 3, verified: 0, created: now - 60 },
  ],
};
let demoAt = Date.now();
window.nosteam = {
  close: async () => {},
  status: async () => {
    const current = Date.now() / 1000;
    const elapsed = (Date.now() - demoAt) / 1000;
    demoAt = Date.now();
    if (snapshot.sync) {
      snapshot.sync.checked_at = current;
      for (const device of snapshot.sync.devices) if (device.availability === 'online') device.seen = current;
    }
    for (const offer of snapshot.game_sync.offers) if (offer.available) offer.seen = current;
    for (const job of snapshot.game_sync.jobs) if (job.state === 'running') {
      job.verified = Math.min(job.total, job.verified + Math.floor(elapsed * 6) * 4 * 1024 ** 2);
      if (job.verified === job.total) job.state = 'completed';
    }
    return structuredClone(snapshot);
  },
  artwork: async (key, kind) => {
    const id = { 'celeste-shortcut': 504230, 'hades-shortcut': 1145360, 'hollow-knight-shortcut': 367520 }[key];
    const file = kind === 'hero' ? 'library_hero.jpg' : kind === 'portrait' ? 'library_600x900_2x.jpg' : 'header.jpg';
    const local = new URLSearchParams(location.search).has('localArt');
    return { image: id ? local ? `/artwork/${id}/${file}` : `https://cdn.akamai.steamstatic.com/steam/apps/${id}/${file}` : null };
  },
  submit: async (action, args) => {
    const id = crypto.randomUUID();
    let result = {};
    if (action === 'refresh-cloud') result = snapshot.sync || { devices: [], games: [], checked_at: now, fresh_seconds: 90 };
    if (action === 'games-enable') {
      snapshot.game_sync.enabled = args.enabled;
      snapshot.game_sync.state = args.enabled ? 'active' : 'off';
    }
    if (action === 'pause-transfer' || action === 'resume-transfer') {
      const job = snapshot.game_sync.jobs.find(job => job.id === args.job);
      job.state = action === 'pause-transfer' ? 'paused' : 'running';
      demoAt = Date.now();
    }
    if (action === 'history') result = [{ revision: '89bf57a1-9365-4b8f-a5aa-adc18e32af14', device: 'Bazzite desktop', created: new Date((now - 3600) * 1000).toISOString() }];
    if (action === 'resolve') {
      const game = snapshot.games.find(game => game.id === args.game);
      game.status = 'synced'; game.pending = false;
      snapshot.sync.games.find(item => item.game === game.id).devices.find(item => item.device_id === 'handheld').state = 'up_to_date';
    }
    tasks.set(id, { state: 'completed', result });
    return { id };
  },
  task: async id => tasks.get(id),
  openSignIn: async () => {},
  shareGame: async key => {
    const game = games.find(game => game.key === key);
    const id = crypto.randomUUID();
    snapshot.game_sync.jobs.unshift({ id, kind: 'index', game: game.id, title: game.title,
      state: 'completed', total: 1280 * 1024 ** 2, verified: 0, created: Date.now() / 1000 });
    tasks.set(id, { state: 'completed', result: { job: id } });
    return { id };
  },
  downloadGame: async (game, version) => {
    const job = snapshot.game_sync.jobs.find(job => job.game === game && job.version === version);
    if (job?.state === 'paused') job.state = 'running';
    const id = crypto.randomUUID();
    tasks.set(id, { state: 'completed', result: { job: job?.id } });
    return { id };
  },
  openInstallation: async () => {},
  playInstallation: async () => {},
};
await import('./main.js');
