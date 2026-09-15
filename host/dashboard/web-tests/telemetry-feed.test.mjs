// 관제 웹 텔레메트리 검증 (WBS 4.6.2 · FR-4.2).
//
// 이 파일이 지키는 것은 셋이다 — **같은 seq 의 반복을 새 측정으로 세지 않는다**,
// **끊긴 값을 살아 있는 값처럼 말하지 않는다**, **RTT 를 지어내지 않는다**.
//
// 소켓과 시계를 주입하므로 서버도 로봇도 필요 없다.

import assert from 'node:assert/strict';
import test from 'node:test';

import {
  RETRY_MS,
  TelemetryFeed,
  TelemetryTracker,
  decodeTelemetryMessage,
  describeTelemetry,
} from '../static/telemetry-feed.js';

/** 서버 `/ws/telemetry` 와 같은 모양. */
function message({ seq = 1, boot = 'boot-a', batt = 7.8, dist = 120, stale = false, age = 50, telemetry = true, ...rest } = {}) {
  return JSON.stringify({
    type: 'telemetry',
    device_id: 'mechdog-01',
    state: 'PATROL',
    escalation: 'L0',
    telemetry: telemetry
      ? {
          device_id: 'mechdog-3c8a1f333208',
          boot_id: boot,
          seq,
          state: 'IDLE',
          batt_v: batt,
          dist_cm: dist,
          imu: { pitch: 1.25, roll: -0.5, yaw: 90 },
          last_cmd_age_ms: 73,
          safety_latched: false,
          flags: { lowbatt: false, tipped: false, link_ok: true, obstacle: false },
          ...rest,
        }
      : null,
    telemetry_age_ms: telemetry ? age : null,
    stale,
    runtime_age_ms: 20,
    runtime_stale: false,
    link_rtt_ms: null,
  });
}

// ── 메시지 형식 ─────────────────────────────────────────────────

test('a snapshot keeps host FSM, onboard state and sensor values apart', () => {
  const snap = decodeTelemetryMessage(message({ seq: 7, batt: 7.91, dist: 45 }));
  assert.equal(snap.deviceId, 'mechdog-01');
  assert.equal(snap.state, 'PATROL');
  assert.equal(snap.telemetry.state, 'IDLE');
  assert.equal(snap.telemetry.seq, 7);
  assert.equal(snap.telemetry.battV, 7.91);
  assert.equal(snap.telemetry.distCm, 45);
  assert.deepEqual(snap.telemetry.imu, { pitch: 1.25, roll: -0.5, yaw: 90 });
  assert.equal(snap.stale, false);
});

test('before the robot speaks, telemetry is null and the snapshot counts as stale', () => {
  const snap = decodeTelemetryMessage(message({ telemetry: false, stale: true }));
  assert.equal(snap.telemetry, null);
  assert.equal(snap.stale, true);
});

test('malformed telemetry is refused, not guessed at', () => {
  assert.throws(() => decodeTelemetryMessage('{"type":"event"}'));
  assert.throws(() => decodeTelemetryMessage(message({ batt_v: 'full' })));
  assert.throws(() => decodeTelemetryMessage(message({ imu: { pitch: 1, roll: NaN, yaw: 0 } })));
  assert.throws(() => decodeTelemetryMessage(new ArrayBuffer(4)));
});

// ── 수신률 · 누락 ───────────────────────────────────────────────

test('the server repeating the same seq at 10Hz is not reception', () => {
  const tracker = new TelemetryTracker();
  const snap = decodeTelemetryMessage(message({ seq: 5 }));
  assert.equal(tracker.push(snap, 0), true);
  for (let t = 100; t <= 3000; t += 100) assert.equal(tracker.push(snap, t), false);
  // 3초 동안 메시지는 30개 왔지만 새 측정은 첫 하나뿐이고, 창 밖으로 밀려 수신률은 0 이다.
  assert.equal(tracker.rateHz(3100), 0);
});

test('ten new seqs per second read as 10 Hz, and a gap counts as lost', () => {
  const tracker = new TelemetryTracker();
  let seq = 1;
  for (let t = 0; t <= 3000; t += 100) {
    if (seq !== 15) tracker.push(decodeTelemetryMessage(message({ seq })), t); // 15 번은 오지 않았다
    seq += 1;
  }
  assert.ok(Math.abs(tracker.rateHz(3000) - 10) <= 0.4, `rate ${tracker.rateHz(3000)}`);
  assert.equal(tracker.lost(3000), 1);
});

test('no rate is claimed from less than a second of observation', () => {
  const tracker = new TelemetryTracker();
  tracker.push(decodeTelemetryMessage(message({ seq: 1 })), 0);
  tracker.push(decodeTelemetryMessage(message({ seq: 2 })), 100);
  assert.equal(tracker.rateHz(500), null);
});

