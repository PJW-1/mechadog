"""ROS2 컨테이너로 가는 두 갈래 — 스캔 전달(WBS 5.4.4)과 ODOM 송신(WBS 5.4.3).

    LiDAR 데이터그램 ─▶ (그대로 복사) ─▶ `scan_forward_host:scan_forward_port`
    보낸 명령 + IMU yaw ─▶ `Odometry` ─▶ ODOM(10Hz) ─▶ `odom_host:odom_port`

중계 노드 펌웨어는 `scan_port` 한 곳으로만 보내므로 그 수신자(런타임의 `LidarFeed` ·
`tools/ops/patrol_run.py`)가 컨테이너에 대신 넘긴다. 두 곳이 같은 코드를 쓴다.
"""

from __future__ import annotations

import secrets
import socket
import threading
from collections.abc import Mapping
from typing import Any

from host.common.config import ConfigError
from host.common.logging_setup import event_logger
from host.common.odom_link import OdomEncoder
from host.slam.odometry import Odometry, hold_of_reading, odom_params_from_config

LOG = event_logger("mechadog.telemetry.ros2_relay")


def open_forward_socket() -> socket.socket:
    """컨테이너 전달 전용 송신 소켓 (WBS 5.4.4).

    ⚠️ **`scan_sock` 으로 보내지 않는다.** Windows 는 닫힌 포트로 보낸 UDP 의
    ICMP 통보를 보낸 소켓의 다음 `recvfrom` 에 `ConnectionResetError` 로 돌려준다
    (`SIO_UDP_CONNRESET` 은 CPython 에 없어 끌 수 없다 · `host/runtime.py` 머리말).
    컨테이너가 꺼져 있으면 스캔 수신 루프가 스캔마다 끊겨 LiDAR 비상정지가 늦는다.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setblocking(False)
    return sock


def forward_peer_of(lidar: Mapping[str, Any]) -> tuple[str, int] | None:
    """전달 목적지. 꺼 두면 None 이다 (WBS 5.4.4).

    ⚠️ **호스트명은 기동 때 한 번만 푼다.** `sendto` 에 이름을 그대로 넘기면
    스캔마다 동기 DNS 조회가 스캔 수신 루프 안에서 돌아 LiDAR 비상정지가 늦는다.
    """
    if not lidar["scan_forward_enabled"]:
        return None
    return (_resolve(lidar, "scan_forward_host"), int(lidar["scan_forward_port"]))


def odom_peer_of(lidar: Mapping[str, Any]) -> tuple[str, int]:
    """ODOM 목적지 (WBS 5.4.3). `forward_peer_of` 와 같은 이유로 이름을 기동 때 한 번만 푼다."""
    return (_resolve(lidar, "odom_host"), int(lidar["odom_port"]))


def _resolve(lidar: Mapping[str, Any], key: str) -> str:
    """이름을 못 풀면 `ConfigError` 다 — `main()` 이 traceback 대신 «설정 오류» 로 알린다."""
    try:
        return socket.gethostbyname(str(lidar[key]))
    except OSError as exc:
        raise ConfigError(f"lidar.{key} 를 풀 수 없음: {lidar[key]!r} ({exc})") from exc


def forward_scan(sock: socket.socket, raw: bytes, peer: tuple[str, int]) -> bool:
    """받은 LiDAR 데이터그램을 컨테이너 전달 목적지로 그대로 복사한다 (WBS 5.4.4).

    **디코드 성패와 무관하게** 받은 바이트를 그대로 보낸다 — 검증은 받는 쪽
    (`docker/ros2/scan_bridge.py` 의 `ScanDecoder`)이 다시 하므로 여기서 거르면
    컨테이너가 우리가 이미 버린 패킷의 존재조차 모르게 된다. LiDAR 비상정지
    (`guard_scan`)는 이 전달과 무관한 직접 경로라 실패해도 영향이 없다.

    실패(목적지가 아직 없어 나는 `ConnectionResetError` · 그 외 `OSError`)는
    예외를 올리지 않는다 — 순찰을 멈출 이유가 아니다. 반환값만 알리고 로그는
    호출부가 상태 전이일 때만 남긴다 (ENGINEERING_GUIDE 1.3).
    """
    try:
        sock.sendto(raw, peer)
    except OSError:
        return False
    return True


def send(
    sock: socket.socket | None,
    peer: tuple[str, int] | None,
    lines: list[str] | tuple[str, ...],
) -> list[str]:
    """보낸 전문을 돌려준다 — 오도메트리는 **실제로 나간 명령**만 적분한다."""
    if sock is None or peer is None:
        return []
    sent = []
    for line in lines:
        try:
            sock.sendto(line.encode("utf-8"), peer)
        except OSError:
            # Windows 는 상대가 없으면 ICMP 로 예외를 낸다. UDP 는 도달을 보장하지
            # 않으므로 여기서 재시도하지 않는다 — 다음 틱이 100ms 뒤에 온다.
            continue
        sent.append(line)
    return sent


def open_odometry(config: Mapping[str, Any], device_id: str) -> tuple[Odometry | None, OdomEncoder]:
    """오도메트리와 ODOM 인코더. **보행 실측이 없는 기체는 `None`** (WBS 5.4.3).

    순찰은 멈추지 않는다 — 지금 순찰 측위는 `PatrolController` 의 스캔 정합이고,
    오도메트리는 컨테이너의 `slam_toolbox` 에만 간다. 대신 크게 남긴다.
    """
    # `boot_id` 는 이 프로세스 한 번의 실행이다 — 다시 켜면 `seq` 가 1 로 돌아온다.
    encoder = OdomEncoder(device_id, secrets.token_hex(8))
    try:
        return Odometry(odom_params_from_config(config)), encoder
    except ConfigError as exc:
        LOG.error("odometry_unavailable", reason=str(exc), effect="odom->base_link 없음")
        return None, encoder


class OdomSender:
    """오도메트리를 쌓고 `odom_rate_hz` 주기로 ODOM 을 보낸다. 시각은 호출자가 준다.

    ⚠️ **`Odometry` 는 스레드 안전하지 않다.** 보낸 명령은 틱(루프 스레드)과 관제 ESTOP
    (대시보드·스캔 스레드)에서 모두 들어오므로 모든 입구를 한 락으로 묶는다. 이 락은
    다른 락을 잡지 않아 `Runtime._send_lock` 안에서 불러도 교착하지 않는다.
    """

    def __init__(
        self,
        odometry: Odometry,
        encoder: OdomEncoder,
        sock: socket.socket,
        peer: tuple[str, int],
        period_ms: int,
    ) -> None:
        self._odometry = odometry
        self._encoder = encoder
        self._sock = sock
        self._peer = peer
        self._period_ms = period_ms
        self._lock = threading.Lock()
        self._due_ms = 0
        self._failing = False

    def note_sent(self, lines: list[str], sent_ms: int) -> None:
        """로봇으로 **실제로 나간** 전문을 넣는다."""
        if not lines:
            return
        with self._lock:
            self._odometry.note_sent(lines, sent_ms)

    def note_telemetry(self, reading: Any, now_ms: int) -> None:
        """로봇이 스스로 멈춰 있다는 보고가 명령 추정보다 먼저다 (odometry.py 머리말)."""
        with self._lock:
            self._odometry.note_hold(
                hold_of_reading(reading.safety_latched, reading.obstacle), now_ms
            )
            if reading.yaw is not None:
                self._odometry.note_imu(reading.yaw, now_ms, reading.boot_id)

    def publish(self, now_ms: int) -> None:
        """마감이 됐으면 ODOM 한 건. 송신 실패는 상태가 바뀔 때만 한 줄 남기고 멈추지 않는다."""
        with self._lock:
            if now_ms < self._due_ms:
                return
            self._due_ms = now_ms + self._period_ms
            pose = self._odometry.pose(now_ms)
            line = self._encoder.encode(
                ts_ms=pose.stamp_ms,
                x_m=pose.x_m,
                y_m=pose.y_m,
                yaw_rad=pose.yaw_rad,
                valid=pose.valid,
            )
        if send(self._sock, self._peer, [line]):
            if self._failing:
                LOG.info("odom_send_recovered", peer=str(self._peer))
                self._failing = False
        elif not self._failing:
            LOG.warning("odom_send_failed", peer=str(self._peer))
            self._failing = True

    def close(self) -> None:
        self._sock.close()


def open_odom_sender(config: Mapping[str, Any], device_id: str) -> OdomSender | None:
    """ODOM 송신기. 실측이 없는 기체는 `None` 이고 목적지도 풀지 않는다."""
    odometry, encoder = open_odometry(config, device_id)
    if odometry is None:
        return None
    lidar = config["lidar"]
    peer = odom_peer_of(lidar)  # 이름을 못 풀면 소켓을 열기 전에 실패한다
    # 명령 소켓과 **따로 연다.** 컨테이너가 없으면 ICMP 오류가 소켓에 남는데, 같은
    # 소켓이면 그 오류가 다음 로봇 명령 송신에서 터져 명령 하나를 잃을 수 있다.
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    return OdomSender(odometry, encoder, sock, peer, round(1000 / float(lidar["odom_rate_hz"])))
