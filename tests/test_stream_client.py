"""MJPEG 수신·복구·최신 프레임 큐 검증 (WBS 4.3.3~4.3.5 · FR-5.3).

**카메라 없이 검증한다.** 파서가 바이트만 받으므로 실제 XIAO 가 보내는 것과 같은
전문을 만들어 먹이면 된다 — 수신기(`4.3.6`)를 목업으로 검증한 것과 같은 방식이다.
"""

from __future__ import annotations

import json

import pytest

from host.vision.stream_client import (
    DEFAULT_BOUNDARY,
    JPEG_EOI,
    JPEG_SOI,
    Frame,
    FrameQueue,
    MjpegParser,
    StreamEndpoints,
    apply_profile,
    decode_jpeg,
    stream_endpoints,
)


def jpeg(size: int = 32, marker: bytes = b"x") -> bytes:
    """JPEG 처럼 보이는 바이트. 표식만 진짜고 내용은 채움이다."""
    return JPEG_SOI + marker * size + JPEG_EOI


def _with_xiao(cfg: dict, ip: str | None) -> dict:
    """⚠️ **주소는 `network` 절 안에 있다.** 최상위에 넣어 주면 시험이 실물보다
    친절해져서, 코드가 최상위를 읽는 버그를 통과시킨다 — 실제로 그랬다."""
    merged = dict(cfg)
    merged["network"] = dict(cfg["network"], xiao_ip=ip)
    return merged


def part(payload: bytes, *, boundary: str = DEFAULT_BOUNDARY, length: bool = True) -> bytes:
    """펌웨어가 실제로 보내는 형식 그대로 만든다."""
    head = f"\r\n--{boundary}\r\nContent-Type: image/jpeg\r\n"
    if length:
        head += f"Content-Length: {len(payload)}\r\n"
    return head.encode("ascii") + b"\r\n" + payload


# ── 기본 파싱 ────────────────────────────────────────────────
def test_single_frame_is_parsed() -> None:
    parser = MjpegParser()
    frames = parser.feed(part(jpeg()), 1000)
    assert len(frames) == 1
    assert frames[0].received_ms == 1000
    assert frames[0].seq == 1
    assert frames[0].looks_like_jpeg


def test_two_frames_in_one_chunk() -> None:
    """한 조각에 프레임 두 개가 들어올 수 있다 — TCP 는 경계를 지켜 주지 않는다."""
    parser = MjpegParser()
    frames = parser.feed(part(jpeg(10)) + part(jpeg(20)), 1000)
    assert [f.seq for f in frames] == [1, 2]
    assert [f.size_bytes for f in frames] == [14, 24]


@pytest.mark.parametrize("step", [1, 3, 7, 64, 4096])
def test_arbitrary_chunk_boundaries(step: int) -> None:
    """**조각이 어디서 끊겨도 같은 결과여야 한다.** 1바이트씩 넣어도 된다."""
    wire = part(jpeg(50)) + part(jpeg(60)) + part(jpeg(70))
    parser = MjpegParser()
    frames = []
    for i in range(0, len(wire), step):
        frames += parser.feed(wire[i : i + step], 1000 + i)
    assert [f.size_bytes for f in frames] == [54, 64, 74]
    assert parser.stats.discarded == 0


def test_incomplete_frame_waits() -> None:
    """본문이 덜 왔으면 내놓지 않는다 — 잘린 JPEG 를 추론에 넘기면 안 된다."""
    wire = part(jpeg(100))
    parser = MjpegParser()
    assert parser.feed(wire[:-30], 1000) == []
    assert len(parser.feed(wire[-30:], 1010)) == 1


def test_timestamp_is_arrival_not_completion_of_first_byte() -> None:
    """**도착 시각은 프레임이 완성된 순간**이다 — 신선도의 기준이므로."""
    wire = part(jpeg(40))
    parser = MjpegParser()
    parser.feed(wire[:20], 1000)
    frame = parser.feed(wire[20:], 1234)[0]
    assert frame.received_ms == 1234


