import '@fontsource/inter/latin-400.css';
import '@fontsource/inter/latin-500.css';
import '@fontsource/inter/latin-600.css';
import '@fontsource/space-grotesk/latin-600.css';
import './style.css';
import { controllerNavigation } from './controller.js';
import { liveState, syncLabels } from './device-status.js';

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
let libraryView = 'local';
let controls;
const artCache = new Map();
const transferSamples = new Map();
const transferRates = new Map();
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
  if (game.key) artObserver.observe(node);
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
  return waitTask(queued);
}
async function waitTask(queued) {
  if (!queued) return null;
  for (let attempt = 0; attempt < 120; attempt++) {
    const task = await bridge.task(queued.id);
    if (task.state === 'completed') return task.result;
    if (task.state === 'failed') throw new Error(task.error);
    await new Promise(resolve => setTimeout(resolve, 500));
  }
  throw new Error('The command is still queued or running. Check Transfers for its progress.');
}

async function enableGames(enabled) {
  await command('games-enable', { enabled });
  await refresh(true);
}
async function setupSaves(game) {
  const result = await waitTask(await bridge.setupSaves(game.key));
  if (result) { showNotice(`Save sync configured for ${game.title}. Reopen Steam to play.`); await refresh(true); }
}
async function shareGame(game) {
  const result = await waitTask(await bridge.shareGame(game.key));
  if (result) { showNotice(`Indexing ${game.title} for sharing.`); location.hash = 'transfers'; await refresh(true); }
}
async function downloadGame(offer) {
  const result = await waitTask(await bridge.downloadGame(offer.game, offer.version));
  if (result) { showNotice(`${offer.title} added to the transfer queue.`); location.hash = 'transfers'; await refresh(true); }
}
function bytes(value) {
  if (value >= 1024 ** 3) return `${(value / 1024 ** 3).toFixed(1)} GiB`;
  if (value >= 1024 ** 2) return `${(value / 1024 ** 2).toFixed(1)} MiB`;
  if (value >= 1024) return `${(value / 1024).toFixed(1)} KiB`;
  return `${Math.round(value)} B`;
}
function recordTransferRates(next) {
  const now = performance.now();
  for (const job of next.game_sync?.jobs || []) {
    const previous = transferSamples.get(job.id);
    transferRates.set(job.id, previous && previous.state === 'running' && job.state === 'running'
      ? Math.max(0, (job.verified - previous.verified) / ((now - previous.at) / 1000)) : null);
    transferSamples.set(job.id, { at: now, verified: job.verified, state: job.state });
  }
}
const gamePhases = {
  off: 'Game transfers are off', active: 'Ready for direct transfers',
  signed_out: 'Sign in to enable transfers', waiting_for_server: 'Waiting for the account server',
  port_in_use: 'Another app is using the transfer port', no_network_address: 'Waiting for a LAN connection',
};

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

function gameDetails(shortcut, game) {
  const dialog = document.querySelector('dialog');
  dialog.replaceChildren(el('h2', shortcut.title));
  dialog.append(game ? status(game) : el('p', 'Save sync not configured', 'muted'));
  const actions = el('div', undefined, 'game-dialog-actions');
  if (game) actions.append(button(game.status === 'conflict' ? 'Choose progress' : 'View saves', () => {
    dialog.close();
    if (game.status === 'conflict') resolve(game); else location.hash = 'saves';
  }, true));
  else if (state.device.connected) actions.append(button('Set up saves', async () => { dialog.close(); await setupSaves(shortcut); }, true));
  if (game && state.game_sync?.enabled) actions.append(button('Share game folder', async () => { dialog.close(); await shareGame({ ...shortcut, id: game.id }); }));
  actions.append(button('Back to library', () => dialog.close()));
  dialog.append(actions);
  dialog.showModal();
}

