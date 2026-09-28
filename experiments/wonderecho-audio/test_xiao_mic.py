"""XIAO :82/audio reader — a local fake chunked server stands in for the board."""

import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from xiao_mic import XiaoMic


def _wait(pred, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return False


class FakeBoard:
    """`/audio` like the firmware: 200 + chunked L16, one script per connection.

    Each script is a list of steps: bytes → one chunk, float → sleep, "close" →
    drop the socket without the terminating chunk, or an int → HTTP status.
    """

    def __init__(self, scripts):
        self.scripts = list(scripts)
        self.paths = []
        board = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_a):
                pass

            def do_GET(self):
                board.paths.append(self.path)
                steps = board.scripts.pop(0) if board.scripts else [10.0]
                if isinstance(steps[0], int):
                    body = b'{"ok":false,"error":"mic unavailable"}'
                    self.send_response(steps[0])
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "audio/L16;rate=16000;channels=1")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                for step in steps:
                    if step == "close":
                        self.close_connection = True
                        return
                    if isinstance(step, float):
                        time.sleep(step)
                        continue
                    self.wfile.write(b"%x\r\n%s\r\n" % (len(step), step))
                    self.wfile.flush()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class XiaoMicTests(unittest.TestCase):
    def _mic(self, board, **kw):
        kw.setdefault("timeout_s", 0.5)
        kw.setdefault("backoff_s", (0.05, 0.1))
        mic = XiaoMic("127.0.0.1", port=board.port, **kw).start()
        self.addCleanup(mic.stop)
        return mic

    def _board(self, scripts):
        board = FakeBoard(scripts)
        self.addCleanup(board.close)
        return board

    def test_frames_arrive_in_order_across_uneven_chunks(self):
        pcm = bytes(range(256)) * 10  # 2560 bytes = 4 frames
        board = self._board([[pcm[:1001], pcm[1001:1500], pcm[1500:], 5.0]])
        mic = self._mic(board, gain=3)
        got = b"".join(mic.read_frame(timeout=2.0) for _ in range(4))
        self.assertEqual(got, pcm)
        self.assertEqual(board.paths, ["/audio?gain=3"])
        self.assertIsNone(mic.read_frame(timeout=0.1))

    def test_drop_reconnects_and_counts(self):
        # DoD ②③ — 끊기면 다시 붙고, 끊긴 횟수를 센다
        board = self._board([[b"\x01\x00" * 320, "close"], [b"\x02\x00" * 320, 5.0]])
        mic = self._mic(board)
        self.assertEqual(mic.read_frame(timeout=2.0), b"\x01\x00" * 320)
        self.assertEqual(mic.read_frame(timeout=3.0), b"\x02\x00" * 320)
        self.assertTrue(_wait(lambda: mic.connected))
        stats = mic.stats()
        self.assertEqual((stats["connects"], stats["drops"]), (2, 1))
        self.assertTrue(stats["last_error"])  # 잘린 chunked 는 IncompleteRead 로 온다

    def test_silent_stream_counts_as_drop(self):
        # 펌웨어는 두 번째 클라이언트를 기다리게 한다 — 클라이언트에서는 죽은
        # 스트림과 구분되지 않으므로 멈춘 스트림도 끊김으로 세고 다시 붙는다
        board = self._board([[b"\x00\x00" * 320, 2.0], [b"\x03\x00" * 320, 5.0]])
        mic = self._mic(board, timeout_s=0.3)
        mic.read_frame(timeout=2.0)
        self.assertEqual(mic.read_frame(timeout=3.0), b"\x03\x00" * 320)
        self.assertEqual(mic.stats()["drops"], 1)

    def test_mic_unavailable_is_a_failure_not_a_connect(self):
        board = self._board([[503], [b"\x04\x00" * 320, 5.0]])
        mic = self._mic(board)
        self.assertEqual(mic.read_frame(timeout=3.0), b"\x04\x00" * 320)
        stats = mic.stats()
        self.assertEqual((stats["failures"], stats["connects"], stats["drops"]), (1, 1, 0))

    def test_board_off_retries_without_raising(self):
        board = self._board([])
        port = board.port
        board.close()  # 아무도 듣지 않는 포트 — 전원이 꺼진 보드
        mic = XiaoMic("127.0.0.1", port=port, timeout_s=0.3, backoff_s=(0.02, 0.05)).start()
        self.addCleanup(mic.stop)
        self.assertTrue(_wait(lambda: mic.failures >= 2))
        self.assertIsNone(mic.read_frame(timeout=0.05))
        self.assertFalse(mic.stats()["connected"])

    def test_flush_drops_stale_audio(self):
        board = self._board([[b"\x05\x00" * 320 * 3, 0.3, b"\x06\x00" * 320, 5.0]])
        mic = self._mic(board)
        self.assertTrue(_wait(lambda: mic.stats()["buffered_ms"] >= 60))
        mic.flush()
        self.assertEqual(mic.read_frame(timeout=2.0), b"\x06\x00" * 320)

    def test_stop_while_streaming_ends_reader_cleanly(self):
        # 스트림을 읽는 도중 끄면 리더 스레드가 예외로 죽지 않고 끝나야 한다
        board = self._board([[b"\x07\x00" * 320, 5.0]])
        mic = self._mic(board)
        self.assertIsNotNone(mic.read_frame(timeout=2.0))
        crashed = []
        hook = threading.excepthook
        threading.excepthook = crashed.append
        try:
            mic.stop()
        finally:
            threading.excepthook = hook
        self.assertFalse(mic._thread.is_alive())
        self.assertEqual(crashed, [])
        self.assertEqual(mic.stats()["drops"], 0)  # 스스로 끈 것은 끊김이 아니다

    def test_overflow_keeps_sample_alignment(self):
        mic = XiaoMic("127.0.0.1", max_buffer_s=0.02)  # 640 bytes
        mic._push(b"\x00" * 641)  # 홀수 바이트가 넘쳐도 샘플 경계는 짝수로 버린다
        self.assertEqual(mic.overflow_bytes, 2)
        mic._push(b"\x00")
        self.assertEqual(mic.stats()["buffered_ms"], 20)

    def test_gain_range_matches_firmware(self):
        with self.assertRaises(ValueError):
            XiaoMic("127.0.0.1", gain=5)
        self.assertEqual(XiaoMic("h", gain=0).url, "http://h:82/audio?gain=0")


if __name__ == "__main__":
    unittest.main()