# ── Content-Length 없는 서버 ─────────────────────────────────
def test_frame_without_content_length_uses_next_boundary() -> None:
    """헤더에 길이가 없는 MJPEG 서버도 있다."""
    parser = MjpegParser()
    wire = (
        part(jpeg(30), length=False)
        + part(jpeg(40), length=False)
        + b"\r\n--"
        + (DEFAULT_BOUNDARY.encode("ascii"))
    )
    frames = parser.feed(wire, 1000)
    assert len(frames) == 2


# ── ⚠️ 경계 desync 를 무해하게 넘긴다 ────────────────────────
def test_garbage_between_frames_is_discarded() -> None:
    """JPEG 표식이 없는 조각은 버리고 다음 경계에서 다시 맞춘다."""
    parser = MjpegParser()
    broken = part(b"NOT-A-JPEG-AT-ALL")
    frames = parser.feed(broken + part(jpeg(20)), 1000)
    assert [f.size_bytes for f in frames] == [24], "정상 프레임은 살아남는다"
    assert parser.stats.discarded == 1
    assert parser.stats.resyncs == 1


def test_absurd_content_length_triggers_resync() -> None:
    """길이가 상한을 넘으면 경계가 어긋난 것으로 본다."""
    parser = MjpegParser(max_frame_bytes=1024)
    head = f"\r\n--{DEFAULT_BOUNDARY}\r\nContent-Length: 999999999\r\n\r\n".encode("ascii")
    parser.feed(head + b"junk", 1000)
    assert parser.stats.resyncs == 1
    frames = parser.feed(part(jpeg(30)), 1010)
    assert len(frames) == 1, "다음 경계에서 회복한다"


def test_buffer_does_not_grow_without_bound() -> None:
    """⚠️ **이게 없으면 깨진 스트림 하나가 메모리를 끝없이 먹는다.**"""
    parser = MjpegParser(max_frame_bytes=1024)
    for _ in range(20):
        parser.feed(b"z" * 1024, 1000)  # 경계가 한 번도 안 나온다
    assert parser.buffered_bytes <= 1024 * 2
    assert parser.stats.resyncs >= 1
    assert len(parser.feed(part(jpeg(30)), 2000)) == 1, "회복 가능해야 한다"


def test_empty_boundary_is_refused() -> None:
    with pytest.raises(ValueError):
        MjpegParser("")


def test_custom_boundary() -> None:
    parser = MjpegParser("other-boundary")
    assert len(parser.feed(part(jpeg(), boundary="other-boundary"), 1000)) == 1
    assert parser.feed(part(jpeg()), 1000) == [], "다른 경계는 인식하지 않는다"


# ── 프레임 큐 ────────────────────────────────────────────────
def test_queue_keeps_the_newest_when_full() -> None:
    """⚠️ **낡은 프레임으로 판단하면 로봇이 과거를 보고 움직인다.**"""
    q = FrameQueue(capacity=2)
    frames = [Frame(jpeg(), 1000 + i, i) for i in range(4)]
    dropped = [q.put(f) for f in frames]
    assert dropped == [False, False, True, True]
    assert [f.seq for f in (q.get(), q.get())] == [2, 3]
    assert q.dropped == 2 and q.received == 4


def test_latest_drains_the_rest() -> None:
    """추론은 최신 하나만 본다 — 나머지를 굳이 디코드하지 않는다."""
    q = FrameQueue(capacity=5)
    for i in range(5):
        q.put(Frame(jpeg(), 1000 + i, i))
    newest = q.latest()
    assert newest is not None and newest.seq == 4
    assert q.depth == 0 and q.dropped == 4


def test_empty_queue_returns_none() -> None:
    q = FrameQueue()
    assert q.get() is None and q.latest() is None


def test_capacity_must_be_positive() -> None:
    with pytest.raises(ValueError):
        FrameQueue(capacity=0)


