"""가상 MechDog — 통신 상대방으로서의 로봇 (WBS 6.1.1).

명령 포트에서 듣고, 가짜 텔레메트리를 10Hz 로 돌려주고, 주문한 대로 고장난다.
호스트 코드 입장에서는 네트워크 저쪽에 진짜 로봇이 있는 것과 구분되지 않는다.

**물리 시뮬레이터가 아니다.** 보행을 계산하지도, 공간을 그리지도 않는다.
흉내 내는 것은 *프로토콜 참여자로서의 로봇*뿐이다 — 명령을 받고, 상태를
보고하고, 가끔 고장나는 역할. 실제로 걷는지는 G1 실기 검수(6.4.1)에서 본다.

왜 필요한가 (WBS 8절):

  ① **CI 러너에는 로봇이 없다.** 로봇을 몇 대 갖고 있든 GitHub Actions 는
     우분투 컨테이너다. 목업이 없으면 통신 계층은 CI 에서 영원히 검증되지 않는다.
  ② **안전 시나리오를 실물로 반복 재현하기 어렵다.** 전도 시험은 로봇을 일부러
     넘어뜨려야 하고 서보 기어가 상한다. 저전압은 방전에 수십 분이 걸린다.
     여기서는 플래그 하나이고, `--seed` 로 같은 고장을 몇 번이든 재현한다.

⚠️ **이 파일은 온보드 안전 로직의 구현이 아니다.** Tier 1 은 펌웨어(WBS 3.2,
L1·L2)에 있고 여기 있는 것은 그 *대역*이다. 임계값을 `config.yaml` 에서 읽는
이유도 그래서다 — 목업이 자체 숫자를 갖게 되면 호스트를 진짜와 다른 기준으로
시험하게 된다.

사용:

    python tools/mock_mechdog.py --device mechdog-ref
    python tools/mock_mechdog.py --tip-at 30 --drop-rate 0.2 --seed 42
"""

import argparse
import contextlib
import math
import random
import socket
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import yaml

# `python tools/mock_mechdog.py` 로 직접 실행해도 host/ 를 찾게 한다.
# (pytest 는 pyproject 의 pythonpath 설정으로 해결된다)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from host.common.protocol import (  # noqa: E402
    BATT_MAX_V,
    BATT_MIN_V,
    CommandDecoder,
    DecodeResult,
    TelemetryEncoder,
    system_clock_ms,
)

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config" / "config.yaml"


def load_config(path: Path = CONFIG) -> dict:
    """⚠️ 임시 로더. WBS 4.4.1 의 `common/config.py` 가 들어오면 그것으로 교체한다.

    지금 포트·임계값을 코드에 박으면 4.4.1 에서 걷어내야 하므로, 임시로라도
    YAML 을 읽는 쪽이 낫다 (NFR-3① 매직 넘버 0개).
    """
    return yaml.safe_load(path.read_text(encoding="utf-8"))


# ══════════════════════════════════════════════════════════════
#  장애 주입
# ══════════════════════════════════════════════════════════════


@dataclass
class Faults:
    """주문 가능한 고장. 전부 끄면 정상 동작하는 로봇이 된다.

    시각 기준 옵션(`*_at_s`)은 **기동 후 경과 초**다.
    """

    drop_rate: float = 0.0  # 수신 명령 유실률 (0.0~1.0)
    corrupt_rate: float = 0.0  # 깨진 텔레메트리 송신률 — 호스트 규칙 ③ 시험용
    battery_start_v: float | None = None  # 기본은 만충
    battery_drain_v_per_min: float = 0.0  # 방전 속도
    tip_at_s: float | None = None  # 이 시각에 전도
    obstacle_at_s: float | None = None  # 이 시각에 장애물 출현 (초음파 반사)
    go_silent_at_s: float | None = None  # 이 시각부터 텔레메트리 중단 (링크 두절)
    seed: int | None = None  # 같은 고장을 재현하기 위한 난수 씨앗


@dataclass
class Stats:
    received: int = 0
    accepted: int = 0
    clamped: int = 0
    discarded: int = 0
    dropped: int = 0  # 장애 주입으로 버린 것 (수신조차 안 한 셈)
    sent: int = 0

    def summary(self) -> str:
        return (
            f"수신 {self.received} · 수락 {self.accepted} · 클램핑 {self.clamped} · "
            f"폐기 {self.discarded} · 유실 {self.dropped} · 송신 {self.sent}"
        )


# ══════════════════════════════════════════════════════════════
#  로봇 — 순수 로직 (소켓을 모른다)
# ══════════════════════════════════════════════════════════════


