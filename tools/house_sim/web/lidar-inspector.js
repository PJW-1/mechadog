// Recorded evidence only. This module has no command, device, or save endpoint.
export function initLidarInspector({ selectObject, getRevision, isDirty }) {
  const $ = (id) => document.getElementById(id);
  const panel = $('lidar-panel');
  let data, index = 9, floorMode = false, groupMode = false;
  let requestPending = false;
  const canvas = $('lidar-plot');
  const ctx = canvas.getContext('2d');

  function setUnderlyingInert(value) {
    for (const selector of ['.view-toolbar', '#remote-change', '#stage', '.scene-footer']) {
      document.querySelector(selector).inert = value;
    }
  }
  function close() { panel.hidden = true; setUnderlyingInert(false); $('open-lidar').setAttribute('aria-expanded', 'false'); $('open-lidar').focus(); }
  function excluded(x, y) {
    return data.excluded_regions.some((r) => {
      const [x0, y0, x1, y1] = r.bounds_xy_m;
      return x >= x0 && x <= x1 && y >= y0 && y <= y1;
    });
  }
  function draw() {
    if (!data || panel.hidden) return;
    const row = data.records[index];
    const rect = canvas.getBoundingClientRect();
    const ratio = Math.min(devicePixelRatio || 1, 2);
    canvas.width = Math.max(1, Math.round(rect.width * ratio));
    canvas.height = Math.max(1, Math.round(rect.height * ratio));
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    const w = rect.width, h = rect.height;
    ctx.fillStyle = '#142226'; ctx.fillRect(0, 0, w, h);
    const pose = floorMode ? row.floor_candidate : groupMode ? row.local_pose : [0, 0, 0];
    const c = Math.cos(pose[2]), s = Math.sin(pose[2]);
    const measured = row.points.map(([x, y]) => [c * x - s * y + pose[0], s * x + c * y + pose[1]]);
    const otherPoints = [];
    if (groupMode) for (const id of row.local_members) {
      if (id === row.id) continue;
      const other = data.records.find((r) => r.id === id), p = other.local_pose;
      const cos = Math.cos(p[2]), sin = Math.sin(p[2]);
      for (const [x, y] of other.points) otherPoints.push([cos*x-sin*y+p[0], sin*x+cos*y+p[1]]);
    }
    const bounds = floorMode ? [-.5, -.5, 11.5, 7.8] : [-5, -5, 5, 5];
    if (!floorMode) {
      const extent = Math.max(1, ...measured.flat().map(Math.abs), ...otherPoints.flat().map(Math.abs)) + .4;
      bounds.splice(0, 4, -extent, -extent, extent, extent);
    }
    const scale = Math.min((w - 42) / (bounds[2] - bounds[0]), (h - 42) / (bounds[3] - bounds[1]));
    const project = ([x, y]) => [w / 2 + (x - (bounds[0] + bounds[2]) / 2) * scale,
      h / 2 - (y - (bounds[1] + bounds[3]) / 2) * scale];
    function line(a, b) { const p = project(a), q = project(b); ctx.moveTo(...p); ctx.lineTo(...q); }
    ctx.lineWidth = 1; ctx.strokeStyle = '#263b40'; ctx.beginPath();
    for (let x = Math.ceil(bounds[0]); x <= bounds[2]; x++) line([x, bounds[1]], [x, bounds[3]]);
    for (let y = Math.ceil(bounds[1]); y <= bounds[3]; y++) line([bounds[0], y], [bounds[2], y]);
    ctx.stroke();
    if (floorMode) {
      ctx.strokeStyle = '#c3ceca'; ctx.lineWidth = 1.5; ctx.beginPath();
      for (const obj of data.sections) for (const [a, b] of obj.segments) line(a, b);
      ctx.stroke();
    }
    let hidden = 0;
    ctx.fillStyle = '#9b88bf';
    for (const point of otherPoints) { const [x,y] = project(point); ctx.beginPath(); ctx.arc(x,y,1.6,0,2*Math.PI); ctx.fill(); }
    ctx.fillStyle = '#81d4bf';
    for (const point of measured) {
      if (floorMode && excluded(...point)) { hidden++; continue; }
      const [x, y] = project(point); ctx.beginPath(); ctx.arc(x, y, 2.1, 0, 2 * Math.PI); ctx.fill();
    }
    const origin = project(pose);
    ctx.strokeStyle = '#f2c378'; ctx.lineWidth = 2; ctx.beginPath();
    ctx.arc(...origin, 5, 0, 2 * Math.PI); ctx.stroke();
    ctx.beginPath(); line(pose, [pose[0] + .45 * c, pose[1] + .45 * s]); ctx.stroke();
    ctx.fillStyle = '#b9cdca'; ctx.font = '12px "Segoe UI", "Malgun Gothic", sans-serif';
    ctx.fillText('격자 1 m', 14, h - 14);
    $('lidar-plot-note').textContent = floorMode
      ? `흰 선: 저장 SIM 충돌 형상의 22.5 cm 단면 · 민트: 관측. 도면 앵커의 임시 배치이며 전역 정합 미완료.${hidden ? ` 제외 범위 ${hidden}점은 이 화면에서만 숨김.` : ''}`
      : groupMode ? `${row.reprocessed_component} · ${row.local_members.length}정지. 민트: 선택 관측 · 보라: 재정합한 이웃 관측. 묶음별 독립 좌표이며 집 안의 절대 위치는 미확정입니다.`
        : '센서가 원점, 오른쪽이 로봇 전방(X), 위쪽이 왼쪽(Y). 모든 저장 거리점을 그대로 표시합니다.';
  }

  function render() {
    const row = data.records[index];
    $('lidar-stop').value = String(index);
    $('lidar-prev').disabled = index === 0;
    $('lidar-next').disabled = index === data.records.length - 1;
    const current = data.scene_revision === getRevision() && !isDirty();
    $('lidar-floor').disabled = !row.floor_candidate || !current;
    $('lidar-group').disabled = !row.local_members || row.local_members.length < 2;
    if ($('lidar-group').disabled) groupMode = false;
    if (!$('lidar-floor').disabled && floorMode) { /* preserve explicitly selected mode */ }
    else if ($('lidar-floor').disabled) floorMode = false;
    $('lidar-local').setAttribute('aria-pressed', String(!floorMode && !groupMode));
    $('lidar-floor').setAttribute('aria-pressed', String(floorMode));
    $('lidar-group').setAttribute('aria-pressed', String(groupMode));
    $('lidar-context').textContent = `${row.room} · ${row.points.length}점 · ${row.time.replace('T', ' ')} · ${index + 1}/${data.records.length}`;
    $('lidar-observation').textContent = row.observation;
    $('lidar-limits').textContent = row.limits;
    $('lidar-registration').textContent = groupMode
      ? `${row.reprocessed_component}: 다른 정지 관측으로 상대 연결을 교차 검토한 묶음입니다. 전체 집 좌표와의 연결은 미확정입니다.`
      : !current
      ? '장면이 수정되어 단면 대조를 잠갔습니다. 원본 스캔·사진은 계속 볼 수 있습니다.'
      : row.floor_candidate ? `${row.component}: 도면 앵커가 있는 상대 묶음. 위치는 검증 후보입니다.`
        : `${row.component}: 집 안의 위치가 확정되지 않은 관측. 센서 기준으로 확인합니다.`;
    const image = $('lidar-photo');
    image.hidden = !row.photo;
    $('lidar-photo-empty').hidden = !!row.photo;
    if (row.photo) { image.src = row.photo; image.alt = `${row.id} 측정 당시 카메라 사진`; }
    else image.removeAttribute('src');
    $('lidar-photo-link').hidden = !row.photo;
    if (row.photo) $('lidar-photo-link').href = row.photo;
    const targets = $('lidar-targets'); targets.replaceChildren();
    for (const id of row.targets) {
      if (!data.editable_ids.includes(id)) continue;
      const button = document.createElement('button');
      button.textContent = data.labels?.[id] || id;
      button.addEventListener('click', () => { close(); selectObject(id); });
      targets.append(button);
    }
    draw();
  }

  async function open() {
    panel.hidden = false;
    setUnderlyingInert(true);
    $('open-lidar').setAttribute('aria-expanded', 'true');
    $('close-lidar').focus();
    if (data) { render(); return; }
    if (requestPending) return;
    requestPending = true;
    try {
      const response = await fetch('/audit/lidar20261001/observations.json', { cache: 'no-store' });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      data = await response.json();
      if (!Array.isArray(data.records) || !data.records.length) throw new Error('관측 목록 없음');
      const select = $('lidar-stop'); select.replaceChildren();
      data.records.forEach((row, i) => { const option = document.createElement('option'); option.value = i; option.textContent = `${row.id} · ${row.room}`; select.append(option); });
      $('lidar-summary').textContent = `10월 1일 저장 관측 · ${data.records.length}개 정지 · 사진 ${data.photo_count}장`;
      $('lidar-body').hidden = false;
      $('lidar-error').hidden = true;
      render();
    } catch (error) {
      data = null;
      $('lidar-error').hidden = false;
      $('lidar-error').textContent = `실측 자료를 열지 못했습니다 (${error.message}). 닫은 뒤 다시 열어 주세요.`;
    } finally { requestPending = false; }
  }
  $('open-lidar').addEventListener('click', open);
  $('close-lidar').addEventListener('click', close);
  $('lidar-stop').addEventListener('change', (e) => { index = Number(e.target.value); render(); });
  $('lidar-prev').addEventListener('click', () => { index = Math.max(0, index - 1); render(); });
  $('lidar-next').addEventListener('click', () => { index = Math.min(data.records.length - 1, index + 1); render(); });
  $('lidar-local').addEventListener('click', () => { floorMode = false; groupMode = false; render(); });
  $('lidar-floor').addEventListener('click', () => { floorMode = true; groupMode = false; render(); });
  $('lidar-group').addEventListener('click', () => { floorMode = false; groupMode = true; render(); });
  panel.addEventListener('keydown', (e) => { if (e.key === 'Escape') close(); });
  new ResizeObserver(draw).observe(canvas);
}
