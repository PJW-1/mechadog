import * as THREE from 'three';

import {createMotion,gaitFeet,legPose} from './robot-motion.mjs';
const HOUSE_FRAME = 'floorplan_xy_m_z_up';
const VISIBLE_STATUSES = new Set(['valid', 'recorded_candidate', 'lost', 'stale']);

// Only the pose API can place a robot. A raw scan or missing registration never
// falls back to the house origin, and recorded stops never interpolate movement.
export function isDisplayablePose(state, sceneRevision) {
  const pose = state?.pose;
  return ['recorded', 'live', 'offline_test'].includes(state?.mode) &&
    (pose?.status !== 'recorded_candidate' || state.mode === 'recorded') &&
    state?.scene_revision === sceneRevision && pose?.visible === true &&
    pose.frame_id === HOUSE_FRAME && VISIBLE_STATUSES.has(pose.status) &&
    [pose.x_m, pose.y_m, pose.yaw_rad].every(Number.isFinite);
}

export function poseStatusAt(state, elapsedSinceResponseMs = 0) {
  const status = state?.pose?.status || 'unregistered';
  if (!['live','offline_test'].includes(state?.mode) || status !== 'valid') return status;
  const age = Math.max(state.freshness?.age_ms??Infinity,state.freshness?.source_age_ms??0);
  if (!Number.isFinite(age) || age < 0) return 'stale';
  return age + Math.max(0, elapsedSinceResponseMs) > (state.freshness?.stale_after_ms ?? 500) ? 'stale' : status;
}

export function matchesRecordedSelection(state, stopId, sceneRevision) {
  return state?.schema_version === 1 && state.mode === 'recorded' &&
    state.scene_revision === sceneRevision && state.pose?.stop_id === stopId;
}

