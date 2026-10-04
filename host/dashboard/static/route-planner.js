import {mapPoint, worldPoint, mapImage} from './map-frame.js';
import {describeNav, pointHintOutline} from './live-map.js';
import {mapClickMode} from './map-click-mode.js';

const clone = value => JSON.parse(JSON.stringify(value));
const NS = 'http://www.w3.org/2000/svg';
const ROUTES = '/api/planning/routes';
const phases = {following: '이동', moving: '이동', navigating: '이동', aiming: '방향 맞추기', inspecting: '점검', inspection: '점검', dwelling: '머무름', dwell: '머무름', completed: '완료', stopped: '정지', cancelled: '취소', blocked: '길 막힘'};
const freshRoute = () => ({id: 'route-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 7), name: '새 동선', points: [], repeat: 1});
const readyPoint = point => Number.isFinite(point?.x) && Number.isFinite(point?.y);

export function pointsFromZones(zones, orderedIds) {
  return orderedIds.map(id => zones.find(zone => zone.id === id)).filter(readyPoint).map(zone => ({
    x: zone.x, y: zone.y, label: zone.id, aim_deg: zone.aim_deg ?? null, dwell_s: 0,
  }));
}

/** Display routes in the live map's frame; keep edits and saved routes in patrol coordinates. */
export class RoutePlanner {
  constructor(panel) {
    this.p = panel;
    this.sessions = new Map();
    this.timer = setInterval(() => this.poll(), 500);
    this.timer?.unref?.();
  }

  context() {
    const store = this.p.store;
    return {key: store.demo ? 'offline' : (store.link?.baseUrl ?? 'offline') + ':' + store.selected, link: store.demo ? null : store.link};
  }

  open() {
    this.cancelDrag?.();
    const {key, link} = this.context();
    if (!this.sessions.has(key)) {
      let local;
      try { local = JSON.parse(this.p.document.defaultView.localStorage.getItem('mechadog.routes.v1.' + key)); } catch {}
      const draft = local?.draft?.id && Array.isArray(local.draft.points) ? local.draft : freshRoute();
      this.sessions.set(key, {key, link, draft, revision: local?.revision ?? null, dirty: !!local,
        invalid: local?.invalid || {}, snapshot: null, mapMeta: null, mapError: '', zones: [], zoneOrder: [], selected: -1,
        mode: 'draw', validation: null, edit: 0, loaded: false, loading: false, busy: false, error: '', notice: '', nav: null});
    }
    this.current = this.sessions.get(key);
    this.current.link = link;
    this.root = this.p.el('section', {class: 'route-workspace op-section', 'aria-label': '동선 편집과 주행'});
    this.p.container.append(this.root);
    this.draw();
    if (link?.get && !this.current.loaded && !this.current.loading) this.load(this.current);
    this.poll();
  }

  visible(session = this.current) {
    return this.current === session && this.root?.isConnected && this.p.view === 'zones' && this.context().key === session.key;
  }

  refresh() {
    if (this.current?.key !== this.context().key) return this.p.render('zones');
    if (this.visible()) this.updateStatus();
  }

  async load(session = this.current, replace = false) {
    if (session.loading || !session.link?.get) return;
    session.loading = true;
    session.error = '';
    session.mapMeta = null;
    session.mapError = '';
    if (this.visible(session)) this.draw();
    try {
      const [data, planning, mapMeta] = await Promise.all([
        session.link.get(ROUTES), session.link.get('/api/planning'),
        session.link.get('/api/map/meta').catch(error => {session.mapError = error.message; return null;}),
      ]);
      if (!Array.isArray(data.saved)) throw new Error('동선 API를 지원하는 서버를 연결해 주세요.');
      session.snapshot = data;
      session.mapMeta = mapMeta;
      session.zones = planning.saved?.zones || [];
      if (replace || !session.dirty) {
        const route = data.saved.find(item => item.id === session.draft.id) || data.saved[0];
        session.draft = route ? clone(route) : freshRoute();
        session.revision = data.revision;
        session.dirty = false;
        session.invalid = {};
        session.selected = session.draft.points.length ? 0 : -1;
        session.validation = null;
        this.forget(session);
      }
      session.revision ??= data.revision;
      session.zoneOrder = session.draft.points.map(point => point.label).filter(id => session.zones.some(zone => zone.id === id));
      session.notice = session.dirty ? '이 브라우저에 작성 중인 동선이 있습니다. 저장본과 비교한 뒤 저장하세요.' : '';
    } catch (error) {
      session.error = '동선 목록을 읽지 못했습니다. ' + error.message;
    } finally {
      session.loading = false;
      session.loaded = true;
      if (this.visible(session)) this.draw();
      if (session.snapshot && session.draft.points.length) this.validate(session);
    }
  }

  remember(session = this.current) {
    try { this.p.document.defaultView.localStorage.setItem('mechadog.routes.v1.' + session.key, JSON.stringify({draft: session.draft, revision: session.revision, invalid: session.invalid})); } catch {}
  }

  forget(session) {
    try { this.p.document.defaultView.localStorage.removeItem('mechadog.routes.v1.' + session.key); } catch {}
  }

  changed({map = true, validate = true} = {}) {
    const session = this.current;
    session.edit++;
    session.dirty = true;
    session.error = '';
    session.notice = '';
    if (validate) session.validation = null;
    this.remember(session);
    this.updateStatus();
    this.drawPoints();
    if (map) this.drawMap();
    this.drawValidation();
    if (validate) {
      clearTimeout(this.validationTimer);
      this.validationTimer = setTimeout(() => this.validate(session), 250);
      this.validationTimer?.unref?.();
    }
  }

  // A successful save from the other editor is a known local revision change.
  // External conflicts retain the old revision and require an explicit reload.
  planningSaved(result, previousRevision, key) {
    const session = this.sessions.get(key);
    if (!session || session.revision !== previousRevision) return;
    session.revision = result.revision;
    if (session.snapshot) session.snapshot.revision = result.revision;
    session.zones = clone(result.saved.zones);
    session.validation = null;
    session.edit++;
    if (session.dirty) this.remember(session);
    if (this.visible(session)) {this.drawZones(); this.drawMap(); this.drawValidation();}
    this.validate(session);
  }

  syncZoneRevision(session, previousRevision) {
    const planning = this.p.planning?.sessions.get(session.key);
    if (!planning || planning.revision !== previousRevision) return;
    planning.revision = session.revision;
    if (planning.snapshot) planning.snapshot.revision = session.revision;
    if (planning.dirty) this.p.planning.remember(planning);
  }

  isLocked() { return this.current.busy || this.current.loading; }
  canCommand() { return !!this.p.store.live && !this.p.store.readOnly && !this.isLocked() && this.visible() && this.current.link === this.p.store.link; }
  canLocate() { return this.canCommand() && typeof this.p.confirmLocatePoint === 'function' && typeof this.p.store.requestLocatePoint === 'function'; }
  savedDigest(session = this.current) {return session.snapshot?.digests?.[session.draft.id];}

  updateStatus() {
    if (!this.status) return;
    const session = this.current, points = session.draft.points;
    const invalid = Object.keys(session.invalid).length > 0 || !session.draft.name.trim();
    this.status.textContent = session.loading ? '동선 불러오는 중' : session.busy ? '처리 중' : invalid ? '숫자 입력을 확인하세요' : session.dirty ? '작성 중 · 브라우저에 보관' : session.snapshot?.saved.some(route => route.id === session.draft.id) ? '저장된 동선' : '새 동선';
    this.saveButton.disabled = !session.snapshot || !session.dirty || this.isLocked() || invalid || !points.length || session.validation?.valid === false || session.snapshot.writable === false;
    this.startButton.disabled = !this.canCommand() || session.dirty || invalid || !this.savedDigest() || !session.snapshot?.saved.some(route => route.id === session.draft.id) || !points.length;
    this.stopButton.disabled = !this.p.store.live || !!this.p.store.readOnly;
    this.previewButton.disabled = !session.snapshot?.map?.available || this.isLocked() || invalid || !points.length;
    for (const button of this.root.querySelectorAll('[data-map-mode]')) {
      const mode = button.dataset.mapMode;
      button.disabled = this.isLocked() || (mode === 'move' && !this.canCommand()) || (mode === 'locate' && !this.canLocate());
    }
    this.progress.textContent = this.progressText();
  }

  progressText() {
    const session = this.current, route = session.nav?.route;
    const simulation = session.nav?.simulated ? 'SIM · ' + (session.nav.simulation || '시뮬레이션 · 실물 측위 검증 아님') + ' | ' : '';
    if (session.navError) return simulation + '로봇 상태 수신 실패 · ' + session.navError;
    if (route?.id) return simulation + route.name + ' · ' + (route.point_index + 1) + '/' + route.point_count + ' 지점 · ' + (phases[route.phase] || route.phase || route.status) + ' · ' + route.cycle + '/' + (route.repeat === 0 ? '계속' : route.repeat) + '회';
    return simulation + describeNav(session.nav);
  }

  draw() {
    const p = this.p, session = this.current;
    this.root.replaceChildren();
    this.status = p.el('span', {class: 'route-state', role: 'status'});
    this.saveButton = p.button('동선 저장', () => this.save(), {class: 'op-button primary', 'data-route-save': ''});
    this.startButton = p.button('이 동선으로 주행', () => this.confirmStart(), {class: 'op-button primary', 'data-route-start': ''});
    // Existing patrol stop goes through the established device-command and safety path.
    this.stopButton = p.button('순찰 정지', () => p.store.requestPatrol(false), {'data-route-stop': ''});
    this.previewButton = p.button('장애물 확인', () => this.validate(), {'data-route-preview': ''});
    const reload = p.button('저장본 불러오기', () => session.dirty ? p.confirmDevice({title: '작성 중인 동선을 바꿀까요?', body: '현재 동선 초안을 버리고 서버 저장본을 불러옵니다.', confirm: '저장본 불러오기', action: () => this.load(session, true)}) : this.load(session, true), {disabled: !session.link?.get || this.isLocked()});
    this.root.append(p.el('header', {class: 'route-header'}, p.el('div', {}, p.el('h2', {}, '동선을 그리고, 보는 방향까지'), p.el('p', {}, '구역을 골라 시작하거나 지도 위에 지점을 더해 순서를 만드세요.')), p.el('div', {class: 'route-actions'}, this.status, reload, this.saveButton)));
    this.feedback = p.el('div', {class: 'route-feedback', role: 'status', 'aria-live': 'polite'});
    this.root.append(this.feedback);

    const routeOptions = [['', '새 동선'], ...(session.snapshot?.saved || []).map(route => [route.id, route.name])];
    const select = p.select('저장된 동선', routeOptions, session.snapshot?.saved.some(route => route.id === session.draft.id) ? session.draft.id : '', value => this.switchRoute(value));
    select.disabled = this.isLocked();
    const name = p.el('input', {name: '동선 이름', value: session.draft.name, maxlength: 60, disabled: this.isLocked(), oninput: event => {session.draft.name = event.target.value; this.changed({map: false, validate: false});}});
    const repeatMode = session.draft.repeat === 0 ? 'forever' : session.draft.repeat === 1 ? 'once' : 'count';
    const repeat = p.select('동선 반복', [['once', '1회'], ['count', '횟수 지정'], ['forever', '계속']], repeatMode, value => {
      session.draft.repeat = value === 'forever' ? 0 : value === 'once' ? 1 : 2;
      delete session.invalid.repeat;
      this.changed(); this.draw();
    });
    repeat.disabled = this.isLocked();
    const repetitions = p.el('input', {name: '반복 횟수', type: 'number', min: 2, max: 9999, step: 1, value: session.invalid.repeat ?? session.draft.repeat, disabled: this.isLocked(), oninput: event => {
      if (!event.target.checkValidity() || !event.target.value) session.invalid.repeat = event.target.value;
      else {delete session.invalid.repeat; session.draft.repeat = Number(event.target.value);}
      event.target.setAttribute('aria-invalid', String('repeat' in session.invalid)); this.changed();
    }});
    this.root.append(p.el('div', {class: 'route-library'}, p.field('저장된 동선', select), p.field('동선 이름', name), p.field('반복', repeat), repeatMode === 'count' ? p.field('반복 횟수', repetitions) : null,
      p.button('새 동선', () => this.switchRoute(''), {disabled: this.isLocked(), 'data-route-new': ''}),
      p.button('동선 삭제', () => this.confirmDelete(), {disabled: this.isLocked() || !session.snapshot?.saved.some(route => route.id === session.draft.id), 'data-route-delete': ''})));

    this.zoneList = p.el('div', {class: 'route-zone-list'});
    this.pointsList = p.el('div', {class: 'route-points', 'aria-label': '동선 지점 목록'});
    const itinerary = p.el('aside', {class: 'route-itinerary'}, p.el('h3', {}, '구역 고르기'), this.zoneList,
      p.button('고른 구역으로 동선 만들기', () => this.useZones(), {disabled: this.isLocked() || !session.zones.some(readyPoint), 'data-route-from-zones': ''}),
      p.note('체크한 순서대로 방문합니다. 위·아래 버튼으로 순서를 바꾸세요.'), p.el('h3', {}, '방문 지점'), this.pointsList);
    this.map = p.el('div', {class: 'route-map'});
    this.validation = p.el('div', {class: 'route-validation', role: 'status', 'aria-live': 'polite'});
    this.modeBar = mapClickMode(p, session.mode, mode => {session.mode = mode; this.draw();}, mode => !this.isLocked() && (mode === 'draw' || (mode === 'move' ? this.canCommand() : this.canLocate())));
    const canvas = p.el('section', {class: 'route-canvas'}, this.modeBar, this.map,
      p.note(session.mode === 'draw' ? '지도를 눌러 지점 추가 · 지점을 끌어 이동 · 화살표 끝을 끌어 보는 방향 설정' : session.mode === 'move' ? '지도에서 목적지를 누르면 이동 확인 창이 열립니다.' : '로봇이 지금 있는 곳을 누르면 위치 확인 창이 열립니다. 파란 원은 알려준 위치를 찾는 범위이며 위치가 확인되면 사라집니다.'),
      this.canLocate() ? null : p.note('「위치 알려주기」는 지도 위치 지정 기능이 연결된 서버에서 사용할 수 있습니다.'), this.previewButton, this.validation);
    this.inspector = p.el('aside', {class: 'route-inspector'});
    this.root.append(p.el('div', {class: 'route-layout'}, itinerary, canvas, this.inspector));
    this.progress = p.el('p', {class: 'route-progress', 'data-route-progress': '', role: 'status', 'aria-live': 'polite'});
    this.root.append(p.el('footer', {class: 'route-footer'}, p.el('div', {}, this.progress,
      p.note('저장본은 다음 서버 시작에도 유지됩니다. 주행은 저장 후 아래 버튼으로 따로 시작합니다. 빨간 구간은 벽·가구·몸 반경에 걸려 저장할 수 없습니다.')),
      p.el('div', {class: 'route-actions'}, this.startButton, this.stopButton)));
    this.drawZones(); this.drawPoints(); this.drawInspector(); this.drawMap(); this.drawValidation(); this.updateStatus();
  }

  drawZones() {
    const p = this.p, session = this.current;
    this.zoneList.replaceChildren();
    const ordered = [...session.zoneOrder, ...session.zones.map(zone => zone.id).filter(id => !session.zoneOrder.includes(id))];
    for (const id of ordered) {
      const zone = session.zones.find(item => item.id === id);
      if (!zone) continue;
      const index = session.zoneOrder.indexOf(id);
      const move = delta => {const to = index + delta; [session.zoneOrder[index], session.zoneOrder[to]] = [session.zoneOrder[to], session.zoneOrder[index]]; this.drawZones();};
      const checkbox = p.el('input', {type: 'checkbox', checked: index >= 0, 'data-route-zone': id, disabled: this.isLocked() || !readyPoint(zone), onchange: event => {
        session.zoneOrder = session.zoneOrder.filter(value => value !== id);
        if (event.target.checked) session.zoneOrder.push(id);
        this.drawZones();
      }});
      this.zoneList.append(p.el('div', {class: 'route-zone-row'}, p.el('label', {}, checkbox,
        p.el('span', {}, (index >= 0 ? (index + 1) + '. ' : '') + id + (zone.name !== id ? ' · ' + zone.name : '') + (!readyPoint(zone) ? ' · 위치 없음' : ''))),
        index < 0 ? null : p.el('div', {class: 'route-reorder'}, p.button('↑', () => move(-1), {'aria-label': '동선 구역 ' + id + ' 위로', disabled: this.isLocked() || index === 0}), p.button('↓', () => move(1), {'aria-label': '동선 구역 ' + id + ' 아래로', disabled: this.isLocked() || index === session.zoneOrder.length - 1}))));
    }
    if (!ordered.length) this.zoneList.append(p.note('구역 위치를 저장하면 여기에 표시됩니다.'));
  }

  useZones() {
    const session = this.current, points = pointsFromZones(session.zones, session.zoneOrder);
    if (!points.length) {session.error = '위치가 지정된 구역을 먼저 고르세요.'; this.drawValidation(); return;}
    const apply = () => {session.draft.points = points; session.selected = 0; session.invalid = {}; this.changed(); this.drawInspector();};
    if (session.draft.points.length) this.p.confirmDevice({title: '지점을 고른 구역으로 바꿀까요?', body: '현재 동선 지점을 선택한 구역 순서로 바꿉니다.', confirm: '지점 바꾸기', action: apply});
    else apply();
  }

  switchRoute(id) {
    const session = this.current;
    const apply = () => {
      const route = session.snapshot?.saved.find(item => item.id === id);
      session.draft = route ? clone(route) : freshRoute();
      session.dirty = false; session.invalid = {}; session.selected = route?.points.length ? 0 : -1;
      session.validation = null; session.edit++; session.error = ''; session.notice = '';
      session.zoneOrder = session.draft.points.map(point => point.label).filter(value => session.zones.some(zone => zone.id === value));
      this.forget(session); this.draw(); if (route) this.validate(session);
    };
    if (session.dirty) this.p.confirmDevice({title: '다른 동선을 열까요?', body: '저장하지 않은 현재 동선 수정 내용이 사라집니다.', confirm: '동선 바꾸기', action: apply});
    else apply();
  }

  drawPoints() {
    if (!this.pointsList) return;
    const p = this.p, session = this.current, active = this.activePoint(), focused = p.document.activeElement;
    const focusPoint = this.pointsList.contains(focused) ? focused.dataset.routePoint : null;
    const focusAction = this.pointsList.contains(focused) ? focused.getAttribute('aria-label') : null;
    this.pointsList.replaceChildren();
    session.draft.points.forEach((point, index) => {
      const choose = () => {session.selected = index; this.drawPoints(); this.drawMap(); this.drawInspector();};
      const move = delta => {const to = index + delta; [session.draft.points[index], session.draft.points[to]] = [session.draft.points[to], session.draft.points[index]]; session.selected = to; this.remapInvalid(old => old === index ? to : old === to ? index : old); this.changed(); this.drawInspector();};
      this.pointsList.append(p.el('div', {class: 'route-point-row' + (index === active ? ' active' : '')},
        p.button([p.el('strong', {}, (index + 1) + '. ' + (point.label || '지점')), p.el('small', {}, point.x.toFixed(2) + ', ' + point.y.toFixed(2) + (point.aim_deg == null ? '' : ' · ' + point.aim_deg + '°'))], choose,
          {class: 'route-point' + (index === session.selected ? ' selected' : ''), 'data-route-point': index, 'aria-pressed': index === session.selected, disabled: this.isLocked()}),
        p.el('div', {class: 'route-reorder'}, p.button('↑', () => move(-1), {'aria-label': (index + 1) + '번 지점 위로', disabled: this.isLocked() || index === 0}), p.button('↓', () => move(1), {'aria-label': (index + 1) + '번 지점 아래로', disabled: this.isLocked() || index === session.draft.points.length - 1}))));
    });
    if (!session.draft.points.length) this.pointsList.append(p.note('아직 지점이 없습니다. 구역을 고르거나 지도에서 선을 그리세요.'));
    if (focusPoint != null || focusAction) {
      const buttons = [...this.pointsList.querySelectorAll('button')];
      (buttons.find(button => focusAction && button.getAttribute('aria-label') === focusAction && !button.disabled) || buttons.find(button => button.dataset.routePoint === focusPoint) || this.pointsList.querySelector('[data-route-point="' + session.selected + '"]'))?.focus();
    }
  }

  remapInvalid(transform) {
    const session = this.current, invalid = {};
    for (const [key, value] of Object.entries(session.invalid)) {
      if (!key.includes(':')) {invalid[key] = value; continue;}
      const [index, field] = key.split(':'), next = transform(Number(index));
      if (next !== null) invalid[next + ':' + field] = value;
    }
    session.invalid = invalid;
  }

  drawInspector() {
    const p = this.p, session = this.current, index = session.selected, point = session.draft.points[index];
    this.inspector.replaceChildren(p.el('h3', {}, point ? (index + 1) + '번 지점 설정' : '지점 설정'));
    if (!point) {this.inspector.append(p.note('지도나 목록에서 지점을 선택하세요.')); return;}
    const number = (label, key, min, max, optional = false) => {
      const invalidKey = index + ':' + key;
      return p.field(label, p.el('input', {type: 'number', name: label, min, max, step: 'any', value: session.invalid[invalidKey] ?? point[key] ?? (key === 'dwell_s' ? 0 : ''), disabled: this.isLocked(), 'aria-invalid': invalidKey in session.invalid, placeholder: optional ? '도착 방향 유지' : '', oninput: event => {
        if (!event.target.checkValidity() || (!optional && !event.target.value)) session.invalid[invalidKey] = event.target.value;
        else {delete session.invalid[invalidKey]; point[key] = event.target.value === '' ? null : Number(event.target.value);}
        event.target.setAttribute('aria-invalid', String(invalidKey in session.invalid)); this.changed();
      }}));
    };
    this.inspector.append(p.field('지점 이름', p.el('input', {name: '지점 이름', maxlength: 60, value: point.label || '', disabled: this.isLocked(), oninput: event => {point.label = event.target.value || null; this.changed({validate: false});}})),
      p.el('div', {class: 'route-coordinates'}, number('지점 X (m)', 'x', -10000, 10000), number('지점 Y (m)', 'y', -10000, 10000)),
      number('보는 방향 (°)', 'aim_deg', -180, 180, true), number('머무름 (초)', 'dwell_s', 0, 3600),
      p.note('화살표 끝을 끌어 방향을 정합니다. 각도는 순찰 좌표 기준(+X가 0°, +Y가 +90°)이며 화살표는 지도 회전에 맞춰 표시됩니다. 방향을 비우면 도착 방향을 유지합니다. 구역 점검은 해당 구역 이름(A~D)의 지점이 저장된 구역 위치에 도착했을 때 수행합니다.'),
      p.button('방향 비우기', () => {point.aim_deg = null; delete session.invalid[index + ':aim_deg']; this.changed(); this.drawInspector();}, {disabled: this.isLocked()}),
      p.button('지점 삭제', () => {session.draft.points.splice(index, 1); session.selected = Math.min(index, session.draft.points.length - 1); this.remapInvalid(old => old === index ? null : old > index ? old - 1 : old); this.changed(); this.drawInspector();}, {'data-route-point-delete': '', disabled: this.isLocked(), class: 'op-button route-delete-point'}));
  }

  svg(tag, attrs = {}, content) {
    const node = this.p.document.createElementNS(NS, tag);
    for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, String(value));
    if (content != null) node.textContent = content;
    return node;
  }

  drawMap() {
    if (!this.map) return;
    const session = this.current, meta = session.mapMeta;
    this.map.replaceChildren();
    if (!meta?.patrol_to_px || !meta.px_to_patrol || !(meta.width > 0 && meta.height > 0)) {this.map.append(this.p.el('div', {class: 'route-map-empty'}, this.p.el('h3', {}, '현장 지도 연결 대기'), this.p.note(session.mapError ? '관제 지도를 불러오지 못했습니다. ' + session.mapError : '저장된 실제 지도를 연결하면 동선을 그릴 수 있습니다.'))); return;}
    const svg = this.svg('svg', {viewBox: '0 0 1000 1000', preserveAspectRatio: 'none', class: 'route-map-svg', 'aria-label': '동선 지도', 'data-route-map': '', 'data-click-mode': session.mode});
    svg.style.aspectRatio = meta.width + '/' + meta.height;
    const imageUrl = (session.link.baseUrl || '') + '/api/map.png?rev=' + session.snapshot.revision;
    if (this.imageUrl !== imageUrl) {this.imageUrl = imageUrl; this.mapImage = mapImage(this.svg.bind(this), session.link.baseUrl, session.snapshot.revision);}
    svg.append(this.mapImage);
    for (const zone of session.zones.filter(readyPoint)) {
      const [x, y] = mapPoint(meta, zone.x, zone.y);
      svg.append(this.svg('circle', {cx: x, cy: y, r: 22, class: 'route-zone-anchor'}), this.svg('text', {x, y: y - 30, class: 'route-zone-label', 'text-anchor': 'middle'}, zone.id));
    }
    const points = session.draft.points;
    const segments = points.slice(1).map((point, index) => ({from_index: index, to_index: index + 1, points: [[points[index].x, points[index].y], [point.x, point.y]]}));
    if (points.length > 1 && session.draft.repeat !== 1) segments.push({from_index: points.length - 1, to_index: 0, points: [[points.at(-1).x, points.at(-1).y], [points[0].x, points[0].y]]});
    for (const segment of segments) {
      const checked = session.validation?.segments?.find(item => item.from_index === segment.from_index && item.to_index === segment.to_index);
      const path = this.svg('polyline', {points: segment.points.map(([x, y]) => mapPoint(meta, x, y).join(',')).join(' '), class: 'route-line' + (checked?.valid === false ? ' blocked' : '') + (!session.validation ? ' unchecked' : ''), 'data-route-segment': segment.from_index + '-' + segment.to_index, 'data-blocked': checked?.valid === false, 'vector-effect': 'non-scaling-stroke'});
      if (checked?.reason) path.append(this.svg('title', {}, checked.reason));
      svg.append(path);
    }
    const active = this.activePoint();
    points.forEach((point, index) => {
      const [x, y] = mapPoint(meta, point.x, point.y), selected = index === session.selected;
      const group = this.svg('g', {class: 'route-map-point' + (selected ? ' selected' : '') + (index === active ? ' active' : ''), 'data-route-pin': index});
      if (point.aim_deg != null || selected) this.drawAim(group, point, index, meta);
      const pin = this.svg('circle', {cx: x, cy: y, r: index === active ? 22 : 17, class: 'route-pin' + (session.validation?.invalid_points?.includes(index) ? ' blocked' : ''), 'vector-effect': 'non-scaling-stroke'});
      pin.addEventListener('pointerdown', event => this.beginDrag(event, index, 'point', svg));
      group.append(pin, this.svg('text', {x, y, 'text-anchor': 'middle', 'dominant-baseline': 'central', class: 'route-pin-label'}, index + 1));
      group.addEventListener('click', event => {if (session.mode !== 'draw') return; event.stopPropagation(); session.selected = index; this.drawPoints(); this.drawMap(); this.drawInspector();});
      svg.append(group);
    });
    const hint = pointHintOutline(session.nav);
    if (hint.length) svg.append(this.svg('polygon', {points: hint.map(([x, y]) => mapPoint(meta, x, y).join(',')).join(' '), class: 'route-location-hint', 'data-route-location-hint': '', 'aria-label': '알려준 위치를 찾는 범위', 'vector-effect': 'non-scaling-stroke'}));
    this.drawRobot(svg, meta);
    svg.addEventListener('click', event => this.mapClick(event, svg));
    this.map.append(svg);
  }

  drawAim(group, point, index, meta) {
    const length = Math.min(meta.width, meta.height) * meta.resolution_m * .065;
    // Transform the whole patrol-space arrow so its heading includes the display rotation.
    const angle = (point.aim_deg ?? 0) * Math.PI / 180, dx = Math.cos(angle), dy = Math.sin(angle), head = length * .25;
    const tx = point.x + dx * length, ty = point.y + dy * length, coords = (x, y) => mapPoint(meta, x, y).join(' ');
    group.append(this.svg('path', {d: 'M ' + coords(point.x, point.y) + ' L ' + coords(tx, ty) + ' M ' + coords(tx - dx * head - dy * head * .6, ty - dy * head + dx * head * .6) + ' L ' + coords(tx, ty) + ' L ' + coords(tx - dx * head + dy * head * .6, ty - dy * head - dx * head * .6), class: 'route-aim' + (point.aim_deg == null ? ' unset' : ''), 'vector-effect': 'non-scaling-stroke'}));
    if (index === this.current.selected && this.current.mode === 'draw') {
      const [x, y] = mapPoint(meta, tx, ty);
      const handle = this.svg('circle', {cx: x, cy: y, r: 16, class: 'route-aim-handle', 'data-route-aim': index, 'vector-effect': 'non-scaling-stroke'});
      handle.append(this.svg('title', {}, point.aim_deg == null ? '끌어서 보는 방향 설정' : '보는 방향 ' + point.aim_deg + '°'));
      handle.addEventListener('pointerdown', event => this.beginDrag(event, index, 'aim', this.map.querySelector('svg')));
      group.append(handle);
    }
  }

  drawRobot(svg, meta) {
    const nav = this.current.nav;
    if (!Array.isArray(nav?.pose) || nav.available === false) return;
    const [x, y, yaw] = nav.pose, length = Math.min(meta.width, meta.height) * meta.resolution_m * .035;
    const angle = yaw * Math.PI / 180, dx = Math.cos(angle), dy = Math.sin(angle);
    const points = [[x + dx * length, y + dy * length], [x - dx * length * .7 - dy * length * .6, y - dy * length * .7 + dx * length * .6], [x - dx * length * .7 + dy * length * .6, y - dy * length * .7 - dx * length * .6]];
    svg.append(this.svg('polygon', {points: points.map(([px, py]) => mapPoint(meta, px, py).join(',')).join(' '), class: 'route-robot' + (this.current.navError || nav.stale || !(nav.verified || nav.seeded) ? ' uncertain' : ''), 'data-route-robot': '', 'aria-label': '로봇 추정 위치', 'vector-effect': 'non-scaling-stroke'}));
  }

  toWorld(event, rect) {
    const meta = this.current.mapMeta;
    if (!meta?.px_to_patrol || !rect.width || !rect.height) return null;
    const x = (event.clientX - rect.left) / rect.width, y = (event.clientY - rect.top) / rect.height;
    if (x < 0 || y < 0 || x > 1 || y > 1) return null;
    return worldPoint(meta, x, y).map(value => +value.toFixed(3));
  }

  mapClick(event, svg) {
    const session = this.current;
    if (!this.visible(session) || session.link !== this.context().link || this.isLocked() || Date.now() < (this.suppressClickUntil || 0)) return;
    const point = this.toWorld(event, svg.getBoundingClientRect());
    if (!point) return;
    if (session.mode === 'move') {if (this.canCommand()) this.p.confirmGoto(...point); return;}
    if (session.mode === 'locate') {if (this.canLocate()) this.p.confirmLocatePoint(...point); return;}
    if (session.draft.points.length >= 256) {session.error = '동선은 최대 256개 지점까지 만들 수 있습니다.'; this.drawValidation(); return;}
    session.draft.points.push({x: point[0], y: point[1], aim_deg: null, dwell_s: 0, label: null});
    session.selected = session.draft.points.length - 1;
    this.changed(); this.drawInspector();
  }

  beginDrag(event, index, kind, svg) {
    const session = this.current;
    if (session.mode !== 'draw' || this.isLocked() || event.button > 0) return;
    event.preventDefault(); event.stopPropagation();
    this.cancelDrag?.();
    session.selected = index;
    const rect = svg.getBoundingClientRect(), point = session.draft.points[index], start = {x: point.x, y: point.y, aim_deg: point.aim_deg};
    let moved = false;
    const document = this.p.document;
    const move = next => {
      if (!this.visible(session)) return;
      const target = this.toWorld(next, rect);
      if (!target) return;
      moved = true;
      if (kind === 'aim') point.aim_deg = Math.round(Math.atan2(target[1] - point.y, target[0] - point.x) * 180 / Math.PI);
      else {point.x = target[0]; point.y = target[1];}
      session.validation = null;
      this.drawMap();
    };
    const cleanup = () => {document.removeEventListener('pointermove', move); document.removeEventListener('pointerup', up); document.removeEventListener('pointercancel', cancel); this.cancelDrag = null;};
    const up = () => {cleanup(); if (!this.visible(session)) {Object.assign(point, start); return;} this.suppressClickUntil = Date.now() + 150; if (moved) {delete session.invalid[index + ':' + (kind === 'aim' ? 'aim_deg' : 'x')]; if (kind === 'point') delete session.invalid[index + ':y']; this.changed(); this.drawInspector();} else {this.drawPoints(); this.drawMap(); this.drawInspector();}};
    const cancel = () => {cleanup(); Object.assign(point, start); if (this.visible(session)) this.drawMap();};
    this.cancelDrag = cancel;
    document.addEventListener('pointermove', move); document.addEventListener('pointerup', up); document.addEventListener('pointercancel', cancel);
  }

  activePoint() {
    const session = this.current, route = session.nav?.route;
    if (route?.points) {
      const shape = points => JSON.stringify(points.map(point => [point.x, point.y, point.aim_deg ?? null, point.dwell_s ?? 0, point.label ?? null]));
      if (shape(route.points) !== shape(session.draft.points)) return -1;
    }
    return !session.dirty && route?.id === session.draft.id && !['completed', 'stopped', 'cancelled'].includes(route.status) ? route.point_index : -1;
  }

  drawValidation() {
    if (!this.validation) return;
    const p = this.p, session = this.current;
    this.feedback.replaceChildren(...[session.error && p.note(session.error, 'warning'), session.notice && p.note(session.notice)].filter(Boolean));
    if (!session.dirty && session.snapshot?.saved.some(route => route.id === session.draft.id) && !this.savedDigest(session)) this.feedback.append(p.note('주행 확인에 필요한 저장본 정보가 없습니다. 저장본을 다시 불러오세요.', 'warning'));
    this.validation.replaceChildren();
    if (!session.validation) {this.validation.append(p.note(session.draft.points.length ? '동선을 바꾸면 저장 지도에서 장애물과 몸 반경을 확인합니다.' : '지도에 지점을 추가하세요.')); return;}
    const result = session.validation;
    this.validation.append(p.el('strong', {'data-route-valid': result.valid}, result.valid ? '장애물 확인 완료 · ' + (result.total_m || 0).toFixed(1) + ' m' : '저장 불가 · 빨간 구간 또는 지점을 옮겨 주세요.'));
    for (const segment of result.segments || []) if (!segment.valid) this.validation.append(p.note((segment.from_index + 1) + ' → ' + (segment.to_index + 1) + ' 지점: ' + (segment.reason || '벽·가구 또는 몸 반경과 겹칩니다.'), 'warning'));
    if (result.invalid_points?.length) this.validation.append(p.note('이동할 지점: ' + result.invalid_points.map(index => index + 1).join(', '), 'warning'));
  }

  async validate(session = this.current) {
    // Drag edits can outrun the preview response. Queue one latest request rather
    // than racing the server's shared map-validation lock.
    while (session.validationWork) {
      await session.validationWork;
      if (session.validation && session.validatedEdit === session.edit && session.validatedRevision === session.revision) return session.validation;
    }
    const pending = this.validateRoute(session);
    session.validationWork = pending;
    try {return await pending;}
    finally {if (session.validationWork === pending) session.validationWork = null;}
  }

  async validateRoute(session) {
    if (!session.snapshot?.map?.available || !session.draft.points.length || Object.keys(session.invalid).length) return null;
    const edit = session.edit, routeId = session.draft.id, revision = session.revision;
    try {
      const result = await session.link.post(ROUTES + '/preview', {revision: session.revision, route: clone(session.draft)}, 30000);
      if (session.edit !== edit || session.draft.id !== routeId || session.revision !== revision) return null;
      session.validation = result;
      session.validatedEdit = edit;
      session.validatedRevision = revision;
      if (this.visible(session)) {this.drawValidation(); this.drawMap(); this.updateStatus();}
      return result;
    } catch (error) {
      if (session.edit !== edit || session.draft.id !== routeId || session.revision !== revision) return null;
      session.error = '장애물을 확인하지 못했습니다. ' + error.message;
      if (this.visible(session)) {this.drawValidation(); this.updateStatus();}
      return null;
    }
  }

  async save() {
    const session = this.current;
    if (this.isLocked() || !session.snapshot || !session.dirty || Object.keys(session.invalid).length || !session.draft.points.length || !session.draft.name.trim()) return;
    clearTimeout(this.validationTimer);
    session.busy = true; session.error = ''; this.draw();
    try {
      const validation = await this.validate(session);
      if (!validation?.valid) {session.error ||= '장애물 확인이 끝난 유효한 동선만 저장할 수 있습니다.'; return;}
      const previousRevision = session.revision;
      const result = await session.link.post(ROUTES, {revision: session.revision, route: clone(session.draft)}, 30000);
      session.snapshot = result; session.revision = result.revision;
      session.draft = clone(result.saved.find(route => route.id === session.draft.id));
      session.dirty = false; this.forget(session);
      session.notice = '동선을 저장했습니다. 「이 동선으로 주행」에서 확인한 뒤 시작할 수 있습니다.';
      this.syncZoneRevision(session, previousRevision);
    } catch (error) {session.error = '저장하지 못했습니다. 작성 내용은 유지됩니다. ' + error.message;}
    finally {session.busy = false; if (this.visible(session)) this.draw();}
  }

  confirmDelete() {
    const session = this.current, id = session.draft.id, revision = session.revision;
    this.p.confirmDevice({title: '이 동선을 삭제할까요?', body: '「' + session.draft.name + '」 저장본을 삭제합니다. 실행 중인 로봇은 위쪽 정지 버튼으로 먼저 멈추세요.', confirm: '동선 삭제', action: async () => {
      if (this.context().key !== session.key || session.revision !== revision) throw new Error('대상 또는 저장본이 바뀌었습니다. 삭제할 동선을 다시 확인하세요.');
      session.busy = true; session.error = ''; if (this.visible(session)) this.draw();
      try {
        const previousRevision = session.revision;
        const result = await session.link.delete(ROUTES + '/' + encodeURIComponent(id), {revision});
        session.snapshot = result; session.revision = result.revision; session.draft = result.saved[0] ? clone(result.saved[0]) : freshRoute();
        session.selected = session.draft.points.length ? 0 : -1; session.validation = null; session.dirty = false; session.invalid = {}; this.forget(session);
        session.notice = '동선을 삭제했습니다.';
        this.syncZoneRevision(session, previousRevision);
      } catch (error) {session.error = '삭제하지 못했습니다. ' + error.message;}
      finally {session.busy = false; if (this.visible(session)) this.draw();}
    }});
  }

  confirmStart() {
    const session = this.current, p = this.p, store = p.store;
    if (session.dirty || !this.canCommand() || !this.savedDigest(session)) return;
    const route = clone(session.draft), link = session.link, robot = store.selected, expectedDigest = this.savedDigest(session), fingerprint = JSON.stringify(route);
    p.confirmDevice({icon: 'route', title: '「' + route.name + '」 동선으로 주행할까요?', body: route.points.length + '개 지점을 순서대로 방문합니다. 반복: ' + (route.repeat === 0 ? '계속' : route.repeat + '회') + '. 지점마다 보는 방향을 맞추고 지정한 시간 동안 머뭅니다. 로봇 주변과 경로를 확인하세요.', confirm: '예, 이 동선으로 주행합니다', action: async () => {
      if (store.link !== link || store.selected !== robot || this.context().key !== session.key) throw new Error('대상 로봇이 바뀌었습니다. 새 지도에서 동선을 다시 확인하세요.');
      if (session.dirty || JSON.stringify(session.draft) !== fingerprint || this.savedDigest(session) !== expectedDigest) throw new Error('동선이 변경되었습니다. 저장 후 다시 확인하세요.');
      const result = await store.requestDevice('동선 시작 · ' + route.name, () => link.route('start', route.id, expectedDigest), robot);
      p.onToast(result?.detail || '동선 주행을 요청했습니다.');
      await this.poll();
    }});
  }

  async poll() {
    const session = this.current;
    if (!session || !this.visible(session) || !session.link?.get || !this.p.store.live || session.polling) return;
    session.polling = true;
    try {
      const previousPoint = this.activePoint();
      const nav = await session.link.get('/api/nav');
      session.nav = nav; session.navError = '';
      if (this.visible(session) && !this.cancelDrag) {this.updateStatus(); this.drawMap(); if (this.activePoint() !== previousPoint) this.drawPoints();}
    } catch (error) {session.navError = error.message; if (this.visible(session)) {this.updateStatus(); if (!this.cancelDrag) this.drawMap();}}
    finally {session.polling = false;}
  }

  dispose() {clearInterval(this.timer); clearTimeout(this.validationTimer); this.cancelDrag?.();}
}
