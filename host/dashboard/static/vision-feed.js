// 관제 웹 FPV — `/ws/vision` 을 받아 영상과 검출 박스를 한 캔버스에 그린다 (WBS 4.6.1 · FR-4.1).
//
// ⚠️ **박스는 같은 메시지의 JPEG 위에만 그린다.** 서버는 추론 결과 하나를
// `[헤더 길이 uint32 BE][JSON 헤더][JPEG]` 한 메시지로 보낸다(4.5.2). 카메라
// 스트림에 박스를 따로 얹으면 추론 지연만큼 박스가 다른 장면 위에 뜬다. 그래서
// 이 창은 카메라 수신률이 아니라 **추론률(`vision.inference_fps`)로 갱신된다.**
//
// ⚠️ **새 메시지가 오지 않으면 멈춘 것이다.** 서버는 새 추론이 나올 때만 보낸다.
// 마지막 장면은 그대로 두되 상태를 `stale` 로 알린다 — 멈춘 화면이 실시간처럼
// 보이면 안 된다.
//
// ⚠️ **디코드가 밀리면 최신 한 장만 남긴다.** 오래된 장면을 차례로 그리면 화면이
// 점점 뒤처진다. 서버가 느린 연결에 하는 것과 같은 원칙이다.

export const PERSON_LABEL = 'person';
export const STALE_AFTER_MS = 2000;
export const RETRY_MS = 2000;
export const MAX_HEADER_BYTES = 64 * 1024;

const PERSON_COLOR = '#edc657';
const OBJECT_COLOR = '#8fd3ff';
const LABEL_INK = '#16151d';

/** 바이너리 메시지 하나 → `{header, jpeg}`. 형식이 틀리면 던진다. */
export function decodeVisionMessage(buffer) {
  if (!(buffer instanceof ArrayBuffer) || buffer.byteLength < 4) {
    throw new Error('검출 메시지 형식이 아닙니다.');
  }
  const headerLength = new DataView(buffer).getUint32(0, false);
  if (headerLength < 2 || headerLength > MAX_HEADER_BYTES || 4 + headerLength > buffer.byteLength) {
    throw new Error('검출 메시지 헤더 길이가 맞지 않습니다.');
  }
  const header = JSON.parse(new TextDecoder().decode(new Uint8Array(buffer, 4, headerLength)));
  const validBox = (box) => Array.isArray(box) && box.length === 4 && box.every(Number.isFinite);
  if (
    header?.type !== 'vision' ||
    !Number.isInteger(header.frame_seq) ||
    !(header.width > 0) ||
    !(header.height > 0) ||
    !Array.isArray(header.detections) ||
    !Array.isArray(header.tracks) ||
    !header.detections.every((item) => typeof item?.label === 'string' && validBox(item.box)) ||
    !header.tracks.every((item) => Number.isInteger(item?.track_id) && validBox(item.box))
  ) {
    throw new Error('검출 메시지 헤더가 올바르지 않습니다.');
  }
  const jpeg = new Uint8Array(buffer, 4 + headerLength);
  if (jpeg.byteLength === 0) throw new Error('검출 메시지에 JPEG가 없습니다.');
  return { header, jpeg };
}

/**
 * 그릴 박스 목록. 좌표는 원본 픽셀 `[x1, y1, x2, y2]` 그대로다.
 *
 * 사람은 **추적 결과로** 그린다 — ID 가 붙어 있고, 추적기는 이번 프레임의 사람
 * 검출 박스를 그대로 쓰므로 사람 검출을 또 그리면 같은 자리에 박스가 두 겹이
 * 된다. 동시 추적 상한(FR-3.8.5)을 넘어 추적하지 않는 사람은 그리지 않는다.
 */
export function overlayBoxes(header) {
  const boxes = (header.tracks ?? []).map((track) => ({
    kind: 'person',
    box: track.box,
    text: `사람 #${track.track_id}`,
  }));
  for (const detection of header.detections ?? []) {
    if (detection.label === PERSON_LABEL) continue;
    boxes.push({
      kind: 'object',
      box: detection.box,
      text: `${detection.label} ${Math.round(detection.score * 100)}%`,
    });
  }
  return boxes;
}

/**
 * 캔버스를 원본 해상도로 맞추고 영상 → 박스 순으로 그린다. 화면 크기에 맞추는
 * 것은 CSS(`object-fit: contain`)가 영상과 박스를 함께 늘려서 한다 — 여기서
 * 좌표를 따로 환산하지 않는다.
 */