def test_slow_consumer_gets_only_latest_and_queue_depth_is_logged(monkeypatch) -> None:
    """25fps 수신·5fps 소비에서도 적체 없이 매번 가장 최신 프레임을 처리한다.

    실제 추론 워커는 `3.3.2`에서 붙지만, 생산자와 소비자의 속도 차이는 가상 시각으로
    재현할 수 있다. 2초 동안 40ms마다 넣고 200ms마다 꺼내면 FIFO라면 40프레임이
    뒤처진다. 최신 우선 정책에서는 소비 순번이 4·9·14…로 현재 생산자를 따라간다.
    """

    class Sink:
        def __init__(self) -> None:
            self.records: list[tuple[str, dict]] = []

        def info(self, event: str, **detail) -> None:
            self.records.append((event, detail))

    sink = Sink()
    monkeypatch.setattr("host.vision.stream_client.LOG", sink)
    queue = FrameQueue(capacity=2)
    consumed: list[int] = []

    for seq in range(50):
        now_ms = seq * 40  # 25fps 입력
        queue.put(Frame(jpeg(), now_ms, seq))
        if (seq + 1) % 5 == 0:  # 5fps의 느린 추론 소비자
            frame = queue.latest(now_ms=now_ms)
            assert frame is not None
            consumed.append(frame.seq)

    assert consumed == list(range(4, 50, 5)), "소비 시점의 최신 순번이어야 한다"
    assert queue.depth == 0
    assert queue.max_depth == 2, "설정한 큐 상한을 넘지 않는다"
    assert queue.received == 50 and queue.processed == 10 and queue.dropped == 40

    summaries = [detail for event, detail in sink.records if event == "frame_queue_summary"]
    assert summaries, "운용 로그에서 드롭 정책을 검증할 수 있어야 한다"
    assert all(summary["queue_depth_max"] <= 2 for summary in summaries)
    assert any(summary["frames_dropped"] > 0 for summary in summaries)


# ── 주소 조립 (하드코딩 IP 제거) ─────────────────────────────
def test_endpoints_come_from_the_device_profile(cfg: dict) -> None:
    config = _with_xiao(cfg, "192.168.1.55")
    endpoints = stream_endpoints(config)
    assert endpoints.control == "http://192.168.1.55:80"
    assert endpoints.stream == "http://192.168.1.55:81/stream"


def test_missing_xiao_ip_is_refused(cfg: dict) -> None:
    """⚠️ **틀린 주소가 적혀 있는 것이 비어 있는 것보다 나쁘다.**

    그래서 전역 설정에 고정 URL 을 두지 않고, 실측 주소가 없으면 거부한다.
    """
    with pytest.raises(ValueError, match="xiao_ip"):
        stream_endpoints(_with_xiao(cfg, None))


def test_global_config_has_no_hardcoded_stream_url(cfg: dict) -> None:
    """전역 설정에 URL 이 되살아나면 여기서 걸린다."""
    assert "vision_stream_url" not in cfg["network"]
    assert cfg["network"]["vision_control_port"] == 80
    assert cfg["network"]["vision_stream_port"] == 81


# ── 기동 시 설정 적용 ────────────────────────────────────────
class _FakeResponse:
    def __init__(self, body: dict) -> None:
        self._body = json.dumps(body).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None


def _fake_opener(body: dict, seen: list[tuple[str, float]]):
    def opener(url: str, timeout: float = 0.0):
        seen.append((url, timeout))
        return _FakeResponse(body)

    return opener


def test_profile_is_applied_from_config(cfg: dict) -> None:
    """**이 호출이 없으면 설정을 고쳐도 카메라가 안 바뀐다** (NFR-3①)."""
    seen: list[tuple[str, float]] = []
    body = {"ok": True, "profile": "VGA", "fps_limit": 25}
    result = apply_profile(
        _with_xiao(cfg, "10.0.0.9"),
        opener=_fake_opener(body, seen),
        timeout_s=2.5,
    )
    url, timeout = seen[0]
    assert url == "http://10.0.0.9:80/profile?name=VGA&fps=25"
    assert timeout == 2.5, "타임아웃 없이 부르면 카메라가 꺼져 있을 때 기동이 매달린다"
    assert result["fps_limit"] == 25