test('a reboot restarts seq without being read as a huge gap', () => {
  const tracker = new TelemetryTracker();
  for (let i = 0; i < 20; i += 1) tracker.push(decodeTelemetryMessage(message({ seq: 500 + i })), i * 100);
  tracker.push(decodeTelemetryMessage(message({ seq: 1, boot: 'boot-b' })), 2100);
  tracker.push(decodeTelemetryMessage(message({ seq: 2, boot: 'boot-b' })), 2200);
  assert.equal(tracker.lost(2200), 0);
});

test('history keeps only the last 60 seconds of new values', () => {
  const tracker = new TelemetryTracker();
  for (let i = 0; i < 700; i += 1) tracker.push(decodeTelemetryMessage(message({ seq: i + 1, batt: 8 - i / 1000 })), i * 100);
  assert.ok(tracker.history.length <= 601);
  assert.ok(tracker.history[0].t >= 69900 - 60000);
});

// ── 문구 ────────────────────────────────────────────────────────

const view = (overrides = {}) => ({ state: 'live', rateHz: 9.87, lost: 0, history: [], snapshot: decodeTelemetryMessage(message()), ...overrides });

test('live values say who, what state, and how well the link receives', () => {
  const text = describeTelemetry(view());
  assert.equal(text.tone, 'live');
  assert.equal(text.headline, 'mechdog-01 · PATROL · L0');
  assert.match(text.summary, /배터리 7\.80 V · 거리 120 cm · 수신 9\.9 Hz/);
  const rows = Object.fromEntries(text.rows);
  assert.equal(rows['배터리 전압'], '7.80 V');
  assert.equal(rows['IMU pitch / roll / yaw'], '1.3° / -0.5° / 90.0°');
  assert.match(rows['링크 지연 (RTT)'], /미측정/);
});

test('a stale robot is said to be cut off, with its last values marked as old', () => {
  const text = describeTelemetry(view({ snapshot: decodeTelemetryMessage(message({ stale: true, age: 3200 })) }));
  assert.equal(text.tone, 'stale');
  assert.equal(text.headline, '로봇 수신 끊김 · 3.2 s 전');
  assert.match(text.summary, /마지막 값/);
  assert.equal(Object.fromEntries(text.rows)['연결 · 마지막 수신'], '끊김 / 3.2 s 전');
  assert.deepEqual(text.badge, ['수신 끊김', 'amber']);
});

test('robot-side alarms are surfaced from its own flags, not recomputed from thresholds', () => {
  const snap = decodeTelemetryMessage(
    message({ safety_latched: true, flags: { lowbatt: true, tipped: false, link_ok: true, obstacle: true } }),
  );
  const text = describeTelemetry(view({ snapshot: snap }));
  assert.deepEqual(text.alerts, ['안전 잠금', '저전압', '근거리 정지']);
  assert.match(Object.fromEntries(text.rows)['배터리 전압'], /저전압 \(로봇 판정\)/);
});

test('connected but nothing from the robot yet is not shown as values', () => {
  const text = describeTelemetry(view({ snapshot: decodeTelemetryMessage(message({ telemetry: false, stale: true })) }));
  assert.equal(text.rows, null);
  assert.equal(text.headline, '관제 서버 연결됨 · 로봇 상태 미수신');
  assert.equal(describeTelemetry({ state: 'connecting', snapshot: null }).headline, '관제 서버 연결됨 · 상태 채널 연결 중');
});

// ── 연결 ────────────────────────────────────────────────────────

class FakeSocket {
  static made = [];
  constructor(url) {
    this.url = url;
    FakeSocket.made.push(this);
  }
  close() {
    this.closed = true;
  }
}

test('the feed goes live on the first message and reconnects after a drop, but not after stop', (t) => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  FakeSocket.made = [];
  const updates = [];
  let now = 0;
  const feed = new TelemetryFeed({ url: 'ws://127.0.0.1:8000/ws/telemetry', WebSocket: FakeSocket, now: () => now, onUpdate: (u) => updates.push(u) });
  feed.start();
  assert.equal(updates.at(-1).state, 'connecting');
  const socket = FakeSocket.made[0];
  socket.onmessage({ data: 'not json' });
  assert.equal(feed.malformed, 1);
  for (let seq = 1; seq <= 15; seq += 1) {
    socket.onmessage({ data: message({ seq }) });
    now += 100;
  }
  assert.equal(updates.at(-1).state, 'live');
  assert.equal(updates.at(-1).snapshot.telemetry.seq, 15);
  assert.equal(updates.at(-1).history.length, 15);
  assert.ok(updates.at(-1).rateHz > 9);

  socket.onclose();
  assert.equal(updates.at(-1).state, 'closed');
  t.mock.timers.tick(RETRY_MS);
  assert.equal(FakeSocket.made.length, 2);

  feed.stop();
  FakeSocket.made[1].onclose();
  t.mock.timers.tick(RETRY_MS * 3);
  assert.equal(FakeSocket.made.length, 2);
});
