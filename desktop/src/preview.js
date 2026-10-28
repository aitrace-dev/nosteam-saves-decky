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
  workers: { saves: { state: empty ? 'signed_out' : 'idle', checked_at: now }, games: { state: 'unavailable', enabled: false } }, tasks: [],
};
window.nosteam = {
  status: async () => structuredClone(snapshot),
  artwork: async (key, kind) => {
    const id = { 'celeste-shortcut': 504230, 'hades-shortcut': 1145360, 'hollow-knight-shortcut': 367520 }[key];
    const file = kind === 'hero' ? 'library_hero.jpg' : kind === 'portrait' ? 'library_600x900_2x.jpg' : 'header.jpg';
    const local = new URLSearchParams(location.search).has('localArt');
    return { image: id ? local ? `/artwork/${id}/${file}` : `https://cdn.akamai.steamstatic.com/steam/apps/${id}/${file}` : null };
  },
  submit: async (action, args) => {
    const id = crypto.randomUUID();
    let result = {};
    if (action === 'refresh-cloud') result = { devices: [
      { id: 'desktop', name: 'Bazzite desktop', expires: now + 86400, revoked: false },
      { id: 'handheld', name: 'Bazzite handheld', expires: now + 86400, revoked: false },
    ], games: [], checked_at: now };
    if (action === 'history') result = [{ revision: '89bf57a1-9365-4b8f-a5aa-adc18e32af14', device: 'Bazzite desktop', created: new Date((now - 3600) * 1000).toISOString() }];
    if (action === 'resolve') {
      const game = snapshot.games.find(game => game.id === args.game);
      game.status = 'synced'; game.pending = false;
    }
    tasks.set(id, { state: 'completed', result });
    return { id };
  },
  task: async id => tasks.get(id),
  openSignIn: async () => {},
};
await import('./main.js');