def test_profile_values_track_the_config(cfg: dict) -> None:
    seen: list[tuple[str, float]] = []
    tweaked = _with_xiao(cfg, "10.0.0.9")
    tweaked["vision"] = dict(cfg["vision"], resolution="QVGA", stream_fps_limit=12)
    apply_profile(tweaked, opener=_fake_opener({"ok": True}, seen))
    assert seen[0][0].endswith("name=QVGA&fps=12")


def test_camera_rejection_is_raised(cfg: dict) -> None:
    seen: list[tuple[str, float]] = []
    with pytest.raises(ValueError, match="거부"):
        apply_profile(
            _with_xiao(cfg, "10.0.0.9"),
            opener=_fake_opener({"ok": False, "error": "fps out of range"}, seen),
        )


def test_endpoints_can_be_passed_in(cfg: dict) -> None:
    """이미 조립한 주소가 있으면 다시 만들지 않는다."""
    seen: list[tuple[str, float]] = []
    apply_profile(
        cfg,
        endpoints=StreamEndpoints("http://1.2.3.4:80", "http://1.2.3.4:81/stream"),
        opener=_fake_opener({"ok": True}, seen),
    )
    assert seen[0][0].startswith("http://1.2.3.4:80/profile")


# ── 디코드 ───────────────────────────────────────────────────
def test_decode_round_trip() -> None:
    """진짜 JPEG 한 장을 만들어 되돌려 본다."""
    cv2 = pytest.importorskip("cv2")
    np = pytest.importorskip("numpy")
    source = np.zeros((16, 24, 3), dtype=np.uint8)
    source[:, :, 2] = 255
    ok, buffer = cv2.imencode(".jpg", source)
    assert ok
    image = decode_jpeg(buffer.tobytes())
    assert image.shape == (16, 24, 3)


def test_decode_rejects_garbage() -> None:
    pytest.importorskip("cv2")
    with pytest.raises(ValueError, match="디코드 실패"):
        decode_jpeg(b"not a jpeg at all")


def test_parsed_frame_decodes() -> None:
    """**파서를 거친 프레임이 실제로 디코드된다** — 경계를 잘못 자르면 여기서 걸린다."""
    cv2 = pytest.importorskip("cv2")
    np = pytest.importorskip("numpy")
    ok, buffer = cv2.imencode(".jpg", np.full((8, 8, 3), 128, dtype=np.uint8))
    assert ok
    payload = buffer.tobytes()
    frames = MjpegParser().feed(part(payload), 1000)
    assert len(frames) == 1
    assert decode_jpeg(frames[0].payload).shape == (8, 8, 3)


# ══════════════════════════════════════════════════════════════
#  연결과 재연결 (WBS 4.3.4)
# ══════════════════════════════════════════════════════════════
from host.vision.stream_client import StreamReader, boundary_from  # noqa: E402


class _FakeStream:
    """응답 하나를 흉내낸다. `chunks` 를 다 내주면 연결이 끊긴 것으로 본다."""

    def __init__(self, chunks: list[bytes], *, content_type: str = "") -> None:
        self._chunks = list(chunks)
        self.headers = {"Content-Type": content_type}

    def read(self, _size: int) -> bytes:
        return self._chunks.pop(0) if self._chunks else b""

    def __enter__(self) -> _FakeStream:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None


class _FakeNetwork:
    """붙을 때마다 미리 정한 대본대로 응답하거나 실패한다."""

    def __init__(self, script: list[object]) -> None:
        self.script = list(script)
        self.opened = 0
        self.slept: list[float] = []

    def opener(self, _url: str, timeout: float = 0.0):  # noqa: ARG002
        self.opened += 1
        if not self.script:
            raise OSError("대본 소진")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def sleeper(self, seconds: float) -> None:
        self.slept.append(seconds)


