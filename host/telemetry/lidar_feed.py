"""LiDAR 스캔 수신 — 호스트 런타임의 순찰 길 찾기에 스캔을 넘긴다 (FR-7 · `--lidar-device`).

    LiDAR 소켓 ─▶ 수신 스레드 ─▶ 전방 위험거리 ─▶ 즉시 ESTOP (런타임 송신 락)   ← 패킷마다
                              └▶ 한 바퀴 조립 ─▶ 최신 한 바퀴 칸 ─▶ 운용 루프가 틱마다 꺼내 측위

⚠️ **측위에는 한 바퀴를 모아 넘긴다** (`RevolutionAssembler`). 실기 중계는 9.9Hz 한 바퀴를
약 7조각(패킷당 약 71점 · 약 50°)으로 보낸다(2026-10-01 실측). 예전에는 마지막 **패킷 한 개**만
측위에 넘겨 한쪽 50° 로 정합했다 — 시뮬 스캔은 한 장이 360° 라 드러나지 않았다. 전방 위험
판정은 지연을 늘리지 않게 패킷마다 그대로 한다.

⚠️ **스캔은 별도 스레드가 받는다.** 운용 루프(`Runtime.serve`)는 텔레메트리 소켓
하나에서 송신 마감까지 기다리므로, 같은 루프에서 스캔 소켓을 훑으면 그 대기가
LiDAR 비상정지를 늦춘다. 그래서 위험 판정은 받은 자리에서 하고 측위·계획은 루프가 한다.

⚠️ **이 스레드는 길 찾기 상태를 만지지 않는다.** 위험하면 `on_danger`(런타임의
`send_emergency_stop` — 송신 락 안에서 인코딩·송신) 만 부른다. 단계(`Phase`)를 여기서
바꾸면 루프가 그 틱의 계획을 세우는 중간에 바뀐다. 래치 이후는 로봇이 보고하는
`safety_latched` 로 FSM 이 `FAILSAFE` 로 간다.

⚠️ **ROS2 컨테이너로의 스캔 전달(WBS 5.4.4)도 이 스레드가 한다.** 중계 노드는 `scan_port` 한
곳으로만 보내므로, 받은 데이터그램을 개체 확인·디코드보다 먼저 바이트 그대로 `scan_forward_*` 로
복사한다(`host/telemetry/ros2_relay.py`). 실패해도 위험 판정과 최신 스캔 저장은 그대로 간다.
(ODOM 송신 — WBS 5.4.3 — 은 운용 루프의 몫이다: `Runtime.step`.)
"""

from __future__ import annotations

import math
import socket
import threading
from collections.abc import Callable, Mapping
from typing import Any

from host.behavior.planner import min_forward_distance
from host.common.config import ConfigError
from host.common.lidar_link import Scan, ScanDecoder, scan_of
from host.common.logging_setup import event_logger
from host.common.protocol import system_clock_ms
from host.common.units import deg_to_rad
from host.telemetry.ros2_relay import forward_peer_of, forward_scan, open_forward_socket

LOG = event_logger("mechadog.telemetry.lidar_feed")

#: 스캔 한 장의 최대 바이트 (`tools/ops/patrol_run.py` 와 같다).
RECV_BYTES = 65536
#: 수신 대기 한 번의 상한 — 멈춤 요청을 이만큼 늦게 본다.
POLL_S = 0.2
#: 한 바퀴 판정 — 5° 칸 72개 중 이만큼 덮이면 한 바퀴로 본다(330°).
REV_BINS = 72
REV_MIN_BINS = 66
#: 이보다 오래 모아도 덮이지 않으면 버린다 — 9.9Hz 한 바퀴는 약 100ms 다.
REV_MAX_AGE_MS = 300


class RevolutionAssembler:
    """패킷 조각을 한 바퀴로 모은다. 덮인 각도로 판정하므로 장착 보정 뒤 경계가 어디든 된다.

    같은 기기·같은 부팅의 패킷만 잇는다. 오래 모아도 덮이지 않은 조각은 **버린다** —
    덜 덮인 스캔을 측위에 넘기면 예전 결함(한쪽만 보고 정합)으로 돌아간다.
    """

    def __init__(self) -> None:
        self._parts: list[Scan] = []
        self._bins: set[int] = set()
        self._first_ms: int | None = None
        self.completed = 0
        self.discarded = 0

    def add(self, scan: Scan, now_ms: int) -> Scan | None:
        if self._parts and (
            scan.boot_id != self._parts[0].boot_id
            or scan.device_id != self._parts[0].device_id
            or (self._first_ms is not None and now_ms - self._first_ms > REV_MAX_AGE_MS)
        ):
            self.discarded += 1
            self._reset()
        if not self._parts:
            self._first_ms = now_ms
        self._parts.append(scan)
        for angle, _ in scan.points:
            self._bins.add(int((angle % (2 * math.pi)) / (2 * math.pi) * REV_BINS) % REV_BINS)
        if len(self._bins) < REV_MIN_BINS:
            return None
        merged = Scan(
            scan.device_id,
            scan.boot_id,
            scan.seq,
            scan.ts_ms,
            tuple(point for part in self._parts for point in part.points),
            sum(part.dropped for part in self._parts),
        )
        self.completed += 1
        self._reset()
        return merged

    def _reset(self) -> None:
        self._parts, self._bins, self._first_ms = [], set(), None


