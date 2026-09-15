// 관제 웹 사건 피드 — `/ws/events` 를 받아 실시간 사건을 목록에 넣는다 (WBS 4.6.4 · FR-4.5).
//
// ⚠️ **사건은 합치지 않는다.** 텔레메트리는 상태라 최신만 남겨도 되지만 사건은
// *"그때 사람이 있었다"* 는 기록이다 — 하나하나 전부 목록에 넣는다. 서버도 같은
// 원칙으로 연결마다 버퍼를 따로 둔다 (4.4.3).
//
// ⚠️ **끊기면 다시 붙는다.** 브라우저는 로봇보다 늦게 열리고 새로고침도 한다.
// 서버는 붙는 순간 버퍼에 남은 사건을 먼저 넘기므로 재연결만 하면 최근 사건은
// 따라온다. 버퍼를 넘겨 못 주는 것은 `event_gap` 으로 오는데, 그것을 숨기면
// 사람이 공백을 *"아무 일도 없었다"* 로 읽는다 — 개수를 그대로 화면에 올린다.
//
// ⚠️ **스냅샷은 가리키는 이름만 온다.** 서버는 JPEG 바이트를 이 소켓에 싣지 않는
// 다 (수십 KB 가 사건 하나를 밀어낸다). 화면에는 파일명만 표시하고 원본 그림은
// 블랙박스 가져오기로만 본다 — 없는 그림을 있는 척 그리지 않는다.

export const RETRY_MS = 2000;

/** 텍스트 메시지 하나 → `{kind:'event'|'gap', ...}`. 형식이 틀리면 던진다. */
export function decodeEventMessage(data) {
  if (typeof data !== 'string') throw new Error('사건 메시지는 JSON 텍스트여야 합니다.');
  const message = JSON.parse(data);
  if (message?.type === 'event_gap') {
    if (!Number.isInteger(message.dropped) || message.dropped <= 0) {
      throw new Error('event_gap 의 dropped 가 올바르지 않습니다.');
    }
    return { kind: 'gap', dropped: message.dropped };
  }
  if (
    message?.type !== 'event' ||
    !Number.isInteger(message.seq) ||
    typeof message.event !== 'string' ||
    !message.event.trim() ||
    typeof message.state !== 'string' ||
    typeof message.escalation !== 'string' ||
    !Number.isSafeInteger(message.ts_ms)
  ) {
    throw new Error('사건 메시지 형식이 올바르지 않습니다.');
  }
  return {
    kind: 'event',
    seq: message.seq,
    event: message.event,
    ts_ms: message.ts_ms,
    state: message.state,
    escalation: message.escalation,
    tracks: Array.isArray(message.tracks) ? message.tracks : [],
    detections: Array.isArray(message.detections) ? message.detections : [],
    telemetry: message.telemetry && typeof message.telemetry === 'object' ? message.telemetry : null,
    entry: typeof message.entry === 'string' ? message.entry : null,
    snapshot: typeof message.snapshot === 'string' ? message.snapshot : null,
  };
}

/**
 * 상태: `connecting` → `live`(첫 메시지 수신), 끊기면 `closed` 후 `RETRY_MS` 뒤
 * 다시 연결한다. 사건이 드물어 메시지가 없어도 연결돼 있으면 `live` 다 —
 * vision 의 stale 과 달리 조용한 것이 곧 정상이다.
 */
export class EventFeed {
  /**
   * @param {object} options
   * @param {string} options.url `ws://…/ws/events`
   * @param {(event:object)=>void} options.onEvent 검증을 통과한 사건 하나씩
   * @param {(dropped:number)=>void} options.onGap 버퍼에서 밀려 못 받은 개수
   * @param {(status:object)=>void} options.onStatus 연결 상태가 바뀔 때마다
   * @param {typeof WebSocket} options.WebSocket 주입해서 시험한다.
   */
  constructor({
    url,
    onEvent = () => {},
    onGap = () => {},
    onStatus = () => {},
    WebSocket: Socket = globalThis.WebSocket,
  }) {
    this.url = url;
    this.onEvent = onEvent;
    this.onGap = onGap;
    this.onStatus = onStatus;
    this.Socket = Socket;
    this.state = 'idle';
    this.socket = null;
    this.stopped = true;
    this.received = 0;
    this.dropped = 0;
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
    socket.onopen = () => {
      // 백로그가 없는 신선한 서버는 메시지 없이 연결만 유지한다 — 열렸다는
      // 사실만으로 수신 준비다. 첫 사건이 와도 상태는 live 그대로다.
      if (this.socket === socket) this.report('live');
    };
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
    let message;
    try {
      message = decodeEventMessage(data);
    } catch {
      this.malformed += 1;
      return;
    }
    if (message.kind === 'gap') {
      this.dropped += message.dropped;
      this.onGap(message.dropped);
    } else {
      this.received += 1;
      this.onEvent(message);
    }
    this.report('live');
  }

  report(state) {
    this.state = state;
    this.onStatus({ state, received: this.received, dropped: this.dropped, malformed: this.malformed });
  }
}
