import test from 'node:test';
import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {JSDOM} from 'jsdom';
import {Operations} from '../static/operations.js';
import {OperationalPanels} from '../static/panels.js';
import {RobotLink} from '../static/robot-link.js';
import {LiveMap} from '../static/live-map.js';
import {pointsFromZones, snapRoutePoint} from '../static/route-planner.js';

const copy = value => JSON.parse(JSON.stringify(value));
const tick = () => new Promise(resolve => setImmediate(resolve));
const zone = (id, x, y) => ({id, name: id, x, y, aim_deg: null, yaw_deg: null, helmet: true, vest: true, hazard: false, note: ''});
const point = (x, y, more = {}) => ({x, y, aim_deg: null, dwell_s: 0, label: null, ...more});
const route = (id = 'route-one') => ({id, name: id === 'route-one' ? '첫 동선' : '다른 동선', repeat: 1, points: [point(1, 1), point(3, 1)]});
const map = {available: true, extent: [0, 10, 0, 10], width: 100, height: 100};
const mapMeta = {width: 100, height: 100, resolution_m: .1, patrol_to_px: [[10, 0, 0], [0, -10, 100]], px_to_patrol: [[.1, 0, 0], [0, -.1, 10]], zones: []};
const goodPreview = body => ({valid: true, invalid_points: [], total_m: 2, segments: body.route.points.slice(1).map((item, index) => ({from_index: index, to_index: index + 1, valid: true, reason: '', points: [[body.route.points[index].x, body.route.points[index].y], [item.x, item.y]]}))});

async function setup({routes = [route()], preview = goodPreview, live = true, displayMap = mapMeta} = {}) {
  const dom = new JSDOM('<h1></h1><div id="panel"></div>', {url: 'http://127.0.0.1:8126'}), document = dom.window.document;
  let revision = 1, saved = copy(routes), zones = [zone('A', 1, 1), zone('B', 3, 1), zone('C', 4, 3), zone('D', 7, 3)];
  const calls = [], confirmations = [], notices = [];
  const digest = value => createHash('sha256').update(JSON.stringify(value)).digest('hex');
  const routesSnapshot = () => ({revision: 'r' + revision, saved: copy(saved), active: copy(saved), digests: Object.fromEntries(saved.map(item => [item.id, digest(item)])), pending_restart: false, map, writable: true});
  const planningSnapshot = () => ({revision: 'r' + revision, device: 'test-device', saved: {zones: copy(zones), random_after_first_cycle: false}, active: {zones: copy(zones), random_after_first_cycle: false}, pending_restart: false, map, arrival_radius_m: .3});
  const link = {
    baseUrl: '',
    async get(path) {
      calls.push({method: 'get', path});
      if (path === '/api/planning/routes') return routesSnapshot();
      if (path === '/api/planning') return planningSnapshot();
      if (path === '/api/map/meta') {if (displayMap instanceof Error) throw displayMap; return displayMap;}
      if (path === '/api/nav') return {available: true, pose: [1, 1, 0], seeded: true, stale: false, route: null};
      throw new Error('unexpected GET ' + path);
    },
    async post(path, body) {
      calls.push({method: 'post', path, body: copy(body)});
      if (path === '/api/command/locate') return {accepted: true, detail: '알려준 점 주변에서 위치 찾는 중'};
      if (body.revision !== 'r' + revision) throw new Error('지도 또는 계획이 바뀌었습니다. 저장본을 다시 불러오세요. (HTTP 409)');
      if (path === '/api/planning/routes/preview') return preview(body);
      if (path === '/api/planning/routes') {
        const index = saved.findIndex(item => item.id === body.route.id);
        if (index < 0) saved.push(copy(body.route)); else saved[index] = copy(body.route);
        revision++; return routesSnapshot();
      }
      if (path === '/api/planning') {zones = copy(body.zones); revision++; return planningSnapshot();}
      throw new Error('unexpected POST ' + path);
    },
    async delete(path, body) {
      calls.push({method: 'delete', path, body: copy(body)});
      if (body.revision !== 'r' + revision) throw new Error('HTTP 409');
      saved = saved.filter(item => item.id !== path.split('/').at(-1)); revision++; return routesSnapshot();
    },
    async route(action, id, expectedDigest) {calls.push({method: 'command', action, id, expectedDigest}); return {accepted: expectedDigest === digest(saved.find(item => item.id === id)), detail: '저장된 동선이 바뀌었습니다'};},
    async patrol(action) {calls.push({method: 'patrol', action}); return {accepted: true};},
    async goto(x, y) {calls.push({method: 'goto', x, y}); return {accepted: true};},
    locatePoint(x, y) {return RobotLink.prototype.locatePoint.call(this, x, y);},
  };
  const store = new Operations({storage: dom.window.localStorage}); store.demo = !live; store.link = link;
  const panels = new OperationalPanels({store, container: document.querySelector('#panel'), title: document.querySelector('h1'), document, onToast: value => notices.push(value), onNavigate: () => {}});
  clearInterval(panels.routePlanner.timer);
  panels.confirmDevice = options => confirmations.push(options);
  panels.render('zones'); await tick(); await tick();
  const loadCalls = calls.slice();
  calls.length = 0;
  return {dom, document, store, panels, planner: panels.routePlanner, link, calls, loadCalls, confirmations, notices,
    bump: () => revision++, replaceRoute: value => {saved[0] = copy(value); revision++;}, snapshot: routesSnapshot,
    close: () => {panels.routePlanner.dispose(); dom.window.close();}};
}

