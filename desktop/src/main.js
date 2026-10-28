import '@fontsource/inter/latin-400.css';
import '@fontsource/inter/latin-500.css';
import '@fontsource/inter/latin-600.css';
import '@fontsource/space-grotesk/latin-600.css';
import './style.css';

const bridge = window.nosteam;
const screens = ['Games', 'Saves', 'Transfers', 'Devices', 'Settings'];
let state;
let cloud;
let cloudError;
let page = screens.find(name => name.toLowerCase() === location.hash.slice(1)) || 'Games';
let busy = false;
let reading = false;
let pollingPair = false;
let lastRender = '';
let notice = '';
let featuredKey;
const artCache = new Map();
const waitingArt = [];
let activeArt = 0;
function loadArt(key, kind) {
  const cacheKey = `${key}:${kind}`;
  if (artCache.has(cacheKey)) return artCache.get(cacheKey);
  if (artCache.size >= 96) artCache.delete(artCache.keys().next().value);
  const result = new Promise(resolve => { waitingArt.push({ key, kind, resolve }); pumpArt(); });
  artCache.set(cacheKey, result);
  return result;
}
function pumpArt() {
  while (activeArt < 4 && waitingArt.length) {
    const item = waitingArt.shift();
    activeArt++;
    Promise.resolve().then(() => bridge?.artwork?.(item.key, item.kind))
      .then(result => item.resolve(result?.image || null), () => item.resolve(null))
      .finally(() => { activeArt--; pumpArt(); });
  }
}
const artObserver = new IntersectionObserver(entries => {
  for (const entry of entries) {
    if (!entry.isIntersecting) continue;
    const node = entry.target;
    artObserver.unobserve(node);
    loadArt(node.dataset.key, node.dataset.kind).then(url => {
      if (!url || !node.isConnected) return;
      const image = el('img');
      image.alt = ''; image.decoding = 'async';
      image.onload = () => {
        node.classList.add('has-art');
        node.querySelector('.cover-fallback')?.remove();
      };
      image.src = url;
      node.prepend(image);
    });
  }
}, { rootMargin: '160px' });

function artwork(game, kind = 'landscape') {
  const node = el('div', undefined, `artwork artwork-${kind}`);
  node.dataset.key = game.key;
  node.dataset.kind = kind;
  node.dataset.palette = [...game.title].reduce((sum, char) => sum + char.charCodeAt(0), 0) % 4;
  const fallback = el('div', undefined, 'cover-fallback');
  fallback.append(el('span', game.title, 'cover-title'), el('span', 'Your non-Steam collection', 'cover-caption'));
  node.append(fallback);
  artObserver.observe(node);
  return node;
}

function el(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}
function button(label, action, primary = false) {
  const node = el('button', label, primary ? 'primary' : '');
  node.type = 'button';
  node.onclick = async () => {
    node.disabled = true;
    try { await action(); }
    catch (error) { showNotice(error.message); }
    finally { if (node.isConnected) node.disabled = false; }
  };
  return node;
}
function time(value) {
  return value ? new Date(typeof value === 'number' ? value * 1000 : value)
    .toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' }) : 'Not yet confirmed';
}
const statuses = {
  ready: 'Ready to check', synced: 'Last sync completed', pending: 'Upload pending',
  conflict: 'Choose save progress', playing: 'Game running', checking: 'Checking saves',
  uploading: 'Uploading saves', downloading: 'Restoring saves', error: 'Needs attention',
  offline: 'Cloud unavailable', interrupted: 'Session interrupted', recovering: 'Recovering saves',
  recovered: 'Recovered local progress', saving: 'Saving progress', removed: 'Shortcut removed',
};
function status(game) {
  if (game.removed) return el('span', 'Shortcut removed', 'muted');
  const value = game.pending && game.status !== 'conflict' ? 'pending' : game.status;
  return el('span', statuses[value] || 'Needs a save check', `status ${value === 'conflict' || value === 'error' ? 'attention' : ''}`);
}
function panel(title, copy) {
  const node = el('section', undefined, 'panel');
  node.append(el('h2', title));
  if (copy) node.append(el('p', copy, 'muted'));
  return node;
}
function empty(container, title, copy) {
  const node = el('div', undefined, 'empty');
  node.append(el('h3', title), el('p', copy, 'muted'));
  container.append(node);
}
function row(title, detail, right) {
  const node = el('div', undefined, 'row');
  const info = el('div', undefined, 'identity');
  info.append(el('h3', title));
  if (detail) info.append(el('p', detail, 'muted'));
  node.append(info);
  if (right) node.append(right);
  return node;
}
function showNotice(message) {
  notice = message;
  document.querySelector('#notice').textContent = notice;
  const dialog = document.querySelector('dialog');
  if (dialog?.open) {
    let result = dialog.querySelector('[role="status"]');
    if (!result) { result = el('p', '', 'muted'); result.setAttribute('role', 'status'); dialog.append(result); }
    result.textContent = message;
  }
}
function heading(title, copy) {
  document.querySelector('h1').textContent = title;
  document.querySelector('#description').textContent = copy;
}

