export const embedded = new URLSearchParams(location.search).get('embed') === '1';
if (embedded) document.body.classList.add('embed');
let timer;
const report = ready => {if (embedded && window.parent !== window) window.parent.postMessage({type:'house-sim-status', ready}, '*');};
export function reportEmbedFailure() { report(false); }
export function startEmbed() {
  const canvas = document.getElementById('scene-canvas');
  canvas.setAttribute('aria-label', '집 3D SIM · 드래그 회전, 휠 확대 · 관측 전용');
  document.getElementById('interaction-hint').textContent = '드래그 회전 · 휠 확대 · 관측 전용';
  let lost = false;
  canvas.addEventListener('webglcontextlost', () => {lost = true; report(false);});
  async function heartbeat() {
    try {
      const r = await fetch('/api/status', {cache:'no-store', signal:AbortSignal.timeout(1500)});
      const status = r.ok ? await r.json() : null;
      report(!lost && status?.service_id === 'house-sim-unified');
    } catch { report(false); }
    timer = setTimeout(heartbeat, 1000);
  }
  heartbeat();
  window.addEventListener('pagehide', () => clearTimeout(timer), {once:true});
}