function input(ctx, name, value) {
  const element = ctx.document.querySelector('[name="' + name + '"]');
  assert.ok(element, name); element.value = value; element.dispatchEvent(new ctx.dom.window.Event('input', {bubbles: true})); return element;
}
function select(ctx, name, value) {
  const element = ctx.document.querySelector('[name="' + name + '"]'); element.value = value; element.dispatchEvent(new ctx.dom.window.Event('change', {bubbles: true}));
}
function mapElement(ctx) {
  const svg = ctx.document.querySelector('[data-route-map]');
  svg.getBoundingClientRect = () => ({left: 0, top: 0, width: 1000, height: 1000}); return svg;
}
function clickMap(ctx, x, y, options = {}) {
  const svg = mapElement(ctx), init = {bubbles: true, clientX: x, clientY: y, ...options};
  svg.dispatchEvent(new ctx.dom.window.MouseEvent('pointerdown', init));
  ctx.document.dispatchEvent(new ctx.dom.window.MouseEvent('pointerup', init));
  svg.dispatchEvent(new ctx.dom.window.MouseEvent('click', init));
}
function normalizedPoint(ctx, x, y) {
  const [left, top, width, height] = mapElement(ctx).getAttribute('viewBox').split(/\s+/).map(Number);
  return [(x - left) / width, (y - top) / height];
}
function circlePoint(ctx, selector) {
  const circle = ctx.document.querySelector(selector);
  assert.ok(circle, selector);
  return normalizedPoint(ctx, Number(circle.getAttribute('cx')), Number(circle.getAttribute('cy')));
}
function polylinePoints(ctx, selector) {
  return ctx.document.querySelector(selector).getAttribute('points').trim().split(/\s+/).map(pair => normalizedPoint(ctx, ...pair.split(',').map(Number)));
}
function near(actual, expected) {
  assert.equal(actual.length, expected.length);
  actual.forEach((value, index) => assert.ok(Math.abs(value - expected[index]) < 1e-6, `${actual} != ${expected}`));
}
function direction(from, to, displayMap) {
  const dx = (to[0] - from[0]) * displayMap.width, dy = (to[1] - from[1]) * displayMap.height;
  const length = Math.hypot(dx, dy);
  assert.ok(length > 0);
  return [dx / length, dy / length];
}
function drag(ctx, selector, x, y) {
  mapElement(ctx);
  ctx.document.querySelector(selector).dispatchEvent(new ctx.dom.window.MouseEvent('pointerdown', {bubbles: true}));
  ctx.document.dispatchEvent(new ctx.dom.window.MouseEvent('pointermove', {clientX: x, clientY: y}));
  ctx.document.dispatchEvent(new ctx.dom.window.MouseEvent('pointerup'));
}

const rotatedMaps = [
  {
    degrees: 180,
    meta: {...mapMeta, width: 200, height: 100, patrol_to_px: [[-10, 0, 200], [0, 10, 0]], px_to_patrol: [[-.1, 0, 20], [0, .1, 0]]},
    pins: [[.95, .1], [.85, .1]], forward: [-1, 0], positiveY: [0, 1],
    clicked: [6, 4], dragged: [4, 3], aimTarget: [800, 500],
  },
  {
    degrees: 90,
    meta: {...mapMeta, width: 100, height: 200, patrol_to_px: [[0, -10, 100], [-10, 0, 200]], px_to_patrol: [[0, -.1, 20], [-.1, 0, 10]]},
    pins: [[.9, .95], [.9, .85]], forward: [0, -1], positiveY: [-1, 0],
    clicked: [12, 3], dragged: [14, 2], aimTarget: [600, 300],
  },
];

for (const rotated of rotatedMaps) {
  test(`${rotated.degrees} degree display map aligns route points, zones, segments, robot and camera arrows`, async () => {
    const ctx = await setup({displayMap: rotated.meta, routes: [{...route(), points: [point(1, 1, {aim_deg: 0}), point(3, 1)]}]});
    try {
      assert.ok(ctx.loadCalls.some(call => call.method === 'get' && call.path === '/api/map/meta'));
      assert.deepEqual(ctx.planner.current.mapMeta, rotated.meta);
      const svg = mapElement(ctx), image = svg.querySelector('image');
      assert.match(image.getAttribute('href'), /^\/api\/map\.png(?:\?|$)/);
      assert.equal(svg.style.aspectRatio, `${rotated.meta.width}/${rotated.meta.height}`);
      rotated.pins.forEach((expected, index) => near(circlePoint(ctx, `[data-route-pin="${index}"] circle.route-pin`), expected));
      const zones = [...svg.querySelectorAll('.route-zone-anchor')];
      rotated.pins.forEach((expected, index) => near(normalizedPoint(ctx, Number(zones[index].getAttribute('cx')), Number(zones[index].getAttribute('cy'))), expected));
      polylinePoints(ctx, '[data-route-segment="0-1"]').forEach((actual, index) => near(actual, rotated.pins[index]));
      const robot = polylinePoints(ctx, '[data-route-robot]');
      const base = [(robot[1][0] + robot[2][0]) / 2, (robot[1][1] + robot[2][1]) / 2];
      near(robot[0].map((value, index) => (7 * value + 10 * base[index]) / 17), rotated.pins[0]);
      near(direction(base, robot[0], rotated.meta), rotated.forward);
      near(direction(rotated.pins[0], circlePoint(ctx, '[data-route-aim="0"]'), rotated.meta), rotated.forward);
      input(ctx, '보는 방향 (°)', '90');
      near(direction(rotated.pins[0], circlePoint(ctx, '[data-route-aim="0"]'), rotated.meta), rotated.positiveY);
      const arrow = ctx.document.querySelector('[data-route-pin="0"] .route-aim').getAttribute('d').match(/-?\d+(?:\.\d+)?(?:e[+-]?\d+)?/gi).map(Number);
      near(normalizedPoint(ctx, arrow[0], arrow[1]), rotated.pins[0]);
      near(normalizedPoint(ctx, arrow[2], arrow[3]), circlePoint(ctx, '[data-route-aim="0"]'));
      assert.equal(ctx.planner.current.draft.points[0].aim_deg, 90);
    } finally {ctx.close();}
  });

  test(`${rotated.degrees} degree map clicks and drags save patrol positions and camera heading`, async () => {
    const ctx = await setup({displayMap: rotated.meta});
    try {
      clickMap(ctx, 700, 400);
      assert.deepEqual(ctx.planner.current.draft.points.at(-1), point(...rotated.clicked));
      drag(ctx, '[data-route-pin="0"] circle.route-pin', 800, 300);
      assert.deepEqual(ctx.planner.current.draft.points[0], point(...rotated.dragged));
      drag(ctx, '[data-route-aim="0"]', ...rotated.aimTarget);
      assert.equal(ctx.planner.current.draft.points[0].aim_deg, 90);
      assert.equal(ctx.document.querySelector('[name="보는 방향 (°)"]').value, '90');
      near(direction(circlePoint(ctx, '[data-route-pin="0"] circle.route-pin'), circlePoint(ctx, '[data-route-aim="0"]'), rotated.meta), rotated.positiveY);
      await ctx.planner.save();
      assert.deepEqual(ctx.calls.findLast(call => call.method === 'post' && call.path === '/api/planning/routes').body.route.points,
        [point(...rotated.dragged, {aim_deg: 90}), point(3, 1), point(...rotated.clicked)]);
      near(circlePoint(ctx, '[data-route-pin="0"] circle.route-pin'), [.8, .3]);
      ctx.planner.suppressClickUntil = 0;
      ctx.document.querySelector('[data-map-mode="move"]').click(); clickMap(ctx, 700, 400);
      await ctx.confirmations.at(-1).action();
      assert.deepEqual(ctx.calls.findLast(call => call.method === 'goto'), {method: 'goto', x: rotated.clicked[0], y: rotated.clicked[1]});
      const located = []; ctx.panels.confirmLocatePoint = (...coords) => located.push(coords); ctx.store.requestLocatePoint = () => {};
      ctx.planner.draw(); ctx.document.querySelector('[data-map-mode="locate"]').click(); clickMap(ctx, 700, 400);
      assert.deepEqual(located, [rotated.clicked]);
    } finally {ctx.close();}
  });
}