@dataclass
class _LastCommand:
    """마지막으로 받아들인 명령. 텔레메트리의 근거가 된다."""

    at_ms: int = 0
    type: str = ""
    step: float = 0.0
    angle: float = 0.0
    pitch: float = 0.0


class MockRobot:
    """통신 상대방으로서의 로봇.

    시각을 인자로 받는다 (ENGINEERING_GUIDE 2.1). 그래서 30초 뒤의 전도도,
    10분간의 방전도 pytest 안에서 즉시 검증된다 — 소켓도 대기도 없이.
    """

    def __init__(
        self,
        device_id: str,
        cfg: dict,
        faults: Faults | None = None,
        start_ms: int = 0,
    ) -> None:
        self._cfg = cfg
        self._safety = cfg["safety"]
        self._faults = faults or Faults()
        self._rng = random.Random(self._faults.seed)
        self._start_ms = start_ms
        self._decoder = CommandDecoder()
        self._encoder = TelemetryEncoder(device_id, clock=lambda: self._now_ms)
        self._now_ms = start_ms
        self._last = _LastCommand(at_ms=start_ms)
        self._link_seen = False  # 한 번이라도 유효 명령을 받았는가
        self.stats = Stats()

    # ── 수신 ────────────────────────────────────────────────

    def receive(self, raw: str | bytes, now_ms: int) -> DecodeResult | None:
        """명령 한 건을 처리한다. `None` 은 장애 주입으로 유실시켰다는 뜻이다.

        유실은 파싱 실패와 다르다 — 패킷이 도착조차 하지 않은 것이므로
        수신 통계에도 잡히지 않는다. UDP 에서 실제로 일어나는 일이다.
        """
        self._now_ms = now_ms
        if self._rng.random() < self._faults.drop_rate:
            self.stats.dropped += 1
            return None

        self.stats.received += 1
        result = self._decoder.decode(raw)
        if not result.accepted:
            self.stats.discarded += 1
            return result

        self.stats.accepted += 1
        if result.clamped:
            self.stats.clamped += 1

        # 규칙 ③ — 받아들인 패킷만 링크 타임아웃을 갱신한다.
        msg = result.message
        self._link_seen = True
        self._last = _LastCommand(
            at_ms=now_ms,
            type=msg["type"],
            step=msg.get("step", 0.0) if msg["type"] == "MOVE" else 0.0,
            angle=msg.get("angle", 0.0) if msg["type"] == "MOVE" else 0.0,
            pitch=msg.get("pitch", self._last.pitch) if msg["type"] == "POSE" else 0.0,
        )
        return result

    # ── 로봇이 스스로 아는 것들 ────────────────────────────

    def _elapsed_s(self, now_ms: int) -> float:
        return (now_ms - self._start_ms) / 1000.0

    def _fault_active(self, at_s: float | None, now_ms: int) -> bool:
        return at_s is not None and self._elapsed_s(now_ms) >= at_s

    def battery_v(self, now_ms: int) -> float:
        """방전 곡선. 물리 하한에서 멈춘다.

        6.0V 아래로 내려가면 호스트가 규칙 ④로 **레코드 자체를 폐기**하므로,
        정작 보여주려던 셧다운 동작이 화면에 나타나지 않는다. 하한에서 붙든다.
        """
        start = self._faults.battery_start_v
        if start is None:
            start = BATT_MAX_V  # 만충
        drained = start - self._faults.battery_drain_v_per_min * (self._elapsed_s(now_ms) / 60.0)
        return round(max(BATT_MIN_V, drained), 2)

    def distance_cm(self, now_ms: int) -> int:
        """초음파. 장애물 주입 전에는 복도를 걷는 정도의 값을 흔들어 준다."""
        if self._fault_active(self._faults.obstacle_at_s, now_ms):
            return max(0, self._safety["obstacle_stop_cm"] - 5)
        return int(170 + 30 * math.sin(self._elapsed_s(now_ms)))

    def tipped(self, now_ms: int) -> bool:
        return self._fault_active(self._faults.tip_at_s, now_ms)

    def last_cmd_age_ms(self, now_ms: int) -> int:
        return max(0, now_ms - self._last.at_ms)

    def link_ok(self, now_ms: int) -> bool:
        if not self._link_seen:
            return False
        return self.last_cmd_age_ms(now_ms) <= self._safety["link_loss_failsafe_ms"]

    def stopped_by_timeout(self, now_ms: int) -> bool:
        """300ms 무명령 → `move(0,0)`. 상태 전이가 아니라 Tier 1 반사다 (PRD 5절)."""
        return not self._link_seen or self.last_cmd_age_ms(now_ms) > self._safety["cmd_timeout_ms"]

    def state(self, now_ms: int) -> str:
        """⚠️ **로봇이 스스로 알 수 있는 상태만 낸다.**

        FSM 은 Host PC 에서 돈다 (PRD 5절). 로봇은 자기가 `ALERT` 인지 `TRACK`
        인지 알 방법이 없다 — 그 판단은 호스트에 있고, 명령 7종 중 상태를
        알려주는 것이 없다. 그래서 여기서 나오는 값은 세 가지뿐이다.

          · `FAILSAFE` — 링크 두절 · 저전압 · 전도 (전부 Tier 1 온보드 판정)
          · `AVOID`    — 초음파 반사 정지 (FR-2.2, Tier 1)
          · `PATROL`   — 그 외

        나머지 5종을 텔레메트리에 실으려면 호스트가 상태를 내려보내야 한다.
        규약의 열린 구멍이며 팀장 판단 대기 중이다.
        """
        if not self.link_ok(now_ms):
            return "FAILSAFE"
        if self.tipped(now_ms):
            return "FAILSAFE"
        if self.battery_v(now_ms) <= self._safety["battery_shutdown_v"]:
            return "FAILSAFE"
        if self.distance_cm(now_ms) < self._safety["obstacle_stop_cm"]:
            return "AVOID"
        return "PATROL"

    # ── 송신 ────────────────────────────────────────────────

    def telemetry(self, now_ms: int) -> str | None:
        """텔레메트리 한 줄. `None` 은 침묵 중이라는 뜻이다 (링크 두절 시험)."""
        self._now_ms = now_ms
        if self._fault_active(self._faults.go_silent_at_s, now_ms):
            return None

        self.stats.sent += 1
        if self._rng.random() < self._faults.corrupt_rate:
            # 호스트 규칙 ③ 시험 — 깨진 패킷이 타임아웃을 갱신하면 안 된다.
            return '{"seq": 무엇인가'

        tipped = self.tipped(now_ms)
        batt_v = self.battery_v(now_ms)
        return self._encoder.encode(
            state=self.state(now_ms),
            dist_cm=self.distance_cm(now_ms),
            imu=self._imu(now_ms, tipped=tipped),
            batt_v=batt_v,
            last_cmd_age_ms=self.last_cmd_age_ms(now_ms),
            flags={
                "lowbatt": batt_v <= self._safety["battery_warn_v"],
                "tipped": tipped,
                "link_ok": self.link_ok(now_ms),
            },
        )

    def _imu(self, now_ms: int, *, tipped: bool) -> dict[str, float]:
        if tipped:
            # 전도 임계를 확실히 넘긴 값. 의도된 앉기 자세와 구분된다 (DR-16).
            return {"pitch": self._safety["tip_angle_deg"] + 15.0, "roll": 8.0, "yaw": 12.5}
        yaw = (self._last.angle * self._elapsed_s(now_ms)) % 360.0
        return {"pitch": round(self._last.pitch, 1), "roll": 0.2, "yaw": round(yaw, 1)}


