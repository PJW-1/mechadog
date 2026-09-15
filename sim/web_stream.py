"""시뮬 카메라 → MJPEG 웹 스트림 — 로봇 시야를 브라우저로 본다.

LabKeeper 의 `live_patrol_stream.py` 패턴 이식: 카메라 RGB 를 JPEG 로 압축해
`multipart/x-mixed-replace` 로 흘린다. 관제 대시보드나 브라우저에서
`http://<PC>:8080/stream` 을 열면 시뮬 로봇의 FPV 가 보인다.

⚠️ Isaac 스텝 루프와 분리 — 프레임은 메인 스레드가 `latest` 에 올리고,
HTTP 스레드는 그 최신본만 송출한다. 느린 클라이언트가 시뮬을 끌지 않는다.
"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class MjpegServer:
    """latest 프레임을 나눠주는 경량 MJPEG 서버 — 채널(경로)마다 따로 든다.

    `/stream` 은 로봇 FPV, `/overview` 는 로봇을 따라가는 조망 카메라다.
    """

    def __init__(self, port: int = 8080) -> None:
        self._lock = threading.Lock()
        self._latest_jpeg: dict[str, bytes] = {}
        self._fps_limit = 25
        self.port = port
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def update(self, rgb, channel: str = "/stream") -> None:
        """메인 루프에서 호출 — RGB(HWC uint8)를 JPEG 로 바꿔 최신본을 올린다."""
        if rgb is None or getattr(rgb, "size", 0) == 0:
            return
        import cv2

        ok, buf = cv2.imencode(
            ".jpg", cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR), [cv2.IMWRITE_JPEG_QUALITY, 80]
        )
        if ok:
            with self._lock:
                self._latest_jpeg[channel] = buf.tobytes()

    def _handler(self):
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                if self.path.startswith("/profile"):
                    # host/vision/stream_client.apply_profile() 과 같은 계약.
                    # 해상도는 카메라 스펙 고정 — 실제 값을 보고하고, fps 요청은
                    # 송출 상한으로만 반영한다.
                    from urllib.parse import parse_qs, urlparse

                    qs = parse_qs(urlparse(self.path).query)
                    fps = int(qs.get("fps", ["25"])[0])
                    outer._fps_limit = max(1, min(30, fps))
                    import json

                    body = json.dumps(
                        {
                            "ok": True,
                            "profile": qs.get("name", ["VGA"])[0],
                            "fps_limit": outer._fps_limit,
                            "resolution": "640x480",
                        }
                    ).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                channel = "/stream" if self.path == "/" else self.path
                if channel not in ("/stream", "/overview"):
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
                self.end_headers()
                while True:
                    with outer._lock:
                        jpeg = outer._latest_jpeg.get(channel)
                    if jpeg is None:
                        import time

                        time.sleep(0.1)
                        continue
                    try:
                        header = (
                            "--frame\r\nContent-Type: image/jpeg\r\n"
                            f"Content-Length: {len(jpeg)}\r\n\r\n"
                        ).encode()
                        self.wfile.write(header + jpeg + b"\r\n")
                        self.wfile.flush()
                    except OSError:
                        break  # 끊긴 클라이언트는 조용히 정리
                    import time

                    time.sleep(1.0 / outer._fps_limit)  # /profile 이 조정하는 상한

            def log_message(self, *args):  # 요청 로그로 콘솔을 채우지 않는다
                pass

        return Handler

    def start(self) -> None:
        self._server = ThreadingHTTPServer(("0.0.0.0", self.port), self._handler())
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        print(f"[sim] MJPEG 스트림: http://localhost:{self.port}/stream")

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