test('unavailable display metadata preserves route loading without drawing the unrotated planning map', async () => {
  const ctx = await setup({displayMap: new Error('지도 메타 수신 실패')});
  try {
    assert.deepEqual(ctx.planner.current.draft, route());
    assert.equal(ctx.planner.current.mapMeta, null);
    assert.equal(ctx.document.querySelector('[data-route-map]'), null);
    assert.equal(ctx.document.querySelector('.route-map image'), null);
    assert.match(ctx.document.querySelector('.route-map').textContent, /지도/);
    input(ctx, '동선 이름', '목록에서 수정'); await ctx.planner.save();
    assert.equal(ctx.snapshot().saved[0].name, '목록에서 수정');
  } finally {ctx.close();}
});

test('zone selection preserves its explicit order and never promotes legacy yaw into camera aim', async () => {
  assert.equal(pointsFromZones([{...zone('A', 1, 1), yaw_deg: 80}], ['A'])[0].aim_deg, null);
  const ctx = await setup({routes: []});
  for (const id of ['C', 'A', 'D']) {
    const checkbox = ctx.document.querySelector('[data-route-zone="' + id + '"]'); checkbox.checked = true; checkbox.dispatchEvent(new ctx.dom.window.Event('change'));
  }
  ctx.document.querySelector('[aria-label="동선 구역 D 위로"]').click();
  ctx.document.querySelector('[data-route-from-zones]').click();
  assert.deepEqual(ctx.planner.current.draft.points.map(item => item.label), ['C', 'D', 'A']);
  assert.deepEqual(ctx.planner.current.draft.points.map(item => [item.x, item.y]), [[4, 3], [7, 3], [1, 1]]);
  assert.equal(ctx.calls.some(call => call.method === 'command' || call.method === 'goto'), false); ctx.close();
});

test('one exclusive map mode draws, confirms movement and sends point localization without changing the route', async () => {
  const ctx = await setup({routes: []});
  assert.deepEqual([...ctx.document.querySelectorAll('[data-map-mode]')].map(button => button.textContent), ['이동', '위치 알려주기', '선 그리기']);
  assert.equal(ctx.document.querySelector('[data-map-mode="locate"]').disabled, false);
  clickMap(ctx, 200, 800); assert.deepEqual(ctx.planner.current.draft.points[0], point(2, 2));
  assert.equal(ctx.calls.filter(call => ['goto', 'command'].includes(call.method)).length, 0);
  ctx.document.querySelector('[data-map-mode="move"]').click(); clickMap(ctx, 400, 600);
  assert.equal(ctx.planner.current.draft.points.length, 1); assert.equal(ctx.confirmations.length, 1); assert.match(ctx.confirmations[0].body, /x 4.00 m, y 4.00 m/);
  assert.equal(ctx.calls.filter(call => call.method === 'goto').length, 0);
  await ctx.confirmations[0].action(); assert.equal(ctx.calls.filter(call => call.method === 'goto').length, 1);
  ctx.planner.draw(); ctx.document.querySelector('[data-map-mode="locate"]').click(); clickMap(ctx, 600, 300);
  assert.match(ctx.confirmations.at(-1).title, /로봇이 여기 있다고 알려줄까요/);
  assert.match(ctx.confirmations.at(-1).body, /x 6.00 m, y 7.00 m/);
  assert.equal(ctx.calls.some(call => call.path === '/api/command/locate'), false);
  await ctx.confirmations.at(-1).action();
  assert.deepEqual(ctx.calls.find(call => call.path === '/api/command/locate'), {method: 'post', path: '/api/command/locate', body: {x: 6, y: 7}});
  assert.equal(ctx.planner.current.draft.points.length, 1);
  assert.deepEqual([...ctx.document.querySelectorAll('[data-map-mode][aria-pressed="true"]')].map(button => button.dataset.mapMode), ['locate']); ctx.close();
});

