// 실제 집 지도 — 로봇의 자기 위치를 그리고, 지도에서 찍은 곳으로 로봇을 보낸다.
//
// 서버(`/api/map/meta`, `/api/map.png`, `/api/nav`)가 그림과 «픽셀 ↔ 순찰 좌표» 행렬을 준다.
// 화면은 행렬만 곱한다 — 좌표계 규칙(출발 자리 원점·평면도 회전)은 서버 한 곳에만 있다.
// 패널은 자주 통째로 다시 그려지므로 이 객체는 한 번 만들고 같은 DOM 을 다시 붙인다.

const POLL_MS = 500;

/** 2×3 아핀 행렬을 점에 곱한다. */
export function applyAffine(m, x, y) {
  return [m[0][0] * x + m[0][1] * y + m[0][2], m[1][0] * x + m[1][1] * y + m[1][2]];
}

/** 로봇 상태를 사람 말로. 위치를 모르면 그렇다고 먼저 말한다. */
export function describeNav(nav) {
  if (!nav || nav.available === false) return '측위 정보 없음 — LiDAR 측위 순찰이 아닙니다.';
  const parts = [];
  if (nav.stale) parts.push('위치 상실 — 다시 찾는 중');
  else if (nav.verified) parts.push('위치 확인됨');
  else if (nav.seeded) parts.push('시작 위치 기준(전역 확인 전)');
  else parts.push('위치 미확인 — 이동 불가');
  if (nav.zone) parts.push('구역 ' + nav.zone);
  if (nav.zone_hint) parts.push('구역 ' + nav.zone_hint + ' 안에서 찾는 중');
  if (nav.goal) parts.push('찍은 곳으로 이동 중');
  else if (nav.holding_goal) parts.push('찍은 곳 도착 · 대기 (순찰 시작으로 복귀)');
  else if (nav.target) parts.push('목표 구역 ' + nav.target);
  if (nav.fsm) parts.push('상태 ' + nav.fsm);
  return parts.join(' · ');
}

export class LiveMap {
  /**
   * @param {object} o
   * @param {Document} o.document
   * @param {() => any} o.getLink 현재 RobotLink (없으면 null)
   * @param {(x:number,y:number,where:string) => void} o.onPick 지도에서 찍은 순찰 좌표
   * @param {(available:boolean) => void} [o.onAvailability] 서버가 지도를 줬는가 — 화면이 «지도 없음» 을 바꾼다
   * @param {(fn: Function, ms: number) => any} [o.setInterval]
   */
  constructor({ document, getLink, onPick, onAvailability = () => {}, setInterval: every = globalThis.setInterval }) {
    Object.assign(this, { document, getLink, onPick, onAvailability });
    this.meta = null;
    this.nav = null;
    this.image = null;
    this.error = null;
    this.root = document.createElement('div');
    this.root.className = 'op-live-map';
    this.root.dataset.liveMap = '';
    this.status = document.createElement('p');
    this.status.className = 'op-note';
    this.status.setAttribute('role', 'status');
    this.canvas = document.createElement('canvas');
    this.canvas.setAttribute('aria-label', '실제 집 지도 — 누르면 그곳으로 로봇을 보냅니다');
    this.canvas.style.cursor = 'crosshair';
    this.canvas.style.border = '1px solid rgba(0,0,0,.12)';
    this.canvas.style.borderRadius = '8px';
    this.canvas.addEventListener('click', (event) => this.click(event));
    this.root.append(this.status, this.canvas);
    this.timer = every(() => this.tick(), POLL_MS);
    // Node(시험)에서는 주기 타이머가 프로세스를 붙잡지 않게 — 브라우저는 숫자라 무시된다.
    this.timer?.unref?.();
    this.tick();
  }

  async tick() {
    const link = this.getLink();
    if (!link || !this.root.isConnected) return;
    try {
      if (!this.meta) {
        try {
          this.meta = await link.get('/api/map/meta');
        } catch (error) {
          this.onAvailability(false);
          throw error;
        }
        this.onAvailability(true);
        // 그림은 기다리지 않는다 — 오면 다시 그린다(못 오면 행렬·로봇만 그린다).
        this.loadImage(link.baseUrl + '/api/map.png');
      }
      this.nav = await link.get('/api/nav');
      this.error = null;
    } catch (error) {
      this.error = error?.message || String(error);
    }
    this.draw();
  }

  loadImage(src) {
    const Image = this.document.defaultView?.Image;
    if (typeof Image !== 'function') return;
    const image = new Image();
    image.onload = () => {
      this.image = image;
      this.draw();
    };
    image.src = src;
  }

  /** 캔버스 좌표(픽셀) → 순찰 좌표. 화면 크기와 그림 크기가 달라도 맞춘다. */
  toPatrol(clientX, clientY) {
    const rect = this.canvas.getBoundingClientRect();
    if (!this.meta || !rect.width || !rect.height) return null;
    // object-fit: contain — 그림이 칸 가운데에 비율을 지켜 들어간다(남는 쪽은 여백).
    const scale = Math.min(rect.width / this.meta.width, rect.height / this.meta.height);
    const left = rect.left + (rect.width - this.meta.width * scale) / 2;
    const top = rect.top + (rect.height - this.meta.height * scale) / 2;
    const u = (clientX - left) / scale;
    const v = (clientY - top) / scale;
    if (u < 0 || v < 0 || u > this.meta.width || v > this.meta.height) return null;
    return applyAffine(this.meta.px_to_patrol, u, v);
  }