function games(container) {
  heading('Games', 'Choose a game to manage its saves and installation.');
  const configured = new Map(state.games.map(game => [game.key, game]));
  const toolbar = el('div', undefined, 'library-toolbar');
  const views = el('div', undefined, 'library-views');
  views.setAttribute('role', 'group'); views.setAttribute('aria-label', 'Game library');
  for (const [key, label] of [['local', 'This device'], ['available', 'Other devices'], ['installed', 'Installed']]) {
    const tab = button(label, () => { libraryView = key; render(true); });
    tab.dataset.focusId = 'library-' + key;
    tab.setAttribute('aria-pressed', String(libraryView === key));
    views.append(tab);
  }
  const sharing = button(state.game_sync?.enabled ? 'Transfers on' : 'Transfers off', () => enableGames(!state.game_sync?.enabled));
  sharing.className = 'sharing-toggle';
  sharing.setAttribute('aria-pressed', String(!!state.game_sync?.enabled));
  sharing.title = gamePhases[state.game_sync?.state || 'off'];
  toolbar.append(views, sharing); container.append(toolbar);
  if (libraryView === 'local') {
    const listing = el('section', undefined, 'library-section');
    const title = el('header', undefined, 'section-heading');
    title.append(el('h2', 'On this device'), el('span', `${state.shortcuts.length} ${state.shortcuts.length === 1 ? 'game' : 'games'}`, 'muted'));
    listing.append(title);
    const grid = el('div', undefined, 'library-grid');
    for (const shortcut of state.shortcuts) {
      const game = configured.get(shortcut.key);
      const card = button('', () => gameDetails(shortcut, game));
      card.className = 'library-game cover-select';
      card.dataset.gameKey = shortcut.key;
      card.setAttribute('aria-label', `Manage ${shortcut.title}`);
      card.setAttribute('aria-haspopup', 'dialog');
      card.append(artwork(shortcut));
      const details = el('div', undefined, 'library-details');
      details.append(el('h3', shortcut.title), game ? status(game) : el('span', 'Save sync not configured', 'muted'));
      card.append(details); grid.append(card);
    }
    listing.append(grid);
    if (!state.shortcuts.length) empty(listing, 'No non-Steam shortcuts found', 'Add your games to Steam to see them here.');
    container.append(listing);
  }
  if (libraryView === 'available') {
  const available = panel('Available on your devices');
  const grouped = new Map();
  for (const offer of state.game_sync?.offers || []) {
    if (offer.device_id === state.game_sync.self_device_id) continue;
    const key = `${offer.game}:${offer.version}`;
    if (!grouped.has(key)) grouped.set(key, { ...offer, sources: [] });
    grouped.get(key).sources.push(offer);
  }
  for (const offer of grouped.values()) {
    const online = offer.sources.filter(source => source.available && Date.now() / 1000 - source.seen < 90).length;
    const action = button(online ? 'Download' : 'Queue download', () => downloadGame(offer));
    action.disabled = !state.game_sync.enabled;
    available.append(row(offer.title, `${bytes(offer.size)} · Version ${offer.version.slice(0, 8)} · ${online} sources online`, action));
  }
  if (!grouped.size) empty(available, 'No games offered by another device yet', 'Enable transfers on another device, then share a game folder from its library.');
  container.append(available);
  }
  if (libraryView === 'installed') {
  const installed = state.game_sync?.jobs.filter(job => job.kind === 'download' && job.state === 'completed') || [];
  if (installed.length) {
    const downloaded = panel('Installed games');
    for (const job of installed) {
      const actions = el('div', undefined, 'actions');
      if (job.ready) actions.append(button('Play in Steam', () => bridge.playInstallation(job.id), true));
      actions.append(button('Open folder', () => bridge.openInstallation(job.id)));
      downloaded.append(row(job.title, job.ready ? `Ready in Steam${job.proton ? ` · ${job.proton}` : ''}` : 'Files only · Re-share from an updated source to include Steam setup', actions));
    }
    container.append(downloaded);
  }    if (!installed.length) {
      const downloaded = panel('Installed games');
      empty(downloaded, 'No transferred installations yet', 'Choose Other devices to download a shared game.');
      container.append(downloaded);
    }
  }
}

