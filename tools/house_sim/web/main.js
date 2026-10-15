import { initPresentation } from './presentation.js';
import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { initLidarInspector } from './lidar-inspector.js';
import { initRobotLink } from './robot-link.js';
import {embedded, startEmbed, reportEmbedFailure} from './embed.js';

const element = (id) => document.getElementById(id);
const clone = (value) => JSON.parse(JSON.stringify(value));
const editableFields = (object) => ({ id: object.id, position: [...object.position], yaw: object.yaw || 0, visible: object.visible !== false });
const state = { saved: null, draft: null, baseline: null, selected: null, history: [], busy: false, moving: false, view: 'overview', cameraId: null, comparing: false, inputSnapshot: null, manualView: false };
const positionLimits = [[-2, 13], [-2, 10], [-0.2, 4]];
const groups = new Map();
const materialCache = new Map();
const textureLoader = new THREE.TextureLoader();
const stage = element('stage');
const canvas = element('scene-canvas');
const renderPane = element('render-pane');
const clippingPlane = new THREE.Plane(new THREE.Vector3(0, 0, -1), 1.45);
let presentation,robotLink;
let world, renderer, camera, controls, selectionBox, sceneBounds, feedbackTimer;
let statusTimer, statusInFlight = false, statusFailures = 0, lastConflictRevision = null;
let orbitInteractionStart = null;

function showFeedback(message, error = false, persistent = false) {
  clearTimeout(feedbackTimer);
  const feedback = element('feedback');
  feedback.textContent = message;
  feedback.classList.toggle('error', error);
  feedback.hidden = false;
  if (!persistent) feedbackTimer = setTimeout(() => { feedback.hidden = true; }, error ? 12000 : 6500);
}

async function request(path, body) {
  const response = await fetch(path, body === undefined ? { cache: 'no-store' } : {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  });
  let result;
  try { result = await response.json(); }
  catch { throw new Error(`서버 응답을 읽을 수 없습니다 (${response.status}).`); }
  if (!response.ok) {
    const error = new Error(result.error || result.message || `요청 실패 (${response.status})`);
    error.status = response.status;
    throw error;
  }
  return result;
}

function transforms(scene = state.draft) { return scene.objects.map(editableFields); }
function isDirty() { return !!state.saved && JSON.stringify(transforms()) !== JSON.stringify(transforms(state.saved)); }
function selectedObject() { return state.draft?.objects.find((object) => object.id === state.selected && object.editable); }
function selectedCamera() { return state.draft?.cameras.find((item) => item.id === state.cameraId); }
function positionAllowed(position) {
  return position.length === 3 && position.every((value, axis) => Number.isFinite(value) && value >= positionLimits[axis][0] && value <= positionLimits[axis][1]);
}
function assetUrl(path) { return '/' + path.replace(/^\/+/, ''); }
function roomLabel(room) {
  const labels = { living: '거실', living_room: '거실', bedroom: '침실', room2: '침실', office: '작업방', study: '작업방', room3: '작업방', kitchen: '주방', hall: '복도', corridor: '복도', passage: '방 앞 통로', interior: '실내' };
  return labels[room] || room || '실내';
}
function hasCeilingTag(object) { return object.tags?.includes('ceiling') || object.role === 'ceiling'; }

function createRenderer() {
  THREE.Object3D.DEFAULT_UP.set(0, 0, 1);
  renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: false, preserveDrawingBuffer: false });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 1.5));
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.toneMappingExposure = 1.0;
  renderer.shadowMap.enabled = true;
  renderer.shadowMap.type = THREE.PCFShadowMap;
  renderer.shadowMap.autoUpdate = false;
  renderer.shadowMap.needsUpdate = true;
  renderer.localClippingEnabled = true;
  world = new THREE.Scene();
  world.background = new THREE.Color('#f5f8fc');
  camera = new THREE.PerspectiveCamera(48, 1, 0.02, 100);
  controls = new OrbitControls(camera, canvas);
  controls.enableDamping = true;
  controls.dampingFactor = 0.09;
  controls.minDistance = 0.08;
  controls.maxDistance = 35;
  controls.maxPolarAngle = Math.PI * 0.97;
  controls.addEventListener('start', () => {
    if (state.comparing) return;
    orbitInteractionStart = { position: camera.position.clone(), quaternion: camera.quaternion.clone() };
  });
  controls.addEventListener('change', () => {
    if (orbitInteractionStart && (camera.position.distanceToSquared(orbitInteractionStart.position) > 1e-10 || 1 - Math.abs(camera.quaternion.dot(orbitInteractionStart.quaternion)) > 1e-10)) {
      state.manualView = true;
      element('view-label').textContent = '사용자 시점';
    }
  });
  controls.addEventListener('end', () => { orbitInteractionStart = null; });
  const ambient = new THREE.HemisphereLight(0xe8f2f6, 0xb8b09e, 1.3);
  ambient.position.set(0, 0, 10);
  world.add(ambient);
  world.add(new THREE.AmbientLight(0xffffff, 0.45));
  renderer.setAnimationLoop((time) => { robotLink?.update(time); presentation?.update(time); if (!state.comparing) controls.update(); renderer.render(world, camera); });
}