async function command(action, args = {}) {
  if (!bridge) throw new Error('Open the installed NoSteam app to manage this device.');
  const queued = await bridge.submit(action, args);
  for (let attempt = 0; attempt < 120; attempt++) {
    const task = await bridge.task(queued.id);
    if (task.state === 'completed') return task.result;
    if (task.state === 'failed') throw new Error(task.error);
    await new Promise(resolve => setTimeout(resolve, 500));
  }
  throw new Error('The command is still queued or running. Check Transfers for its progress.');
}

async function sync(game) {
  showNotice(`Checking saves for ${game.title}…`);
  await command('sync', { game: game.id });
  showNotice(`Save check finished for ${game.title}.`);
  await refresh(true);
}
async function history(game) {
  const rows = await command('history', { game: game.id });
  const dialog = document.querySelector('dialog');
  dialog.replaceChildren(el('h2', `${game.title} save history`));
  if (!rows.length) dialog.append(el('p', 'No cloud revisions yet.', 'muted'));
  for (const item of rows) {
    dialog.append(row(time(item.created), `From ${item.device}`, el('code', item.revision.slice(0, 8))));
  }
  dialog.append(button('Close history', () => dialog.close()));
  dialog.showModal();
}
function resolve(game) {
  const dialog = document.querySelector('dialog');
  dialog.replaceChildren(el('h2', `Choose progress for ${game.title}`),
    el('p', 'Both versions are preserved. Choose the progress to use next.', 'muted'));
  for (const [label, choice] of [['Use this device’s progress', 'local'], ['Use cloud progress', 'cloud']]) {
    dialog.append(button(label, async () => {
      await command('resolve', { game: game.id, choice });
      dialog.close();
      showNotice(`Save choice recorded for ${game.title}.`);
      await refresh(true);
    }));
  }
  dialog.append(button('Decide later', () => dialog.close()));
  dialog.showModal();
}
async function signIn() {
  if (busy) return;
  busy = true;
  try {
    await command('sign-in');
    await bridge.openSignIn();
    showNotice('Confirm sign-in in your browser. NoSteam will connect automatically.');
    await refresh(true);
  } finally { busy = false; }
}