class LidarFeed:
    """스캔을 받아 위험을 즉시 알리고 최신 한 장만 남긴다. 소켓 없이도 `handle` 로 시험된다."""

    def __init__(
        self,
        *,
        lidar_device: str,
        decoder: ScanDecoder,
        forward_fan_rad: float,
        estop_m: float,
        armed: Callable[[], bool],
        on_danger: Callable[[], object],
        sock: socket.socket | None = None,
        forward_sock: socket.socket | None = None,
        forward_peer: tuple[str, int] | None = None,
        on_raw: Callable[[bytes, int], object] | None = None,
        clock: Callable[[], int] = system_clock_ms,
    ) -> None:
        self._lidar_device = lidar_device
        self._decoder = decoder
        self._fan_rad = forward_fan_rad
        self._estop_m = estop_m
        self._armed = armed
        self._on_danger = on_danger
        self._sock = sock
        self._forward_sock = forward_sock
        self._forward_peer = forward_peer
        self._forward_failing = False
        self._lock = threading.Lock()
        self._latest: Scan | None = None
        self._assembler = RevolutionAssembler()
        #: 원본 데이터그램을 받은 자리에서 기록기로 넘긴다 (`SessionRecorder.record_raw`).
        self._on_raw = on_raw
        self._clock = clock
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def handle(self, raw: bytes) -> None:
        """데이터그램 하나 — 기록 → 전달 → 디코드 → 개체 확인 → 전방 위험거리 → 한 바퀴 조립."""
        now_ms = self._clock()
        if self._on_raw is not None:
            self._on_raw(raw, now_ms)
        self._forward(raw)
        result = self._decoder.decode(raw)
        if result.warns:
            LOG.warning("scan_unknown_type", reason=result.reason)
            return
        scan = scan_of(result)
        if scan is None:
            return
        if scan.device_id != self._lidar_device:
            LOG.warning("foreign_lidar_scan", expected=self._lidar_device, received=scan.device_id)
            return
        # ⚠️ **순찰로 걷는 동안만 세운다.** 대기 중에 사람이 다가왔다고 래치를 걸면
        # 사람이 풀어야 하는 정지가 이유 없이 생긴다 — 온보드 초음파 반사는 그대로 있다.
        if self._armed():
            forward = min_forward_distance(scan.points, self._fan_rad)
            if forward is not None and forward < self._estop_m:
                LOG.error("lidar_estop", forward_m=round(forward, 3), limit_m=self._estop_m)
                self._on_danger()
        revolution = self._assembler.add(scan, now_ms)
        if revolution is None:
            return
        with self._lock:
            self._latest = revolution

    @property
    def revolutions(self) -> tuple[int, int]:
        """(완성한 바퀴 수, 덮이지 않아 버린 조각 묶음 수)."""
        return self._assembler.completed, self._assembler.discarded

    def _forward(self, raw: bytes) -> None:
        """컨테이너로 원본을 복사한다 — 디코드 성패·개체와 무관하다. 실패는 전이 때만 로그."""
        if self._forward_sock is None or self._forward_peer is None:
            return
        if forward_scan(self._forward_sock, raw, self._forward_peer):
            if self._forward_failing:
                LOG.info("scan_forward_recovered", peer=str(self._forward_peer))
                self._forward_failing = False
        elif not self._forward_failing:
            LOG.warning("scan_forward_failed", peer=str(self._forward_peer))
            self._forward_failing = True

    def take(self) -> Scan | None:
        """최신 스캔을 꺼낸다. 루프가 늦어 쌓인 옛 스캔은 버린다 — 측위는 최신 한 장이면 된다."""
        with self._lock:
            scan, self._latest = self._latest, None
        return scan

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="lidar-feed", daemon=True)
        self._thread.start()
        LOG.info("lidar_feed_started", lidar_device=self._lidar_device)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2 * POLL_S + 1.0)
        if self._sock is not None:
            self._sock.close()
        if self._forward_sock is not None:
            self._forward_sock.close()

    def _run(self) -> None:
        assert self._sock is not None
        self._sock.settimeout(POLL_S)
        while not self._stop.is_set():
            try:
                raw, _ = self._sock.recvfrom(RECV_BYTES)
            except (TimeoutError, ConnectionResetError):
                continue  # Windows ICMP — `host/runtime.py` 의 `open_socket` 설명 참고
            except OSError as exc:
                if self._stop.is_set():
                    return
                LOG.error("lidar_feed_failed", error=f"{type(exc).__name__}: {exc}")
                return
            try:
                self.handle(raw)
            except Exception as exc:  # noqa: BLE001 — 스캔 한 장이 수신 스레드를 죽이면 안 된다
                LOG.error("lidar_scan_failed", error=f"{type(exc).__name__}: {exc}")


def open_lidar_feed(
    config: Mapping[str, Any],
    lidar_device: str,
    *,
    armed: Callable[[], bool],
    on_danger: Callable[[], object],
    on_raw: Callable[[bytes, int], object] | None = None,
) -> LidarFeed:
    """`lidar.scan_port` 에 묶은 수신기. ⚠️ `SO_REUSEADDR` 를 쓰지 않는다(`runtime.open_socket`)."""
    lidar = config["lidar"]
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("", int(lidar["scan_port"])))
    # 이름 해석 실패(`ConfigError`)는 소켓을 더 열기 전에 난다. 꺼 두면 소켓도 열지 않는다.
    try:
        forward_peer = forward_peer_of(lidar)
    except ConfigError:
        sock.close()
        raise
    return LidarFeed(
        lidar_device=lidar_device,
        decoder=ScanDecoder(
            float(lidar.get("mount_yaw_deg", 0.0)), int(lidar.get("angle_direction", 1))
        ),
        forward_fan_rad=deg_to_rad(float(lidar["forward_fan_deg"])),
        estop_m=float(lidar["estop_distance_mm"]) / 1000.0,
        armed=armed,
        on_danger=on_danger,
        sock=sock,
        forward_sock=None if forward_peer is None else open_forward_socket(),
        forward_peer=forward_peer,
        on_raw=on_raw,
    )
