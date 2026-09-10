const views = [...document.querySelectorAll('.view')];
const navigationButtons = [...document.querySelectorAll('[data-view]')];
const operation = new OperationState();
const toast = document.querySelector('#toast');
const estopDialog = document.querySelector('#estop-dialog');
const driveButtons = [...document.querySelectorAll('[data-command]')];
const auditEntries = [];
let activeView = 'dashboard';
let toastTimer;
let activePointer = null;
let activeKey = null;
let twinInitialized = false;

function showToast(message) {
  toast.textContent = message;
  toast.classList.add('is-visible');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toast.classList.remove('is-visible'), 4200);
}

function recordActivity(action, detail) {
  const entry = { time: new Date().toISOString(), action, detail };
  auditEntries.push(entry);
  window.dispatchEvent(new CustomEvent('preview:audit', { detail: entry }));
}

function stopDrive() {
  activePointer = null;
  activeKey = null;
  operation.stop();
  driveButtons.forEach(button => {
    button.classList.remove('is-pressed');
    button.setAttribute('aria-pressed', 'false');
  });
  document.querySelector('#last-command').textContent = operation.blocked ? 'LOCKED' : 'STOP';
}

function showView(viewName, updateHash = true) {
  const target = views.find(view => view.id === 'view-' + viewName) || views[0];
  const name = target.id.replace('view-', '');
  if (name !== activeView && operation.hasControl) {
    stopDrive();
    operation.release();
    recordActivity('제어권 반납', '화면 이동으로 수동 제어를 종료했습니다.');
    renderOperation();
  }
  activeView = name;
  views.forEach(view => view.classList.toggle('is-visible', view === target));
  navigationButtons.forEach(button => {
    const selected = button.dataset.view === name;
    button.classList.toggle('is-active', selected);
    if (selected) button.setAttribute('aria-current', 'page');
    else button.removeAttribute('aria-current');
  });
  document.title = target.dataset.title + ' · MechaDog';
  if (updateHash) window.history.replaceState(null, '', '#' + name);
  if (name === 'robot' && !twinInitialized) {
    twinInitialized = true;
    try { createDigitalTwin(); }
    catch { document.querySelector('#webgl-fallback').hidden = false; }
  }
  window.dispatchEvent(new CustomEvent('preview:view', { detail: name }));
  window.scrollTo({ top: 0, behavior: 'instant' });
}
navigationButtons.forEach(button => button.addEventListener('click', () => showView(button.dataset.view)));
document.querySelectorAll('[data-jump]').forEach(button => button.addEventListener('click', () => showView(button.dataset.jump)));
document.querySelector('#robot-selector').addEventListener('click', () => showView('robot'));
window.addEventListener('hashchange', () => {
  if (location.hash === '#main-content') return;
  showView(location.hash.slice(1), false);
});

// Original example values are retained separately; missing data is never zero.
const metricValues = [...document.querySelectorAll('.metric-feature strong, .metric-row strong')].map(element => ({
  element, html: element.innerHTML,
}));
const sampleSurfaces = ['.mission-meta', '.progress-line', '.spark-chart', '.state-timeline',
  '.pose-strip', '.twin-status-grid', '.joint-list', '.log-list', '.mission-queue', '.zone-list'];
