import {applyAffine, mapImage, mapRobotPoints} from './map-frame.js';
import {describeNav} from './live-map.js';

const point = p => Array.isArray(p) && p.length >= 2 && p.slice(0, 2).every(Number.isFinite);

// 같은 DOM과 PNG를 유지하고 SVG 오버레이만 갱신한다. 클릭·터치로 이동 명령을 보내지 않는다.
export class ControlMinimap {
  constructor({document, getLink, onOpen, setInterval: every = globalThis.setInterval}) {
    Object.assign(this, {document, getLink});
    this.sessions = new WeakMap();
    this.root = document.createElement('section'); this.root.className = 'control-minimap'; this.root.dataset.controlMinimap = '';
    const heading = document.createElement('div'); heading.className = 'minimap-heading';
    const title = document.createElement('h4'); title.textContent = '자기 위치 · 2D 미니맵';
    const button = document.createElement('button'); button.type = 'button'; button.className = 'op-button'; button.textContent = '크게 보기'; button.addEventListener('click', onOpen);
    heading.append(title, button);
    this.canvas = this.svg('svg', {viewBox:'0 0 1000 1000', preserveAspectRatio:'xMidYMid meet', role:'img', 'aria-label':'로봇 위치, 경로와 현재 동선 · 관측 전용'});
    this.status = document.createElement('p'); this.status.className = 'op-note'; this.status.setAttribute('role','status');
    this.routeStatus = document.createElement('p'); this.routeStatus.className = 'op-note minimap-legend';
    this.root.append(heading, this.canvas, this.status, this.routeStatus);
    this.timer = every(() => this.tick(), 100); this.timer?.unref?.(); this.draw();
  }
  svg(tag, attrs = {}, text) {
    const node = this.document.createElementNS('http://www.w3.org/2000/svg', tag);
    for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, String(value));
    if (text != null) node.textContent = text;
    return node;
  }
  async tick() {
    if (!this.root.isConnected) return;
    const link = this.getLink();
    if (this.link !== link) {
      this.link = link; this.current = null;
      if (link) {
        if (!this.sessions.has(link)) this.sessions.set(link, {meta:null, nav:null});
        this.current = this.sessions.get(link);
      }
      this.draw();
    }
    const session = this.current;
    if (!session || session.busy || !link?.get) return;
    session.busy = true;
    try {
      if (!session.meta) {
        session.meta = await link.get('/api/map/meta');
        session.image = mapImage(this.svg.bind(this), link.baseUrl, session.meta.revision || 0);
      }
      session.nav = await link.get('/api/nav'); session.error = '';
    } catch (error) {session.error = error.message || '지도 연결 대기';}
    finally {session.busy = false; if (this.current === session && this.getLink() === link) this.draw();}
  }
  draw() {
    const s = this.current, meta = s?.meta, nav = s?.nav;
    this.status.textContent = s?.error ? '지도 연결 끊김 · ' + s.error : this.link ? describeNav(nav) : '지도·위치 미수신 · 관제 서버 연결 대기';
    this.routeStatus.textContent = '';
    this.canvas.hidden = !meta;
    if (!meta) {this.canvas.replaceChildren(); return;}
    this.canvas.style.aspectRatio = meta.width + '/' + meta.height;
    this.canvas.setAttribute('viewBox', `0 0 ${meta.width} ${meta.height}`);
    s.image.setAttribute('width', meta.width); s.image.setAttribute('height', meta.height);
    if (s.image.parentNode !== this.canvas) this.canvas.replaceChildren(s.image);
    this.overlay?.remove(); this.overlay = this.svg('g'); this.canvas.append(this.overlay);
    const px = (x, y) => applyAffine(meta.patrol_to_px, x, y);
    const unit = Math.min(meta.width, meta.height) / 1000;
    const rect = this.canvas.getBoundingClientRect();
    const scale = Math.min(rect.width / meta.width, rect.height / meta.height) || 1;
    const labelSize = Math.max(32 * unit, 12 / scale);
    const zoneRadius = Math.max(19 * unit, 5 / scale);
    const line = (points, cls) => {if (points?.length > 1) this.overlay.append(this.svg('polyline', {points:points.filter(point).map(p => px(...p).join(',')).join(' '), class:cls}));};
    const route = nav?.route;
    const selected = route;
    if (selected?.points?.length) {
      line(selected.points.map(p => [p.x, p.y]), 'minimap-route');
      this.routeStatus.textContent = '주황: 현재 동선 · ' + (selected.name || selected.id);
    } else this.routeStatus.textContent = route?.id ? '현재 동선 · ' + (route.name || route.id) : '현재 동선 없음';
    if (point(nav?.pose)) line([nav.pose, ...(nav.path || [])], 'minimap-path');
    const skipped = new Set(nav?.blockage?.skipped_zones || []);
    for (const zone of meta.zones || []) {
      if (skipped.has(zone.id)) for (const [x,y,w,h] of zone.runs || []) {
        this.overlay.append(this.svg('polygon',{points:[[x,y],[x+w,y],[x+w,y+h],[x,y+h]].map(p=>px(...p).join(',')).join(' '),fill:'#9ca3af','fill-opacity':'.6','data-skipped-zone':zone.id}));
      }
      const [x,y] = px(zone.x, zone.y);
      this.overlay.append(this.svg('circle',{cx:x,cy:y,r:zoneRadius,class:'minimap-zone',fill:skipped.has(zone.id)?'#9ca3af':'none','vector-effect':'non-scaling-stroke'}),this.svg('text',{x,y:y-zoneRadius-labelSize*.7,'text-anchor':'middle',class:'minimap-zone-label',style:`font-size:${labelSize}px;stroke-width:${Math.max(5*unit,2/scale)}px`},zone.name || zone.label || zone.id));
    }
    for (const obstacle of nav?.blockage?.obstacles || []) {
      for (const p of obstacle.points || [[obstacle.x,obstacle.y]]) {
        const [cx,cy]=px(...p);
        this.overlay.append(this.svg('circle',{cx,cy,r:Math.max(4*unit,zoneRadius*.6),fill:'#dc2626','fill-opacity':'.6','data-blockage-id':obstacle.id}));
      }
    }
    if (nav?.available !== false && point(nav?.pose) && Number.isFinite(nav.pose[2])) {
      const m = meta.patrol_to_px, yaw = nav.pose[2] * Math.PI / 180;
      const pixelsPerMetre = Math.hypot(m[0][0]*Math.cos(yaw)+m[0][1]*Math.sin(yaw),m[1][0]*Math.cos(yaw)+m[1][1]*Math.sin(yaw));
      const length = Math.max(.3, 12 / (scale * pixelsPerMetre || 1));
      const corners = mapRobotPoints(meta, nav.pose, {normalized:false, length}).map(p => p.join(','));
      this.overlay.append(this.svg('polygon',{points:corners.join(' '),class:'minimap-robot' + (s.error || nav.stale || !(nav.verified || nav.seeded) ? ' uncertain' : '')}));
      if (nav.zone) this.status.textContent += ' · 현재 구역 ' + nav.zone;
    }
  }
  dispose() {globalThis.clearInterval(this.timer);}
}
