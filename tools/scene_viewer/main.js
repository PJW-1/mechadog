import * as THREE from 'three';
import {OrbitControls} from 'three/addons/controls/OrbitControls.js';

const canvas = document.querySelector('#scene');
const renderer = new THREE.WebGLRenderer({canvas, antialias:true});
renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
renderer.outputColorSpace = THREE.SRGBColorSpace;
const scene = new THREE.Scene();
scene.background = new THREE.Color(0x18231e);
scene.fog = new THREE.Fog(0x18231e, 20, 55);
scene.add(new THREE.HemisphereLight(0xd9f4e1, 0x18231e, 2.2));
const sun = new THREE.DirectionalLight(0xffe3bb, 2.3);
sun.position.set(4, 10, 5);
scene.add(sun);
const camera = new THREE.PerspectiveCamera(48, 1, 0.01, 150);
const controls = new OrbitControls(camera, canvas);
controls.enableDamping = true;
controls.maxPolarAngle = Math.PI * 0.48;
controls.minDistance = 0.3;
controls.maxDistance = 80;

function addRuns(runs, geometry, material, height, y) {
  if (!runs.length) return;
  const mesh = new THREE.InstancedMesh(new THREE.BoxGeometry(1, 1, 1), material, runs.length);
  const object = new THREE.Object3D();
  for (let i = 0; i < runs.length; i++) {
    const [row, start, end] = runs[i];
    const r = geometry.resolution_m;
    object.position.set(geometry.origin_xy_m[0] + (start + end) * r / 2, y,
      -geometry.origin_xy_m[1] - (row + .5) * r);
    object.scale.set((end - start) * r, height, r);
    object.updateMatrix();
    mesh.setMatrixAt(i, object.matrix);
  }
  mesh.instanceMatrix.needsUpdate = true;
  scene.add(mesh);
}

function lineForTrack(track) {
  if (track.length < 2) return;
  const coordinates = [];
  for (const [x, y] of track) coordinates.push(x, .13, -y);
  const path = new THREE.Line(
    new THREE.BufferGeometry().setFromPoints(
      Array.from({length:coordinates.length / 3}, (_, i) =>
        new THREE.Vector3(...coordinates.slice(i * 3, i * 3 + 3)))),
    new THREE.LineBasicMaterial({color:0x86e7b5})
  );
  scene.add(path);
}

function showFacts(bundle) {
  const geometry = bundle.geometry;
  const details = [
    ['데이터 출처', bundle.source === 'simulation' ? '시뮬레이션' :
      bundle.source === 'stationary_single_pose_snapshot_not_slam' ? '실물 정지 스캔' : '저장 지도'],
    ['격자 범위', `${(geometry.width_cells * geometry.resolution_m).toFixed(1)} × ${(geometry.height_cells * geometry.resolution_m).toFixed(1)} m`],
    ['격자 해상도', `${Math.round(geometry.resolution_m * 100)} cm`],
    ['카메라 기록', `${bundle.camera_observations.length}개`],
    ['현실 주행', bundle.navigation_ready ? '준비됨' : '아직 미검증'],
  ];
  const list = document.querySelector('#facts');
  for (const [label, value] of details) {
    const row = document.createElement('div');
    const term = document.createElement('dt');
    const result = document.createElement('dd');
    term.textContent = label;
    result.textContent = value;
    row.append(term, result);
    list.append(row);
  }
  const simulated = bundle.source === 'simulation';
  const stationary = bundle.source === 'stationary_single_pose_snapshot_not_slam';
  document.querySelector('#status').textContent = simulated ? '시뮬레이션 지도' :
    stationary ? '실물 단일 위치 스캔' : '저장된 센서 지도';
  document.querySelector('#source').textContent = simulated ? 'SIMULATION' :
    stationary ? 'REAL · ONE POSE' : 'SAVED MAP';
  if (stationary) document.querySelector('#scope').textContent =
    '한 자리에서 라이다가 본 범위만 표시합니다. 방 전체 지도나 이동 경로는 아직 아닙니다.';
}

const clickTargets = [];
function addCameras(observations) {
  for (const observation of observations) {
    const [x, y] = observation.pose_lidar_centre;
    const marker = new THREE.Mesh(
      new THREE.SphereGeometry(.08, 12, 8),
      new THREE.MeshStandardMaterial({color:0x83dbff, emissive:0x144d62, roughness:.25})
    );
    marker.position.set(x, .18, -y);
    marker.userData.observation = observation;
    scene.add(marker);
    clickTargets.push(marker);
  }
}

function setPhoto(observation) {
  const note = document.querySelector('#photo-note');
  const image = document.querySelector('#photo');
  const photo = observation.photo;
  note.textContent = `라이다 중심 기준 (${observation.pose_lidar_centre[0].toFixed(2)}, ${observation.pose_lidar_centre[1].toFixed(2)}) m · 촬영 위치 기록`;
  if (typeof photo === 'string' && /^photos\/[A-Za-z0-9._-]+\.jpe?g$/i.test(photo)) {
    image.src = `/${photo}`;
    image.hidden = false;
  } else {
    image.hidden = true;
    note.textContent += ' · 원본 사진을 열 수 없습니다';
  }
}