sampleSurfaces.forEach(selector => {
  const element = document.querySelector(selector);
  const empty = document.createElement('div');
  empty.className = 'empty-state sample-empty';
  empty.textContent = '수신된 데이터가 없습니다.';
  empty.hidden = true;
  element.after(empty);
});
function renderOperation() {
  const blocked = operation.blocked;
  document.body.classList.toggle('no-data', !operation.preview);
  document.querySelector('#preview-mode').textContent = operation.preview ? '예시 데이터 켜짐' : '예시 데이터 꺼짐';
  document.querySelector('#preview-mode').setAttribute('aria-pressed', String(operation.preview));
  document.querySelector('#stale-alert').hidden = !operation.stale;
  const freshness = document.querySelector('#freshness');
  freshness.textContent = !operation.preview ? '미수신' : operation.stale ? '만료된 예시' : '예시 값';
  freshness.classList.toggle('is-stale', blocked);
  document.querySelector('#claim-control').disabled = blocked;
  document.querySelector('#control-lock').hidden = operation.hasControl;
  document.querySelector('#drive-controls').hidden = !operation.hasControl;
  document.querySelector('#control-lock p').textContent = !operation.preview ? '장비가 연결되지 않아 수동 제어를 사용할 수 없습니다.'
    : operation.estopPending ? '정지 확인 대기 상태입니다. 미리보기의 제어가 잠겼습니다.'
    : operation.stale ? '데이터가 만료되었습니다. 연결 상태를 먼저 확인하세요.'
    : '제어권을 가져오면 순찰이 일시정지됩니다.';
  const owner = document.querySelector('#owner-badge');
  owner.textContent = operation.hasControl ? '내 제어 · 미리보기' : '제어자 없음';
  owner.classList.toggle('is-owned', operation.hasControl);
  driveButtons.forEach(button => { button.disabled = blocked || !operation.hasControl; });
  document.querySelector('#last-command').textContent = blocked ? 'LOCKED' : operation.command;
  const label = !operation.preview ? '데이터 대기' : operation.estopPending ? '정지 확인 대기'
    : operation.stale ? '연결 만료' : operation.hasControl ? '수동 조작 중'
    : operation.mission === 'running' ? '순찰 진행 중' : '순찰 일시정지';
  document.querySelector('#mission-heading').textContent = label;
  document.querySelector('#overview-mission').textContent = label;
  document.querySelector('.mission-number').innerHTML = 'PATROL <span>/ 0' + operation.missionNumber + '</span>';
  const seal = document.querySelector('#state-seal');
  seal.classList.toggle('is-paused', operation.mission !== 'running');
  seal.querySelector('strong').textContent = !operation.preview || operation.stale ? 'UNKNOWN'
    : operation.estopPending ? 'PENDING' : operation.hasControl ? 'MANUAL'
    : operation.mission === 'running' ? 'PATROL' : 'PAUSED';
  document.querySelector('.escalation-seal strong').textContent = !operation.preview || operation.stale || operation.estopPending ? '—' : 'L0';
  document.querySelector('.escalation-seal small').textContent = !operation.preview || operation.stale || operation.estopPending ? '미확인' : 'NORMAL';
  const missionButton = document.querySelector('#mission-toggle');
  missionButton.disabled = !operation.canToggleMission;
  missionButton.textContent = operation.mission === 'running' ? '일시정지' : '순찰 재개';
  const startButton = document.querySelector('#start-mission');
  startButton.disabled = !operation.canStart;
  startButton.textContent = 'PATROL / 02 시작 · 예시';
  document.querySelector('#mission-start-help').textContent = blocked ? '데이터와 안전 상태를 먼저 확인하세요.'
    : operation.hasControl ? '수동 제어권을 반납한 뒤 시작할 수 있습니다.'
    : operation.mission === 'running' ? '진행 중인 순찰을 일시정지한 뒤 다음 임무를 시작하세요.'
    : '예시 임무를 시작할 수 있습니다. 실제 로봇으로 전송되지 않습니다.';
  const checks = {
    telemetry: [operation.preview && !operation.stale, operation.stale ? '예시 데이터 만료' : operation.preview ? '예시 데이터 사용 중' : '데이터 미수신'],
    battery: [operation.preview && !operation.stale, operation.preview && !operation.stale ? '7.62 V · 예시' : '현재 전압 미확인'],
    safety: [operation.preview && !operation.estopPending, operation.estopPending ? '정지 확인 대기' : operation.preview ? '정지 요청 없음 · 예시' : '안전 상태 미확인'],
    control: [!operation.hasControl, operation.hasControl ? '현재 운영자가 수동 조작 중' : '수동 제어자 없음'],
  };
  Object.entries(checks).forEach(([key, [pass, detail]]) => {
    const row = document.querySelector('[data-check="' + key + '"]');
    row.querySelector('small').textContent = detail;
    row.querySelector('em').textContent = pass ? '확인' : '대기';
    row.classList.toggle('check-pending', !pass);
  });
  document.querySelector('#preflight-result').textContent = Object.values(checks).filter(([pass]) => pass).length + ' / 4 확인';
  const estop = document.querySelector('#estop-open');
  estop.classList.toggle('is-pending', operation.estopPending);
  estop.querySelector('b').textContent = operation.estopPending ? '정지 확인 대기' : '긴급 정지';
  estop.querySelector('small').textContent = operation.estopPending ? '미리보기 · 잠금' : 'E-STOP · SHIFT+E';
  metricValues.forEach(({element, html}) => { element.innerHTML = operation.preview ? html : '—'; });
  document.querySelector('.battery-rail').hidden = !operation.preview;
  document.querySelector('.metric-feature em').textContent = !operation.preview ? '현재 전압 미확인' : operation.stale ? '만료된 값 · 현재값 아님' : '정상 범위 · 예시';
  sampleSurfaces.forEach(selector => {
    const element = document.querySelector(selector);
    element.hidden = !operation.preview;
    element.nextElementSibling.hidden = operation.preview;
  });
  document.querySelectorAll('[data-mission-number]').forEach(queue => {
    const selected = Number(queue.dataset.missionNumber) === operation.missionNumber;
    queue.classList.toggle('is-running', selected);
    queue.querySelector('span').textContent = selected ? (operation.mission === 'running' && !blocked ? 'RUNNING · 예시' : 'PAUSED · 예시') : '대기 · 예시';
  });
  const missionProgress = operation.missionNumber === 1 ? 64 : 0;
  const missionValues = document.querySelectorAll('.mission-meta span b');
  missionValues[0].textContent = missionProgress + '%';
  missionValues[1].textContent = operation.missionNumber === 1 ? '18:42' : '00:00';
  missionValues[2].textContent = operation.missionNumber === 1 ? 'ZONE B' : '구역 확인 대기';
  document.querySelector('.progress-line span').style.width = missionProgress + '%';
  document.querySelector('.progress-line').setAttribute('aria-label', '예시 임무 진행률 ' + missionProgress + '%');
  const currentTimeline = document.querySelector('.state-timeline .is-current');
  currentTimeline.querySelector('span').textContent = seal.querySelector('strong').textContent;
  currentTimeline.querySelector('small').textContent = label;
  document.querySelector('.twin-status-grid b').textContent = seal.querySelector('strong').textContent;
  document.querySelector('#robot-canvas').hidden = !operation.preview;
  document.querySelector('.twin-live-label').textContent = operation.preview ? '예시 3D 자세 · 센서 연동 전' : '자세 데이터 미수신';
  window.dispatchEvent(new CustomEvent('preview:state', { detail: { preview:operation.preview, stale:operation.stale } }));
}
function changeOperation(action, detail, change) {
  stopDrive();
  change();
  renderOperation();
  recordActivity(action, detail);
  showToast(detail);
}
document.querySelector('#preview-mode').addEventListener('click', () => {
  changeOperation('예시 표시 변경', operation.preview ? '예시 데이터를 숨겼습니다.' : '예시 데이터를 표시합니다. 순찰은 자동 재개되지 않습니다.', () => operation.setPreview(!operation.preview));
});
document.querySelector('#stale-toggle').addEventListener('click', () => {
  changeOperation('연결 만료 시험', '예시 데이터가 만료되어 제어권과 진행 중인 이동을 해제했습니다.', () => operation.setStale(true));
});
document.querySelector('#restore-telemetry').addEventListener('click', () => {
  changeOperation('연결 복구 시험', '예시 연결을 복구했습니다. 제어권과 순찰은 자동 복구되지 않습니다.', () => operation.setStale(false));
});
document.querySelector('#claim-control').addEventListener('click', () => {
  if (!operation.claim()) return;
  renderOperation();
  recordActivity('제어권 획득', '예시 순찰을 일시정지하고 수동 제어를 시작했습니다.');
  showToast('순찰을 일시정지했습니다. 누르는 동안만 이동합니다.');
});
document.querySelector('#release-control').addEventListener('click', () => {
  changeOperation('제어권 반납', '수동 조작을 종료했습니다. 순찰 재개는 별도로 선택하세요.', () => operation.release());
});
function beginDrive(button) {
  if (!operation.move(button.dataset.command)) return;
  driveButtons.forEach(item => {
    item.classList.toggle('is-pressed', item === button);
    item.setAttribute('aria-pressed', String(item === button));
  });
  document.querySelector('#last-command').textContent = operation.command;
}
driveButtons.forEach(button => {
  button.setAttribute('aria-pressed', 'false');
  button.addEventListener('pointerdown', event => {
    if (button.dataset.command === 'STOP' && operation.hasControl) {
      event.preventDefault(); stopDrive(); return;
    }
    if (event.button !== 0 || activePointer !== null || activeKey !== null || operation.blocked || !operation.hasControl) return;
    event.preventDefault();
    activePointer = event.pointerId;
    button.setPointerCapture?.(event.pointerId);
    beginDrive(button);
  });
  ['pointerup','pointercancel','lostpointercapture'].forEach(type => button.addEventListener(type, event => {
    if (event.pointerId === activePointer) stopDrive();
  }));
  button.addEventListener('keydown', event => {
    if (![' ', 'Enter'].includes(event.key)) return;
    event.preventDefault();
    if (event.repeat || activePointer !== null || activeKey !== null) return;
    activeKey = event.key;
    beginDrive(button);
  });
  button.addEventListener('keyup', event => {
    if (event.key === activeKey) { event.preventDefault(); stopDrive(); }
  });
  button.addEventListener('blur', stopDrive);
});
window.addEventListener('blur', stopDrive);
document.addEventListener('visibilitychange', () => {
  if (document.hidden && operation.hasControl) {
    stopDrive(); operation.release(); renderOperation();
    recordActivity('제어권 반납', '페이지가 비활성 상태가 되어 수동 제어를 종료했습니다.');
  }
});
document.querySelector('#mission-toggle').addEventListener('click', () => {
  if (!operation.toggleMission()) return;
  renderOperation();
  recordActivity('순찰 상태 변경', operation.mission === 'running' ? '예시 순찰 재개' : '예시 순찰 일시정지');
});
document.querySelector('#start-mission').addEventListener('click', () => {
  if (!operation.startMission()) return;
  renderOperation();
  recordActivity('임무 시작', 'PATROL / 02 예시를 시작했습니다.');
  showView('dashboard');
  showToast('PATROL / 02 예시를 시작했습니다.');
});
function openEstopDialog() {
  stopDrive();
  if (!estopDialog.open) { estopDialog.returnValue = ''; estopDialog.showModal(); }
}
document.querySelector('#estop-open').addEventListener('click', openEstopDialog);
window.addEventListener('keydown', event => {
  if (event.key === 'Escape') stopDrive();
  const editing = event.target.closest?.('input, textarea, select, [contenteditable="true"]');
  if (editing || event.repeat || !event.shiftKey || event.key.toLowerCase() !== 'e') return;
  event.preventDefault();
  openEstopDialog();
});
estopDialog.addEventListener('close', () => {
  if (estopDialog.returnValue !== 'confirm') return;
  changeOperation('긴급정지 UI 잠금', '미리보기 제어를 잠갔습니다. 실제 장비의 물리 정지는 확인되지 않았습니다.', () => operation.requestEstop());
});
document.querySelector('#reset-preview').addEventListener('click', () => {
  changeOperation('예시 초기화', '미리보기를 대기 상태로 초기화했습니다.', () => operation.resetPreview());
});
document.querySelector('#camera-focus').addEventListener('click', event => {
  const expanded = document.querySelector('.dashboard-grid').classList.toggle('camera-expanded');
  event.currentTarget.setAttribute('aria-pressed', String(expanded));
  event.currentTarget.textContent = expanded ? '기본 크기 ↙' : '넓게 보기 ↗';
});
function updateClock() {
  const time = new Intl.DateTimeFormat('ko-KR', { timeZone:'Asia/Seoul', hour:'2-digit', minute:'2-digit', second:'2-digit', hour12:false }).format(new Date());
  document.querySelector('#system-time').textContent = time + ' KST';
  document.querySelector('#header-clock').textContent = time + ' KST';
}
renderOperation();
showView(location.hash.slice(1) || 'dashboard');
updateClock();
setInterval(updateClock, 1000);