export function drawOverlay(context, image, header) {
  const { canvas } = context;
  // 크기를 바꾸면 캔버스 상태가 초기화되므로 선·글꼴은 그 뒤에 정한다.
  if (canvas.width !== header.width) canvas.width = header.width;
  if (canvas.height !== header.height) canvas.height = header.height;
  context.drawImage(image, 0, 0, header.width, header.height);

  const fontSize = Math.max(12, Math.round(header.width / 40));
  const pad = Math.round(fontSize / 4);
  context.lineWidth = Math.max(2, Math.round(header.width / 320));
  context.font = `600 ${fontSize}px system-ui, sans-serif`;
  context.textBaseline = 'top';
  for (const item of overlayBoxes(header)) {
    const [rawX1, rawY1, rawX2, rawY2] = item.box;
    const x1 = Math.max(0, Math.min(header.width, rawX1));
    const y1 = Math.max(0, Math.min(header.height, rawY1));
    const x2 = Math.max(0, Math.min(header.width, rawX2));
    const y2 = Math.max(0, Math.min(header.height, rawY2));
    if (x2 <= x1 || y2 <= y1) continue;
    const color = item.kind === 'person' ? PERSON_COLOR : OBJECT_COLOR;
    context.strokeStyle = color;
    context.strokeRect(x1, y1, x2 - x1, y2 - y1);
    const labelWidth = context.measureText(item.text).width + pad * 2;
    const labelHeight = fontSize + pad * 2;
    // 박스 위에 여백이 없으면 박스 안쪽 위에 붙인다.
    const top = y1 >= labelHeight ? y1 - labelHeight : y1;
    context.fillStyle = color;
    context.fillRect(x1, top, labelWidth, labelHeight);
    context.fillStyle = LABEL_INK;
    context.fillText(item.text, x1 + pad, top + pad);
  }
}

/**
 * 상태: `connecting` → `waiting`(연결됨·영상 전) → `live` ⇄ `stale`, 끊기면
 * `closed` 후 `RETRY_MS` 뒤 다시 연결한다.
 */
export class VisionFeed {
  /**
   * @param {object} options
   * @param {string} options.url `ws://…/ws/vision`
   * @param {{getContext(type:string):CanvasRenderingContext2D}} options.canvas
   * @param {(status:object)=>void} options.onStatus 상태가 바뀌거나 새 장면을 그릴 때마다
   * @param {typeof WebSocket} options.WebSocket 주입해서 시험한다.
   * @param {(blob:Blob)=>Promise<ImageBitmap>} options.decodeImage 주입해서 시험한다.
   */
  constructor({
    url,
    canvas,
    onStatus = () => {},
    WebSocket: Socket = globalThis.WebSocket,
    decodeImage = (blob) => globalThis.createImageBitmap(blob),
  }) {
    this.url = url;
    this.context = canvas.getContext('2d');
    this.onStatus = onStatus;
    this.Socket = Socket;
    this.decodeImage = decodeImage;
    this.state = 'idle';
    this.socket = null;
    this.pending = null;
    this.decoding = false;
    this.stopped = true;
    this.lastFrameAt = null;
    this.last = null;
    this.malformed = 0;
  }

  start() {
    if (!this.stopped) return;
    this.stopped = false;
    this.ticker = setInterval(() => this.checkStale(), 500);
    // Node 기반 계약 시험에서는 이 타이머 하나가 프로세스 종료를 붙잡지 않는다.
    // 브라우저의 숫자 타이머에는 unref 가 없으므로 실제 화면 동작은 그대로다.
    this.ticker.unref?.();
    this.connect();
  }

  stop() {
    this.stopped = true;
    clearTimeout(this.retry);
    clearInterval(this.ticker);
    const socket = this.socket;
    this.socket = null;
    this.pending = null;
    socket?.close();
  }

  connect() {
    const socket = new this.Socket(this.url);
    socket.binaryType = 'arraybuffer';
    this.socket = socket;
    this.report('connecting');
    socket.onopen = () => {
      if (this.socket === socket) this.report('waiting');
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
    };
  }

  receive(data) {
    let message;
    try {
      message = decodeVisionMessage(data);
    } catch {
      this.malformed += 1;
      return;
    }
    this.pending = message;
    if (!this.decoding) this.drain();
  }

  async drain() {
    this.decoding = true;
    try {
      while (this.pending && !this.stopped) {
        const { header, jpeg } = this.pending;
        this.pending = null;
        let image;
        try {
          image = await this.decodeImage(new Blob([jpeg], { type: 'image/jpeg' }));
        } catch {
          this.malformed += 1;
          continue;
        }
        // 디코드 중 더 새 프레임이 왔다면 이 장면은 이미 낡았다. 잠깐이라도
        // 그리지 않고 바로 최신 프레임을 디코드한다.
        if (this.pending) {
          image.close?.();
          continue;
        }
        if (!this.stopped) {
          drawOverlay(this.context, image, header);
          this.lastFrameAt = Date.now();
          this.last = header;
          this.report('live');
        }
        image.close?.();
      }
    } finally {
      this.decoding = false;
    }
  }

  checkStale() {
    if (this.state === 'live' && Date.now() - this.lastFrameAt > STALE_AFTER_MS) this.report('stale');
  }

  report(state) {
    this.state = state;
    this.onStatus({
      state,
      lastFrameAt: this.lastFrameAt,
      frameSeq: this.last?.frame_seq ?? null,
      width: this.last?.width ?? null,
      height: this.last?.height ?? null,
      detections: this.last?.detections?.length ?? 0,
      persons: this.last?.tracks?.length ?? 0,
    });
  }
}
