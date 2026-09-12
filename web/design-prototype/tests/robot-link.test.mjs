// 관제 웹 → 대시보드 명령 API 연결 검증 (WBS 4.6.3 · FR-4.3/4.4).
//
// 이 파일이 지키는 것은 둘이다 — **링크가 없으면 아무것도 나가지 않는다**(예시
// 모드가 기본), 그리고 **링크가 있으면 비상정지가 조건 없이 나간다**.
//
// fetch 를 주입하므로 서버도 로봇도 필요 없다.

import assert from 'node:assert/strict';
import test from 'node:test';

import { Operations } from '../operations.js';
import { RobotLink, motionFor } from '../robot-link.js';

/** 호출을 기록하는 가짜 fetch. `fail` 을 주면 그 경로만 실패시킨다. */
function fakeFetch({ fail = null, status = 200 } = {}) {
  const calls = [];
  const impl = async (url, init) => {
    calls.push({ url, body: JSON.parse(init.body), method: init.method });
    if (fail && String(url).includes(fail)) throw new Error('네트워크 끊김');
    return {
      ok: status >= 200 && status < 300,
      status,
      json: async () => ({ accepted: true, state: 'MANUAL' }),
    };
  };
  impl.calls = calls;
  return impl;
}

function liveOps(fetchImpl) {
  const link = new RobotLink({ baseUrl: 'http://127.0.0.1:8000', fetch: fetchImpl });
  const ops = new Operations({ link });
  ops.setDemo(false); // 실제 연결 모드
  return ops;
}

// ── 방향 → 보폭·조향 ─────────────────────────────────────────────

test('direction names become step and angle once, in one place', () => {
  assert.deepEqual(motionFor('FORWARD'), { step: 60, angle: 0 });
  assert.deepEqual(motionFor('BACKWARD'), { step: -60, angle: 0 });
  assert.deepEqual(motionFor('STOP'), { step: 0, angle: 0 });
});

test('left turns positive and right turns negative (PROTOCOL sign rule)', () => {
  // 양수가 반시계 = 좌회전. 뒤집으면 조이스틱이 반대로 움직인다.
  assert.ok(motionFor('LEFT').angle > 0);
  assert.ok(motionFor('RIGHT').angle < 0);
  assert.equal(motionFor('LEFT').angle, -motionFor('RIGHT').angle);
});

test('an unknown direction is refused', () => {
  assert.throws(() => motionFor('UP'), /허용되지 않은 방향/);
});

test('motion magnitudes are configurable', () => {
  assert.deepEqual(motionFor('LEFT', { step: 40, angle: 12 }), { step: 40, angle: 12 });
});

// ── 링크가 없으면 아무것도 나가지 않는다 ─────────────────────────

test('without a link the web stays preview-only', () => {
  const ops = new Operations({});
  assert.equal(ops.live, false);
  ops.claim();
  ops.move('FORWARD');
  assert.match(ops.records[0].detail, /전송 안 함/);
});

test('without a link, leaving preview mode still blocks control', () => {
  const ops = new Operations({});
  ops.setDemo(false);
  assert.throws(() => ops.claim(), /실제 제어는 연결되지 않았습니다/);
});

// ── 링크가 있으면 실제로 나간다 ──────────────────────────────────

test('claiming control asks the server for MANUAL', async () => {
  const fetchImpl = fakeFetch();
  const ops = liveOps(fetchImpl);
  ops.claim();
  await new Promise((resolve) => setImmediate(resolve));
  const call = fetchImpl.calls.at(-1);
  assert.match(call.url, /\/api\/command\/manual$/);
  assert.deepEqual(call.body, { on: true });
});

test('a direction command reaches the drive endpoint', async () => {
  const fetchImpl = fakeFetch();
  const ops = liveOps(fetchImpl);
  ops.claim();
  ops.move('LEFT');
  await new Promise((resolve) => setImmediate(resolve));
  const call = fetchImpl.calls.at(-1);
  assert.match(call.url, /\/api\/command\/drive$/);
  assert.ok(call.body.angle > 0);
});

test('releasing control clears MANUAL on the server too', async () => {
  const fetchImpl = fakeFetch();
  const ops = liveOps(fetchImpl);
  ops.claim();
  ops.release('시험');
  await new Promise((resolve) => setImmediate(resolve));
  const manual = fetchImpl.calls.filter((c) => c.url.endsWith('/manual'));
  assert.deepEqual(manual.at(-1).body, { on: false });
});

test('stop is sent even when the web already believes it is stopped', async () => {
  // 웹이 STOP 이라고 믿는 것과 로봇이 실제로 선 것은 다른 일이다.
  const fetchImpl = fakeFetch();
  const ops = liveOps(fetchImpl);
  ops.stop('시험');
  await new Promise((resolve) => setImmediate(resolve));
  const drive = fetchImpl.calls.filter((c) => c.url.endsWith('/drive'));
  assert.deepEqual(drive.at(-1).body, { step: 0, angle: 0 });
});

// ── 비상정지는 조건을 검사하지 않는다 ────────────────────────────

test('estop goes out without control authority', async () => {
  const fetchImpl = fakeFetch();
  const ops = liveOps(fetchImpl);
  assert.equal(ops.control, null); // 제어권을 잡지 않았다
  ops.requestEstop();
  await new Promise((resolve) => setImmediate(resolve));
  assert.ok(fetchImpl.calls.some((c) => c.url.endsWith('/estop')));
});

test('estop goes out for a non-operator role as well', async () => {
  const fetchImpl = fakeFetch();
  const ops = liveOps(fetchImpl);
  ops.setRole('reviewer');
  ops.requestEstop();
  await new Promise((resolve) => setImmediate(resolve));
  assert.ok(fetchImpl.calls.some((c) => c.url.endsWith('/estop')));
});

test('estop goes out again even when already latched', async () => {
  const fetchImpl = fakeFetch();
  const ops = liveOps(fetchImpl);
  ops.requestEstop();
  ops.requestEstop();
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(fetchImpl.calls.filter((c) => c.url.endsWith('/estop')).length, 2);
});

// ── 실패를 삼키지 않는다 ─────────────────────────────────────────

test('a failed estop is recorded, not swallowed', async () => {
  const ops = liveOps(fakeFetch({ fail: '/estop' }));
  ops.requestEstop();
  await new Promise((resolve) => setImmediate(resolve));
  assert.match(ops.linkError, /비상정지/);
  assert.ok(ops.records.some((r) => r.action === '전송 실패'));
});

test('a failed manual entry does not pretend control was taken', async () => {
  const ops = liveOps(fakeFetch({ fail: '/manual' }));
  ops.claim();
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(ops.control, null);
  assert.match(ops.linkError, /수동 진입/);
});

test('an HTTP rejection is surfaced', async () => {
  const ops = liveOps(fakeFetch({ status: 403 }));
  ops.claim();
  await new Promise((resolve) => setImmediate(resolve));
  assert.match(ops.linkError, /403/);
});

// ── 링크 자체 ────────────────────────────────────────────────────

test('a link without fetch is refused at construction', () => {
  assert.throws(() => new RobotLink({ fetch: null }), /fetch 구현이 필요/);
});

test('a trailing slash in the base url does not double up', async () => {
  const fetchImpl = fakeFetch();
  await new RobotLink({ baseUrl: 'http://host:8000/', fetch: fetchImpl }).estop();
  assert.equal(fetchImpl.calls[0].url, 'http://host:8000/api/command/estop');
});