  click(event) {
    const point = this.toPatrol(event.clientX, event.clientY);
    if (!point) return;
    this.onPick(point[0], point[1], this.nearestZone(point));
  }

  nearestZone([x, y]) {
    let best = null;
    for (const zone of this.meta?.zones || []) {
      const d = Math.hypot(zone.x - x, zone.y - y);
      if (d < 1.2 && (!best || d < best.d)) best = { id: zone.id, d };
    }
    return best ? '구역 ' + best.id + ' 근처' : '';
  }

  draw() {
    if (this.error && !this.meta) {
      this.status.textContent = '지도를 받지 못했습니다 — ' + this.error;
      return;
    }
    this.status.textContent = (this.error ? '연결 끊김 — ' : '') + describeNav(this.nav);
    const meta = this.meta;
    if (!meta) return;
    const canvas = this.canvas;
    if (canvas.width !== meta.width) canvas.width = meta.width;
    if (canvas.height !== meta.height) canvas.height = meta.height;
    const ctx = canvas.getContext?.('2d');
    if (!ctx) return;
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    if (this.image) ctx.drawImage(this.image, 0, 0, canvas.width, canvas.height);
    const px = (x, y) => applyAffine(meta.patrol_to_px, x, y);
    const metre = 1 / meta.resolution_m;
    ctx.font = 'bold ' + Math.round(metre * 0.35) + 'px sans-serif';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    for (const zone of meta.zones) {
      const [u, v] = px(zone.x, zone.y);
      ctx.fillStyle = 'rgba(255,255,255,.85)';
      ctx.beginPath();
      ctx.arc(u, v, metre * 0.28, 0, Math.PI * 2);
      ctx.fill();
      ctx.strokeStyle = zone.color;
      ctx.lineWidth = 3;
      ctx.stroke();
      ctx.fillStyle = '#1f2933';
      ctx.fillText(zone.id, u, v);
    }
    const nav = this.nav;
    if (!nav || nav.available === false) return;
    if (nav.path?.length) {
      ctx.strokeStyle = 'rgba(37,99,235,.8)';
      ctx.lineWidth = 3;
      ctx.setLineDash([8, 6]);
      ctx.beginPath();
      const [sx, sy] = px(nav.pose[0], nav.pose[1]);
      ctx.moveTo(sx, sy);
      for (const [x, y] of nav.path) ctx.lineTo(...px(x, y));
      ctx.stroke();
      ctx.setLineDash([]);
    }
    if (nav.goal) {
      const [u, v] = px(nav.goal[0], nav.goal[1]);
      ctx.strokeStyle = '#2563eb';
      ctx.lineWidth = 3;
      ctx.beginPath();
      ctx.moveTo(u - 10, v - 10);
      ctx.lineTo(u + 10, v + 10);
      ctx.moveTo(u + 10, v - 10);
      ctx.lineTo(u - 10, v + 10);
      ctx.stroke();
    }
    if (Array.isArray(nav.sim_truth)) {
      // 시뮬 전용 — 가상 로봇의 진짜 자리(옅은 원). 실제 로봇에서는 서버가 보내지 않는다.
      const [u, v] = px(nav.sim_truth[0], nav.sim_truth[1]);
      ctx.strokeStyle = 'rgba(217,119,6,.9)';
      ctx.lineWidth = 2;
      ctx.beginPath();
      ctx.arc(u, v, metre * 0.18, 0, Math.PI * 2);
      ctx.stroke();
    }
    // 로봇 — 앞을 가리키는 삼각형. 위치를 못 믿으면 속이 빈 회색으로 그린다.
    const [x, y, yawDeg] = nav.pose;
    const yaw = (yawDeg * Math.PI) / 180;
    const tip = px(x + 0.3 * Math.cos(yaw), y + 0.3 * Math.sin(yaw));
    const left = px(x + 0.15 * Math.cos(yaw + 2.5), y + 0.15 * Math.sin(yaw + 2.5));
    const right = px(x + 0.15 * Math.cos(yaw - 2.5), y + 0.15 * Math.sin(yaw - 2.5));
    const trusted = !nav.stale && (nav.verified || nav.seeded);
    ctx.beginPath();
    ctx.moveTo(...tip);
    ctx.lineTo(...left);
    ctx.lineTo(...right);
    ctx.closePath();
    ctx.lineWidth = 3;
    ctx.strokeStyle = trusted ? '#0f766e' : '#6b7280';
    ctx.fillStyle = trusted ? 'rgba(15,118,110,.85)' : 'rgba(255,255,255,.6)';
    ctx.fill();
    ctx.stroke();
  }
}