test('point localization cancellation and target changes cannot send a hint or a movement command', async () => {
  const ctx = await setup({routes: []});
  try {
    ctx.document.querySelector('[data-map-mode="locate"]').click();
    const queuedConfirmation = ctx.panels.confirmDevice;
    ctx.panels.confirmDevice = OperationalPanels.prototype.confirmDevice.bind(ctx.panels);
    const prompts = []; ctx.dom.window.confirm = prompt => {prompts.push(prompt); return false;};
    clickMap(ctx, 200, 700); await tick();
    assert.match(prompts.at(-1), /로봇이 여기 있다고 알려줄까요/);
    assert.equal(ctx.calls.some(call => call.method === 'goto' || call.path === '/api/command/locate'), false);
    assert.equal(ctx.planner.current.draft.points.length, 0);
    ctx.panels.confirmDevice = queuedConfirmation;
    clickMap(ctx, 200, 700);
    const confirmation = ctx.confirmations.at(-1);
    ctx.store.link = {...ctx.link, baseUrl: '/robots/other'};
    await assert.rejects(confirmation.action(), /대상 로봇이 바뀌었습니다/);
    clickMap(ctx, 400, 500);
    assert.equal(ctx.confirmations.length, 1, '다른 로봇으로 바뀐 뒤 옛 지도에서는 새 확인도 열지 않는다');
    assert.equal(ctx.calls.some(call => call.path === '/api/command/locate'), false);
  } finally {ctx.close();}
});

for (const rotated of rotatedMaps) test(`${rotated.degrees} degree route map shows the server hint radius until verified and clears it on robot switch`, async () => {
  const ctx = await setup({displayMap: rotated.meta});
  try {
    let nav = {available: true, pose: [1, 1, 0], verified: false, point_hint: {x: 1, y: 1, radius: .6}};
    const get = ctx.link.get; ctx.link.get = path => path === '/api/nav' ? Promise.resolve(nav) : get(path);
    await ctx.planner.poll();
    const outline = polylinePoints(ctx, '[data-route-location-hint]');
    assert.equal(outline.length, 65);
    near(outline[0], rotated.degrees === 180 ? [.92, .1] : [.9, .92]);
    near(outline[16], rotated.degrees === 180 ? [.95, .16] : [.84, .95]);
    assert.match(ctx.document.querySelector('[data-route-progress]').textContent, /알려준 점 주변/);
    nav = {...nav, verified: true}; await ctx.planner.poll();
    assert.equal(ctx.document.querySelector('[data-route-location-hint]'), null);
    nav = {...nav, verified: false}; await ctx.planner.poll();
    ctx.store.link = {...ctx.link, baseUrl: '/robots/other', get}; ctx.panels.render('zones'); await tick(); await tick();
    assert.equal(ctx.document.querySelector('[data-route-location-hint]'), null);
    assert.equal(ctx.planner.current.mode, 'draw');
  } finally {ctx.close();}
});

test('pointer drag moves the vertex, aim handle turns toward +Y, and drag release does not append a point', async () => {
  const ctx = await setup(); mapElement(ctx);
  ctx.document.querySelector('[data-route-pin="0"] circle.route-pin').dispatchEvent(new ctx.dom.window.MouseEvent('pointerdown', {bubbles: true, clientX: 100, clientY: 900}));
  ctx.document.dispatchEvent(new ctx.dom.window.MouseEvent('pointermove', {clientX: 200, clientY: 800}));
  ctx.document.dispatchEvent(new ctx.dom.window.MouseEvent('pointerup'));
  clickMap(ctx, 200, 800);
  assert.equal(ctx.planner.current.draft.points.length, 2); assert.equal(ctx.planner.current.draft.points[0].x, 2); assert.equal(ctx.planner.current.draft.points[0].y, 2);
  mapElement(ctx); ctx.document.querySelector('[data-route-aim="0"]').dispatchEvent(new ctx.dom.window.MouseEvent('pointerdown', {bubbles: true, clientX: 265, clientY: 800}));
  ctx.document.dispatchEvent(new ctx.dom.window.MouseEvent('pointermove', {clientX: 200, clientY: 700})); ctx.document.dispatchEvent(new ctx.dom.window.MouseEvent('pointerup'));
  assert.equal(ctx.planner.current.draft.points[0].aim_deg, 90); assert.equal(ctx.document.querySelector('[name="보는 방향 (°)"]').value, '90');
  input(ctx, '머무름 (초)', '3.5'); assert.equal(ctx.planner.current.draft.points[0].dwell_s, 3.5);
  ctx.document.querySelector('[data-route-point-delete]').click(); assert.equal(ctx.planner.current.draft.points.length, 1); ctx.close();
});

test('cancelled drag restores coordinates and never mutates the next robot draft', async () => {
  const ctx = await setup(); mapElement(ctx);
  ctx.document.querySelector('[data-route-pin="0"] circle.route-pin').dispatchEvent(new ctx.dom.window.MouseEvent('pointerdown', {bubbles: true}));
  ctx.document.dispatchEvent(new ctx.dom.window.MouseEvent('pointermove', {clientX: 500, clientY: 500}));
  ctx.document.dispatchEvent(new ctx.dom.window.MouseEvent('pointercancel'));
  assert.deepEqual(ctx.planner.current.draft.points[0], point(1, 1)); assert.equal(ctx.planner.current.dirty, false);
  mapElement(ctx); ctx.document.querySelector('[data-route-pin="0"] circle.route-pin').dispatchEvent(new ctx.dom.window.MouseEvent('pointerdown', {bubbles: true}));
  ctx.document.dispatchEvent(new ctx.dom.window.MouseEvent('pointermove', {clientX: 500, clientY: 500}));
  const first = ctx.planner.current; ctx.store.link = {...ctx.link, baseUrl: '/robots/other'}; ctx.panels.render('zones');
  ctx.document.dispatchEvent(new ctx.dom.window.MouseEvent('pointerup')); await tick();
  assert.deepEqual(first.draft.points[0], point(1, 1)); assert.equal(ctx.planner.current.dirty, false); ctx.close();
});