function games(container) {
  heading('Games', 'Your collection. Your devices. Your next session.');
  const configured = new Map(state.games.map(game => [game.key, game]));
  if (state.shortcuts.length) {
    const featured = state.shortcuts.find(shortcut => shortcut.key === featuredKey)
      || state.shortcuts.find(shortcut => configured.get(shortcut.key)?.status === 'conflict') || state.shortcuts[0];
    const game = configured.get(featured.key);
    const feature = el('section', undefined, 'featured-game');
    feature.append(artwork(featured, 'hero'));
    const details = el('div', undefined, 'featured-details');
    details.append(el('p', `On ${state.device.name}`, 'muted'), el('h2', featured.title));
    details.append(game ? status(game) : el('p', 'Save sync not configured', 'muted'));
    details.append(el('p', game?.status === 'conflict'
      ? 'Local and cloud saves differ. Choose your progress before your next session.'
      : 'Keep your progress close, wherever you play next.', 'feature-copy'));
    details.append(button(game?.status === 'conflict' ? 'Choose progress' : 'View saves',
      () => game?.status === 'conflict' ? resolve(game) : (location.hash = 'saves'), true));
    feature.append(details);
    container.append(feature);
  }
  const listing = el('section', undefined, 'library-section');
  const title = el('header', undefined, 'section-heading');
  title.append(el('h2', 'On this device'), el('span', `${state.shortcuts.length} ${state.shortcuts.length === 1 ? 'game' : 'games'}`, 'muted'));
  listing.append(title);
  const grid = el('div', undefined, 'library-grid');
  for (const shortcut of state.shortcuts) {
    const game = configured.get(shortcut.key);
    const selected = shortcut.key === (featuredKey || state.shortcuts.find(item => configured.get(item.key)?.status === 'conflict')?.key || state.shortcuts[0]?.key);
    const card = el('article', undefined, `library-game${selected ? ' selected' : ''}`);
    const select = button('', () => { featuredKey = shortcut.key; render(true); });
    select.className = 'cover-select';
    select.dataset.gameKey = shortcut.key;
    select.setAttribute('aria-label', `Show ${shortcut.title}`);
    select.setAttribute('aria-pressed', String(selected));
    select.append(artwork(shortcut));
    card.append(select);
    const details = el('div', undefined, 'library-details');
    details.append(el('h3', shortcut.title));
    details.append(game ? status(game) : el('span', 'Save sync not configured', 'muted'));
    card.append(details);
    grid.append(card);
  }
  listing.append(grid);
  if (!state.shortcuts.length) empty(listing, 'No non-Steam shortcuts found',
    'Add your games to Steam. They will appear here when NoSteam checks your library.');
  container.append(listing);
  const sharing = panel('Game sharing is off', 'Direct game transfers are not available in this build.');
  sharing.classList.add('sharing-strip');
  container.append(sharing);
}
function saves(container) {
  heading('Saves', 'Your save progress, with its last confirmed revision on this device.');
  const listing = panel('Save progress');
  if (!state.games.length) empty(listing, 'No games configured for saves yet',
    'Existing NoSteam save settings appear here automatically. Setup for additional games is coming next.');
  for (const game of state.games) {
    const node = el('article', undefined, 'save-row');
    const top = row(game.title, `Last synced ${time(game.synced_at)}`, status(game));
    top.prepend(artwork(game, 'portrait'));
    const device = el('div', undefined, 'save-device');
    const detail = el('div');
    detail.append(el('span', state.device.name), el('p', game.revision
      ? `Revision ${game.revision.slice(0, 8)}` : 'No applied cloud revision', 'muted'));
    const actions = el('div', undefined, 'actions');
    if (!game.removed) actions.append(button(game.status === 'conflict' ? 'Choose progress' : 'Check saves',
      () => game.status === 'conflict' ? resolve(game) : sync(game)));
    actions.append(button('History', () => history(game)));
    device.append(detail, actions);
    node.append(top, device);
    listing.append(node);
  }
  container.append(listing);
  const other = panel('Other devices', 'Other devices have not reported their applied save revisions yet.');
  other.append(el('p', 'A completed upload here does not confirm a download on another device.', 'muted'));
  container.append(other);
}
function transfers(container) {
  heading('Transfers', 'Work managed by the background service.');
  const queue = panel('Background commands');
  for (const task of state.tasks) queue.append(row(task.action.replaceAll('-', ' '), time(task.created), el('span', task.state)));
  if (!state.tasks.length) empty(queue, 'Nothing waiting in the queue', 'Your save uploads are retried in the background.');
  container.append(queue);
  const workers = panel('Background work');
  const saveState = { idle: 'Waiting for pending saves', checking: 'Checking pending uploads', signed_out: 'Sign in required', starting: 'Starting', error: 'Needs attention' };
  workers.append(row('Save upload retries', `Last checked ${time(state.workers.saves.checked_at)}`,
    el('span', saveState[state.workers.saves.state] || 'Unknown')),
  row('Game transfers', 'Game sharing is off', el('span', 'Unavailable in this build', 'muted')));
  container.append(workers);
}
async function refreshCloud() {
  cloudError = undefined;
  try { cloud = await command('refresh-cloud'); }
  catch (error) { cloudError = error.message; }
  render(true);
}
function devices(container) {
  heading('Devices', 'This device and the devices linked to your account.');
  const local = panel('This device');
  local.append(row(state.device.name, 'Background service running',
    el('span', state.device.connected ? 'Account linked' : 'Sign in required')));
  container.append(local);
  const linked = panel('Account devices');
  linked.append(button('Refresh account devices', refreshCloud));
  if (cloudError) linked.append(el('p', cloudError, 'attention'));
  if (cloud) {
    linked.append(el('p', `Account checked ${time(cloud.checked_at)}. Live availability is unknown.`, 'muted'));
    for (const device of cloud.devices) linked.append(row(device.name, device.revoked ? 'Signed out' :
      device.expires <= Date.now() / 1000 ? 'Credential expired' : 'Linked to your account', el('span', 'Online status unknown', 'muted')));
    if (!cloud.devices.length) empty(linked, 'No linked devices', 'Sign in on each device to link it to your account.');
  } else if (!cloudError) linked.append(el('p', 'Refresh to retrieve your account’s devices.', 'muted'));
  container.append(linked);
}
function settings(container) {
  heading('Settings', 'Account and background sync on this device.');
  const account = panel('Account');
  account.append(row(state.device.name, state.device.connected ? 'This device is linked to your account.' : 'Sign in to connect cloud saves.',
    button(state.sign_in.status === 'waiting' ? 'Continue sign-in' : state.device.connected ? 'Renew sign-in' : 'Sign in', signIn, true)));
  container.append(account);
  const background = panel('Background service');
  background.append(row('Save sync', 'Existing Steam launch wrappers check saves before play and upload after exit.', el('span', 'Available')),
    row('Window behavior', 'Closing this window leaves background work running.', el('span', 'Runs independently')),
    row('Game sharing', 'Direct game transfers are not available in this build.', el('span', 'Off')));
  container.append(background);
}

