// 관제 웹 텔레메트리 — `/ws/telemetry` 를 받아 상태 게이지와 추이를 채운다 (WBS 4.6.2 · FR-4.2).
//
// ⚠️ **메시지 수로 수신률을 세지 않는다.** 서버는 로봇 수신이 끊겨도 마지막 값을
// 초당 10번 계속 보낸다(표시 갱신률 · DASHBOARD.md). 메시지를 세면 로봇이 꺼져
// 있어도 10Hz 로 보인다. **`telemetry.seq` 가 새로 올라온 것만** 새 측정으로 센다.
//
// ⚠️ **`stale` 인 값을 정상처럼 그리지 않는다.** 마지막 관측은 남기되 화면이
// "수신 끊김" 을 말하게 한다 — 멈춘 배터리 숫자가 살아 있는 값처럼 보이면 안 된다.
//
// ⚠️ **링크 RTT 는 재지 않는다.** 서버가 `link_rtt_ms: null` 로 보낸다 — 로봇 uptime 과
// PC 시계는 맞춰져 있지 않아 둘을 빼면 거짓 숫자가 된다. 대신 **마지막 수신 경과**와
// **새 seq 기준 수신률 · 누락 수**를 보여 준다.

export const RETRY_MS = 2000;
export const RATE_WINDOW_MS = 3000;
export const HISTORY_MS = 60000;

const isNumber = (value) => typeof value === 'number' && Number.isFinite(value);

/** 운용 모드의 사람 이름 (FR-11.1). ⚠️ **모르는 이름을 지어내지 않는다** —
 * 서버가 새 모드를 보내면 원문 그대로 보이게 두고, 화면이 뜻을 추측하지 않는다. */
export const MODE_NAMES = Object.freeze({
  guard: '경비 모드',
  factory: '공장 모드',
  assist: '현장지원 모드',
});

/** 텍스트 메시지 하나 → 화면이 쓰는 스냅샷. 형식이 틀리면 던진다. */
export function decodeTelemetryMessage(data) {
  if (typeof data !== 'string') throw new Error('텔레메트리 메시지는 JSON 텍스트여야 합니다.');
  const message = JSON.parse(data);
  if (message?.type !== 'telemetry' || typeof message.device_id !== 'string') {
    throw new Error('텔레메트리 메시지 형식이 올바르지 않습니다.');
  }
  const raw = message.telemetry;
  let telemetry = null;
  if (raw != null) {
    const imu = raw.imu ?? {};
    const flags = raw.flags ?? {};
    if (
      !Number.isInteger(raw.seq) ||
      typeof raw.boot_id !== 'string' ||
      !isNumber(raw.batt_v) ||
      !isNumber(raw.dist_cm) ||
      ![imu.pitch, imu.roll, imu.yaw].every(isNumber) ||
      typeof flags !== 'object'
    ) {
      throw new Error('텔레메트리 본문 형식이 올바르지 않습니다.');
    }
    telemetry = {
      deviceId: typeof raw.device_id === 'string' ? raw.device_id : null,
      bootId: raw.boot_id,
      seq: raw.seq,
      state: typeof raw.state === 'string' ? raw.state : null,
      battV: raw.batt_v,
      distCm: raw.dist_cm,
      imu: { pitch: imu.pitch, roll: imu.roll, yaw: imu.yaw },
      lastCmdAgeMs: isNumber(raw.last_cmd_age_ms) ? raw.last_cmd_age_ms : null,
      safetyLatched: raw.safety_latched === true,
      flags: {
        lowbatt: flags.lowbatt === true,
        tipped: flags.tipped === true,
        obstacle: flags.obstacle === true,
        linkOk: flags.link_ok === true,
        // 선택 필드다 — 확장 이전 펌웨어는 보내지 않는다. **없는 것을 '꺼짐' 으로 읽지 않는다.**
        service: typeof flags.service === 'boolean' ? flags.service : null,
      },
    };
  }
  return {
    deviceId: message.device_id,
    state: typeof message.state === 'string' ? message.state : null,
    escalation: typeof message.escalation === 'string' ? message.escalation : null,
    // 운용 모드 (FR-4.7 · FR-11.5). ⚠️ **없는 것을 기본값으로 읽지 않는다** —
    // 모드를 모르는 채 «경비» 라고 그리면 공장 순찰을 경비로 착각한다.
    mode: typeof message.mode === 'string' ? message.mode : null,
    telemetry,
    ageMs: isNumber(message.telemetry_age_ms) ? message.telemetry_age_ms : null,
    stale: message.stale !== false,
    runtimeStale: message.runtime_stale !== false,
  };
}

/**
 * 새 seq 만 세서 수신률·누락과 최근 60초 추이를 만든다. 시각은 PC 쪽 도착 시각이다.
 * 부팅이 바뀌면(`boot_id`) seq 가 1 부터 다시 시작하므로 창을 비운다.
 */