function materialFor(id, structure) {
  const key = `${id}:${structure ? 'structure' : 'object'}`;
  if (materialCache.has(key)) return materialCache.get(key);
  const source = state.draft.materials[id];
  if (!source) throw new Error(`재질을 찾을 수 없습니다: ${id}`);
  const color = new THREE.Color().setRGB(...(source.color || [1, 1, 1]), THREE.SRGBColorSpace);
  const opacity = source.opacity ?? 1;
  const material = new THREE.MeshStandardMaterial({ color, roughness: source.roughness ?? 0.65, metalness: source.metalness ?? 0, opacity, transparent: opacity < 1, side: THREE.DoubleSide, depthWrite: opacity >= 1 });
  if (source.emissive) material.emissive.setRGB(...source.emissive);
  if (source.texture) {
    material.map = textureLoader.load(assetUrl(source.texture), () => {}, undefined, () => {
      showFeedback(`질감 파일을 읽지 못했습니다: ${source.texture}`, true, true);
    });
    material.map.colorSpace = THREE.SRGBColorSpace;
    material.map.wrapS = material.map.wrapT = THREE.RepeatWrapping;
    material.map.repeat.set(...(source.repeat || [1, 1]));
    material.map.anisotropy = Math.min(renderer.capabilities.getMaxAnisotropy(), 8);
  }
  material.userData.clipOverview = structure;
  materialCache.set(key, material);
  return material;
}

function mergedParts(parts) {
 const batches=new Map();
 for(const part of parts){let b=batches.get(part.material);if(!b){b={name:part.material,material:part.material,vertices:[],triangles:[],normals:[],uvs:[]};batches.set(part.material,b);}const offset=b.vertices.length;b.vertices.push(...part.vertices);b.triangles.push(...part.triangles.map(t=>t.map(v=>v+offset)));if(part.normals)b.normals.push(...part.normals);if(part.uvs)b.uvs.push(...part.uvs);}
 return [...batches.values()];
}

function buildScene() {
  for (const object of state.draft.objects) {
    const group = new THREE.Group();
    group.name = object.id;
    group.userData.objectId = object.id;
    group.position.fromArray(object.position);
    group.rotation.z = THREE.MathUtils.degToRad(object.yaw || 0);
    group.visible = object.visible !== false;
    const high = new THREE.Group();
    for (const part of mergedParts(object.parts)) {
      const material = materialFor(part.material, object.category === 'structure');
      const mesh = buildPartMesh(part, material);
      mesh.name = part.name || object.label;
      mesh.userData.objectId = object.id;
      mesh.castShadow = material.opacity >= 1;
      mesh.receiveShadow = true;
      high.add(mesh);
    }
    // The canonical mesh has actual open leg gaps. A decorative LOD can fill
    // those gaps, so retain one merged canonical mesh per material in all views.
    group.add(high);
    groups.set(object.id, group);
    world.add(group);
  }
  world.updateMatrixWorld(true);
  sceneBounds = new THREE.Box3();
  for (const group of groups.values()) if (group.visible) sceneBounds.union(new THREE.Box3().setFromObject(group));
  if (sceneBounds.isEmpty()) throw new Error('표시할 장면 형상이 없습니다.');
  const center = sceneBounds.getCenter(new THREE.Vector3());
  const size = sceneBounds.getSize(new THREE.Vector3());
  const keyLight = new THREE.DirectionalLight(0xfff6e8, 2.0);
  keyLight.position.copy(center).add(new THREE.Vector3(-5, -6, 10));
  keyLight.target.position.copy(center);
  keyLight.castShadow = true;
  keyLight.shadow.mapSize.set(1024, 1024);
  keyLight.shadow.camera.left = -Math.max(size.x, size.y);
  keyLight.shadow.camera.right = Math.max(size.x, size.y);
  keyLight.shadow.camera.top = Math.max(size.x, size.y);
  keyLight.shadow.camera.bottom = -Math.max(size.x, size.y);
  keyLight.shadow.camera.near = 0.1;
  keyLight.shadow.camera.far = 40;
  keyLight.shadow.bias = -0.00008;
  keyLight.shadow.normalBias = 0.015;
  keyLight.shadow.radius = 2;
  world.add(keyLight, keyLight.target);
  const fillLight = new THREE.DirectionalLight(0xe1ebf2, 1.1);
  fillLight.position.copy(center).add(new THREE.Vector3(5, 2, 5));
  fillLight.target.position.copy(center);
  world.add(fillLight, fillLight.target);
}

// Furniture and the robot's exported visual asset share this canonical mesh format.
function buildPartMesh(part, material) {
  const geometry = new THREE.BufferGeometry();
  geometry.setAttribute('position', new THREE.Float32BufferAttribute(part.vertices.flat(), 3));
  geometry.setIndex(part.triangles.flat());
  if (part.normals?.length === part.vertices.length) geometry.setAttribute('normal', new THREE.Float32BufferAttribute(part.normals.flat(), 3));
  else geometry.computeVertexNormals();
  if (part.uvs?.length === part.vertices.length) geometry.setAttribute('uv', new THREE.Float32BufferAttribute(part.uvs.flat(), 2));
  geometry.computeBoundingSphere();
  return new THREE.Mesh(geometry, material);
}