const pointer = new THREE.Vector2();
const raycaster = new THREE.Raycaster();
canvas.addEventListener('click', event => {
  const rect = canvas.getBoundingClientRect();
  pointer.set((event.clientX - rect.left) / rect.width * 2 - 1,
    -(event.clientY - rect.top) / rect.height * 2 + 1);
  raycaster.setFromCamera(pointer, camera);
  const hit = raycaster.intersectObjects(clickTargets)[0];
  if (hit) setPhoto(hit.object.userData.observation);
});

function resize() {
  const width = canvas.clientWidth;
  const height = canvas.clientHeight;
  if (canvas.width !== Math.floor(width * renderer.getPixelRatio()) ||
      canvas.height !== Math.floor(height * renderer.getPixelRatio())) {
    renderer.setSize(width, height, false);
    camera.aspect = width / height;
    camera.updateProjectionMatrix();
  }
}

function observedBounds(geometry) {
  let minX = Infinity, maxX = -Infinity, minZ = Infinity, maxZ = -Infinity;
  for (const [row, start, end] of
    [...geometry.free_runs_row_start_end, ...geometry.occupied_runs_row_start_end]) {
    const x0 = geometry.origin_xy_m[0] + start * geometry.resolution_m;
    const x1 = geometry.origin_xy_m[0] + end * geometry.resolution_m;
    const z = -geometry.origin_xy_m[1] - row * geometry.resolution_m;
    minX = Math.min(minX, x0);
    maxX = Math.max(maxX, x1);
    minZ = Math.min(minZ, z);
    maxZ = Math.max(maxZ, z);
  }
  return Number.isFinite(minX) ? {minX, maxX, minZ, maxZ} : null;
}

function animate() {
  requestAnimationFrame(animate);
  resize();
  controls.update();
  renderer.render(scene, camera);
}

async function start() {
  const response = await fetch('/scene.json', {cache:'no-store'});
  if (!response.ok) throw new Error(`지도 파일을 읽을 수 없습니다 (${response.status})`);
  const bundle = await response.json();
  if (bundle.schema !== 'mechadog_scene_bundle_v1' || bundle.coordinate_frame !== 'lidar_centre_xy_metres' ||
      bundle.geometry.dimension !== '2d_scan_plane_only') throw new Error('지원하지 않는 지도 형식입니다');
  const geometry = bundle.geometry;
  const middleX = geometry.origin_xy_m[0] + geometry.width_cells * geometry.resolution_m / 2;
  const middleZ = -geometry.origin_xy_m[1] - geometry.height_cells * geometry.resolution_m / 2;
  const seen = observedBounds(geometry);
  const focusX = seen ? (seen.minX + seen.maxX) / 2 : middleX;
  const focusZ = seen ? (seen.minZ + seen.maxZ) / 2 : middleZ;
  const span = seen ? Math.max(1, seen.maxX - seen.minX, seen.maxZ - seen.minZ) :
    Math.max(geometry.width_cells, geometry.height_cells) * geometry.resolution_m;
  const unknown = new THREE.Mesh(
    new THREE.PlaneGeometry(geometry.width_cells * geometry.resolution_m, geometry.height_cells * geometry.resolution_m),
    new THREE.MeshStandardMaterial({color:0x39423e, roughness:1, side:THREE.DoubleSide})
  );
  unknown.rotation.x = -Math.PI / 2;
  unknown.position.set(middleX, -.018, middleZ);
  scene.add(unknown);
  addRuns(geometry.free_runs_row_start_end, geometry,
    new THREE.MeshStandardMaterial({color:0x426455, roughness:1}), .012, .002);
  // Vertical thickness is symbolic only; the sensor measured a horizontal slice.
  addRuns(geometry.occupied_runs_row_start_end, geometry,
    new THREE.MeshStandardMaterial({color:0xe1b778, roughness:.68}), .14, .07);
  lineForTrack(bundle.lidar_centre_track || []);
  addCameras(bundle.camera_observations || []);
  controls.target.set(focusX, 0, focusZ);
  const angleView = () => camera.position.set(focusX + span * .55, Math.max(2, span * .8), focusZ + span * .8);
  document.querySelector('#top-view').addEventListener('click', () => {
    camera.position.set(focusX, Math.max(2, span * 1.6), focusZ + .001);
    controls.update();
  });
  document.querySelector('#angle-view').addEventListener('click', () => {angleView();controls.update();});
  angleView();
  showFacts(bundle);
  animate();
}

start().catch(error => {
  document.querySelector('#status').textContent = '지도를 열지 못했습니다';
  document.querySelector('#scope').textContent = error.message;
});