export class TelemetryTracker {
  constructor({ rateWindowMs = RATE_WINDOW_MS, historyMs = HISTORY_MS } = {}) {
    this.rateWindowMs = rateWindowMs;
    this.historyMs = historyMs;
    this.bootId = null;
    this.lastSeq = null;
    this.firstSeenMs = null;
    this.samples = [];
    this.history = [];
  }

  /** 스냅샷 하나를 넣고 새 측정이었는지 돌려준다. */
  push(snapshot, nowMs) {
    const telemetry = snapshot.telemetry;
    this.prune(nowMs);
    if (!telemetry) return false;
    if (telemetry.bootId !== this.bootId) {
      this.bootId = telemetry.bootId;
      this.lastSeq = null;
      this.samples = [];
      this.firstSeenMs = nowMs;
    }
    // 같은 seq 가 다시 온 것은 서버의 표시 갱신이지 새 측정이 아니다.
    if (telemetry.seq === this.lastSeq) return false;
    this.lastSeq = telemetry.seq;
    this.samples.push({ t: nowMs, seq: telemetry.seq });
    this.history.push({ t: nowMs, battV: telemetry.battV, distCm: telemetry.distCm });
    return true;
  }

  prune(nowMs) {
    while (this.samples.length && nowMs - this.samples[0].t > this.rateWindowMs) this.samples.shift();
    while (this.history.length && nowMs - this.history[0].t > this.historyMs) this.history.shift();
  }

  /** 초당 새 seq 수. 관측이 1초 미만이면 아직 말하지 않는다(`null`). */
  rateHz(nowMs) {
    this.prune(nowMs);
    if (this.firstSeenMs == null) return null;
    const span = Math.min(this.rateWindowMs, nowMs - this.firstSeenMs);
    if (span < 1000) return null;
    return this.samples.length / (span / 1000);
  }

  /** 창 안에서 seq 가 건너뛴 수 — 로봇이 보냈는데 PC 가 못 받은 것. */
  lost(nowMs) {
    this.prune(nowMs);
    if (this.samples.length < 2) return 0;
    const expected = this.samples.at(-1).seq - this.samples[0].seq + 1;
    return Math.max(0, expected - this.samples.length);
  }
}

const ago = (ms) => (ms == null ? '—' : ms < 1000 ? `${Math.round(ms)} ms 전` : `${(ms / 1000).toFixed(1)} s 전`);
const deg = (value) => `${value.toFixed(1)}°`;

/**
 * 화면 문구. 하단 상태 칸과 장치 화면의 게이지가 **같은 판단**을 쓰도록 한 곳에 둔다.
 *
 * `tone`: `waiting`(채널 연결 중·로봇 미수신) · `live` · `stale`(로봇 수신 끊김) · `closed`(채널 끊김)
 */
