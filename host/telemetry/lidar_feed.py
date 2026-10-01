"""LiDAR 스캔 수신 — 호스트 런타임의 순찰 길 찾기에 스캔을 넘긴다 (FR-7 · `--lidar-device`).

    LiDAR 소켓 ─▶ 수신 스레드 ─▶ 전방 위험거리 ─▶ 즉시 ESTOP (런타임 송신 락)
                              └▶ 최신 스캔 한 칸 ─▶ 운용 루프가 틱마다 꺼내 측위

⚠️ **스캔은 별도 스레드가 받는다.** 운용 루프(`Runtime.serve`)는 텔레메트리 소켓
하나에서 송신 마감까지 기다리므로, 같은 루프에서 스캔 소켓을 훑으면 그 대기가
LiDAR 비상정지를 늦춘다. 그래서 위험 판정은 받은 자리에서 하고 측위·계획은 루프가 한다.

⚠️ **이 스레드는 길 찾기 상태를 만지지 않는다.** 위험하면 `on_danger`(런타임의
`send_emergency_stop` — 송신 락 안에서 인코딩·송신) 만 부른다. 단계(`Phase`)를 여기서
바꾸면 루프가 그 틱의 계획을 세우는 중간에 바뀐다. 래치 이후는 로봇이 보고하는
`safety_latched` 로 FSM 이 `FAILSAFE` 로 간다.

(범위 밖) ROS2 컨테이너로의 스캔 전달(WBS 5.4.4)과 ODOM 송신(WBS 5.4.3)은 아직
`tools/ops/patrol_run.py` 에만 있다 — 이 수신기가 `scan_port` 를 쥐는 동안 컨테이너는 스캔을 못 받는다.
"""

from __future__ import annotations

import socket
import threading
from collections.abc import Callable, Mapping
from typing import Any

from host.behavior.planner import min_forward_distance
from host.common.lidar_link import Scan, ScanDecoder, scan_of
from host.common.logging_setup import event_logger
from host.common.units import deg_to_rad

LOG = event_logger("mechadog.telemetry.lidar_feed")

#: 스캔 한 장의 최대 바이트 (`tools/ops/patrol_run.py` 와 같다).
RECV_BYTES = 65536
#: 수신 대기 한 번의 상한 — 멈춤 요청을 이만큼 늦게 본다.
POLL_S = 0.2


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
    ) -> None:
        self._lidar_device = lidar_device
        self._decoder = decoder
        self._fan_rad = forward_fan_rad
        self._estop_m = estop_m
        self._armed = armed
        self._on_danger = on_danger
        self._sock = sock
        self._lock = threading.Lock()
        self._latest: Scan | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def handle(self, raw: bytes) -> None:
        """데이터그램 하나 — 디코드 → 개체 확인 → 전방 위험거리 → 최신 칸."""
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
        with self._lock:
            self._latest = scan

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
) -> LidarFeed:
    """`lidar.scan_port` 에 묶은 수신기. ⚠️ `SO_REUSEADDR` 를 쓰지 않는다(`runtime.open_socket`)."""
    lidar = config["lidar"]
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("", int(lidar["scan_port"])))
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
    )