test('invalid obstacle segments are red and prevent a save while retaining the editable route', async () => {
  const ctx = await setup({preview: () => ({valid: false, total_m: 2, invalid_points: [1], segments: [{from_index: 0, to_index: 1, valid: false, reason: '벽 또는 몸 반경과 겹칩니다.'}]})});
  input(ctx, '동선 이름', '벽을 지나는 초안'); await ctx.planner.save();
  assert.ok(ctx.document.querySelector('[data-route-segment="0-1"].blocked')); assert.equal(ctx.document.querySelector('[data-route-save]').disabled, true);
  assert.match(ctx.document.querySelector('.route-validation').textContent, /1 → 2 지점: 벽 또는 몸 반경/);
  assert.equal(ctx.calls.some(call => call.path === '/api/planning/routes' && call.method === 'post'), false);
  assert.equal(ctx.planner.current.dirty, true); assert.equal(ctx.planner.current.draft.name, '벽을 지나는 초안'); ctx.close();
});

test('repeat 1, N and forever serialize correctly and validate the closing segment', async () => {
  const ctx = await setup();
  assert.equal(ctx.document.querySelector('[data-route-segment="1-0"]'), null);
  select(ctx, '동선 반복', 'count'); input(ctx, '반복 횟수', '4');
  assert.ok(ctx.document.querySelector('[data-route-segment="1-0"]')); await ctx.planner.save();
  assert.equal(ctx.calls.findLast(call => call.path === '/api/planning/routes').body.route.repeat, 4);
  select(ctx, '동선 반복', 'forever'); await ctx.planner.save(); assert.equal(ctx.calls.findLast(call => call.path === '/api/planning/routes').body.route.repeat, 0);
  select(ctx, '동선 반복', 'once'); await ctx.planner.save(); assert.equal(ctx.calls.findLast(call => call.path === '/api/planning/routes').body.route.repeat, 1); ctx.close();
});

test('named route save round trip and deletion preserve other routes', async () => {
  const ctx = await setup({routes: [route(), route('route-two')]});
  input(ctx, '동선 이름', '첫 동선 수정'); input(ctx, '보는 방향 (°)', '70'); await ctx.planner.save();
  assert.equal(ctx.snapshot().saved.length, 2); assert.equal(ctx.snapshot().saved[0].name, '첫 동선 수정'); assert.equal(ctx.snapshot().saved[0].points[0].aim_deg, 70);
  assert.equal(ctx.planner.current.dirty, false); assert.equal(ctx.document.querySelector('[data-route-start]').disabled, false);
  ctx.document.querySelector('[data-route-delete]').click(); assert.equal(ctx.calls.filter(call => call.method === 'delete').length, 0);
  await ctx.confirmations.at(-1).action(); assert.deepEqual(ctx.snapshot().saved.map(item => item.id), ['route-two']);
  assert.equal(ctx.planner.current.draft.id, 'route-two'); ctx.close();
});

test('local route and zone saves share revisions in both directions even while the other editor has a draft', async () => {
  const ctx = await setup();
  input(ctx, '구역 이름', '구역 초안'); input(ctx, '동선 이름', '동선 초안'); await ctx.planner.save();
  assert.equal(ctx.panels.planning.current.revision, 'r2'); assert.equal(ctx.panels.planning.current.dirty, true);
  await ctx.panels.planning.save(); assert.equal(ctx.planner.current.revision, 'r3');
  assert.equal(ctx.planner.current.zones[0].name, '구역 초안');
  input(ctx, '동선 이름', '두 번째 동선 초안'); input(ctx, '구역 이름', '두 번째 구역'); await ctx.panels.planning.save();
  assert.equal(ctx.planner.current.revision, 'r4'); assert.equal(ctx.planner.current.dirty, true);
  await ctx.planner.save(); assert.equal(ctx.snapshot().saved[0].name, '두 번째 동선 초안'); ctx.close();
});

test('external revision conflict never overwrites saved routes or silently changes the draft base', async () => {
  const ctx = await setup(); input(ctx, '동선 이름', '충돌 초안'); ctx.bump(); await ctx.planner.save();
  assert.equal(ctx.planner.current.revision, 'r1'); assert.equal(ctx.planner.current.dirty, true); assert.equal(ctx.snapshot().saved[0].name, '첫 동선');
  assert.match(ctx.document.querySelector('.route-feedback').textContent, /계획이 바뀌었습니다/);
  ctx.panels.render('settings'); ctx.panels.render('zones'); assert.equal(ctx.document.querySelector('[name="동선 이름"]').value, '충돌 초안');
  assert.equal(ctx.planner.current.revision, 'r1'); ctx.close();
});

test('late validation results cannot recolor a newer edit or another robot', async () => {
  const ctx = await setup(); let finish;
  const previous = ctx.link.post; ctx.link.post = (path, body) => path.endsWith('/preview') ? new Promise(resolve => {finish = resolve;}) : previous(path, body);
  const pending = ctx.planner.validate(); input(ctx, '지점 X (m)', '2');
  finish({valid: false, total_m: 0, segments: [{from_index: 0, to_index: 1, valid: false}], invalid_points: [0]}); await pending;
  assert.equal(ctx.planner.current.validation, null); assert.equal(ctx.document.querySelector('.route-line.blocked'), null); ctx.close();
});

test('invalid number survives reordering and blocks save until that same point is corrected', async () => {
  const ctx = await setup(); input(ctx, '보는 방향 (°)', '181');
  ctx.document.querySelector('[aria-label="1번 지점 아래로"]').click(); assert.equal(ctx.document.querySelector('[name="보는 방향 (°)"]').value, '181');
  assert.equal(ctx.document.querySelector('[data-route-save]').disabled, true); await ctx.planner.save(); assert.equal(ctx.calls.some(call => call.path === '/api/planning/routes'), false);
  input(ctx, '보는 방향 (°)', '90'); await ctx.planner.save(); assert.equal(ctx.snapshot().saved[0].points[1].aim_deg, 90); ctx.close();
});