function saves(container) {
  heading('Saves', 'See which device has the latest progress for each game.');
  const listing = panel('Save progress');
  const shared = sharedStatus();
  const rows = new Map((shared?.games || []).map(game => [game.game, {
    id: game.game, title: game.title, revision: game.revision, remoteOnly: true,
  }]));
  for (const game of state.games) rows.set(game.id, game);
  if (!rows.size) empty(listing, 'No games configured for saves yet',
    'Existing NoSteam save settings appear here automatically. Setup for additional games is coming next.');
  for (const game of rows.values()) {
    const node = el('article', undefined, 'save-row');
    node.dataset.gameId = game.id;
    const top = row(game.title, game.remoteOnly ? 'On your other devices' : `Last synced ${time(game.synced_at)}`,
      game.remoteOnly ? undefined : status(game));
    top.prepend(artwork(game, 'portrait'));
    node.append(top);
    if (shared) {
      node.append(deviceMatrix(game, shared));
      const actions = el('div', undefined, 'actions save-actions');
      actions.append(button('History', () => history(game)));
      node.append(actions);
      listing.append(node);
      continue;
    }
    const device = el('div', undefined, 'save-device');
    const detail = el('div');
    detail.append(el('span', state.device.name), el('p', game.revision
      ? `Revision ${game.revision.slice(0, 8)}` : 'No applied cloud revision', 'muted'));
    const actions = el('div', undefined, 'actions');
    if (!game.removed) actions.append(button(game.status === 'conflict' ? 'Choose progress' : 'Check saves',
      () => game.status === 'conflict' ? resolve(game) : sync(game)));
    actions.append(button('History', () => history(game)));
    device.append(detail, actions);
    node.append(device);
    listing.append(node);
  }
  container.append(listing);
  if (shared) container.append(el('p', `Device reports checked ${time(shared.checked_at)}. Offline devices show their last confirmed state.`, 'muted'));
  else {
    const other = panel('Device reports', state.device.connected
      ? 'Waiting for the first device report. Refresh to check your account.'
      : 'Sign in to see save progress across your devices.');
    other.append(button('Refresh device reports', refreshCloud));
    if (cloudError) other.append(el('p', cloudError, 'attention'));
    container.append(other);
  }
}