export function initRobotLink({ world, buildPartMesh, request, getRevision, isDirty, focusRobot }) {
  const $ = (id) => document.getElementById(id);
  const group = new THREE.Group(); group.name = 'MechaDogPoseOverlay'; group.visible = false;
  const scanGroup = new THREE.Group(); scanGroup.name = 'RecordedLidarOverlay'; scanGroup.visible = false;
  world.add(group, scanGroup);
  const stageNote = document.createElement('div'); stageNote.className = 'robot-stage-note'; stageNote.hidden = true;
  const stageTitle = document.createElement('strong'), stageDetail = document.createElement('span');
  stageNote.append(stageTitle, stageDetail); $('render-pane').append(stageNote);
  let records = [], byId = new Map(), model, currentState, activeRecord, marker, direction;
  let pending = false, timer, clockTimer, pollInFlight = false, failureCount = 0, lastResponseAt = 0;
  let responseAgeMs = 0, modelLoaded = false, selectionVersion = 0;
  let selectionFailed = false;
  const skin = new Map();
  const motion=createMotion(),meshes=new Map();let renderedPose=null;

  function error(message) { $('robot-error').textContent = message || ''; $('robot-error').hidden = !message; }
  function selectedIndex() { return records.findIndex((row) => row.id === $('robot-stop').value); }
  function updateButtons() {
    const index = selectedIndex();
    $('robot-stop').disabled = pending || !records.length;
    $('robot-load').disabled = pending || index < 0 || !modelLoaded;
    $('robot-prev').disabled = pending || index <= 0 || !modelLoaded;
    $('robot-next').disabled = pending || index < 0 || index >= records.length - 1 || !modelLoaded;
    $('robot-focus').disabled = !group.visible;
    $('robot-eye').disabled = !group.visible;
  }
  function showPhoto(row) {
    $('robot-photo-figure').hidden = !row?.photo;
    $('robot-photo-empty').hidden = !row || !!row.photo;
    if (row?.photo) {
      $('robot-photo').src = row.photo;
      $('robot-photo').alt = `${row.id} · ${row.room} 측정 당시 사진`;
      $('robot-photo-link').href = row.photo;
      $('robot-photo-caption').textContent = `${row.id} · ${row.room} · 원본 사진 열기`;
    } else { $('robot-photo').removeAttribute('src'); $('robot-photo-link').removeAttribute('href'); }
  }
  function buildModel(asset) {
    if (!Array.isArray(asset.parts) || !asset.parts.length || !asset.materials) throw new Error('MechaDog 시각 모델 형식을 확인할 수 없습니다.');
    const materials = new Map();
    for (const [id, source] of Object.entries(asset.materials)) {
      materials.set(id, new THREE.MeshStandardMaterial({
        color: new THREE.Color().setRGB(...source.color, THREE.SRGBColorSpace),
        roughness: source.roughness ?? .65, metalness: source.metalness ?? 0,
        side: THREE.DoubleSide,
      }));
    }
    for (const part of asset.parts) {
      const material = materials.get(part.material);
      if (!material) throw new Error(`로봇 재질을 찾을 수 없습니다: ${part.material}`);
      const mesh = buildPartMesh(part, material);
      if(!skin.has(material))skin.set(material,material.color.clone());
      mesh.name = part.name; mesh.castShadow = false; mesh.receiveShadow = true; group.add(mesh);
      meshes.set(part.name,mesh);
    }
    // The ring is an annotation, not a collision or nominal robot footprint.
    const ring = new THREE.BufferGeometry().setFromPoints(Array.from({ length: 65 }, (_, i) => new THREE.Vector3(.22*Math.cos(i*Math.PI/32), .22*Math.sin(i*Math.PI/32), .008)));
    marker = new THREE.Line(ring, new THREE.LineDashedMaterial({ color: 0xe2b66a, dashSize: .04, gapSize: .024 }));
    marker.computeLineDistances(); group.add(marker);
    direction = new THREE.ArrowHelper(new THREE.Vector3(1, 0, 0), new THREE.Vector3(0, 0, .014), .37, 0xe2b66a, .075, .045); group.add(direction);
    model = asset; modelLoaded = true;
    const c=document.createElement('canvas');c.width=c.height=64;const ctx=c.getContext('2d'),gradient=ctx.createRadialGradient(32,32,4,32,32,30);gradient.addColorStop(0,'rgba(35,30,22,.3)');gradient.addColorStop(1,'rgba(35,30,22,0)');ctx.fillStyle=gradient;ctx.fillRect(0,0,64,64);
    const shadow=new THREE.Mesh(new THREE.PlaneGeometry(.29,.17),new THREE.MeshBasicMaterial({map:new THREE.CanvasTexture(c),transparent:true,depthWrite:false}));shadow.position.z=.012;shadow.renderOrder=1;group.add(shadow);
    $('robot-model-note').textContent = 'MechaDog-02 · 214 × 126 × 138 mm 기준 · LiDAR 높이 225 mm · 속도에 맞춘 트롯 보행. 몸체와 LiDAR의 XY 오프셋은 미확정입니다.';
  }
  function clearScan() {
    for (const child of [...scanGroup.children]) { scanGroup.remove(child); child.geometry?.dispose(); child.material?.dispose(); }
  }
  function buildScan(row, pose) {
    clearScan();
    if (!row || !Array.isArray(row.points)) return;
    const values = [], c = Math.cos(pose.yaw_rad), s = Math.sin(pose.yaw_rad);
    const height = model?.sensor_mount?.lidar_xyz_m?.[2] ?? .225;
    for (const point of row.points) {
      if (!Array.isArray(point) || !point.slice(0, 2).every(Number.isFinite)) continue;
      const [x, y] = point; values.push(c*x-s*y+pose.x_m, s*x+c*y+pose.y_m, height);
    }
    const geometry = new THREE.BufferGeometry(); geometry.setAttribute('position', new THREE.Float32BufferAttribute(values, 3));
    const material = new THREE.PointsMaterial({ color: 0xf3bd67, size: .024, sizeAttenuation: true, transparent: true, opacity: .88 });
    scanGroup.add(new THREE.Points(geometry, material));
  }
  function currentStatus() {
    // Recorded data keeps its acquisition time; it is never called a live fix.
    return poseStatusAt(currentState, performance.now() - lastResponseAt);
  }
  function render() {
    const pose = currentState?.pose, status = currentStatus();
    const visible = !selectionFailed && !failureCount && modelLoaded && !isDirty() && isDisplayablePose(currentState, getRevision()) && VISIBLE_STATUSES.has(status);
    group.visible = visible;
    const ghost=['lost','stale'].includes(status);for(const [mat,color] of skin){mat.color.copy(ghost?new THREE.Color('#9fa7b0'):color);mat.transparent=ghost;mat.opacity=ghost?.35:1;mat.depthWrite=!ghost;}
    group.userData.ghost=ghost;
    scanGroup.visible = visible && currentState?.mode === 'recorded' && $('robot-points').checked;
    if (visible) {
      const candidate = pose.registration_status !== 'validated';
      marker.material.color.setHex(candidate ? 0xe2b66a : 0x81d4bf);
      marker.material.dashSize = candidate ? .04 : 10;
      marker.material.gapSize = candidate ? .024 : 0;
      direction.setColor(candidate ? 0xe2b66a : 0x81d4bf);
    }
    const recorded = currentState?.mode === 'recorded';
    const labels = { unregistered: '집 좌표 미확정 · 모델 숨김', recorded_candidate: '기록 재생 · 정합 후보', valid: recorded ? '검증된 기록 위치' : '실시간 위치 수신', stale: '위치 갱신 지연 · 마지막 정상 위치', lost: '위치 추적 상실 · 마지막 정상 위치' };
    let label = currentState ? labels[status] || '위치 정보 확인 대기' : '정지 기록을 선택하세요';
    if(currentState?.demo) label='시연 자세 · 실물 로봇 없음';
    if (selectionFailed) label = '정지 선택 실패 · 모델 숨김';
    else if (failureCount) label = '위치 API 연결 대기 · 모델 숨김';
    else if (isDirty()) label = '가구 수정 중 · 로봇 위치 대조 보류';
    else if (currentState?.scene_revision && currentState.scene_revision !== getRevision()) label = '장면 버전 불일치 · 모델 숨김';
    $('robot-state-label').textContent = label;
    const coordinate = visible ? `X ${pose.x_m.toFixed(3)} · Y ${pose.y_m.toFixed(3)} m\n방향 ${THREE.MathUtils.radToDeg(pose.yaw_rad).toFixed(1)}°` : '표시 가능한 집 좌표 없음';
    $('robot-coordinate').textContent = coordinate;
    $('robot-timestamp').textContent = activeRecord?.time?.replace('T', ' ') || (currentState?.source?.source_timestamp_ms ? `${currentState.source.source_timestamp_ms} ms (기기 시계)` : '—');
    const apiAge = lastResponseAt ? Math.round((performance.now() - lastResponseAt) / 1000) : null;
    $('robot-freshness').textContent = recorded ? `저장 기록 · API ${apiAge ?? '—'}초 전 확인` : currentState?.mode === 'live' ? `${status} · 데이터 나이 ${Math.round(responseAgeMs + performance.now() - lastResponseAt)} ms` : '실시간 연결 없음';
    const registration = pose?.registration_status || 'missing';
    $('robot-registration').textContent = recorded && status === 'unregistered'
      ? '이 정지는 집 좌표를 확정하지 못했습니다. 측정 사진은 확인할 수 있으며 로봇과 점군은 숨깁니다.'
      : recorded
      ? registration === 'validated' ? '검증된 정지 기록입니다. 실시간 로봇 위치가 아닙니다.'
        : '집 안의 위치·방향은 정합 후보입니다. 라이다 지상 원점으로 표시하며, 몸체 위치는 임시입니다.'
      : visible ? '센서 좌표와 장면 좌표의 정합 상태를 확인하며 표시합니다.'
        : '집 좌표가 없거나 최신 위치를 확인할 수 없어 로봇과 점군을 숨겼습니다.';
    if (isDirty()) $('robot-registration').textContent = '수정 중인 가구와 저장된 위치 근거가 달라졌습니다. 가구 편집은 보존되며 위치 대조는 저장 후 다시 선택하세요.';
    $('robot-frame').textContent = `${pose?.frame_id || HOUSE_FRAME} · ${pose?.anchor_frame || '미확정'} 원점 · m · yaw rad\n출처: ${pose?.source_kind || '미선택'} · 정합: ${registration}`;
    stageNote.hidden = !selectionFailed && (!currentState || currentState.mode === 'none');
    stageNote.dataset.status = status;
    stageTitle.textContent = label;
    stageDetail.textContent = visible ? `${pose.stop_id || 'MechaDog'} · ${activeRecord?.points?.length || 0}개 실측점 · 실물 명령 없음` : '유효한 집 좌표에서만 표시합니다.';
    updateButtons();
  }
  function applyState(value, roundTripMs = 0) {
    if (!value || value.schema_version !== 1 || !value.pose) throw new Error('로봇 위치 응답 형식을 확인할 수 없습니다.');
    const selectionChanged = pending || value.pose.stop_id !== currentState?.pose?.stop_id;
    // Include the full request duration conservatively: a delayed HTTP response
    // cannot make an old live packet look newly acquired in this browser.
    if (['live','offline_test'].includes(value.mode) && Number.isFinite(value.freshness?.age_ms)) {
      value = { ...value, freshness: { ...value.freshness, age_ms: value.freshness.age_ms + Math.max(0, roundTripMs) } };
    }
    currentState = value; lastResponseAt = performance.now(); responseAgeMs = Math.max(0, Number(value.freshness?.age_ms) || 0);
    if(isDisplayablePose(value,getRevision()))motion.ingest(value,lastResponseAt);
    activeRecord = byId.get(value.pose.stop_id);
    if (activeRecord && selectionChanged) { $('robot-stop').value = activeRecord.id; showPhoto(activeRecord); }
    if (isDisplayablePose(value, getRevision()) && value.mode === 'recorded') buildScan(activeRecord, value.pose);
    else clearScan();
    render();
  }
  async function loadSelected() {
    const row = byId.get($('robot-stop').value);
    if (!row || pending) return;
    if (isDirty()) { error('가구 편집을 저장하거나 취소한 뒤 정지 위치를 선택하세요.'); return; }
    pending = true; selectionFailed = false; selectionVersion++; error(''); updateButtons();
    try {
      const startedAt = performance.now();
      const result = await request('/api/robot/pose', { schema_version: 1, mode: 'recorded', scene_revision: getRevision(), stop_id: row.id });
      if (!matchesRecordedSelection(result.state, row.id, getRevision())) throw new Error('응답의 측정 정지 또는 장면 버전이 선택과 다릅니다.');
      failureCount = 0; applyState(result.state, performance.now() - startedAt);
      if (group.visible && $('robot-follow').checked) focusRobot(currentState.pose, model.sensor_mount);
    } catch (cause) {
      // A later timer or GET must not restore the previous stop beneath a failed
      // new selection. A successful explicit selection releases this latch.
      selectionFailed = true; currentState = null; activeRecord = undefined;
      clearScan(); group.visible = false; scanGroup.visible = false; showPhoto(row);
      error(`위치를 불러오지 못했습니다. 다시 선택하세요. ${cause.message}`); render();
    }
    finally { pending = false; updateButtons(); }
  }
  async function poll() {
    clearTimeout(timer);
    if (document.hidden || pollInFlight || pending) { schedule(); return; }
    pollInFlight = true; const version = selectionVersion;
    try {
      const startedAt = performance.now();
      const value = await request('/api/robot/state');
      failureCount = 0;
      if (version === selectionVersion && !pending && !selectionFailed) applyState(value, performance.now() - startedAt);
    } catch {
      failureCount++;
      group.visible = false; scanGroup.visible = false;
      $('robot-state-label').textContent = '위치 API 연결 대기 · 모델 숨김';
      $('robot-freshness').textContent = '서버 응답 없음';
      stageNote.hidden = false; stageTitle.textContent = '위치 API 연결 대기'; stageDetail.textContent = '로봇 모델과 실측 점 표시를 중단했습니다.';
      updateButtons();
    } finally { pollInFlight = false; schedule(); }
  }
  function schedule() {
    clearTimeout(timer);
    if (!document.hidden) timer = setTimeout(poll, failureCount ? Math.min(30000, 1000*2**failureCount) : ['live','offline_test'].includes(currentState?.mode) ? 100 : 2500);
  }
  $('open-robot').addEventListener('click', () => { $('robot-panel').scrollIntoView({ block: 'start' }); $('robot-panel').focus({ preventScroll: true }); });
  $('robot-load').addEventListener('click', loadSelected);
  $('robot-stop').addEventListener('change', updateButtons);
  for (const [id, delta] of [['robot-prev', -1], ['robot-next', 1]]) $(id).addEventListener('click', () => {
    const row = records[selectedIndex() + delta]; if (row) { $('robot-stop').value = row.id; loadSelected(); }
  });
  $('robot-focus').addEventListener('click', () => { if (group.visible) focusRobot(currentState.pose, model.sensor_mount); });
  $('robot-eye').addEventListener('click', () => { if (group.visible) focusRobot(currentState.pose, model.sensor_mount, true); });
  $('robot-points').addEventListener('change', render);
  document.addEventListener('visibilitychange', () => {
    clearTimeout(timer); clearInterval(clockTimer);
    if (!document.hidden) { poll(); clockTimer = setInterval(() => { if (!failureCount) render(); }, 250); }
    else { group.visible = false; scanGroup.visible = false; }
  });
  window.addEventListener('pagehide', () => { clearTimeout(timer); clearInterval(clockTimer); });

  async function init() {
    const results = await Promise.allSettled([
      request('/assets/robot/mechdog02_visual.json'),
      request('/audit/lidar20261001/observations.json'),
    ]);
    const [assetResult, recordsResult] = results;
    if (assetResult.status === 'fulfilled') {
      try { buildModel(assetResult.value); } catch (cause) { error(cause.message); }
    } else error(`로봇 시각 모델을 불러오지 못했습니다. ${assetResult.reason.message}`);
    if (recordsResult.status === 'fulfilled' && Array.isArray(recordsResult.value.records)) {
      records = recordsResult.value.records; byId = new Map(records.map((row) => [row.id, row]));
      $('robot-stop').replaceChildren(new Option(`${records.length}개 정지 중 선택`, ''));
      for (const row of records) $('robot-stop').append(new Option(`${row.id} · ${row.room}${row.photo ? '' : ' · 사진 없음'}`, row.id));
    } else { $('robot-stop').replaceChildren(new Option('기록을 읽지 못했습니다', '')); error('측정 정지 목록을 읽지 못했습니다. 새로고침하여 다시 확인하세요.'); }
    updateButtons(); poll(); clockTimer = setInterval(() => { if (!failureCount) render(); }, 250);
  }
  init();
  function update(time){
    const active=group.visible&&currentStatus()==='valid'&&currentState?.mode!=='recorded';
    const pose=motion.update(time,active);if(!pose||!modelLoaded)return;
    renderedPose={...currentState?.pose,...pose};
    group.position.set(pose.x_m,pose.y_m,0);group.rotation.z=pose.yaw_rad;
    const bob=Math.sin(pose.phase*2)*.0015*pose.blend,roll=Math.sin(pose.phase)*.012*pose.blend;
    for(const [name,mesh]of meshes){mesh.position.set(0,0,0);mesh.rotation.set(0,0,0);if(!/_(upper|lower|foot)$/.test(name)){mesh.position.z=bob;mesh.rotation.x=roll;}}
    for(const foot of gaitFeet(pose)){
      const ik=legPose(foot,bob);
      const transform=(mesh,pivot,target,theta)=>{mesh.rotation.y=-theta;const c=Math.cos(theta),s=Math.sin(theta);mesh.position.set(target[0]-c*pivot[0]+s*pivot[1],0,target[1]-s*pivot[0]-c*pivot[1]);};
      transform(meshes.get(foot.id+'_upper'),ik.restHip,ik.hip,ik.upperAngle);
      transform(meshes.get(foot.id+'_lower'),ik.restKnee,ik.knee,ik.lowerAngle);
      const mesh=meshes.get(foot.id+'_foot');mesh.position.set(foot.x-ik.restFoot[0],0,foot.z-ik.restFoot[1]);
    }
  }
  return { refresh: poll, update, getState: () => currentState, getDisplayPose:()=>group.visible?renderedPose:null, getModel:()=>model };
}

