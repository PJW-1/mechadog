// 관제 웹 → 대시보드 명령 API 연결 (WBS 4.6.3 · FR-4.3/4.4).
//
// 이 파일이 생기기 전까지 수동 명령은 **웹 상태와 이력만 바꿨다**
// (FEATURE_ALIGNMENT "제어와 표시"). 조이스틱을 눌러도 로봇은 가만히 있었고,
// 그것이 의도였다 — 붙일 서버가 없었으니까. 이제 `4.5.3` 의 `/api/command/*`
// 가 생겼으므로 그 경로만 채운다.
//
// ⚠️ **예시 모드를 없애지 않는다.** 링크를 주지 않으면 웹은 지금까지와 똑같이
// 동작한다. 실제 전송은 링크를 명시적으로 붙였을 때만 일어난다 — 공개된 화면이
// 저 혼자 로봇을 움직이게 두지 않는다.
//
// ⚠️ **E-Stop 은 다른 명령과 같은 줄에 세우지 않는다.** 방향 명령은 실패하면
// 다음 입력에서 다시 시도하면 그만이지만, 비상정지는 **한 번의 실패가 곧
// 사고**다. 그래서 타임아웃을 짧게 잡고 실패를 삼키지 않고 던진다.
//
// ⚠️ **방향을 보폭·조향으로 바꾸는 것은 여기서 한다.** 화면은 `FORWARD` 같은
// 사람 말을 쓰고 로봇은 `step`·`angle` 을 받는다. 그 사이를 웹 곳곳에서 각자
// 변환하면 부호가 어긋나는 자리가 여럿 생긴다.
//
// 조향 부호는 PROTOCOL 규약을 따른다 — **양수가 반시계 = 좌회전**.

const DEFAULT_TIMEOUT_MS = 1500;
const ESTOP_TIMEOUT_MS = 800;

/** 화면의 방향 이름 → 로봇의 `step`·`angle`. 크기는 호출자가 설정에서 준다. */
export function motionFor(command, { step = 60, angle = 20 } = {}) {
  switch (command) {
    case 'FORWARD':
      return { step, angle: 0 };
    case 'BACKWARD':
      return { step: -step, angle: 0 };
    case 'LEFT':
      return { step, angle };
    case 'RIGHT':
      return { step, angle: -angle };
    case 'STOP':
      return { step: 0, angle: 0 };
    default:
      throw new Error('허용되지 않은 방향입니다.');
  }
}

export class RobotLink {
  /**
   * @param {object} options
   * @param {string} options.baseUrl 대시보드 주소. 같은 출처면 빈 문자열.
   * @param {typeof fetch} options.fetch 주입해서 시험한다.
   * @param {{step:number,angle:number}} options.motion 보폭·조향 크기.
   */
  constructor({ baseUrl = '', fetch: fetchImpl = globalThis.fetch, motion = {} } = {}) {
    if (typeof fetchImpl !== 'function') throw new Error('fetch 구현이 필요합니다.');
    this.baseUrl = baseUrl.replace(/\/+$/, '');
    // ⚠️ **`bind` 를 빼면 브라우저에서 아무 명령도 나가지 않는다.**
    // 네이티브 `fetch` 는 `this` 가 창이 아니면 `Illegal invocation` 으로 거부한다.
    // `this.fetch(...)` 로 부르면 `this` 는 이 링크 객체이므로 전부 실패한다.
    // 주입한 가짜 fetch 는 `this` 를 보지 않으므로 묶어도 그대로 동작한다 —
    // 그래서 **시험만으로는 이 결함이 잡히지 않는다.** 실제로 그랬다.
    this.fetch = fetchImpl.bind(globalThis);
    this.motion = { step: 60, angle: 20, ...motion };
  }

