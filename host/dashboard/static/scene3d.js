// 외부 SIM은 준비 완료와 주기 상태를 postMessage로 알린다. iframe load만으로 성공을 판단하지 않는다.
export function sceneURL(value) {
  try {
    const url = new URL(value);
    if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password) return '';
    url.searchParams.set('embed', '1');
    return url.href;
  } catch { return ''; }
}

export class Scene3D {
  constructor({document, mount, controls, notice, onChange, now = Date.now, setInterval: every = globalThis.setInterval}) {
    Object.assign(this, {document, mount, controls, notice, onChange, now});
    this.url = ''; this.mode = '2d'; this.active = true;
    for (const mode of ['2d', '3d']) {
      const button = document.createElement('button');
      button.type = 'button'; button.dataset.mapView = mode; button.textContent = mode.toUpperCase();
      button.addEventListener('click', () => this.select(mode));
      controls.append(button);
    }
    this.message = event => {
      if (!this.frame || event.source !== this.frame.contentWindow || event.origin !== new URL(this.url).origin || event.data?.type !== 'house-sim-status') return;
      if (event.data.ready === true) {this.ready = true; this.lastSeen = this.now(); this.notice.textContent = ''; this.render();}
      else this.fallback();
    };
    document.defaultView.addEventListener('message', this.message);
    this.timer = every(() => {
      if (this.active && this.frame && this.now() - this.lastSeen > (this.ready ? 4500 : 12000)) this.fallback();
    }, 1000);
    this.timer?.unref?.();
    this.render();
  }
  setUrl(value) {
    const url = sceneURL(value);
    if (url === this.url) return;
    this.removeFrame(); this.url = url; this.ready = false; this.notice.textContent = '';
    this.mode = url ? '3d' : '2d';
    if (url && this.active) this.open();
    this.render();
  }
  open() {
    this.removeFrame(); this.ready = false; this.lastSeen = this.now();
    this.frame = this.document.createElement('iframe');
    this.frame.title = '집 3D SIM · 드래그 회전, 휠 확대';
    this.frame.src = this.url; this.frame.referrerPolicy = 'no-referrer';
    this.frame.addEventListener('error', () => this.fallback());
    this.mount.append(this.frame);
    this.notice.textContent = '3D 집 화면을 연결하고 있습니다.';
  }
  select(mode) {
    this.mode = mode === '3d' && this.url ? '3d' : '2d';
    if (this.mode === '3d' && !this.frame && this.active) this.open();
    this.render();
  }
  fallback() {
    this.removeFrame(); this.ready = false; this.mode = '2d';
    this.notice.textContent = '3D 집 화면에 연결할 수 없어 2D 지도로 전환했습니다. SIM 실행 후 3D를 눌러 다시 연결하세요.';
    this.render();
  }
  setActive(active) {
    if (active === this.active) return;
    this.active = active;
    if (!active) {this.removeFrame(); this.ready = false;}
    else if (this.url && this.mode === '3d') this.open();
    this.render();
  }
  get shown() { return this.active && this.mode === '3d' && this.ready; }
  render() {
    const stage = this.mount.closest('#stage');
    const entering = this.shown && !stage?.classList.contains('scene3d-active');
    stage?.classList.toggle('scene3d-active', this.shown);
    if (entering && !stage.querySelector('#camera-dock')?.classList.contains('collapsed')) stage.querySelector('#collapse-camera')?.click();
    this.controls.hidden = !this.url; this.mount.hidden = !this.shown;
    this.notice.hidden = !this.url || !this.active || !this.notice.textContent;
    for (const button of this.controls.children) button.setAttribute('aria-pressed', String(button.dataset.mapView === this.mode));
    this.onChange?.();
  }
  removeFrame() { this.frame?.remove(); this.frame = null; }
  dispose() {this.removeFrame(); globalThis.clearInterval(this.timer); this.document.defaultView.removeEventListener('message', this.message);}
}