function sharedStatus() {
  const candidates = [cloud, state?.sync].filter(Boolean);
  return candidates.sort((a, b) => b.checked_at - a.checked_at)[0];
}
function deviceMatrix(game, shared) {
  const matrix = el('div', undefined, 'device-matrix');
  const remote = shared.games.find(item => item.game === game.id);
  for (const device of shared.devices) {
    const cell = remote?.devices.find(item => item.device_id === device.id);
    const current = liveState(shared, device, cell);
    const block = el('section', undefined, `device-cell state-${current}`);
    const header = el('div', undefined, 'device-cell-heading');
    header.append(el('h4', device.name), el('span', device.id === shared.self_device_id ? 'This device' :
      device.availability === 'online' && current !== 'last_confirmed' ? 'Online' :
      device.availability === 'offline' ? 'Offline' : 'Linked', 'muted'));
    block.append(header, el('p', syncLabels[current] || 'Not checked', 'device-sync-state'));
    block.append(el('p', cell?.revision ? `Revision ${cell.revision.slice(0, 8)}` : 'No applied cloud revision', 'muted'));
    block.append(el('p', cell?.reported_at ? `Reported ${time(cell.reported_at)}` : 'Waiting for a device report', 'muted'));
    if (device.id === shared.self_device_id && !game.remoteOnly && !game.removed) {
      block.append(button(game.status === 'conflict' ? 'Choose progress' : 'Check saves',
        () => game.status === 'conflict' ? resolve(game) : sync(game)));
    }
    matrix.append(block);
  }
  return matrix;
}
function transfers(container) {
  heading('Transfers', 'Work managed by the background service.');
  const downloads = panel('Game transfers');
  const labels = { queued: 'Queued', running: 'Downloading', verifying: 'Verifying files',
    activating: 'Finishing installation', waiting_for_source: 'Waiting for a source',
    paused: 'Paused', failed: 'Needs attention', completed: 'Complete',
    awaiting_steam: 'Close Steam to finish setup', requires_dependency: 'Host setup required' };
  for (const job of state.game_sync?.jobs || []) {
    const block = el('article', undefined, 'transfer-job');
    block.dataset.jobId = job.id;
    const controls = el('div', undefined, 'actions');
    if (job.state !== 'completed') controls.append(button(['paused', 'failed', 'requires_dependency'].includes(job.state) ? 'Resume' : 'Pause', async () => {
      await command(['paused', 'failed', 'requires_dependency'].includes(job.state) ? 'resume-transfer' : 'pause-transfer', { job: job.id });
      await refresh(true);
    }));
    else if (job.kind === 'download') {
      if (job.ready) controls.append(button('Play in Steam', () => bridge.playInstallation(job.id), true));
      controls.append(button('Open folder', () => bridge.openInstallation(job.id)));
    }
    const label = job.kind === 'index' ? job.state === 'completed' ? 'Ready to share' : job.state === 'running' ? 'Indexing game folder' : labels[job.state] : labels[job.state];
    block.append(row(job.title, `${label || job.state} · ${job.kind === 'index' ? 'Sharing' : 'Download'}`, controls));
    if (job.kind === 'download') {
      const progress = el('progress'); progress.max = job.total || 1; progress.setAttribute('aria-label', `${job.title} verified download progress`);
      if (job.total) progress.value = job.verified;
      block.append(progress, el('p', `${bytes(job.verified)} verified${job.total ? ` of ${bytes(job.total)}` : ''}`, 'muted'));
      if (job.state === 'running') {
        const rate = transferRates.get(job.id);
        const remaining = rate > 0 && job.total ? Math.ceil((job.total - job.verified) / rate) : null;
        block.append(el('p', rate > 0 ? `${bytes(rate)}/s verified${remaining !== null ? ` · About ${remaining < 60 ? `${remaining} seconds` : `${Math.ceil(remaining / 60)} minutes`} remaining` : ''}` : 'Calculating transfer speed…', 'muted'));
      }
    } else if (job.total) block.append(el('p', `${bytes(job.total)} indexed`, 'muted'));
    if (job.ready) block.append(el('p', `Ready in Steam${job.proton ? ` · ${job.proton}` : ''}`, 'muted'));
    if (job.error) block.append(el('p', job.error, 'attention'));
    downloads.append(block);
  }
  if (!state.game_sync?.jobs.length) empty(downloads, 'No game transfers queued', 'Share a game on one device, then choose Download on another.');
  container.append(downloads);
  const queue = panel('Background commands');
  for (const task of state.tasks) queue.append(row(task.action.replaceAll('-', ' '), time(task.created), el('span', task.state)));
  if (!state.tasks.length) empty(queue, 'Nothing waiting in the queue', 'Your save uploads are retried in the background.');
  container.append(queue);
  const workers = panel('Background work');
  const saveState = { idle: 'Waiting for pending saves', checking: 'Checking pending uploads', signed_out: 'Sign in required', starting: 'Starting', error: 'Needs attention' };
  workers.append(row('Save upload retries', `Last checked ${time(state.workers.saves.checked_at)}`,
    el('span', saveState[state.workers.saves.state] || 'Unknown')),
  row('Device reports', `Last sent ${time(state.workers.reporting?.checked_at)}`,
    el('span', ({ idle: 'Reporting devices', signed_out: 'Sign in required', unavailable: 'Could not update device reports' })[state.workers.reporting?.state] || 'Starting')),
  row('Game transfers', state.game_sync?.enabled ? 'Enabled on this device' : 'Disabled on this device', el('span', gamePhases[state.game_sync?.state || 'off'] || 'Needs attention', 'muted')));
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
  const shared = sharedStatus();
  if (shared) {
    linked.append(el('p', `Account checked ${time(shared.checked_at)}.`, 'muted'));
    for (const device of shared.devices) {
      const current = liveState(shared, device, { state: device.availability === 'unknown' ? 'not_reported' : 'up_to_date' });
      const available = current === 'signed_out' ? 'Signed out' : current === 'not_reported' ? 'Not reported yet' : current === 'last_confirmed' ? 'Offline · last confirmed' : 'Online';
      linked.append(row(device.name, device.seen ? `Last seen ${time(device.seen)}` : 'Linked to your account', el('span', available, 'muted')));
    }
    if (!shared.devices.length) empty(linked, 'No linked devices', 'Sign in on each device to link it to your account.');
  } else if (!cloudError) linked.append(el('p', 'Refresh to retrieve your account’s devices.', 'muted'));
  container.append(linked);
}
function settings(container) {
  heading('Settings', 'Account and background sync on this device.');
  if (state.setup_error) {
    const recovery = panel('Interrupted save setup');
    recovery.append(el('p', state.setup_error), button('Finish recovery', async () => {
      await command('recover-save-setup'); await refresh(true);
    })); container.append(recovery);
  }
  const account = panel('Account');
  account.append(row(state.device.name, state.device.connected ? 'This device is linked to your account.' : 'Sign in to connect cloud saves.',
    button(state.sign_in.status === 'waiting' ? 'Continue sign-in' : state.device.connected ? 'Renew sign-in' : 'Sign in', signIn, true)));
  container.append(account);
  const background = panel('Background service');
  background.append(row('Save sync', 'Existing Steam launch wrappers check saves before play and upload after exit.', el('span', 'Available')),
    row('Window behavior', 'Closing this window leaves background work running.', button('Close app', () => bridge.close())),
    row('Game transfers', 'Direct downloads, verified chunks, and resumable progress.', button(state.game_sync?.enabled ? 'Disable transfers' : 'Enable transfers', () => enableGames(!state.game_sync?.enabled))));
  container.append(background);
}