# ══════════════════════════════════════════════════════════════
#  소켓 셸 — 위의 순수 로직을 UDP 에 연결한다
# ══════════════════════════════════════════════════════════════


@dataclass
class _LogState:
    """엣지 트리거 로깅용. 값이 **변할 때만** 찍는다 (ENGINEERING_GUIDE 1.3).

    10Hz × 10분이면 6,000줄이다. 매 명령을 찍으면 정작 중요한 전이가 묻힌다.
    """

    state: str = ""
    command: str = ""
    next_summary_ms: int = 0
    fields: dict = field(default_factory=dict)


def _describe(result: DecodeResult | None) -> str:
    if result is None:
        return "유실"
    msg = result.message or {}
    if not result.accepted:
        return f"폐기({result.reason})"
    body = " ".join(f"{k}={v}" for k, v in msg.items() if k not in ("seq", "ts", "type"))
    tail = f" ⟨클램핑 {','.join(result.clamped)}⟩" if result.clamped else ""
    return f"{msg.get('type')} {body}{tail}".strip()


def _log(now_ms: int, message: str) -> None:
    print(f"[{now_ms % 1_000_000:6d}] {message}", flush=True)


def run(robot: MockRobot, cfg: dict, peer_host: str | None = None) -> None:
    """UDP 루프. 여기만 소켓과 실시간을 만진다."""
    net = cfg["network"]
    period_s = 1.0 / net["telemetry_rate_hz"]

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("", net["cmd_port"]))
    sock.setblocking(False)

    # ⚠️ Windows 전용 — 이게 없으면 목업이 호스트보다 먼저 떠 있을 때 죽는다.
    # 아직 아무도 듣지 않는 포트로 텔레메트리를 보내면 ICMP Port Unreachable 이
    # 돌아오고, Windows 는 그것을 **다음 recvfrom 의 ConnectionResetError 로**
    # 돌려준다. UDP 에는 연결이 없으므로 의미 없는 오류이며, 실제로 목업을
    # 먼저 띄우는 것이 정상 사용 순서다. 아래 ioctl 로 그 통보를 끈다.
    if hasattr(socket, "SIO_UDP_CONNRESET"):
        with contextlib.suppress(OSError):
            sock.ioctl(socket.SIO_UDP_CONNRESET, False)

    log = _LogState()
    peer: tuple[str, int] | None = (peer_host, net["telemetry_port"]) if peer_host else None
    next_tx = time.monotonic()

    _log(system_clock_ms(), f"수신 대기 :{net['cmd_port']} · 텔레메트리 :{net['telemetry_port']}")
    while True:
        now_ms = system_clock_ms()

        while True:  # 도착한 것을 전부 비운다
            try:
                data, addr = sock.recvfrom(2048)
            except BlockingIOError:
                break
            except ConnectionResetError:
                # 위 ioctl 이 없는 경로(구 Windows 등)를 위한 이중 방어.
                # 로봇 대역이 호스트 사정 때문에 죽으면 안 된다.
                continue
            if peer is None:
                peer = (addr[0], net["telemetry_port"])
                _log(now_ms, f"호스트 발견 {peer[0]}")
            result = robot.receive(data, now_ms)
            described = _describe(result)
            if described != log.command:  # 엣지 트리거
                _log(now_ms, f"◀ {described}")
                log.command = described

        state = robot.state(now_ms)
        if state != log.state:
            _log(now_ms, f"■ 상태 {log.state or '(기동)'} → {state}")
            log.state = state

        if time.monotonic() >= next_tx:
            line = robot.telemetry(now_ms)
            if line is not None and peer is not None:
                # 호스트가 내려가 있어도 목업은 계속 보낸다 — 진짜 로봇도 그렇다.
                with contextlib.suppress(OSError):
                    sock.sendto(line.encode("utf-8"), peer)
            next_tx += period_s

        if now_ms >= log.next_summary_ms:  # 주기 요약
            if log.next_summary_ms:
                _log(now_ms, f"… {robot.stats.summary()} · batt {robot.battery_v(now_ms)}V")
            log.next_summary_ms = now_ms + 1000

        time.sleep(0.002)


