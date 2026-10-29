import assert from 'node:assert/strict';
import { test } from 'node:test';
import { nextInDirection, gamepadActions } from '../src/controller.js';
import { consoleZoom, gamingSession } from '../window-layout.mjs';

test('spatial navigation follows rows and columns without jumping to a distant control', () => {
  const items = [{x:0,y:0,width:100,height:80},{x:120,y:0,width:100,height:80},{x:0,y:100,width:100,height:80},{x:120,y:100,width:100,height:80}];
  assert.equal(nextInDirection(items, items[0], 'right'), items[1]);
  assert.equal(nextInDirection(items, items[0], 'down'), items[2]);
  assert.equal(nextInDirection(items, items[3], 'up'), items[1]);
  assert.equal(nextInDirection(items, items[0], 'left'), null);
  assert.equal(nextInDirection([items[0], {x:120,y:-200,width:100,height:50}], items[0], 'right'), null);
});

test('controller directions have a deadzone and pad buttons use console actions', () => {
  const pad = { axes: [.2, -.3], buttons: Array.from({ length: 16 }, () => ({pressed:false})) };
  assert.deepEqual(gamepadActions(pad), []);
  pad.axes[0] = .8;
  pad.buttons[0].pressed = true;
  pad.buttons[5].pressed = true;
  assert.deepEqual(gamepadActions(pad), ['right', 'confirm', 'next']);
});

test('Gamescope and Steam launches get console sizing without shrinking handheld controls', () => {
  assert.equal(gamingSession({}, 'GAMESCOPE_PID(CARDINAL) = 2369'), true);
  assert.equal(gamingSession({XDG_CURRENT_DESKTOP:'gamescope'}), true);
  assert.equal(gamingSession({SteamGameId:'10259495419163705344'}), true);
  assert.equal(gamingSession({XDG_CURRENT_DESKTOP:'KDE'}, 'GAMESCOPE_PID: not found.'), false);
  assert.equal(consoleZoom(2560,1600),2);
  assert.equal(consoleZoom(3840,2160),2.7);
  assert.equal(consoleZoom(1280,720),1);
});