function robotInspectionPosition(origin, forward, left, target) {
  // Floor meshes are already in world coordinates, including reviewed room-size
  // corrections. Only the observation camera is constrained; pose is untouched.
  world.updateMatrixWorld(true);
  const floors = state.draft.objects.filter((object) => object.tags?.includes('floor') && object.visible !== false)
    .map((object) => ({ object, bounds: new THREE.Box3().setFromObject(groups.get(object.id)) }))
    .filter(({ bounds }) => origin.x >= bounds.min.x && origin.x <= bounds.max.x && origin.y >= bounds.min.y && origin.y <= bounds.max.y)
    .sort((a, b) => (a.bounds.max.x-a.bounds.min.x)*(a.bounds.max.y-a.bounds.min.y) - (b.bounds.max.x-b.bounds.min.x)*(b.bounds.max.y-b.bounds.min.y));
  const floor = floors[0]?.bounds;
  if (!floor) return origin.clone().add(new THREE.Vector3(0, -.001, 1.3));
  const marginX = Math.min(.3, (floor.max.x-floor.min.x)*.3);
  const marginY = Math.min(.3, (floor.max.y-floor.min.y)*.3);
  const constrain = (point) => {
    point.x = THREE.MathUtils.clamp(point.x, floor.min.x+marginX, floor.max.x-marginX);
    point.y = THREE.MathUtils.clamp(point.y, floor.min.y+marginY, floor.max.y-marginY);
    return point;
  };
  const desired = constrain(origin.clone().addScaledVector(forward, -.85).addScaledVector(left, -.55));
  desired.z = .85;
  const candidates = [desired];
  const bearing = Math.atan2(desired.y-origin.y, desired.x-origin.x);
  for (const height of [.7, 1.05, .38]) {
    for (const turn of [0, 1, -1, 2, -2, 3, -3, 4]) {
      const angle = bearing + turn * Math.PI/4;
      const radius = height <= .7 ? .6 : 1;
      candidates.push(constrain(new THREE.Vector3(origin.x+radius*Math.cos(angle), origin.y+radius*Math.sin(angle), height)));
    }
  }
  const obstacles = state.draft.objects.filter((object) => groups.get(object.id)?.visible && !hasCeilingTag(object))
    .map((object) => groups.get(object.id));
  const samples = [new THREE.Vector3(origin.x, origin.y, .105),
    target.clone().addScaledVector(left, .06), target.clone().add(new THREE.Vector3(0,0,.065))];
  const ray = new THREE.Raycaster();
  let best = desired, bestScore = Infinity;
  for (const candidate of candidates) {
    let blocked = 0;
    for (const sample of samples) {
      const direction = sample.clone().sub(candidate), distance = direction.length();
      ray.set(candidate, direction.normalize()); ray.near = .015; ray.far = Math.max(.015, distance-.10);
      const obstruction = ray.intersectObjects(obstacles, true).some((hit) => {
        const material = Array.isArray(hit.object.material) ? hit.object.material[hit.face?.materialIndex || 0] : hit.object.material;
        if (!material || material.visible === false || material.opacity < .85) return false;
        // Raycasting includes triangles discarded by the existing wall cutaway.
        return !(material.clippingPlanes || []).some((plane) => plane.distanceToPoint(hit.point) < 0);
      });
      if (obstruction) blocked++;
    }
    const score = blocked*100 + candidate.distanceToSquared(desired);
    if (score < bestScore) { best = candidate; bestScore = score; }
    if (blocked === 0 && candidate === desired) break;
  }
  return best;
}

function focusRobot(pose, sensorMount, eyeView = false) {
  state.comparing = false;
  changeView('overview');
  state.view = 'robot';
  state.manualView = true;
  const forward = new THREE.Vector3(Math.cos(pose.yaw_rad), Math.sin(pose.yaw_rad), 0);
  const left = new THREE.Vector3(-forward.y, forward.x, 0);
  const origin = new THREE.Vector3(pose.x_m, pose.y_m, 0);
  camera = new THREE.PerspectiveCamera(eyeView ? 65 : 46, renderPane.clientWidth / Math.max(1, renderPane.clientHeight), .015, 100);
  camera.up.set(0, 0, 1);
  if (eyeView) {
    const mount = sensorMount?.camera_xyz_m || [0, 0, .14];
    camera.position.copy(origin).addScaledVector(forward, mount[0]).addScaledVector(left, mount[1]);
    camera.position.z = mount[2];
    const pitch = THREE.MathUtils.degToRad(sensorMount?.camera_pitch_up_deg || 0);
    controls.target.copy(camera.position).addScaledVector(forward, 2 * Math.cos(pitch));
    controls.target.z += 2 * Math.sin(pitch);
  } else {
    controls.target.copy(origin).add(new THREE.Vector3(0, 0, .16));
    camera.position.copy(robotInspectionPosition(origin, forward, left, controls.target));
  }
  controls.object = camera;
  camera.lookAt(controls.target);
  controls.update();
  // Preserve the overview's wall cutaway when inspecting the robot from outside.
  if (eyeView) updateVisibility();
  element('view-overview').setAttribute('aria-pressed', 'false');
  element('view-plan').setAttribute('aria-pressed', 'false');
  element('view-label').textContent = eyeView ? '로봇 카메라 위치 · 가상 시점' : 'MechaDog · 정지 위치';
  element('view-note').textContent = pose.anchor_frame === 'laser'
    ? '기록 후보 · 라이다 지상 원점 기준 · 몸체 오프셋 미확정'
    : '수신 위치 · base_link 기준 · 가상 카메라 시점';
  resizeRenderer();
}

