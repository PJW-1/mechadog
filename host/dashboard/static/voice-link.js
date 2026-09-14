// 관제 웹 → 음성 중계 API 연결 (WBS 4.7.14).
//
// `voice_pipeline.py --web 8090` 이 여는 HTTP API 에 붙는다. 이 링크가 없으면
// 음성 패널은 "연결 준비" 만 표시하고 아무 요청도 보내지 않는다 — 명령 링크와
// 같은 규칙이다: **붙일 서버가 없는데 열어 두지 않는다.**
//
// ⚠️ **주소는 로컬만 받는다.** `?voice=` 나 메타 태그로 원격 주소를 적어도
// 붙지 않는다. 음성 API 서버 쪽도 로컬 출처만 CORS 허용한다.

const DEFAULT_TIMEOUT_MS = 2000;

export class VoiceLink {
  /**
   * @param {object} options
   * @param {string} options.baseUrl 음성 API 주소 (예: http://127.0.0.1:8090)
   * @param {typeof fetch} options.fetch 주입해서 시험한다.
   */
  constructor({ baseUrl = 'http://127.0.0.1:8090', fetch: fetchImpl = globalThis.fetch } = {}) {
    if (typeof fetchImpl !== 'function') throw new Error('fetch 구현이 필요합니다.');
    this.baseUrl = baseUrl.replace(/\/+$/, '');
    // RobotLink 와 같은 이유로 묶는다 — 바인딩 없이 this.fetch 를 부르면
    // 브라우저 네이티브 fetch 가 Illegal invocation 으로 거부한다.
    this.fetch = fetchImpl.bind(globalThis);
  }

  async get(path, timeoutMs = DEFAULT_TIMEOUT_MS) {
    const controller = typeof AbortController === 'function' ? new AbortController() : null;
    const timer = controller ? setTimeout(() => controller.abort(), timeoutMs) : null;
    try {
      const response = await this.fetch(this.baseUrl + path, { signal: controller?.signal });
      if (!response.ok) throw new Error('음성 서버 응답 오류 (HTTP ' + response.status + ')');
      return await response.json();
    } finally {
      if (timer) clearTimeout(timer);
    }
  }

  async post(path, body, timeoutMs = DEFAULT_TIMEOUT_MS) {
    const controller = typeof AbortController === 'function' ? new AbortController() : null;
    const timer = controller ? setTimeout(() => controller.abort(), timeoutMs) : null;
    try {
      const response = await this.fetch(this.baseUrl + path, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body ?? {}),
        signal: controller?.signal,
      });
      if (!response.ok) throw new Error('요청이 거절되었습니다. (HTTP ' + response.status + ')');
      return await response.json();
    } finally {
      if (timer) clearTimeout(timer);
    }
  }

  status() {
    return this.get('/status');
  }

  transcript() {
    return this.get('/transcript');
  }

  say(text, { urgent = false } = {}) {
    return this.post('/say', { text, urgent });
  }

  mode(mode) {
    return this.post('/mode', mode ? { mode } : {});
  }
}

/**
 * 음성 API 주소를 정한다. 우선순위:
 *   1) ?voice=http://127.0.0.1:8090
 *   2) <meta name="mechadog-voice" content="http://127.0.0.1:8090">
 *   3) 페이지가 로컬에서 열렸을 때만 127.0.0.1:8090/status 를 한 번 찔러 본다.
 *      응답이 음성 API 모양(robot 필드)이면 그대로 쓰고, 아니면 연결 없음.
 */
export async function resolveVoiceBase({ location = globalThis.location, document = globalThis.document, fetch: fetchImpl = globalThis.fetch } = {}) {
  try {
    const raw = (new URLSearchParams(location.search).get('voice')
      || document.querySelector('meta[name="mechadog-voice"]')?.content || '').trim();
    if (raw) {
      const url = new URL(raw, location.href);
      return ['127.0.0.1', 'localhost', '[::1]'].includes(url.hostname) ? url.origin : null;
    }
    if (!['127.0.0.1', 'localhost', '[::1]'].includes(location.hostname)) return null;
    const probe = await fetchImpl('http://127.0.0.1:8090/status', {
      signal: globalThis.AbortSignal?.timeout?.(1200),
    }).catch(() => null);
    if (!probe?.ok) return null;
    const info = await probe.json().catch(() => null);
    return info && typeof info.robot === 'string' ? 'http://127.0.0.1:8090' : null;
  } catch {
    return null;
  }
}
