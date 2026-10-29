const selector = 'button:not(:disabled), a[href], input:not(:disabled), select:not(:disabled), [tabindex="0"]';

export function nextInDirection(items, current, direction) {
  if (!items.length) return null;
  if (!current) return items[0];
  const horizontal = direction === 'left' || direction === 'right';
  const sign = direction === 'left' || direction === 'up' ? -1 : 1;
  const cx = current.x + current.width / 2, cy = current.y + current.height / 2;
  return items.filter(item => item !== current).map(item => {
    const dx = item.x + item.width / 2 - cx, dy = item.y + item.height / 2 - cy;
    const forward = (horizontal ? dx : dy) * sign;
    const cross = Math.abs(horizontal ? dy : dx);
    const beam = cross < ((horizontal ? current.height + item.height : current.width + item.width) / 2);
    return { item, forward, score: forward + cross * 3 + (beam ? 0 : 10000) };
  }).filter(item => item.forward > 2 && (!horizontal || item.score < 10000)).sort((a, b) => a.score - b.score)[0]?.item || null;
}

export function gamepadActions(pad) {
  const pressed = index => !!pad.buttons[index]?.pressed;
  const x = pad.axes[0] || 0, y = pad.axes[1] || 0;
  const actions = [];
  if (pressed(14) || x < -.55) actions.push('left');
  else if (pressed(15) || x > .55) actions.push('right');
  else if (pressed(12) || y < -.55) actions.push('up');
  else if (pressed(13) || y > .55) actions.push('down');
  if (pressed(0)) actions.push('confirm');
  if (pressed(1)) actions.push('back');
  if (pressed(4)) actions.push('previous');
  if (pressed(5)) actions.push('next');
  return actions;
}

export function controllerNavigation({ changeTab }) {
  let previous = new Set(), nextRepeat = 0, dialogReturn;
  function scope() { return document.querySelector('dialog[open]') || document.querySelector('#app'); }
  function controls() {
    return [...scope().querySelectorAll(selector)].filter(node => node.getClientRects().length && !node.closest('[hidden]'));
  }
  function focus(node) {
    if (!node) return;
    node.focus({ preventScroll: true });
    node.scrollIntoView({ block: 'nearest', inline: 'nearest', behavior: 'instant' });
  }
  function initial() {
    focus(document.querySelector('#content .cover-select') || document.querySelector('#content button') || document.querySelector('nav a[aria-current]'));
  }
  function act(action) {
    document.documentElement.classList.add('controller-mode');
    if (action === 'previous' || action === 'next') {
      if (!document.querySelector('dialog[open]')) changeTab(action === 'next' ? 1 : -1);
      return;
    }
    if (action === 'back') {
      const dialog = document.querySelector('dialog[open]');
      if (dialog) dialog.close();
      else focus(document.querySelector('nav a[aria-current]'));
      return;
    }
    if (action === 'right' && document.activeElement.closest('nav')) { initial(); return; }
    const nodes = controls();
    if (action === 'confirm') {
      if (nodes.includes(document.activeElement)) document.activeElement.click();
      else initial();
      return;
    }
    const items = nodes.map(node => ({ node, ...rect(node) }));
    const current = items.find(item => item.node === document.activeElement);
    focus(nextInDirection(items, current, action)?.node);
  }
  document.addEventListener('keydown', event => {
    if (event.altKey || event.ctrlKey || event.metaKey || /INPUT|TEXTAREA|SELECT/.test(event.target.tagName)) return;
    const action = { ArrowLeft: 'left', ArrowRight: 'right', ArrowUp: 'up', ArrowDown: 'down', Escape: 'back', PageUp: 'previous', PageDown: 'next' }[event.key];
    if (action) { event.preventDefault(); act(action); }
    else if (event.key === 'Tab') document.documentElement.classList.add('controller-mode');
  });
  document.addEventListener('pointerdown', () => document.documentElement.classList.remove('controller-mode'));
  // Dialogs keep navigation inside their controls and return to the original tile.
  const dialog = document.querySelector('dialog');
  document.addEventListener('focusin', event => { if (!dialog.open && event.target.matches(selector)) dialogReturn = event.target; });
  const observer = new MutationObserver(() => {
    if (dialog.open) focus(dialog.querySelector(selector));
  });
  observer.observe(dialog, { attributes: true, attributeFilter: ['open'] });
  dialog.addEventListener('close', () => {
    const replacement = dialogReturn?.dataset.gameKey && [...document.querySelectorAll('[data-game-key]')].find(node => node.dataset.gameKey === dialogReturn.dataset.gameKey);
    if (dialogReturn?.isConnected) focus(dialogReturn); else if (replacement) focus(replacement); else initial();
  });
  function poll(now) {
    const pad = [...(navigator.getGamepads?.() || [])].find(item => item?.connected);
    const actions = new Set(pad ? gamepadActions(pad) : []);
    const scroll = pad?.axes[3] || 0;
    if (Math.abs(scroll) > .25) {
      document.documentElement.classList.add('controller-mode');
      (document.querySelector('dialog[open]') || document.querySelector('main')).scrollBy({ top: scroll * 18, behavior: 'instant' });
    }
    let repeated = false;
    for (const action of actions) {
      const held = previous.has(action);
      const directional = ['left', 'right', 'up', 'down'].includes(action);
      if (!held || (directional && now >= nextRepeat)) { act(action); if (directional) repeated = true; }
    }
    if (repeated) nextRepeat = now + ([...actions].some(action => previous.has(action)) ? 120 : 330);
    previous = actions;
    requestAnimationFrame(poll);
  }
  requestAnimationFrame(poll);
  return { initial, focus };
}

function rect(node) {
  const { x, y, width, height } = node.getBoundingClientRect();
  return { x, y, width, height };
}