export function describeTelemetry(view) {
  const snapshot = view?.snapshot ?? null;
  const telemetry = snapshot?.telemetry ?? null;
  const channel = view?.state ?? 'off';
  let tone = 'waiting';
  if (channel === 'closed') tone = 'closed';
  else if (channel === 'live' && telemetry) tone = snapshot.stale ? 'stale' : 'live';

  const rate = view?.rateHz == null ? '수신률 측정 중' : `수신 ${view.rateHz.toFixed(1)} Hz`;
  const lost = view?.lost ? ` · 누락 ${view.lost}` : '';
  const alerts = [];
  if (telemetry?.safetyLatched) alerts.push('안전 잠금');
  if (telemetry?.flags.lowbatt) alerts.push('저전압');
  if (telemetry?.flags.tipped) alerts.push('전도');
  if (telemetry?.flags.obstacle) alerts.push('근거리 정지');
  if (snapshot && telemetry && snapshot.runtimeStale) alerts.push('운용 루프 멈춤');

  let headline;
  let summary;
  if (channel === 'closed') {
    headline = '상태 채널 끊김 · 다시 연결 중';
    summary = '영상·사건·명령은 따로 연결됩니다.';
  } else if (channel !== 'live') {
    headline = '관제 서버 연결됨 · 상태 채널 연결 중';
    summary = '영상·사건·명령은 따로 연결됩니다.';
  } else if (!telemetry) {
    headline = '관제 서버 연결됨 · 로봇 상태 미수신';
    summary = '로봇에서 받은 상태가 아직 없습니다.';
  } else {
    const values = `배터리 ${telemetry.battV.toFixed(2)} V · 거리 ${Math.round(telemetry.distCm)} cm`;
    const flagged = alerts.length ? ` · ${alerts.join(' · ')}` : '';
    if (snapshot.stale) {
      headline = `로봇 수신 끊김 · ${ago(snapshot.ageMs)}`;
      summary = `마지막 값 — ${values}${flagged}. 지금 상태로 판단하지 마세요.`;
    } else {
      headline = `${snapshot.deviceId} · ${MODE_NAMES[snapshot.mode] ?? '모드 미수신'} · ${snapshot.state ?? '기동 전'} · ${snapshot.escalation ?? '—'}`;
      summary = `${values} · ${rate}${lost}${flagged}`;
    }
  }

  const rows = telemetry
    ? [
        ['연결 · 마지막 수신', `${tone === 'live' ? '수신 중' : '끊김'} / ${ago(snapshot.ageMs)}`],
        ['실제 device_id', `${snapshot.deviceId} · 펌웨어 ${telemetry.deviceId ?? '미상'}`],
        ['운용 모드', MODE_NAMES[snapshot.mode] ?? '미수신 — 판단 규칙을 알 수 없음'],
        ['FSM · 대응 단계', `${snapshot.state ?? '기동 전'} / ${snapshot.escalation ?? '—'} · 온보드 ${telemetry.state ?? '—'}`],
        ['배터리 전압', `${telemetry.battV.toFixed(2)} V${telemetry.flags.lowbatt ? ' · 저전압 (로봇 판정)' : ''}`],
        ['전방 거리', `${Math.round(telemetry.distCm)} cm${telemetry.flags.obstacle ? ' · 근거리 정지' : ''}`],
        ['IMU pitch / roll / yaw', `${deg(telemetry.imu.pitch)} / ${deg(telemetry.imu.roll)} / ${deg(telemetry.imu.yaw)}${telemetry.flags.tipped ? ' · 전도' : ''}`],
        ['링크 · 안전 래치', `${rate}${lost} / ${telemetry.safetyLatched ? '잠김' : '해제'}`],
        ['마지막 명령 수락 나이', telemetry.lastCmdAgeMs == null ? '— · 미수신' : `${Math.round(telemetry.lastCmdAgeMs)} ms · 로봇 기준`],
        ['링크 지연 (RTT)', '미측정 — 로봇과 PC 시계가 달라 빼지 않는다'],
      ]
    : null;
  const badge = { live: ['실시간', ''], stale: ['수신 끊김', 'amber'], closed: ['채널 끊김', 'amber'], waiting: ['연결 대기', ''] }[tone];
  return { tone, headline, summary, rows, badge, alerts, age: telemetry ? ago(snapshot.ageMs) : '—' };
}

/**
 * 상태: `connecting` → `live`(첫 메시지 수신), 끊기면 `closed` 후 `RETRY_MS` 뒤
 * 다시 연결한다. 로봇 쪽 끊김은 연결 상태가 아니라 스냅샷의 `stale` 이 말한다.
 */
export class TelemetryFeed {
  /**
   * @param {object} options
   * @param {string} options.url `ws://…/ws/telemetry`
   * @param {(view:object)=>void} options.onUpdate 상태가 바뀌거나 메시지가 올 때마다
   * @param {typeof WebSocket} options.WebSocket 주입해서 시험한다.
   * @param {()=>number} options.now 주입해서 시험한다.
   */
  constructor({ url, onUpdate = () => {}, WebSocket: Socket = globalThis.WebSocket, now = () => Date.now() }) {
    this.url = url;
    this.onUpdate = onUpdate;
    this.Socket = Socket;
    this.now = now;
    this.tracker = new TelemetryTracker();
    this.state = 'idle';
    this.socket = null;
    this.stopped = true;
    this.snapshot = null;
    this.malformed = 0;
  }

  start() {
    if (!this.stopped) return;
    this.stopped = false;
    this.connect();
  }

  stop() {
    this.stopped = true;
    clearTimeout(this.retry);
    const socket = this.socket;
    this.socket = null;
    socket?.close();
  }

  connect() {
    const socket = new this.Socket(this.url);
    this.socket = socket;
    this.report('connecting');
    socket.onmessage = (event) => {
      if (this.socket === socket) this.receive(event.data);
    };
    // 오류 뒤에는 close 가 따라온다 — 재연결은 close 한 곳에서만 건다.
    socket.onerror = () => {};
    socket.onclose = () => {
      if (this.socket !== socket) return;
      this.socket = null;
      if (this.stopped) return;
      this.report('closed');
      this.retry = setTimeout(() => this.connect(), RETRY_MS);
      this.retry.unref?.();
    };
  }

  receive(data) {
    let snapshot;
    try {
      snapshot = decodeTelemetryMessage(data);
    } catch {
      this.malformed += 1;
      return;
    }
    this.snapshot = snapshot;
    this.tracker.push(snapshot, this.now());
    this.report('live');
  }

  report(state) {
    this.state = state;
    const now = this.now();
    this.onUpdate({
      state,
      snapshot: this.snapshot,
      rateHz: this.tracker.rateHz(now),
      lost: this.tracker.lost(now),
      history: this.tracker.history.slice(),
      malformed: this.malformed,
    });
  }
}