def _reader(cfg: dict, net: _FakeNetwork, clock=None) -> StreamReader:
    return StreamReader(
        _with_xiao(cfg, "10.0.0.9"),
        opener=net.opener,
        clock=clock or (lambda: 0),
        sleeper=net.sleeper,
    )


# ── 경계는 응답이 알려준다 ───────────────────────────────────
def test_boundary_comes_from_the_response() -> None:
    """펌웨어의 경계를 코드에 박으면 이름이 바뀌는 순간 프레임이 0개가 된다."""
    assert boundary_from("multipart/x-mixed-replace;boundary=abc-123") == "abc-123"
    assert boundary_from('multipart/x-mixed-replace; boundary="quoted"') == "quoted"
    assert boundary_from("multipart/x-mixed-replace") == DEFAULT_BOUNDARY
    assert boundary_from("") == DEFAULT_BOUNDARY


def test_reader_uses_the_advertised_boundary(cfg: dict) -> None:
    wire = part(jpeg(20), boundary="server-side")
    net = _FakeNetwork(
        [_FakeStream([wire], content_type="multipart/x-mixed-replace;boundary=server-side")]
    )
    frames = list(_reader(cfg, net).frames(max_frames=1))
    assert len(frames) == 1


# ── 백오프 ───────────────────────────────────────────────────
def test_backoff_grows_then_saturates(cfg: dict) -> None:
    """1 → 2 → 4 … 로 늘고 마지막 값에서 멈춘다 (최대 30초)."""
    net = _FakeNetwork([OSError("refused")] * 8)
    reader = _reader(cfg, net)
    list(reader.frames(max_failures=7))
    expected = cfg["vision"]["reconnect_backoff_s"]
    assert net.slept[: len(expected)] == [float(s) for s in expected]
    assert net.slept[-1] == float(expected[-1]), "마지막 값에서 포화한다"


def test_backoff_resets_only_after_a_frame(cfg: dict) -> None:
    """⚠️ **연결 성공으로 되돌리면 1초 간격 영구 재시도가 된다.**

    붙자마자 끊기는 상태에서는 백오프가 계속 늘어야 한다.
    """
    net = _FakeNetwork([_FakeStream([b""]), _FakeStream([b""]), _FakeStream([b""])])
    reader = _reader(cfg, net)
    list(reader.frames(max_failures=2))
    assert net.slept == [1.0, 2.0, 4.0], "프레임이 없었으니 계속 늘어난다"


def test_backoff_resets_when_a_frame_arrives(cfg: dict) -> None:
    net = _FakeNetwork(
        [
            OSError("첫 시도 실패"),
            _FakeStream([part(jpeg(20))]),  # 프레임 하나 받고 끊김
            OSError("다시 실패"),
        ]
    )
    reader = _reader(cfg, net)
    list(reader.frames(max_failures=2))
    assert net.slept[0] == 1.0
    assert net.slept[1] == 1.0, "프레임을 받았으므로 다시 1초부터"


def test_stream_recovers_and_keeps_yielding(cfg: dict) -> None:
    """차단 뒤 자동 복구 — DoD 조항이다."""
    net = _FakeNetwork(
        [
            _FakeStream([part(jpeg(10))]),
            OSError("강제 차단"),
            _FakeStream([part(jpeg(20)) + part(jpeg(30))]),
        ]
    )
    reader = _reader(cfg, net)
    frames = list(reader.frames(max_frames=3))
    assert [f.size_bytes for f in frames] == [14, 24, 34]
    assert net.opened == 3, "세 번 붙었다 — 그중 한 번은 열리지도 않았다"
    assert reader.stats.connects == 2, "connects 는 실제로 열린 횟수만 센다"
    assert reader.stats.failures >= 1