function render(force = false) {
  const signature = JSON.stringify([page, state, cloud, cloudError]);
  if (!force && lastRender === signature) return;
  lastRender = signature;
  const active = document.activeElement;
  const focusedGame = active?.dataset.gameKey;
  const focusedLabel = active?.tagName === 'BUTTON' ? active.textContent : null;
  for (const link of document.querySelectorAll('nav a')) {
    if (link.textContent === page) link.setAttribute('aria-current', 'page');
    else link.removeAttribute('aria-current');
  }
  const container = document.querySelector('#content');
  container.className = page.toLowerCase();
  container.replaceChildren();
  const account = document.querySelector('#account-state');
  account.textContent = state.device.connected ? state.device.name : 'Sign in';
  account.onclick = () => { location.hash = 'settings'; };
  ({ Games: games, Saves: saves, Transfers: transfers, Devices: devices, Settings: settings })[page](container);
  document.querySelector('#service-state').textContent = 'Background service connected';
  if (focusedGame) [...document.querySelectorAll('[data-game-key]')].find(node => node.dataset.gameKey === focusedGame)?.focus();
  else if (focusedLabel) [...document.querySelectorAll('#content button')].find(node => node.textContent === focusedLabel)?.focus();
}

async function refresh(force = false) {
  if (!bridge || reading) return;
  reading = true;
  try {
    state = await bridge.status();
    render(force);
  } catch (error) {
    document.querySelector('#service-state').textContent = 'Background service unavailable';
    showNotice(error.message);
  } finally { reading = false; }
}

function mount() {
  document.querySelector('#app').innerHTML = `
    <header class="topbar"><div class="topbar-inner">
      <a class="brand" href="#games"><svg viewBox="0 0 32 32" aria-hidden="true"><path d="M5 7h14l8 8v11H5zM11 7v9h11M11 26v-6h10v6"/></svg>NoSteam</a>
      <nav aria-label="Main navigation"></nav><button id="account-state">Sign in</button>
    </div></header>
    <main><header class="page-heading"><div><h1>Games</h1><p id="description" class="muted">Connecting to this device…</p></div><button id="refresh">Refresh</button></header>
      <p id="notice" role="status" aria-live="polite"></p><div id="content"></div>
      <footer><span id="service-state">Connecting to background service…</span><span>Non-Steam games · Your devices</span></footer>
    </main><dialog aria-label="Save details"></dialog>`;
  for (const name of screens) {
    const link = el('a', name); link.href = `#${name.toLowerCase()}`;
    document.querySelector('nav').append(link);
  }
  document.querySelector('#refresh').onclick = () => refresh(true);
  window.addEventListener('hashchange', () => {
    page = screens.find(name => name.toLowerCase() === location.hash.slice(1)) || 'Games';
    if (state) render(true);
  });
}
mount();
if (bridge) {
  refresh(true);
  setInterval(refresh, 3000);
  setInterval(async () => {
    if (!state || state.sign_in.status !== 'waiting' || pollingPair) return;
    pollingPair = true;
    try { await command('poll-sign-in'); await refresh(true); }
    catch (error) { showNotice(error.message); }
    finally { pollingPair = false; }
  }, 5500);
} else {
  document.querySelector('#service-state').textContent = 'Desktop app required';
  showNotice('Open the installed NoSteam app to connect to this device.');
  document.querySelector('#refresh').disabled = true;
}
