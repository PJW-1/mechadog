// 관제 웹 FPV 검증 (WBS 4.6.1 · FR-4.1).
//
// 이 파일이 지키는 것은 셋이다 — **박스는 같은 메시지의 사진 위에만 그린다**,
// **밀리면 최신 한 장만 그린다**, **새 장면이 끊기면 실시간이라 하지 않는다**.
//
// 소켓·이미지 디코더·캔버스를 주입하므로 서버도 브라우저도 필요 없다.

import assert from 'node:assert/strict';
import test, {afterEach} from 'node:test';

import {
  RETRY_MS,
  STALE_AFTER_MS,
  VisionFeed,
  decodeVisionMessage,
  drawOverlay,
  overlayBoxes,
} from '../static/vision-feed.js';

/** 서버와 같은 형식 — `[헤더 길이 uint32 BE][JSON][JPEG]`. */
function message(header, jpeg = 'JPEG') {
  const head = new TextEncoder().encode(JSON.stringify({
    type: 'vision', width: 640, height: 480, frame_seq: 0, detections: [], tracks: [], ...header,
  }));
  const body = new TextEncoder().encode(jpeg);
  const bytes = new Uint8Array(4 + head.length + body.length);
  new DataView(bytes.buffer).setUint32(0, head.length, false);
  bytes.set(head, 4);
  bytes.set(body, 4 + head.length);
  return bytes.buffer;
}

/** 그리기 호출을 기록하는 가짜 2D 컨텍스트. */
function fakeCanvas() {
  const calls = [];
  const canvas = { width: 300, height: 150 };
  const context = {
    canvas,
    measureText: (text) => ({ width: text.length * 8 }),
    drawImage: (image, ...rect) => calls.push(['image', image, ...rect]),
    strokeRect: (...rect) => calls.push(['box', ...rect]),
    fillRect: () => {},
    fillText: (text) => calls.push(['text', text]),
  };
  canvas.getContext = () => context;
  return { canvas, calls };
}

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

/** 부를 때마다 멈춰 있다가 `resolveNext()` 로 하나씩 풀리는 디코더. 사진은 JPEG 본문 문자열이다. */
function manualDecoder() {
  const waiting = [];
  const decode = (blob) =>
    new Promise((resolve, reject) => waiting.push({ blob, resolve, reject }));
  decode.waiting = waiting;
  decode.resolveNext = async () => {
    const next = waiting.shift();
    next.resolve({ jpeg: await next.blob.text() });
    await new Promise((resolve) => setImmediate(resolve));
  };
  return decode;
}

function startFeed({ decodeImage = manualDecoder() } = {}) {
  FakeSocket.made = [];
  const { canvas, calls } = fakeCanvas();
  const statuses = [];
  const feed = new VisionFeed({
    url: 'ws://127.0.0.1:8000/ws/vision',
    canvas,
    onStatus: (status) => statuses.push(status),
    WebSocket: FakeSocket,
    decodeImage,
  });
  feed.start();
  activeFeeds.push(feed);
  return { feed, canvas, calls, statuses, decodeImage, socket: () => FakeSocket.made.at(-1) };
}

const activeFeeds = [];
afterEach(() => {
  for (const feed of activeFeeds.splice(0)) feed.stop();
});

const flush = () => new Promise((resolve) => setImmediate(resolve));

// ── 메시지 형식 ─────────────────────────────────────────────────

test('a message splits into its header and the JPEG it was computed on', () => {
  const { header, jpeg } = decodeVisionMessage(message({ frame_seq: 7, detections: [], tracks: [] }, 'JPEG-7'));
  assert.equal(header.frame_seq, 7);
  assert.equal(new TextDecoder().decode(jpeg), 'JPEG-7');
});

test('malformed messages are refused, not guessed at', () => {
  assert.throws(() => decodeVisionMessage(new ArrayBuffer(2)));
  const lying = new Uint8Array(message({}));
  new DataView(lying.buffer).setUint32(0, 10_000, false);
  assert.throws(() => decodeVisionMessage(lying.buffer));
  assert.throws(() => decodeVisionMessage(message({ type: 'telemetry', frame_seq: 1, detections: [], tracks: [] })));
  assert.throws(() => decodeVisionMessage(message({ frame_seq: 1, detections: [{ label: 'person', box: [0, 1, NaN, 3] }], tracks: [] })));
  assert.throws(() => decodeVisionMessage('text frame'));
});

// ── 무엇을 그리나 ───────────────────────────────────────────────

test('people are drawn once, by track id; other objects by label', () => {
  const boxes = overlayBoxes({
    tracks: [{ track_id: 3, score: 0.9, box: [10, 20, 110, 300] }],
    detections: [
      { label: 'person', score: 0.9, box: [10, 20, 110, 300] },
      { label: 'tv', score: 0.456, box: [300, 40, 500, 200] },
    ],
  });
  assert.deepEqual(boxes, [
    { kind: 'person', box: [10, 20, 110, 300], text: '사람 #3' },
    { kind: 'object', box: [300, 40, 500, 200], text: 'tv 46%' },
  ]);
});