function render(force = false) {
  const shared = sharedStatus();
  const freshness = shared?.devices.map(device => liveState(shared, device, { state: 'up_to_date' }));
  const signature = JSON.stringify([page, state, cloud, cloudError, freshness]);
  if (!force && lastRender === signature) return;
  lastRender = signature;
  const active = document.activeElement;
  const focusId = active?.dataset.focusId;
  const focusedContext = active?.closest('[data-job-id], [data-game-id]');
  const contextId = focusedContext?.dataset.jobId || focusedContext?.dataset.gameId;
  const focusedGame = active?.dataset.gameKey;
  const focusedSave = active?.closest('.save-row')?.dataset.gameId;
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
  if (focusId) document.querySelector(`[data-focus-id="${focusId}"]`)?.focus({ preventScroll: true });
  else if (focusedGame) [...document.querySelectorAll('[data-game-key]')].find(node => node.dataset.gameKey === focusedGame)?.focus({ preventScroll: true });
  else if (contextId) [...document.querySelectorAll('[data-job-id], [data-game-id]')].find(node => (node.dataset.jobId || node.dataset.gameId) === contextId)?.querySelectorAll('button').forEach(node => { if (node.textContent === focusedLabel) node.focus({ preventScroll: true }); });
  else if (focusedSave) [...document.querySelectorAll('.save-row')].find(node => node.dataset.gameId === focusedSave)
    ?.querySelectorAll('button').forEach(node => { if (node.textContent === focusedLabel) node.focus(); });
  else if (focusedLabel) [...document.querySelectorAll('#content button')].find(node => node.textContent === focusedLabel)?.focus({ preventScroll: true });
  if (!active || active === document.body) controls?.initial();
  requestAnimationFrame(updateScrollHint);
}

async function refresh(force = false) {
  if (!bridge || reading) return;
  reading = true;
  try {
    state = await bridge.status();
    recordTransferRates(state);
    render(force);
  } catch (error) {
    document.querySelector('#service-state').textContent = 'Background service unavailable';
    showNotice(error.message);
  } finally { reading = false; }
}

function updateScrollHint() {
  const main = document.querySelector('main');
  document.querySelector('#scroll-hint').textContent = main.scrollHeight - main.scrollTop - main.clientHeight > 12 ? '↓ More below' : '';
}

function mount() {
  document.querySelector('#app').innerHTML = `
    <header class="topbar"><div class="topbar-inner">
      <a class="brand" href="#games"><svg viewBox="0 0 32 32" aria-hidden="true"><path d="M5 7h14l8 8v11H5zM11 7v9h11M11 26v-6h10v6"/></svg>NoSteam</a>
      <nav aria-label="Main navigation"></nav><button id="account-state">Sign in</button>
    </div></header>
    <main><header class="page-heading"><div><h1>Games</h1><p id="description" class="muted">Connecting to this device…</p></div><button id="refresh">Refresh</button></header>
      <p id="notice" role="status" aria-live="polite"></p><div id="content"></div>
    </main>
    <footer class="controller-bar"><span id="service-state">Connecting to background service…</span><span id="scroll-hint"></span><div class="controller-hints"><span><kbd>LB</kbd><kbd>RB</kbd> Tabs</span><span><kbd>A</kbd> Select</span><span><kbd>B</kbd> Back</span><span><kbd>RS</kbd> Scroll</span></div></footer>
    <dialog aria-label="Game and save details"></dialog>`;
  controls = controllerNavigation({ changeTab: direction => {
    const index = screens.indexOf(page);
    location.hash = screens[(index + direction + screens.length) % screens.length].toLowerCase();
  } });
  document.querySelector('main').addEventListener('scroll', updateScrollHint, { passive: true });
  new ResizeObserver(updateScrollHint).observe(document.querySelector('main'));
  for (const name of screens) {
    const link = el('a', name); link.href = `#${name.toLowerCase()}`;
    document.querySelector('nav').append(link);
  }
  document.querySelector('#refresh').onclick = async event => {
    event.currentTarget.disabled = true;
    try { await refresh(true); if (state?.device.connected) { await refreshCloud(); await command('refresh-games'); await refresh(true); } }
    catch (error) { showNotice(error.message); }
    finally { document.querySelector('#refresh').disabled = false; }
  };
  window.addEventListener('hashchange', () => {
    page = screens.find(name => name.toLowerCase() === location.hash.slice(1)) || 'Games';
    if (state) { render(true); document.querySelector('main').scrollTop = 0; controls.initial(); }
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