  async post(path, body, timeoutMs = DEFAULT_TIMEOUT_MS) {
    // AbortController 가 없는 환경에서도 죽지 않는다 — 타임아웃만 포기한다.
    const controller = typeof AbortController === 'function' ? new AbortController() : null;
    const timer = controller ? setTimeout(() => controller.abort(), timeoutMs) : null;
    try {
      const response = await this.fetch(this.baseUrl + path, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body ?? {}),
        signal: controller?.signal,
      });
      if (!response.ok) {
        const detail=await response.json().catch(()=>null);
        throw new Error((typeof detail?.error==='string'?detail.error:'요청이 거절되었습니다.')+' (HTTP '+response.status+')');
      }
      return await response.json();
    } finally {
      if (timer) clearTimeout(timer);
    }
  }

  /** 상태만 물어보는 GET — 명령이 아니므로 origin 검사 대상이 아니다. */
  async get(path, timeoutMs = DEFAULT_TIMEOUT_MS) {
    const controller = typeof AbortController === 'function' ? new AbortController() : null;
    const timer = controller ? setTimeout(() => controller.abort(), timeoutMs) : null;
    try {
      const response = await this.fetch(this.baseUrl + path, { signal: controller?.signal });
      if (!response.ok) throw new Error('조회가 거절되었습니다. (HTTP ' + response.status + ')');
      return await response.json();
    } finally {
      if (timer) clearTimeout(timer);
    }
  }

  /** **실패를 삼키지 않는다.** 비상정지가 안 갔다면 화면이 그것을 말해야 한다. */
  estop() {
    return this.post('/api/command/estop', {}, ESTOP_TIMEOUT_MS);
  }

  manual(on) {
    return this.post('/api/command/manual', { on: !!on });
  }

  drive(command) {
    return this.post('/api/command/drive', motionFor(command, this.motion));
  }

  /** 본체 자세 — 'up'|'level'|'down'. 각도는 서버가 config 에서 정한다 (임의 각도 없음). */
  pose(preset) {
    return this.post('/api/command/pose', { preset });
  }

  /** 서비스 모드 전환 — 'enter'|'exit'. 해제 후 보행 복귀에는 resetSafe() 가 필요하다. */
  service(mode) {
    return this.post('/api/command/service', { mode });
  }

  /** FAILSAFE 안전 래치 해제 요청 — 서버가 다음 틱에 RESET_SAFE 를 보낸다. */
  resetSafe() {
    return this.post('/api/command/reset', {});
  }

  /**
   * 경보(L3) 확인 — 사람이 상황을 보고 누른다 (FR-10.3.2).
   *
   * ⚠️ **`resetSafe()` 와 다른 문이다.** 저쪽은 물리 상태(F), 이쪽은 상황 판단(L3)을
   * 확인한다 (ADR-26). 하나로 묶으면 **비상정지를 눌렀다 푸는 것으로 경보가 지워진다.**
   */
  confirmAlarm() {
    return this.post('/api/command/alarm', {});
  }

  /** 실제 순찰 예약/정지 — action 은 'start'|'stop'. */
  patrol(action) {
    return this.post('/api/command/patrol', { action });
  }

  /** 위치 알려주기 — «로봇은 지금 이 구역 안» . 서버가 그 구역 안에서만 위치를 다시 찾는다(찾을 때까지 정지). */
  /** 지도에서 찍은 곳(순찰 좌표 m)으로 — 서버가 다음 틱에 경로를 푼다. 결과는 /api/nav 의 goal_feedback. */
  goto(x, y) {
    return this.post('/api/command/goto', { x, y });
  }

  locate(zone) {
    return this.post('/api/command/locate', { zone });
  }

  // 운용 모드 전환 (FR-4.7 · FR-11.3). ⚠️ **온보드 `SERVICE` 와 다른 축이다** —
  // 저쪽은 정비 상태이고 이쪽은 임무 모드다. 거절이 흔하므로 사유를 그대로 돌려준다.
  mode(name) {
    return this.post('/api/command/mode', { mode: name });
  }

  /**
   * 관제 PC 스피커 방송 음량 · 무음 상태 (`4.8.2`). 로봇 스피커(`/api/command/sound`)와는
   * 별개다. 방송기는 플릿 전체가 하나를 나눠 쓰므로 아무 로봇의 서버에서 물어봐도 같다.
   */
  broadcastStatus() {
    return this.get('/api/broadcast');
  }

  /** `{volume}` 또는 `{muted}` 중 온 필드만 바꾼다. */
  setBroadcast(patch) {
    return this.post('/api/broadcast', patch);
  }
}