function updateVisibility() {
  renderer.shadowMap.needsUpdate = true;
  const overview = state.view === 'overview' || state.view === 'plan';
  for (const object of state.draft.objects) {
    const group = groups.get(object.id);
    group.visible = object.visible !== false && !(overview && hasCeilingTag(object));
    group.position.fromArray(object.position);
    group.rotation.z = THREE.MathUtils.degToRad(object.yaw || 0);
  }
  clippingPlane.constant = state.view === 'plan' ? 1.15 : 1.45;
  for (const material of materialCache.values()) {
    material.clippingPlanes = overview && material.userData.clipOverview ? [clippingPlane] : [];
    material.needsUpdate = true;
  }
  updateSelectionBox();
}

function updateSelectionBox() {
  if (selectionBox) {
    world.remove(selectionBox);
    selectionBox.geometry.dispose();
    selectionBox.material.dispose();
    selectionBox = null;
  }
  const object = selectedObject();
  const group = object && groups.get(object.id);
  if (!group?.visible) return;
  group.updateWorldMatrix(true, true);
  const bounds = new THREE.Box3().setFromObject(group);
  if (bounds.isEmpty()) return;
  selectionBox = new THREE.Box3Helper(bounds.expandByScalar(0.012), 0x9ce3cc);
  selectionBox.material.depthTest = false;
  selectionBox.material.transparent = true;
  selectionBox.material.opacity = 0.8;
  selectionBox.renderOrder = 5;
  world.add(selectionBox);
}

function renderObjectList() {
  const query = element('object-search').value.trim().toLocaleLowerCase();
  const room = element('room-filter').value;
  const objects = state.draft.objects.filter((object) => object.editable && object.visible !== false);
  const filtered = objects.filter((object) => (room === 'all' || object.room === room) && `${object.label} ${roomLabel(object.room)}`.toLocaleLowerCase().includes(query));
  element('object-count').textContent = `${objects.length}개`;
  const list = element('object-list');
  list.replaceChildren();
  for (const object of filtered) {
    const button = document.createElement('button');
    button.type = 'button';
    button.dataset.objectId = object.id;
    button.setAttribute('aria-pressed', String(object.id === state.selected));
    const label = document.createElement('span'); label.textContent = object.label;
    const roomName = document.createElement('span'); roomName.textContent = roomLabel(object.room);
    button.append(label, roomName);
    button.disabled = state.busy;
    button.addEventListener('click', () => selectObject(object.id));
    list.append(button);
  }
  element('list-empty').hidden = filtered.length !== 0;
  renderRemovedList();
}

function renderRemovedList() {
  const removed = state.draft.objects.filter((object) => object.editable && object.visible === false);
  const select = element('removed-select');
  const previous = select.value;
  select.replaceChildren();
  if (!removed.length) select.append(new Option('삭제한 가구 없음', ''));
  for (const object of removed) select.append(new Option(`${roomLabel(object.room)} · ${object.label}`, object.id));
  if (removed.some((object) => object.id === previous)) select.value = previous;
  element('removed-count').textContent = removed.length;
  select.disabled = !removed.length || state.busy;
  element('restore').disabled = !removed.length || state.busy;
}

function renderEditor() {
  const object = selectedObject();
  const enabled = !!object && object.visible !== false && !state.busy;
  element('selected-title').textContent = object?.label || '가구를 선택하세요';
  element('selected-room').textContent = object ? `${roomLabel(object.room)} · 사진에 근거한 배치` : '3D 장면이나 목록에서 선택';
  element('position-x').value = object ? Number(object.position[0].toFixed(3)) : '';
  element('position-y').value = object ? Number(object.position[1].toFixed(3)) : '';
  element('rotation').value = object ? Number((object.yaw || 0).toFixed(1)) : '';
  for (const id of ['position-x', 'position-y', 'rotation', 'move-floor', 'remove']) element(id).disabled = !enabled;
  element('object-evidence').textContent = object ? [...(object.evidence || []), object.uncertainty || '치수와 배치는 실측 항법 기준으로 검증되지 않았습니다.'].join('\n') : '가구를 선택하면 출처를 확인할 수 있습니다.';
  element('selection-indicator').textContent = object ? `${object.label} 선택` : '선택 없음';
  updateButtons();
}

function updateButtons() {
  const dirty = isDirty();
  element('save').disabled = state.busy || !dirty;
  element('export').disabled = state.busy || dirty || !state.saved;
  element('undo').disabled = state.busy || !state.history.length;
  element('cancel').disabled = state.busy || !dirty;
  element('save-state').textContent = state.busy ? '처리 중…' : dirty ? '저장 전 변경 있음' : '저장된 배치';
  element('save-state').classList.toggle('dirty', dirty && !state.busy);
  element('move-floor').setAttribute('aria-pressed', String(state.moving));
  element('reload-scene').disabled = state.busy;
  canvas.style.cursor = state.moving ? 'crosshair' : '';
  element('interaction-hint').textContent = state.moving ? '새 바닥 위치를 클릭하세요 · Esc 취소' : selectedObject() ? '방향키 5cm 이동 · Shift 1cm · R 회전 · Delete 삭제' : '드래그 회전 · 휠 확대 · 가구 클릭 선택';
}

