// 관제 웹 사건 피드 검증 (WBS 4.6.4 · FR-4.5).
//
// 이 파일이 지키는 것은 셋이다 — **사건은 한 건씩 전부 넘긴다**,
// **버퍼에서 놓친 것은 서버가 보낸 개수 그대로 알린다**,
// **끊기면 스스로 다시 붙고 멈추면 붙지 않는다**.
//
// 소켓을 주입하므로 서버도 브라우저도 필요 없다.

import assert from 'node:assert/strict';
import test, {afterEach} from 'node:test';

import {EventFeed, RETRY_MS, decodeEventMessage} from '../static/event-feed.js';
import {Operations} from '../static/operations.js';

class FakeSocket {
  static made = [];
  constructor(url) {
    this.url = url;
    this.closed = false;
    FakeSocket.made.push(this);
  }
  close() {
    this.closed = true;
  }
}

function event(seq, extra = {}) {
  return JSON.stringify({
    type: 'event', seq, event: 'person_found', ts_ms: 1000 + seq,
    state: 'OBSERVE', escalation: 'L1', tracks: [], detections: [],
    telemetry: {device_id: 'MD-01'}, entry: 'dir', snapshot: 'snapshot.jpg',
    ...extra,
  });
}

function startFeed(overrides = {}) {
  FakeSocket.made = [];
  const events = [], gaps = [], statuses = [];
  const feed = new EventFeed({
    url: 'ws://127.0.0.1:8000/ws/events',
    onEvent: (event) => events.push(event),
    onGap: (dropped) => gaps.push(dropped),
    onStatus: (status) => statuses.push(status),
    WebSocket: FakeSocket,
    ...overrides,
  });
  feed.start();
  activeFeeds.push(feed);
  return {feed, events, gaps, statuses, socket: () => FakeSocket.made.at(-1)};
}

const activeFeeds = [];
afterEach(() => {
  for (const feed of activeFeeds.splice(0)) feed.stop();
});

// ── 메시지 형식 ─────────────────────────────────────────────────

test('an event message becomes a normalized event; a gap becomes a count', () => {
  const parsed = decodeEventMessage(event(7, {snapshot: null}));
  assert.equal(parsed.kind, 'event');
  assert.equal(parsed.seq, 7);
  assert.equal(parsed.event, 'person_found');
  assert.equal(parsed.snapshot, null);
  const gap = decodeEventMessage(JSON.stringify({type: 'event_gap', dropped: 4}));
  assert.deepEqual(gap, {kind: 'gap', dropped: 4});
});

test('malformed messages are refused, not guessed at', () => {
  assert.throws(() => decodeEventMessage(new ArrayBuffer(2)));
  assert.throws(() => decodeEventMessage('not json'));
  assert.throws(() => decodeEventMessage(JSON.stringify({type: 'telemetry', seq: 1})));
  assert.throws(() => decodeEventMessage(JSON.stringify({type: 'event', seq: 'x', event: 'e', state: 's', escalation: 'L1', ts_ms: 1})));
  assert.throws(() => decodeEventMessage(JSON.stringify({type: 'event', seq: 1, event: ' ', state: 's', escalation: 'L1', ts_ms: 1})));
  assert.throws(() => decodeEventMessage(JSON.stringify({type: 'event_gap', dropped: 0})));
});

// ── 연결과 전달 ─────────────────────────────────────────────────

test('connecting is not live; opening the socket is, and events pass one by one', () => {
  const {feed, events, statuses, socket} = startFeed();
  assert.equal(socket().url, 'ws://127.0.0.1:8000/ws/events');
  assert.equal(statuses.at(-1).state, 'connecting');
  socket().onopen();
  assert.equal(statuses.at(-1).state, 'live');
  socket().onmessage({data: event(1)});
  socket().onmessage({data: event(2)});
  // 사건은 합치지 않는다 — 두 건 전부 순서대로 넘긴다.
  assert.deepEqual(events.map((e) => e.seq), [1, 2]);
  assert.equal(feed.received, 2);
});

test('a gap is reported with the server count, malformed input only increments a counter', () => {
  const {feed, events, gaps, statuses, socket} = startFeed();
  socket().onopen();
  socket().onmessage({data: 'garbage'});
  socket().onmessage({data: JSON.stringify({type: 'event_gap', dropped: 6})});
  assert.equal(feed.malformed, 1);
  assert.deepEqual(events, []);
  assert.deepEqual(gaps, [6]);
  assert.equal(statuses.at(-1).dropped, 6);
});

test('a dropped connection reconnects; stopping does not', (t) => {
  t.mock.timers.enable({apis: ['setInterval', 'setTimeout']});
  const {feed, socket, statuses} = startFeed();
  const first = socket();
  first.onclose();
  assert.equal(statuses.at(-1).state, 'closed');
  t.mock.timers.tick(RETRY_MS);
  assert.equal(FakeSocket.made.length, 2);
  assert.equal(statuses.at(-1).state, 'connecting');

  feed.stop();
  const second = socket();
  assert.equal(second.closed, true);
  second.onclose();
  t.mock.timers.tick(RETRY_MS * 3);
  assert.equal(FakeSocket.made.length, 2);
});

// ── 목록 반영 ───────────────────────────────────────────────────

test('live events land in the review list once, capped, without touching demo events', () => {
  const store = new Operations();
  const payload = {seq: 9, event: 'person_found', ts_ms: 5000, state: 'OBSERVE', escalation: 'L2',
    tracks: [{track_id: 1, box: [0, 0, 9, 9], score: 0.8}], detections: [], telemetry: {device_id: 'MD-02'},
    entry: '20260914_x', snapshot: 'snapshot.jpg'};
  const event = store.ingestLiveEvent(payload);
  assert.equal(event.id, 'LIVE-9');
  assert.equal(event.robot, 'MD-02');
  assert.equal(event.review, 'pending');
  // 재수신(백로그 재전송)은 같은 사건을 두 번 만들지 않는다.
  assert.equal(store.ingestLiveEvent(payload), event);
  assert.equal(store.events.filter((e) => e.id === 'LIVE-9').length, 1);
  // 스냅샷은 파일명만 온다 — 없는 그림을 있는 척하지 않는다.
  assert.equal(event.snapshot, null);
  assert.equal(event.meta.tracks[0].track_id, 1);
  store.setEventFeed({state: 'live', received: 1, dropped: 0});
  assert.equal(store.liveFeed.state, 'live');
  store.noteEventGap(5);
  assert.match(store.records[0].detail, /5건/);
});

test('the live buffer drops the oldest live event, never demo or imported rows', () => {
  const store = new Operations();
  const live = (seq) => store.ingestLiveEvent({seq, event: 'person_found', ts_ms: seq, state: 'S', escalation: 'L1', tracks: [], detections: [], telemetry: null});
  for (let seq = 1; seq <= 101; seq++) live(seq);
  assert.equal(store.events.filter((e) => e.source === 'LIVE_FEED').length, 100);
  assert.equal(store.events.some((e) => e.id === 'LIVE-1'), false);
  assert.equal(store.events.some((e) => e.id === 'LIVE-101'), true);
  assert.equal(store.events.filter((e) => e.source === 'DEMO').length, 3);
});