test('route start requires confirmation, rejects dirty drafts and detects a changed robot or route', async () => {
  const ctx = await setup(); ctx.document.querySelector('[data-route-start]').click(); assert.equal(ctx.calls.filter(call => call.method === 'command').length, 0);
  assert.match(ctx.confirmations.at(-1).body, /2개 지점/); await ctx.confirmations.at(-1).action(); assert.equal(ctx.calls.filter(call => call.method === 'command').length, 1);
  ctx.planner.confirmStart(); const confirmation = ctx.confirmations.at(-1); input(ctx, '동선 이름', '변경됨'); await assert.rejects(confirmation.action(), /동선이 변경/);
  assert.equal(ctx.document.querySelector('[data-route-start]').disabled, true);
  await ctx.planner.save(); ctx.planner.confirmStart(); const moved = ctx.confirmations.at(-1); ctx.store.link = {...ctx.link}; await assert.rejects(moved.action(), /대상 로봇이 바뀌었습니다/); ctx.close();
});

test('confirmation captures saved route digest and rejects external replacement before execution', async () => {
  const ctx = await setup(); const digest = ctx.snapshot().digests['route-one'];
  ctx.planner.confirmStart(); const confirmation = ctx.confirmations.at(-1);
  ctx.replaceRoute({...route(), name: '외부 변경', points: [point(1, 1), point(8, 8)]});
  await assert.rejects(confirmation.action(), /저장된 동선이 바뀌었습니다/);
  assert.equal(ctx.calls.find(call => call.method === 'command').expectedDigest, digest);
  assert.equal(ctx.planner.current.draft.name, '첫 동선'); ctx.close();
});

test('a confirmation cannot start a locally edited and re-saved route, or a route without its digest', async () => {
  const ctx = await setup(); ctx.planner.confirmStart(); const confirmation = ctx.confirmations.at(-1);
  input(ctx, '보는 방향 (°)', '35'); await ctx.planner.save();
  await assert.rejects(confirmation.action(), /동선이 변경되었습니다/);
  delete ctx.planner.current.snapshot.digests; ctx.planner.draw(); assert.equal(ctx.document.querySelector('[data-route-start]').disabled, true);
  assert.match(ctx.document.querySelector('.route-feedback').textContent, /저장본을 다시 불러오세요/); ctx.close();
});

test('new routes save alongside existing named routes and empty names cannot be submitted', async () => {
  const ctx = await setup(); ctx.document.querySelector('[data-route-new]').click();
  clickMap(ctx, 300, 300); input(ctx, '동선 이름', ''); assert.equal(ctx.document.querySelector('[data-route-save]').disabled, true);
  input(ctx, '동선 이름', '새로 그린 동선'); await ctx.planner.save();
  assert.equal(ctx.snapshot().saved.length, 2); assert.equal(ctx.snapshot().saved[1].name, '새로 그린 동선');
  select(ctx, '저장된 동선', 'route-one'); assert.equal(ctx.planner.current.draft.name, '첫 동선'); ctx.close();
});

test('nav polling preserves point and form focus and labels simulated state as SIM', async () => {
  const ctx = await setup(); const button = ctx.document.querySelector('[data-route-point="0"]'); button.focus(); await ctx.planner.poll();
  assert.equal(ctx.document.activeElement, button);
  ctx.link.get = async () => ({available: true, simulated: true, simulation: '운동 모델의 참 자세 주입 · 실물 측위 검증 아님', pose: [3, 1, 90], seeded: true, route: {id: 'route-one', name: '첫 동선', point_index: 1, point_count: 2, cycle: 1, repeat: 1, phase: 'dwell', status: 'active'}});
  await ctx.planner.poll(); assert.equal(ctx.document.activeElement.dataset.routePoint, '0');
  const field = ctx.document.querySelector('[name="머무름 (초)"]'); field.focus(); await ctx.planner.poll(); assert.equal(ctx.document.activeElement, field);
  assert.match(ctx.document.querySelector('[data-route-progress]').textContent, /SIM · 운동 모델의 참 자세 주입 · 실물 측위 검증 아님/);
  assert.match(ctx.document.querySelector('[data-route-progress]').textContent, /머무름/); ctx.close();
});

test('existing stop command remains immediate and progress draws actual robot pose and current point', async () => {
  const ctx = await setup(); ctx.document.querySelector('[data-route-stop]').click(); await tick();
  assert.deepEqual(ctx.calls.filter(call => call.method === 'patrol'), [{method: 'patrol', action: 'stop'}]); assert.equal(ctx.confirmations.length, 0);
  ctx.link.get = async () => ({available: true, pose: [3, 1, 90], seeded: true, route: {id: 'route-one', name: '첫 동선', point_index: 1, point_count: 2, cycle: 1, repeat: 1, phase: 'aiming', status: 'active'}});
  await ctx.planner.poll(); assert.ok(ctx.document.querySelector('[data-route-pin="1"].active')); assert.ok(ctx.document.querySelector('[data-route-robot]'));
  assert.match(ctx.document.querySelector('[data-route-progress]').textContent, /2\/2 지점 · 방향 맞추기/);
  input(ctx, '지점 X (m)', '5'); assert.equal(ctx.document.querySelector('.route-map-point.active'), null); ctx.close();
});

test('read-only and offline panels never issue move, locate, route start or stop', async () => {
  const ctx = await setup(); const slot = ctx.store.slot(); slot.commandsKnown = true; slot.commandsOpen = false; ctx.planner.draw();
  for (const selector of ['[data-route-start]', '[data-route-stop]', '[data-map-mode="move"]', '[data-map-mode="locate"]']) assert.equal(ctx.document.querySelector(selector).disabled, true);
  ctx.close(); const offline = await setup({live: false}); assert.equal(offline.document.querySelector('[data-route-map]'), null); assert.equal(offline.calls.length, 0); offline.close();
});