function selectObject(id) {
  if (state.busy) return;
  finishNumericEdit();
  state.selected = id;
  state.moving = false;
  updateSelectionBox();
  renderObjectList();
  renderEditor();
}

function pushUndo(snapshot = transforms()) {
  if (!state.history.length || JSON.stringify(state.history.at(-1)) !== JSON.stringify(snapshot)) state.history.push(clone(snapshot));
  if (state.history.length > 50) state.history.shift();
}

function applyTransforms(values) {
  const byId = new Map(values.map((value) => [value.id, value]));
  for (const object of state.draft.objects) {
    if (!object.editable) continue;
    const value = byId.get(object.id);
    if (value) Object.assign(object, { position: [...value.position], yaw: value.yaw, visible: value.visible });
  }
  if (!selectedObject() || selectedObject().visible === false) state.selected = null;
  updateVisibility();
  renderObjectList();
  renderEditor();
}

function changeObject(change) {
  if (state.busy) return false;
  const object = selectedObject();
  if (!object || object.visible === false) return false;
  finishNumericEdit();
  const candidate = editableFields(object);
  change(candidate);
  if (!positionAllowed(candidate.position)) {
    showFeedback('가구 위치는 X −2~13m, Y −2~10m 범위 안에서 이동하세요.', true);
    return false;
  }
  pushUndo();
  Object.assign(object, { position: candidate.position, yaw: candidate.yaw, visible: candidate.visible });
  updateVisibility();
  renderObjectList();
  renderEditor();
  return true;
}

function finishNumericEdit() {
  if (!state.inputSnapshot) return;
  if (JSON.stringify(state.inputSnapshot) !== JSON.stringify(transforms())) pushUndo(state.inputSnapshot);
  state.inputSnapshot = null;
  updateButtons();
}

function numericPreview(id) {
  const object = selectedObject();
  if (!object || state.busy) return;
  const value = Number(element(id).value);
  if (element(id).value === '' || !Number.isFinite(value)) return;
  const proposedPosition = [...object.position];
  if (id === 'position-x') proposedPosition[0] = value;
  if (id === 'position-y') proposedPosition[1] = value;
  if (id !== 'rotation' && !positionAllowed(proposedPosition)) {
    showFeedback(id === 'position-x' ? 'X 위치는 −2m에서 13m 사이로 입력하세요.' : 'Y 위치는 −2m에서 10m 사이로 입력하세요.', true);
    return;
  }
  if (id === 'position-x') object.position[0] = value;
  if (id === 'position-y') object.position[1] = value;
  if (id === 'rotation') object.yaw = value;
  updateVisibility();
  updateButtons();
}

function overviewCamera(plan) {
  const bounds = new THREE.Box3();
  for (const object of state.draft.objects) {
    if (object.visible !== false && !hasCeilingTag(object)) bounds.union(new THREE.Box3().setFromObject(groups.get(object.id)));
  }
  const center = bounds.getCenter(new THREE.Vector3());
  center.z = 0.45;
  const size = bounds.getSize(new THREE.Vector3());
  const aspect = renderPane.clientWidth / Math.max(1, renderPane.clientHeight);
  camera = new THREE.PerspectiveCamera(plan ? 42 : 46, aspect, 0.02, 100);
  camera.up.set(0, 0, 1);
  const direction = plan ? new THREE.Vector3(0, -0.0001, 1) : new THREE.Vector3(-0.8, -1.12, 1.0).normalize();
  const radius = Math.max(size.x, size.y, 5);
  const distance = radius / (2 * Math.tan(THREE.MathUtils.degToRad(camera.fov / 2))) * Math.max(1, 1 / aspect) * (plan ? 1.16 : 1.15);
  camera.position.copy(center).addScaledVector(direction, distance);
  controls.object = camera;
  controls.target.copy(center);
  camera.lookAt(center);
  controls.update();
}

function applyPhotoCamera(source) {
  const calibrated = source.world_from_camera && source.intrinsics;
  const aspect = calibrated ? source.intrinsics.width / source.intrinsics.height : source.aspect || 4 / 3;
  camera = new THREE.PerspectiveCamera(source.fov_y_deg || 55, aspect, 0.02, 100);
  camera.up.fromArray(source.up || [0, 0, 1]);
  if (calibrated) {
    const rows = source.world_from_camera.flat();
    if (rows.length !== 16) throw new Error('사진 카메라 변환 행렬의 크기가 올바르지 않습니다.');
    const matrix = new THREE.Matrix4().set(...rows);
    matrix.decompose(camera.position, camera.quaternion, camera.scale);
    camera.updateMatrixWorld(true);
    const { width, height, fx, fy, cx, cy } = source.intrinsics;
    const near = camera.near, far = camera.far;
    camera.projectionMatrix.set(2 * fx / width, 0, 1 - 2 * cx / width, 0, 0, 2 * fy / height, 2 * cy / height - 1, 0, 0, 0, -(far + near) / (far - near), -2 * far * near / (far - near), 0, 0, -1, 0);
    camera.projectionMatrixInverse.copy(camera.projectionMatrix).invert();
    camera.userData.calibrated = true;
    controls.target.copy(camera.position).add(new THREE.Vector3(0, 0, -1).applyQuaternion(camera.quaternion).multiplyScalar(2));
  } else {
    camera.position.fromArray(source.position);
    controls.target.fromArray(source.target);
    camera.lookAt(controls.target);
  }
  camera.userData.sourceAspect = aspect;
  controls.object = camera;
  // OrbitControls updates replace a supplied camera quaternion; keep calibration exact until interaction.
  if (!calibrated) controls.update();
}