function createDigitalTwin() {
  const canvas = document.querySelector('#robot-canvas');
  const fallback = document.querySelector('#webgl-fallback');
  const gl = canvas.getContext('webgl', { antialias: true, alpha: false });
  if (!gl) {
    fallback.hidden = false;
    return;
  }

  const vertexShaderSource = `
    attribute vec3 aPosition;
    uniform mat4 uMatrix;
    void main() {
      gl_Position = uMatrix * vec4(aPosition, 1.0);
    }
  `;
  const fragmentShaderSource = `
    precision mediump float;
    uniform vec4 uColor;
    void main() {
      gl_FragColor = uColor;
    }
  `;

  function compileShader(type, source) {
    const shader = gl.createShader(type);
    gl.shaderSource(shader, source);
    gl.compileShader(shader);
    if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(shader));
    return shader;
  }

  const program = gl.createProgram();
  gl.attachShader(program, compileShader(gl.VERTEX_SHADER, vertexShaderSource));
  gl.attachShader(program, compileShader(gl.FRAGMENT_SHADER, fragmentShaderSource));
  gl.linkProgram(program);
  if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {
    fallback.hidden = false;
    return;
  }

  const cubeVertices = new Float32Array([
    -1,-1,-1, 1,-1,-1, 1,1,-1, -1,1,-1,
    -1,-1, 1, 1,-1, 1, 1,1, 1, -1,1, 1,
  ]);
  const cubeIndices = new Uint16Array([
    0,1,2, 0,2,3, 4,6,5, 4,7,6,
    0,4,5, 0,5,1, 3,2,6, 3,6,7,
    1,5,6, 1,6,2, 0,3,7, 0,7,4,
  ]);
  const edgeIndices = new Uint16Array([
    0,1,1,2,2,3,3,0, 4,5,5,6,6,7,7,4, 0,4,1,5,2,6,3,7,
  ]);

  const positionBuffer = gl.createBuffer();
  gl.bindBuffer(gl.ARRAY_BUFFER, positionBuffer);
  gl.bufferData(gl.ARRAY_BUFFER, cubeVertices, gl.STATIC_DRAW);
  const indexBuffer = gl.createBuffer();
  gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, indexBuffer);
  gl.bufferData(gl.ELEMENT_ARRAY_BUFFER, cubeIndices, gl.STATIC_DRAW);

  const positionLocation = gl.getAttribLocation(program, 'aPosition');
  const matrixLocation = gl.getUniformLocation(program, 'uMatrix');
  const colorLocation = gl.getUniformLocation(program, 'uColor');

  const multiply = (a, b) => {
    const out = new Float32Array(16);
    for (let row = 0; row < 4; row += 1) {
      for (let col = 0; col < 4; col += 1) {
        out[col * 4 + row] =
          a[row] * b[col * 4] + a[4 + row] * b[col * 4 + 1] +
          a[8 + row] * b[col * 4 + 2] + a[12 + row] * b[col * 4 + 3];
      }
    }
    return out;
  };
  const identity = () => new Float32Array([1,0,0,0, 0,1,0,0, 0,0,1,0, 0,0,0,1]);
  const translate = (x, y, z) => new Float32Array([1,0,0,0, 0,1,0,0, 0,0,1,0, x,y,z,1]);
  const scale = (x, y, z) => new Float32Array([x,0,0,0, 0,y,0,0, 0,0,z,0, 0,0,0,1]);
  const rotateX = (a) => { const c=Math.cos(a),s=Math.sin(a); return new Float32Array([1,0,0,0, 0,c,s,0, 0,-s,c,0, 0,0,0,1]); };
  const rotateY = (a) => { const c=Math.cos(a),s=Math.sin(a); return new Float32Array([c,0,-s,0, 0,1,0,0, s,0,c,0, 0,0,0,1]); };
  const rotateZ = (a) => { const c=Math.cos(a),s=Math.sin(a); return new Float32Array([c,s,0,0, -s,c,0,0, 0,0,1,0, 0,0,0,1]); };
  const perspective = (fov, aspect, near, far) => {
    const f = 1 / Math.tan(fov / 2);
    const range = 1 / (near - far);
    return new Float32Array([f/aspect,0,0,0, 0,f,0,0, 0,0,(near+far)*range,-1, 0,0,near*far*2*range,0]);
  };

  const parts = [
    { p:[0,.3,0], s:[1.45,.42,.72], c:[.44,.53,.25,1] },
    { p:[1.55,.44,0], s:[.45,.34,.55], c:[.55,.64,.34,1] },
    { p:[1.98,.48,0], s:[.12,.18,.34], c:[.12,.15,.13,1] },
    { p:[.78,-.48,.52], s:[.18,.56,.18], r:[0,0,-.28], c:[.34,.39,.31,1] },
    { p:[.94,-1.36,.52], s:[.16,.52,.16], r:[0,0,.15], c:[.47,.56,.29,1] },
    { p:[.78,-.48,-.52], s:[.18,.56,.18], r:[0,0,-.28], c:[.34,.39,.31,1] },
    { p:[.94,-1.36,-.52], s:[.16,.52,.16], r:[0,0,.15], c:[.47,.56,.29,1] },
    { p:[-.78,-.48,.52], s:[.18,.56,.18], r:[0,0,.25], c:[.34,.39,.31,1] },
    { p:[-.94,-1.36,.52], s:[.16,.52,.16], r:[0,0,-.15], c:[.47,.56,.29,1] },
    { p:[-.78,-.48,-.52], s:[.18,.56,.18], r:[0,0,.25], c:[.34,.39,.31,1] },
    { p:[-.94,-1.36,-.52], s:[.16,.52,.16], r:[0,0,-.15], c:[.47,.56,.29,1] },
    { p:[.99,-1.88,.52], s:[.34,.10,.26], c:[.12,.14,.12,1] },
    { p:[.99,-1.88,-.52], s:[.34,.10,.26], c:[.12,.14,.12,1] },
    { p:[-.99,-1.88,.52], s:[.34,.10,.26], c:[.12,.14,.12,1] },
    { p:[-.99,-1.88,-.52], s:[.34,.10,.26], c:[.12,.14,.12,1] },
  ];

  let rotationX = -.22;
  let rotationY = -.6;
  const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)');
  let autoRotate = !reducedMotion.matches;
  document.querySelector('#twin-rotate').textContent = `자동 회전 ${autoRotate ? 'ON' : 'OFF'}`;
  document.querySelector('#twin-rotate').classList.toggle('is-active', autoRotate);
  document.querySelector('#twin-rotate').setAttribute('aria-pressed', String(autoRotate));
  let dragging = false;
  let lastX = 0;
  let lastY = 0;
  let animationFrame = null;
  let inViewport = true;
  let previousFrameTime = null;
  let contextLost = false;
  function canRender() { return !contextLost && !document.hidden && operation.preview && activeView === 'robot' && inViewport; }
  function requestRender() {
    if (!canRender()) {
      if (animationFrame !== null) cancelAnimationFrame(animationFrame);
      animationFrame = null;
      previousFrameTime = null;
      return;
    }
    if (animationFrame === null) animationFrame = requestAnimationFrame(render);
  }

  function resize() {
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const width = Math.max(1, Math.floor(canvas.clientWidth * dpr));
    const height = Math.max(1, Math.floor(canvas.clientHeight * dpr));
    if (canvas.width !== width || canvas.height !== height) {
      canvas.width = width;
      canvas.height = height;
      gl.viewport(0, 0, width, height);
    }
  }

  function drawPart(part, viewProjection) {
    let model = identity();
    model = multiply(model, translate(...part.p));
    if (part.r) {
      model = multiply(model, rotateX(part.r[0]));
      model = multiply(model, rotateY(part.r[1]));
      model = multiply(model, rotateZ(part.r[2]));
    }
    model = multiply(model, scale(...part.s));
    gl.uniformMatrix4fv(matrixLocation, false, multiply(viewProjection, model));
    gl.uniform4fv(colorLocation, part.c);
    gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, indexBuffer);
    gl.drawElements(gl.TRIANGLES, cubeIndices.length, gl.UNSIGNED_SHORT, 0);

    gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, edgeBuffer);
    gl.uniform4fv(colorLocation, [0.72,0.78,0.65,1]);
    gl.drawElements(gl.LINES, edgeIndices.length, gl.UNSIGNED_SHORT, 0);
  }

  const edgeBuffer = gl.createBuffer();
  gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, edgeBuffer);
  gl.bufferData(gl.ELEMENT_ARRAY_BUFFER, edgeIndices, gl.STATIC_DRAW);
  gl.useProgram(program);
  gl.bindBuffer(gl.ARRAY_BUFFER, positionBuffer);
  gl.enableVertexAttribArray(positionLocation);
  gl.vertexAttribPointer(positionLocation, 3, gl.FLOAT, false, 0, 0);
  gl.enable(gl.DEPTH_TEST);

  function render(timestamp) {
    animationFrame = null;
    if (!canRender()) { previousFrameTime = null; return; }
    resize();
    if (autoRotate && previousFrameTime !== null) rotationY += Math.min(timestamp - previousFrameTime, 50) * .00021;
    previousFrameTime = timestamp;
    gl.clearColor(.043, .063, .086, 1);
    gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);
    const projection = perspective(Math.PI / 4.2, canvas.width / canvas.height, .1, 100);
    let view = translate(0, .35, -7.2);
    view = multiply(view, rotateX(rotationX));
    view = multiply(view, rotateY(rotationY));
    const viewProjection = multiply(projection, view);
    parts.forEach((part) => drawPart(part, viewProjection));
    if (autoRotate) requestRender();
  }

  canvas.addEventListener('pointerdown', (event) => {
    dragging = true;
    autoRotate = false;
    document.querySelector('#twin-rotate').textContent = '자동 회전 OFF';
    document.querySelector('#twin-rotate').classList.remove('is-active');
    document.querySelector('#twin-rotate').setAttribute('aria-pressed', 'false');
    lastX = event.clientX;
    lastY = event.clientY;
    canvas.setPointerCapture(event.pointerId);
    requestRender();
  });
  canvas.addEventListener('pointermove', (event) => {
    if (!dragging) return;
    rotationY += (event.clientX - lastX) * .009;
    rotationX += (event.clientY - lastY) * .006;
    rotationX = Math.max(-1.1, Math.min(.8, rotationX));
    lastX = event.clientX;
    lastY = event.clientY;
    requestRender();
  });
  canvas.addEventListener('pointerup', () => { dragging = false; });
  canvas.addEventListener('pointercancel', () => { dragging = false; });
  canvas.addEventListener('lostpointercapture', () => { dragging = false; });

  document.querySelector('#twin-reset').addEventListener('click', () => {
    rotationX = -.22;
    rotationY = -.6;
    requestRender();
  });
  document.querySelector('#twin-rotate').addEventListener('click', (event) => {
    autoRotate = !autoRotate;
    event.currentTarget.textContent = `자동 회전 ${autoRotate ? 'ON' : 'OFF'}`;
    event.currentTarget.classList.toggle('is-active', autoRotate);
    event.currentTarget.setAttribute('aria-pressed', String(autoRotate));
    previousFrameTime = null;
    requestRender();
  });

  window.addEventListener('preview:view', requestRender);
  window.addEventListener('preview:state', requestRender);
  document.addEventListener('visibilitychange', requestRender);
  window.addEventListener('resize', requestRender);
  reducedMotion.addEventListener('change', () => {
    if (reducedMotion.matches) {
      autoRotate = false;
      document.querySelector('#twin-rotate').textContent = '자동 회전 OFF';
      document.querySelector('#twin-rotate').classList.remove('is-active');
      document.querySelector('#twin-rotate').setAttribute('aria-pressed', 'false');
    }
    requestRender();
  });
  if ('IntersectionObserver' in window) new IntersectionObserver(entries => {
    inViewport = entries[0].isIntersecting;
    requestRender();
  }).observe(canvas);
  if ('ResizeObserver' in window) new ResizeObserver(requestRender).observe(canvas);
  canvas.addEventListener('webglcontextlost', event => {
    event.preventDefault(); contextLost = true; requestRender();
    fallback.textContent = '3D 표시 연결이 끊겼습니다. 새로고침하면 다시 초기화합니다.';
    fallback.hidden = false;
  });
  requestRender();
}