test('RobotLink routes and deletion carry exact paths and deletion rejection reason', async () => {
  const calls = [], link = new RobotLink({baseUrl: '/robots/one', fetch: async (url, init) => {calls.push({url, method: init.method, body: JSON.parse(init.body)}); return {ok: true, json: async () => ({accepted: true})};}});
  await link.route('start', 'route-one', 'a'.repeat(64)); await link.route('stop'); await link.delete('/api/planning/routes/route-one', {revision: 'r1'});
  assert.deepEqual(calls, [{url: '/robots/one/api/command/route', method: 'POST', body: {action: 'start', route_id: 'route-one', expected_digest: 'a'.repeat(64)}}, {url: '/robots/one/api/command/route', method: 'POST', body: {action: 'stop'}}, {url: '/robots/one/api/planning/routes/route-one', method: 'DELETE', body: {revision: 'r1'}}]);
  const refused = new RobotLink({fetch: async () => ({ok: false, status: 409, json: async () => ({error: '저장본이 바뀌었습니다'})})}); await assert.rejects(refused.delete('/api/planning/routes/one', {revision: 'old'}), /저장본이 바뀌었습니다/);
});

for (const rotated of rotatedMaps) {
  test(`${rotated.degrees} degree route, zone and location maps share clicks and image orientation`, async () => {
    const ctx = await setup({displayMap: rotated.meta});
    try {
      const zoneSvg = ctx.document.querySelector('.plan-map-svg');
      assert.ok(zoneSvg);
      zoneSvg.getBoundingClientRect = () => ({left: 30, top: 50, width: 500, height: 250});
      const routeImage = mapElement(ctx).querySelector('image');
      const zoneImage = zoneSvg.querySelector('image');
      assert.equal(zoneImage.getAttribute('href'), routeImage.getAttribute('href'));
      assert.equal(zoneSvg.style.aspectRatio, mapElement(ctx).style.aspectRatio);
      const zonePin = zoneSvg.querySelector('[data-zone="A"] .plan-pin');
      near([+zonePin.getAttribute('cx') / 1000, +zonePin.getAttribute('cy') / 1000], rotated.pins[0]);
      clickMap(ctx, 700, 400);
      zoneSvg.dispatchEvent(new ctx.dom.window.MouseEvent('click', {clientX: 380, clientY: 150, bubbles: true}));
      const selected = ctx.panels.planning.current.draft.zones[0];
      near([selected.x, selected.y], rotated.clicked);
      near([selected.x, selected.y], [ctx.planner.current.draft.points.at(-1).x, ctx.planner.current.draft.points.at(-1).y]);
      ctx.planner.drawMap(); ctx.panels.planning.drawMap();
      assert.equal(mapElement(ctx).querySelector('image'), routeImage, 'route redraw reuses raster DOM');
      assert.equal(ctx.document.querySelector('.plan-map-svg image'), zoneImage, 'zone redraw reuses raster DOM');
      const picks = [];
      const live = new LiveMap({document: ctx.document, getLink: () => ctx.link, onPick: (...p) => picks.push(p), setInterval: () => 0});
      ctx.document.body.append(live.root); await live.tick();
      live.canvas.getBoundingClientRect = () => ({left: 10, top: 20, width: rotated.meta.width * 2, height: rotated.meta.height * 2});
      live.click({clientX: 10 + rotated.meta.width * 2 * .7, clientY: 20 + rotated.meta.height * 2 * .4});
      near(picks[0].slice(0, 2), rotated.clicked);
      live.dispose();
    } finally {ctx.close();}
  });
}

test('zone map does not fall back to an unrotated image when display metadata fails', async () => {
  const ctx = await setup({displayMap: new Error('no map metadata')});
  try {assert.equal(ctx.document.querySelector('.plan-map-svg'), null); assert.equal(ctx.document.querySelector('.plan-map image'), null);}
  finally {ctx.close();}
});


test('snap tolerance covers all eight directions, with Shift and grid choices', () => {
  const previous = {x: 1.03, y: 2.07};
  for (const degrees of [0, 45, 90, 135, 180, -45, -90, -135]) {
    const a = (degrees + 7) * Math.PI / 180, target = [previous.x + 2 * Math.cos(a), previous.y + 2 * Math.sin(a)];
    const result = snapRoutePoint(target, previous);
    assert.equal((result.angle + 360) % 360, (degrees + 360) % 360);
    const delta = result.point.map((v, i) => v - [previous.x, previous.y][i]);
    assert.ok(Math.abs(delta[0] * Math.sin(degrees * Math.PI / 180) - delta[1] * Math.cos(degrees * Math.PI / 180)) < 1e-6);
    assert.deepEqual(snapRoutePoint(target, previous, {shift: true, grid: true}), {point: target, angle: null});
    const outside = (degrees + 9) * Math.PI / 180;
    assert.equal(snapRoutePoint([previous.x + Math.cos(outside), previous.y + Math.sin(outside)], previous).angle, null);
  }
  near(snapRoutePoint([1.24, 2.36], null, {grid: true}).point, [1.2, 2.4]);
  const diagonal = snapRoutePoint([2.05, 3.04], previous, {grid: true});
  near(diagonal.point, [2.03, 3.07]);
});