# ── 멈춘 스트림 ──────────────────────────────────────────────
def test_silent_stream_is_treated_as_broken(cfg: dict) -> None:
    """⚠️ **TCP 는 조용한 것과 살아 있는 것을 구분해 주지 않는다.**

    바이트는 오는데 프레임이 안 만들어지는 경우도 끊긴 것으로 본다 (NFR-2.6).
    """
    ticks = iter([0, 0, 500, 1000, 1500, 2000, 2500] + [3000] * 20)
    net = _FakeNetwork([_FakeStream([b"garbage"] * 6), _FakeStream([part(jpeg(20))])])
    reader = StreamReader(
        _with_xiao(cfg, "10.0.0.9"),
        opener=net.opener,
        clock=lambda: next(ticks),
        sleeper=net.sleeper,
    )
    frames = list(reader.frames(max_frames=1))
    assert reader.stats.stalls >= 1, "침묵을 감지해야 한다"
    assert len(frames) == 1, "그 뒤 다시 붙어 프레임을 받는다"


# ── 설정 검증 ────────────────────────────────────────────────
def test_backoff_must_be_positive_and_increasing(cfg: dict) -> None:
    """줄어드는 간격은 백오프가 아니라 폭주다."""
    from host.common.config import ConfigError, validate_base_config

    broken = dict(cfg)
    broken["vision"] = dict(cfg["vision"], reconnect_backoff_s=[4, 2, 1])
    with pytest.raises(ConfigError, match="증가하는"):
        validate_base_config(broken)

    broken["vision"] = dict(cfg["vision"], reconnect_backoff_s=[])
    with pytest.raises(ConfigError, match="비어 있지 않은"):
        validate_base_config(broken)

    broken["vision"] = dict(cfg["vision"], reconnect_backoff_s=[1, 0, 4])
    with pytest.raises(ConfigError, match="양수"):
        validate_base_config(broken)


def test_reader_refuses_empty_backoff(cfg: dict) -> None:
    bad = _with_xiao(cfg, "10.0.0.9")
    bad["vision"] = dict(cfg["vision"], reconnect_backoff_s=[])
    with pytest.raises(ValueError):
        StreamReader(bad)


# ══════════════════════════════════════════════════════════════
#  ⚠️ 실제 개체 프로파일의 구조를 물린다
# ══════════════════════════════════════════════════════════════
def test_endpoints_read_the_real_device_profile() -> None:
    """**시험이 만든 사전이 아니라 저장소의 실제 파일을 쓴다.**

    주소가 `network` 절 안에 있는데 코드가 최상위를 읽고 있었고, 시험은 최상위에
    값을 넣어 주고 있어서 통과했다. 실기 세션에서야 드러났을 버그다.
    """
    from host.common.config import load_config

    config = load_config("mechdog-01")
    assert "xiao_ip" in config["network"], "프로파일 구조가 바뀌면 여기서 걸린다"
    assert "xiao_ip" not in config, "최상위에는 없다 — 코드가 여기를 읽으면 안 된다"

    config["network"]["xiao_ip"] = "192.168.1.77"
    endpoints = stream_endpoints(config)
    assert endpoints.stream == "http://192.168.1.77:81/stream"
    assert endpoints.control == "http://192.168.1.77:80"


def test_real_profile_leaves_the_address_empty() -> None:
    """실측 전에는 비워 두는 것이 정본이다 — 틀린 주소보다 낫다."""
    from host.common.config import load_config

    config = load_config("mechdog-01")
    assert config["network"]["xiao_ip"] is None
    with pytest.raises(ValueError, match="network.xiao_ip"):
        stream_endpoints(config)


def test_runtime_reads_the_robot_address_from_network() -> None:
    """같은 실수가 명령 쪽에도 있었다 — `mechdog_ip` 도 `network` 절 안이다."""
    from conftest import FakeClock

    from host.common.config import load_config
    from host.runtime import Runtime

    config = load_config("mechdog-01")
    config["network"]["mechdog_ip"] = "192.168.1.88"
    runtime = Runtime(config, device_id="mechdog-01", clock=FakeClock())
    assert runtime.peer == ("192.168.1.88", config["network"]["cmd_port"])