function changeView(view, cameraId = null) {
  state.view = view;
  state.cameraId = cameraId;
  state.moving = false;
  state.manualView = false;
  orbitInteractionStart = null;
  const source = selectedCamera();
  if (state.comparing && !source?.source) state.comparing = false;
  controls.enabled = !state.comparing;
  if (view === 'camera' && source) applyPhotoCamera(source);
  else overviewCamera(view === 'plan');
  updateVisibility();
  element('view-overview').setAttribute('aria-pressed', String(view === 'overview'));
  element('view-plan').setAttribute('aria-pressed', String(view === 'plan'));
  element('camera-select').value = cameraId || '';
  element('view-label').textContent = source?.label || (view === 'plan' ? '평면 · 위에서 보기' : '전체 장면');
  element('compare').disabled = !state.draft.cameras.some((item) => item.source);
  updateComparison();
  updateButtons();
}

function updateComparison() {
  const source = selectedCamera();
  stage.classList.toggle('comparing', state.comparing);
  element('reference-pane').hidden = !state.comparing;
  element('canvas-caption').hidden = !state.comparing;
  element('compare').setAttribute('aria-pressed', String(state.comparing));
  controls.enabled = !state.comparing;
  if (state.comparing && source?.source) {
    element('reference-image').src = assetUrl(source.source);
    element('reference-image').alt = `${source.source_label || source.label} 원본 사진`;
    element('source-label').textContent = source.source_label || '원본 촬영';
    element('alignment-label').textContent = source.alignment_status === 'calibrated' || source.alignment_status === 'verified' ? '사진 카메라 정합' : '시점 추정';
    element('alignment-label').title = source.alignment_note || '원본 사진과의 시점 정합은 별도 확인이 필요합니다.';
    element('view-note').textContent = source.alignment_note || '같은 시점으로 사진과 형상을 대조합니다.';
    element('view-note').title = source.alignment_note || '';
    element('canvas-caption').textContent = `같은 카메라 · SIM${isDirty() ? ' · 저장 전 배치' : ''}`;
  } else {
    element('view-note').textContent = '현관 · 방1 · 발코니 제외';
    element('view-note').removeAttribute('title');
  }
  resizeRenderer();
}

function resizeRenderer() {
  if (element('render-1080')?.checked && renderer && camera) { renderer.setPixelRatio(1); renderer.setSize(1920,1080,false); camera.aspect=1920/1080; camera.updateProjectionMatrix(); return; }
  if (!renderer || !camera) return;
  const source = selectedCamera();
  if (state.comparing && source) {
    const aspect = source.intrinsics ? source.intrinsics.width / source.intrinsics.height : source.aspect || 4 / 3;
    const padding = window.innerWidth <= 760 ? 16 : window.innerWidth <= 1040 ? 20 : 36;
    const gap = window.innerWidth <= 760 ? 7 : window.innerWidth <= 1040 ? 10 : 18;
    const availableWidth = (stage.clientWidth - padding - gap) / 2 - 2;
    const availableHeight = stage.clientHeight - padding - 2;
    const width = Math.max(1, Math.min(availableWidth, availableHeight * aspect));
    const height = width / aspect;
    renderPane.style.width = `${width}px`; renderPane.style.height = `${height}px`;
    element('reference-pane').style.width = `${width}px`;
    element('reference-image').style.width = `${width}px`; element('reference-image').style.height = `${height}px`;
  } else {
    renderPane.style.width = '100%'; renderPane.style.height = '100%';
    if ((state.view === 'overview' || state.view === 'plan') && !state.manualView) {
      overviewCamera(state.view === 'plan');
    } else if (!camera.userData.calibrated) {
      camera.aspect = renderPane.clientWidth / Math.max(1, renderPane.clientHeight);
      camera.updateProjectionMatrix();
    }
  }
  renderer.setSize(Math.max(1, renderPane.clientWidth), Math.max(1, renderPane.clientHeight), false);
}

function populateSelectors() {
  const roomSelect = element('room-filter');
  const rooms = [...new Set(state.draft.objects.filter((object) => object.editable).map((object) => object.room))];
  for (const room of rooms) roomSelect.append(new Option(roomLabel(room), room));
  const cameraSelect = element('camera-select');
  for (const source of state.draft.cameras) cameraSelect.append(new Option(source.label, source.id));
}

async function saveScene() {
  finishNumericEdit();
  if (!isDirty() || state.busy) return;
  state.busy = true;
  state.moving = false;
  renderObjectList(); renderEditor();
  try {
    const result = await request('/api/save', { expected_revision: state.saved.revision, objects: transforms() });
    state.saved = clone(result.scene);
    state.draft = clone(result.scene);
    state.history = [];
    updateVisibility();
    showFeedback('배치를 저장했습니다. 웹 장면과 Isaac 내보내기는 같은 저장본을 사용합니다.');
  } catch (error) {
    showFeedback(error.status === 409 ? '다른 창에서 배치가 바뀌었습니다. 현재 수정은 유지됩니다. 수정 취소 후 새로고침하여 최신 배치를 확인하세요.' : `저장하지 못했습니다. 수정은 유지됩니다. ${error.message}`, true, true);
  } finally {
    state.busy = false;
    renderObjectList(); renderEditor(); updateComparison();
  }
}