test('drawing shows a guide, snaps clicks and Shift releases the same target', async () => {
  const ctx = await setup();
  try {
    const svg = mapElement(ctx);
    svg.dispatchEvent(new ctx.dom.window.MouseEvent('pointermove', {clientX: 500, clientY: 880}));
    assert.ok(svg.querySelector('[data-route-snap-guide]'));
    assert.match(ctx.document.querySelector('[data-route-snap-status]').textContent, /0° 맞춤/);
    clickMap(ctx, 500, 880); near([ctx.planner.current.draft.points.at(-1).x, ctx.planner.current.draft.points.at(-1).y], [5, 1]);
    clickMap(ctx, 700, 880, {shiftKey: true}); near([ctx.planner.current.draft.points.at(-1).x, ctx.planner.current.draft.points.at(-1).y], [7, 1.2]);
    ctx.document.querySelector('[data-route-grid]').click();
    clickMap(ctx, 824, 637, {shiftKey: true}); near([ctx.planner.current.draft.points.at(-1).x, ctx.planner.current.draft.points.at(-1).y], [8.24, 3.63]);
    clickMap(ctx, 934, 517); near([ctx.planner.current.draft.points.at(-1).x, ctx.planner.current.draft.points.at(-1).y], [9.44, 4.83]);
  } finally {ctx.close();}
});

test('dragging a later vertex follows the previous point and Shift disables snapping', async () => {
  const ctx = await setup();
  try {
    mapElement(ctx); const down = () => ctx.document.querySelector('[data-route-pin="1"] circle.route-pin').dispatchEvent(new ctx.dom.window.MouseEvent('pointerdown', {bubbles: true}));
    down(); ctx.document.dispatchEvent(new ctx.dom.window.MouseEvent('pointermove', {clientX: 400, clientY: 880}));
    near([ctx.planner.current.draft.points[1].x, ctx.planner.current.draft.points[1].y], [4, 1]);
    assert.ok(ctx.document.querySelector('[data-route-snap-guide]'));
    ctx.document.dispatchEvent(new ctx.dom.window.MouseEvent('pointermove', {clientX: 400, clientY: 880, shiftKey: true}));
    near([ctx.planner.current.draft.points[1].x, ctx.planner.current.draft.points[1].y], [4, 1.2]);
    assert.equal(ctx.document.querySelector('[data-route-snap-guide]'), null);
    ctx.document.dispatchEvent(new ctx.dom.window.MouseEvent('pointerup'));
  } finally {ctx.close();}
});

test('map adds only a small primary click and ignores drag, scrolling, cancellation and outside', async () => {
  const ctx = await setup();
  try {
    for (const action of ['drag', 'wheel', 'scroll', 'cancel', 'outside', 'right', 'bare']) {
      const svg = mapElement(ctx), init = {bubbles: true, clientX: 500, clientY: 500, button: action === 'right' ? 2 : 0};
      if (action !== 'bare') svg.dispatchEvent(new ctx.dom.window.MouseEvent('pointerdown', init));
      if (action === 'drag') ctx.document.dispatchEvent(new ctx.dom.window.MouseEvent('pointermove', {...init, clientX: 520}));
      if (action === 'wheel') svg.dispatchEvent(new ctx.dom.window.WheelEvent('wheel', {bubbles: true}));
      if (action === 'scroll') ctx.document.dispatchEvent(new ctx.dom.window.Event('scroll'));
      if (action === 'cancel') ctx.document.dispatchEvent(new ctx.dom.window.MouseEvent('pointercancel', init));
      const end = {...init, clientX: action === 'outside' ? 1001 : 500};
      ctx.document.dispatchEvent(new ctx.dom.window.MouseEvent('pointerup', end));
      svg.dispatchEvent(new ctx.dom.window.MouseEvent('click', end));
      assert.equal(ctx.planner.current.draft.points.length, 2, action);
    }
    clickMap(ctx, 500, 500); assert.equal(ctx.planner.current.draft.points.length, 3);
  } finally {ctx.close();}
});

test('blocked segment hover and keyboard focus expose the actual server reason', async () => {
  const reason = '벽·가구까지 거리 12 cm — 몸 반경 미달';
  const ctx = await setup({preview: () => ({valid: false, invalid_points: [], segments: [{from_index: 0, to_index: 1, valid: false, reason}]})});
  try {
    await ctx.planner.validate(); const segment = ctx.document.querySelector('[data-route-segment="0-1"]'), hint = ctx.document.querySelector('[data-route-blocked-hint]');
    segment.dispatchEvent(new ctx.dom.window.Event('pointerenter')); assert.match(hint.textContent, /거리 12 cm — 몸 반경 미달/);
    segment.dispatchEvent(new ctx.dom.window.Event('pointerleave')); assert.equal(hint.textContent, '');
    segment.focus(); assert.match(hint.textContent, /거리 12 cm/); segment.blur(); assert.equal(hint.textContent, '');
    assert.match(segment.getAttribute('aria-label'), /몸 반경 미달/);
  } finally {ctx.close();}
});


test('eight degree boundary is inclusive on both sides of every snap direction', () => {
  for (const degrees of [0, 45, 90, 135, 180, -45, -90, -135]) for (const offset of [-8, 8]) {
    const angle = (degrees + offset) * Math.PI / 180;
    const result = snapRoutePoint([Math.cos(angle), Math.sin(angle)], {x: 0, y: 0});
    assert.equal((result.angle + 360) % 360, (degrees + 360) % 360);
  }
});


test('blocked explanation survives navigation polling while the pointer stays on the segment', async () => {
  const ctx = await setup({preview: () => ({valid: false, invalid_points: [], segments: [{from_index: 0, to_index: 1, valid: false, reason: '미관측 영역과 몸 반경이 겹칩니다.'}]})});
  try {
    await ctx.planner.validate();
    ctx.document.querySelector('[data-route-segment="0-1"]').dispatchEvent(new ctx.dom.window.Event('pointerenter'));
    await ctx.planner.poll();
    assert.match(ctx.document.querySelector('[data-route-blocked-hint]').textContent, /미관측 영역과 몸 반경/);
    mapElement(ctx).dispatchEvent(new ctx.dom.window.MouseEvent('pointermove', {clientX: 600, clientY: 500}));
    assert.equal(ctx.document.querySelector('[data-route-blocked-hint]').textContent, '');
  } finally {ctx.close();}
});