test('the canvas takes the frame size and boxes stay in source pixels', () => {
  const { canvas, calls } = fakeCanvas();
  const image = { id: 'frame' };
  drawOverlay(canvas.getContext('2d'), image, {
    width: 640,
    height: 480,
    tracks: [{ track_id: 1, box: [10, 20, 110, 300] }],
    detections: [{ label: 'tv', score: 0.5, box: [300, 40, 500, 200] }],
  });
  assert.equal(canvas.width, 640);
  assert.equal(canvas.height, 480);
  // 사진이 먼저, 박스는 그 위에 — 좌표를 화면 크기로 환산하지 않는다.
  assert.deepEqual(calls[0], ['image', image, 0, 0, 640, 480]);
  assert.deepEqual(
    calls.filter((call) => call[0] === 'box'),
    [
      ['box', 10, 20, 100, 280],
      ['box', 300, 40, 200, 160],
    ],
  );
});

// ── 연결과 갱신 ─────────────────────────────────────────────────

test('connecting is not live; the first drawn frame is', async () => {
  const { socket, statuses, decodeImage } = startFeed();
  assert.equal(socket().url, 'ws://127.0.0.1:8000/ws/vision');
  assert.equal(socket().binaryType, 'arraybuffer');
  assert.equal(statuses.at(-1).state, 'connecting');
  socket().onopen();
  assert.equal(statuses.at(-1).state, 'waiting');
  socket().onmessage({ data: message({ frame_seq: 1, tracks: [{ track_id: 1, box: [0, 0, 1, 1] }] }) });
  await decodeImage.resolveNext();
  assert.deepEqual(
    { state: statuses.at(-1).state, frameSeq: statuses.at(-1).frameSeq, persons: statuses.at(-1).persons },
    { state: 'live', frameSeq: 1, persons: 1 },
  );
});

test('while a frame decodes, only the newest waiting frame is drawn next — each with its own boxes', async () => {
  const { socket, calls, decodeImage } = startFeed();
  const send = (seq) =>
    socket().onmessage({
      data: message({ frame_seq: seq, detections: [{ label: 'cup', score: 1, box: [seq, seq, seq + 5, seq + 5] }], tracks: [] }, `JPEG-${seq}`),
    });
  send(1);
  send(2);
  send(3);
  await decodeImage.resolveNext();
  await decodeImage.resolveNext();
  assert.equal(decodeImage.waiting.length, 0);
  const drawn = calls.filter((call) => call[0] !== 'text');
  // 1 을 디코드하는 사이 2·3 이 오면 1·2 는 그리지 않고 3 만 그린다.
  assert.deepEqual(drawn, [
    ['image', { jpeg: 'JPEG-3' }, 0, 0, 640, 480],
    ['box', 3, 3, 5, 5],
  ]);
});

test('a broken message or undecodable JPEG is skipped without stopping the feed', async () => {
  let fail = true;
  const decodeImage = async (blob) => {
    if (fail) throw new Error('bad jpeg');
    return { jpeg: await blob.text() };
  };
  const { feed, socket, calls, statuses } = startFeed({ decodeImage });
  socket().onmessage({ data: new ArrayBuffer(1) });
  socket().onmessage({ data: message({ frame_seq: 1 }) });
  await flush();
  assert.equal(feed.malformed, 2);
  assert.equal(calls.length, 0);
  fail = false;
  socket().onmessage({ data: message({ frame_seq: 2 }) });
  await flush();
  assert.equal(statuses.at(-1).state, 'live');
});

test('no new frame for a while turns live into stale', async (t) => {
  t.mock.timers.enable({ apis: ['setInterval', 'setTimeout', 'Date'], now: 1_000_000 });
  const decodeImage = async (blob) => ({ jpeg: await blob.text() });
  const { socket, statuses } = startFeed({ decodeImage });
  socket().onmessage({ data: message({ frame_seq: 1 }) });
  await flush();
  assert.equal(statuses.at(-1).state, 'live');
  t.mock.timers.tick(STALE_AFTER_MS);
  assert.equal(statuses.at(-1).state, 'live');
  t.mock.timers.tick(1000);
  assert.equal(statuses.at(-1).state, 'stale');
  socket().onmessage({ data: message({ frame_seq: 2 }) });
  await flush();
  assert.equal(statuses.at(-1).state, 'live');
});

test('a dropped connection reconnects; stopping does not', (t) => {
  t.mock.timers.enable({ apis: ['setInterval', 'setTimeout'] });
  const { feed, socket, statuses } = startFeed();
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

test('a frame that finishes decoding after stop is not drawn', async () => {
  const { feed, socket, calls, decodeImage } = startFeed();
  socket().onmessage({ data: message({ frame_seq: 1 }) });
  feed.stop();
  await decodeImage.resolveNext();
  assert.equal(calls.length, 0);
});