# ══════════════════════════════════════════════════════════════
#  CLI
# ══════════════════════════════════════════════════════════════


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mock_mechdog",
        description="가상 MechDog — 통신 상대방으로서의 로봇 (WBS 6.1.1)",
    )
    parser.add_argument("--device", default="mechdog-mock", help="device_id (텔레메트리 필수 필드)")
    parser.add_argument("--host", default=None, help="텔레메트리 수신지. 기본은 첫 명령의 송신자")

    faults = parser.add_argument_group("장애 주입")
    faults.add_argument("--drop-rate", type=float, default=0.0, help="수신 명령 유실률 0.0~1.0")
    faults.add_argument("--corrupt-rate", type=float, default=0.0, help="깨진 텔레메트리 송신률")
    faults.add_argument("--battery-start", type=float, default=None, help="시작 전압 V")
    faults.add_argument("--battery-drain", type=float, default=0.0, help="방전 속도 V/분")
    faults.add_argument("--tip-at", type=float, default=None, help="N초 후 전도")
    faults.add_argument("--obstacle-at", type=float, default=None, help="N초 후 장애물 출현")
    faults.add_argument("--go-silent", type=float, default=None, help="N초 후 텔레메트리 중단")
    faults.add_argument("--seed", type=int, default=None, help="같은 고장을 재현하기 위한 씨앗")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = load_config()
    robot = MockRobot(
        device_id=args.device,
        cfg=cfg,
        faults=Faults(
            drop_rate=args.drop_rate,
            corrupt_rate=args.corrupt_rate,
            battery_start_v=args.battery_start,
            battery_drain_v_per_min=args.battery_drain,
            tip_at_s=args.tip_at,
            obstacle_at_s=args.obstacle_at,
            go_silent_at_s=args.go_silent,
            seed=args.seed,
        ),
        start_ms=system_clock_ms(),
    )
    with contextlib.suppress(KeyboardInterrupt):
        run(robot, cfg, peer_host=args.host)
    _log(system_clock_ms(), f"종료 · {robot.stats.summary()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
