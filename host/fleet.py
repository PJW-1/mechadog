"""여러 대를 한 프로세스로 — 다중 개체 관제 (MD-01 ~ MD-03).

왜 한 프로세스인가: 펌웨어는 텔레메트리를 **호스트의 고정 포트(5101)** 로 보낸다
(`telemetry_publisher.cpp` 의 `kTelemetryPort`). 런타임을 로봇마다 따로 띄우면 둘째부터
5101 을 묶지 못한다. 펌웨어를 로봇마다 다시 굽는 대신, 5101 을 **한 번만** 묶고 받은
텔레메트리를 **개체 ID** 로 나눠 로봇별 `Runtime` 에 넘긴다.

로봇 하나의 판단(FSM·단계·인증·비전)은 전부 한 대짜리 `Runtime` 그대로다. 여기서
새로 하는 일은 소켓 하나를 나눠 쓰는 것과 관제 서버를 `/robots/<id>` 로 묶는 것뿐이다.

사용:

    python -m host.fleet --devices mechdog-01 mechdog-02 mechdog-03 --dashboard-port 8000
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import threading
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from typing import Any

from host.behavior.mission import Mission
from host.common.blackbox import EventBlackbox
from host.common.config import ConfigError, load_config
from host.common.logging_setup import LogContext, event_logger, setup_logging
from host.common.protocol import system_clock_ms
from host.dashboard.state import DashboardState
from host.runtime import (
    RECV_BYTES,
    WSAEMSGSIZE,
    Runtime,
    _is_oversized_datagram,
    _publish_event,
    dashboard_wiring,
    open_socket,
)
from host.vision.worker import build_worker

LOG = event_logger("mechadog.fleet")

_CURRENT = threading.local()


class FleetLogContext(LogContext):
    """운용 스레드가 **지금 다루는 로봇**의 컨텍스트를 레코드에 싣는다.

    로그 핸들러는 프로세스에 하나라 컨텍스트도 하나다. 그대로 두면 세 로봇의 기록이
    전부 같은 `device_id` 로 찍힌다. 운용 루프가 로봇마다 바꿔 끼우고, 다른 스레드
    (관제 웹)는 바꿔 끼운 적이 없으므로 플릿 자신(`fleet`)으로 찍힌다 — 틀린 로봇
    이름보다 "어느 로봇인지 모름" 이 낫다.
    """

    def as_dict(self) -> dict[str, Any]:
        current = getattr(_CURRENT, "context", None)
        return current.as_dict() if current is not None else super().as_dict()


@contextlib.contextmanager
def _acting(runtime: Runtime) -> Iterator[None]:
    _CURRENT.context = runtime.context
    try:
        yield
    finally:
        _CURRENT.context = None


def telemetry_device(data: bytes) -> str | None:
    """텔레메트리의 개체 ID. 텔레메트리가 아니면(명령 응답 문자열·깨진 줄) None."""
    try:
        message = json.loads(data)
    except (ValueError, UnicodeDecodeError):
        return None
    device = message.get("device_id") if isinstance(message, dict) else None
    return device if isinstance(device, str) and device else None


class Fleet:
    """로봇별 `Runtime` 여럿을 소켓 하나로 돌린다."""

    def __init__(self, runtimes: Sequence[Runtime]) -> None:
        if not runtimes:
            raise ValueError("로봇이 하나도 없다")
        self._runtimes = list(runtimes)
        self.unrouted = 0
        self._unrouted_seen: set[str] = set()

    def route(self, data: bytes, addr: tuple[str, int]) -> Runtime | None:
        """이 datagram 을 받을 로봇.

        ⚠️ **텔레메트리는 IP 가 아니라 개체 ID 로 가른다** (DR-17). 명령 응답은 개체
        ID 가 없는 문자열이라, 이미 그 주소로 명령을 보내고 있는 로봇에게만 넘긴다.
        응답은 세기만 하고 판단에 쓰지 않는다.
        """
        device = telemetry_device(data)
        if device is not None:
            owner = next((rt for rt in self._runtimes if rt.owns(device)), None)
            if owner is None and device not in self._unrouted_seen:
                self._unrouted_seen.add(device)
                LOG.warning("fleet_unknown_device", received=device)
            return owner
        return next(
            (rt for rt in self._runtimes if rt.peer is not None and rt.peer[0] == addr[0]),
            None,
        )

    def serve(
        self,
        sock: Any,
        *,
        duration_s: float | None = None,
        clock: Callable[[], int] = system_clock_ms,
    ) -> None:
        """운용 루프. `Runtime.serve` 와 같은 순서를 로봇마다 돈다."""
        for runtime in self._runtimes:
            with _acting(runtime):
                runtime.begin(sock)
        started = clock()
        end_ms = started + int(duration_s * 1000) if duration_s is not None else None
        try:
            while end_ms is None or clock() < end_ms:
                now = clock()
                wait_ms = min(
                    0 if (due := runtime.next_due_ms) is None else max(0, due - now)
                    for runtime in self._runtimes
                )
                if end_ms is not None:
                    wait_ms = min(wait_ms, max(0, end_ms - now))
                sock.settimeout(wait_ms / 1000)
                try:
                    data, addr = sock.recvfrom(RECV_BYTES)
                except (TimeoutError, BlockingIOError, ConnectionResetError):
                    pass  # `Runtime.serve` 와 같은 이유 — 받을 것이 없거나 Windows ICMP
                except OSError as exc:
                    if not _is_oversized_datagram(exc):
                        raise
                    LOG.warning(
                        "telemetry_datagram_too_large", max_bytes=RECV_BYTES, winerror=WSAEMSGSIZE
                    )
                else:
                    owner = self.route(data, addr)
                    if owner is None:
                        self.unrouted += 1
                    else:
                        with _acting(owner):
                            owner.receive(data, addr, clock())
                for runtime in self._runtimes:
                    with _acting(runtime):
                        runtime.step(clock())
        finally:
            # ⚠️ **전부 먼저 세운다.** 한 대의 비전 워커 정리를 기다리는 동안 다른
            # 로봇이 마지막 이동 명령을 계속 실행하면 안 된다.
            for runtime in self._runtimes:
                with _acting(runtime):
                    runtime.stop_robot(sock)
            for runtime in self._runtimes:
                with _acting(runtime):
                    runtime.release()


@dataclass
class Member:
    device_id: str
    config: dict[str, Any]
    mission: Mission
    registered: bool


def _pairs(values: Sequence[str], flag: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for value in values:
        device, sep, address = value.partition("=")
        if not sep or not device or not address:
            raise ConfigError(f"{flag} 는 <개체>=<주소> 형식이어야 함: {value!r}")
        out[device] = address
    return out


def load_members(
    devices: Sequence[str],
    *,
    mode: str | None,
    robot_ips: dict[str, str],
    xiao_ips: dict[str, str],
    log_level: str | None,
) -> list[Member]:
    """개체마다 설정을 읽는다. **소켓을 나눠 쓰므로 포트가 같아야 한다.**"""
    if len(set(devices)) != len(devices):
        raise ConfigError("같은 개체를 두 번 넣었다")
    unknown = (set(robot_ips) | set(xiao_ips)) - set(devices)
    if unknown:
        raise ConfigError(f"--devices 에 없는 개체의 주소: {sorted(unknown)}")
    members = []
    for device in devices:
        config = dict(load_config(device))
        network = dict(config["network"])
        if device in robot_ips:
            network["mechdog_ip"] = robot_ips[device]
        if device in xiao_ips:
            network["xiao_ip"] = xiao_ips[device]
        config["network"] = network
        # 사건 폴더를 로봇마다 나눈다 — 한 폴더면 `/robots/<id>` 의 사건 그림 경로가
        # 다른 로봇의 기록도 내준다.
        logging_section = dict(
            config["logging"], blackbox_dir=f"{config['logging']['blackbox_dir']}/{device}"
        )
        if log_level:
            logging_section["level"] = log_level.upper()
        config["logging"] = logging_section
        members.append(
            Member(
                device_id=device,
                config=config,
                mission=Mission(config, mode=mode),
                # 텔레메트리 개체 ID(MAC)가 없으면 실물이 아직 없다 — 화면에 «미등록».
                registered=config.get("telemetry_device_id") is not None,
            )
        )
    ports = {int(m.config["network"]["telemetry_port"]) for m in members}
    if len(ports) != 1:
        raise ConfigError(
            f"개체들의 telemetry_port 가 다르다: {sorted(ports)} — 한 소켓을 나눠 쓴다"
        )
    return members


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="여러 대를 한 프로세스로 운용한다 (텔레메트리 5101 공유)"
    )
    parser.add_argument(
        "--devices", nargs="+", required=True, help="개체 id 들 — config/devices/<id>.yaml"
    )
    parser.add_argument(
        "--dashboard-port", type=int, required=True, help="PC 로컬 관제 서버 포트 (예: 8000)"
    )
    parser.add_argument(
        "--robot-ip",
        action="append",
        default=[],
        help="<개체>=<IP> — 설정의 mechdog_ip 를 덮어쓴다",
    )
    parser.add_argument(
        "--xiao-ip", action="append", default=[], help="<개체>=<IP> — 설정의 xiao_ip 를 덮어쓴다"
    )
    parser.add_argument(
        "--no-vision",
        action="store_true",
        help="진단용: 카메라·검출 워커 없이 모션 런타임만 실행한다",
    )
    parser.add_argument("--mode", default=None, help="운용 모드 — guard | factory")
    parser.add_argument("--duration", type=float, default=None, help="N초 후 종료 (기본 무한)")
    parser.add_argument("--log-level", default=None, help="config.logging.level 을 덮어쓴다")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not 1 <= args.dashboard_port <= 65535:
        raise SystemExit("dashboard-port must be between 1 and 65535")
    try:
        members = load_members(
            args.devices,
            mode=args.mode,
            robot_ips=_pairs(args.robot_ip, "--robot-ip"),
            xiao_ips=_pairs(args.xiao_ip, "--xiao-ip"),
            log_level=args.log_level,
        )
        if args.no_vision and any(m.mission.enables("ppe") for m in members):
            raise ConfigError("factory 모드는 PPE 비전 없이 시작할 수 없다")
    except (ConfigError, OSError) as exc:
        logging.basicConfig(level="ERROR")
        logging.getLogger("mechadog.fleet").error("설정을 읽을 수 없다 — %s", exc)
        return 2

    setup_logging(members[0].config, device_id="fleet", context=FleetLogContext())

    from host.dashboard.server import create_app, create_fleet_app, serving

    runtimes: list[Runtime] = []
    apps = {}
    for member in members:
        config = member.config
        # ⚠️ 카메라 주소가 없는 로봇은 비전 없이 돈다. 없는 주소로 워커를 켜면
        # 스트림 연결만 계속 실패한다.
        vision = None
        if not args.no_vision and config["network"].get("xiao_ip"):
            vision = build_worker(config)
        elif not args.no_vision:
            LOG.warning("fleet_vision_off", device=member.device_id, reason="xiao_ip 없음")
        blackbox = EventBlackbox(config)
        dashboard = DashboardState(
            member.device_id, stale_after_ms=int(config["safety"]["link_loss_failsafe_ms"])
        )
        runtime = Runtime(
            config,
            device_id=member.device_id,
            vision=vision,
            blackbox=blackbox,
            dashboard=dashboard,
            mission=member.mission,
            event_publisher=_publish_event(dashboard),
        )
        runtimes.append(runtime)
        apps[member.device_id] = create_app(
            dashboard,
            static_dir=None,
            **dashboard_wiring(runtime, config, vision=vision, blackbox=blackbox),
        )
    fleet_app = create_fleet_app(apps, registered={m.device_id: m.registered for m in members})

    # ⚠️ 콘솔 확인 키를 붙이지 않는다 — 여러 대면 그 키가 어느 로봇의 확인인지
    # 정할 수 없다. 경보 확인·안전 해제는 관제 화면의 로봇별 버튼으로 한다.
    sock = open_socket(runtimes[0].telemetry_port)
    try:
        with serving(fleet_app, args.dashboard_port):
            LOG.info(
                "fleet_started", devices=[m.device_id for m in members], port=args.dashboard_port
            )
            Fleet(runtimes).serve(sock, duration_s=args.duration)
    except KeyboardInterrupt:
        LOG.info("interrupted", action="모든 로봇에 ESTOP 송신 후 종료")
    finally:
        sock.close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