async function exportScene() {
  if (state.busy || isDirty()) return;
  state.busy = true; renderObjectList(); renderEditor();
  try {
    const result = await request('/api/export', {});
    const download = element('download');
    download.href = result.url; download.download = '';
    download.click();
    showFeedback('저장된 장면의 USD와 질감을 묶었습니다. ZIP을 풀고 house_sim.usda를 Isaac Sim에서 여세요.');
  } catch (error) { showFeedback(`내보내지 못했습니다. ${error.message}`, true, true); }
  finally { state.busy = false; renderObjectList(); renderEditor(); }
}

function bindEditing() {
  element('object-search').addEventListener('input', renderObjectList);
  element('room-filter').addEventListener('change', renderObjectList);
  for (const id of ['position-x', 'position-y', 'rotation']) {
    element(id).addEventListener('focus', () => { state.inputSnapshot = transforms(); });
    element(id).addEventListener('input', () => numericPreview(id));
    element(id).addEventListener('blur', () => { finishNumericEdit(); renderEditor(); });
  }
  element('move-floor').addEventListener('click', () => { state.moving = !state.moving; updateButtons(); canvas.focus(); });
  element('remove').addEventListener('click', () => {
    const label = selectedObject()?.label;
    changeObject((object) => { object.visible = false; });
    state.selected = null; state.moving = false;
    renderEditor(); renderObjectList();
    showFeedback(`${label} 삭제. 아래 ‘삭제한 가구’에서 복원할 수 있습니다.`);
  });
  element('restore').addEventListener('click', () => {
    const object = state.draft.objects.find((item) => item.id === element('removed-select').value && item.editable);
    if (!object || state.busy) return;
    pushUndo(); object.visible = true;
    state.selected = object.id;
    updateVisibility(); renderObjectList(); renderEditor();
    showFeedback(`${object.label} 복원. 배치를 저장하면 적용됩니다.`);
  });
  element('undo').addEventListener('click', () => {
    finishNumericEdit();
    const previous = state.history.pop();
    if (previous) applyTransforms(previous);
  });
  element('cancel').addEventListener('click', () => {
    state.draft = clone(state.saved); state.history = []; state.moving = false; state.inputSnapshot = null;
    applyTransforms(transforms(state.saved));
    showFeedback('저장 전 수정을 취소하고 마지막 저장 배치로 돌아왔습니다.');
  });
  element('save').addEventListener('click', saveScene);
  element('export').addEventListener('click', exportScene);
  element('reload-scene').addEventListener('click', () => {
    if (state.busy) return;
    state.draft = clone(state.saved);
    state.history = [];
    state.inputSnapshot = null;
    window.location.reload();
  });
  element('view-overview').addEventListener('click', () => changeView('overview'));
  element('view-plan').addEventListener('click', () => changeView('plan'));
  element('camera-select').addEventListener('change', () => {
    if (element('camera-select').value) changeView('camera', element('camera-select').value);
    else changeView('overview');
  });
  element('reset-view').addEventListener('click', () => changeView(state.view, state.cameraId));
  element('compare').addEventListener('click', () => {
    state.comparing = !state.comparing;
    if (state.comparing && !selectedCamera()?.source) {
      const source = state.draft.cameras.find((item) => item.source);
      if (!source) return;
      state.cameraId = source.id; state.view = 'camera';
    }
    changeView(state.view, state.cameraId);
  });
  element('reference-image').addEventListener('error', () => showFeedback('원본 사진을 읽지 못했습니다. 장면은 유지됩니다.', true, true));
  let pointerStart = null;
  canvas.addEventListener('pointerdown', (event) => { if (event.button === 0) pointerStart = [event.clientX, event.clientY]; });
  canvas.addEventListener('pointerup', (event) => {
    if (!pointerStart || Math.hypot(event.clientX - pointerStart[0], event.clientY - pointerStart[1]) > 5 || state.busy) { pointerStart = null; return; }
    pointerStart = null;
    const rect = canvas.getBoundingClientRect();
    const pointer = new THREE.Vector2((event.clientX - rect.left) / rect.width * 2 - 1, 1 - (event.clientY - rect.top) / rect.height * 2);
    const raycaster = new THREE.Raycaster(); raycaster.setFromCamera(pointer, camera);
    if (state.moving) {
      const point = raycaster.ray.intersectPlane(new THREE.Plane(new THREE.Vector3(0, 0, 1), 0), new THREE.Vector3());
      if (!point) return;
      const moved = changeObject((object) => { object.position[0] = Math.round(point.x * 1000) / 1000; object.position[1] = Math.round(point.y * 1000) / 1000; });
      if (moved) state.moving = false;
      updateButtons();
      return;
    }
    const candidates = state.draft.objects.filter((object) => object.editable && object.visible !== false).map((object) => groups.get(object.id));
    const hit = raycaster.intersectObjects(candidates, true)[0];
    if (hit) selectObject(hit.object.userData.objectId);
  });
  canvas.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') { state.moving = false; updateButtons(); return; }
    const object = selectedObject();
    if (!object || state.busy) return;
    const distance = event.shiftKey ? 0.01 : 0.05;
    const directions = { ArrowLeft: [-distance, 0], ArrowRight: [distance, 0], ArrowUp: [0, distance], ArrowDown: [0, -distance] };
    if (directions[event.key]) {
      event.preventDefault();
      changeObject((item) => { const delta = directions[event.key]; item.position[0] = Math.round((item.position[0] + delta[0]) * 1000) / 1000; item.position[1] = Math.round((item.position[1] + delta[1]) * 1000) / 1000; });
    } else if (event.key.toLowerCase() === 'r') {
      event.preventDefault(); changeObject((item) => { item.yaw = (item.yaw || 0) + (event.shiftKey ? 1 : 5); });
    } else if (event.key === 'Delete' || event.key === 'Backspace') {
      event.preventDefault(); element('remove').click();
    }
  });
  window.addEventListener('beforeunload', (event) => { if (isDirty()) { event.preventDefault(); event.returnValue = ''; } });
  document.addEventListener('visibilitychange', () => {
    clearTimeout(statusTimer);
    if (!document.hidden && !statusInFlight) updateConnectionStatus();
  });
  new ResizeObserver(resizeRenderer).observe(stage);
}

async function updateConnectionStatus() {
  if (statusInFlight || document.hidden) return;
  statusInFlight = true;
  try {
    const status = await request('/api/status');
    statusFailures = 0;
    const cameraStatus = status.sensors?.camera === 'connected' ? '카메라 수신 중' : '카메라 미연결';
    const lidarStatus = status.sensors?.lidar === 'connected' ? '라이다 수신 중' : '라이다 미연결';
    element('sensor-status').textContent = `${cameraStatus} · ${lidarStatus}\n지도 위치 정합 미확인`;
    element('sensor-status').style.whiteSpace = 'pre-line';
    const savedRevision = state.saved.revision;
    const usdMatches = status.usd_revision === savedRevision;
    element('revision-status').textContent = `장면 ${String(savedRevision).slice(0, 8)} · ${usdMatches ? 'USD 일치' : 'USD 동기화 대기'}`;
    element('revision-status').title = `현재 웹 장면: ${savedRevision}\nUSD 저장본: ${status.usd_revision || '확인되지 않음'}`;
    if (status.revision && status.revision !== savedRevision && !state.busy) {
      if (!isDirty()) {
        window.location.reload();
        return;
      }
      element('remote-change').hidden = false;
      if (lastConflictRevision !== status.revision) {
        lastConflictRevision = status.revision;
        showFeedback('새 장면이 저장됐습니다. 현재 수정은 유지됩니다. 최신본을 불러오려면 위의 ‘수정 취소 · 최신본 불러오기’를 선택하세요.', true);
      }
    } else if (status.revision === savedRevision) {
      element('remote-change').hidden = true;
      lastConflictRevision = null;
    }
  } catch {
    statusFailures += 1;
    element('sensor-status').textContent = 'SIM 서버 상태 확인 대기\n실제 지도 위치는 미검증입니다.';
    element('sensor-status').style.whiteSpace = 'pre-line';
    element('revision-status').textContent = `장면 ${String(state.saved.revision).slice(0, 8)} · USD 확인 대기`;
  } finally {
    statusInFlight = false;
    if (!document.hidden) statusTimer = setTimeout(updateConnectionStatus, statusFailures ? Math.min(30000, 3000 * 2 ** statusFailures) : 3000);
  }
}

async function init() {
  try {
    const scene = await request('/api/scene');
    if (!Array.isArray(scene.objects) || !scene.materials || !Array.isArray(scene.cameras)) throw new Error('장면 데이터 구조를 확인할 수 없습니다.');
    state.saved = clone(scene); state.draft = clone(scene);
    createRenderer(); buildScene(); populateSelectors(); if(!embedded) bindEditing(); else new ResizeObserver(resizeRenderer).observe(stage);
    if(!embedded) initLidarInspector({ selectObject, getRevision: () => state.saved.revision, isDirty });
    robotLink = initRobotLink({ world, buildPartMesh, request, getRevision: () => state.saved.revision, isDirty, focusRobot });
    presentation = initPresentation({world,request,robotLink,changeView,focusRobot,getCamera:()=>camera,getControls:()=>controls,renderNow:()=>renderer.render(world,camera),rendererInfo:()=>({calls:renderer.info.render.calls,triangles:renderer.info.render.triangles,pixel_ratio:renderer.getPixelRatio(),shadow_auto_update:renderer.shadowMap.autoUpdate})});
    element('render-1080').addEventListener('change',()=>{renderer.setPixelRatio(Math.min(window.devicePixelRatio||1,2));resizeRenderer();});
    renderObjectList(); renderEditor();
    changeView('overview');
    element('loading').hidden = true;
    if(embedded) {presentation.select('plan');startEmbed();element('reset-view').addEventListener('click',()=>presentation.select('plan'));}
    updateConnectionStatus();
    try { state.baseline = await request('/api/baseline'); } catch { /* Saving and undo remain available without the baseline endpoint. */ }
  } catch (error) {
    reportEmbedFailure();
    element('loading').classList.add('error');
    element('loading').textContent = `장면을 열지 못했습니다. ${error.message}`;
    element('save-state').textContent = '장면 연결 오류';
  }
}

init();
