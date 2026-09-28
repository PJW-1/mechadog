"""개체별 오도메트리 — 보낸 명령 + IMU yaw 변화량 → `odom` 자세 (WBS 5.4.3 · FR-6.2).

    보낸 MOVE/STOP/ESTOP ─┐
                          ├─▶ Odometry ─▶ (x m, y m, yaw rad, 기준 시각, 유효)
    텔레메트리 imu.yaw ───┘

`slam_toolbox` 는 `odom → base_link` 를 요구하는데 로봇에는 **센서 오도메트리가 없다**
— 다리에 엔코더가 없다(`tools/gait_calibrate.py` 머리말). 그래서 두 가지를 합친다.

  ① **거리는 명령의 시간 창 × 그 기체의 실측 속도** (`gait_calibration`)
  ② **방향은 IMU yaw 의 변화량**

**이 모듈은 소켓도 실시각도 만지지 않는다.** 보낸 전문과 그 시각, IMU 값과 수신
시각만 받는다 (ENGINEERING_GUIDE 2.1 · `lidar_link.py` 와 같은 구조). 실제 배선은
`tools/patrol_run.py` 가 한다.

⚠️ **이것은 «정확한 위치» 가 아니다.** 명령값을 적분한 추정이며, 보정은
`slam_toolbox` 가 정지 스캔으로 한다. WBS 5.4.3 DoD 가 *«명령값만으로 정확한 위치라고
간주하지 않는다»* 고 못박은 이유다. 그래서 **IMU 가 없거나 오래되면 자세를 무효로
낸다** — 방향 없이 명령만으로 만든 위치를 유효하다고 내보내지 않는다.

## step 과 속도 — 비례로 본다 (실측 확인 전 가정)

`MOVE.step` 은 이름과 달리 **보폭이 아니라 전진 속도 지령**이다 — 벤더 서명이
`move(speed_x, angle_rate)` 다 (`config.yaml` `gait.step_length_mm` 주석). 그래서
속도를 **`실측 속도 × |step| / 실측 때의 step`** 으로 둔다.

  - **실측 때의 step 은 ±60 이다.** `mechdog-01` 의 104.0 mm/s 는 `gait_calibrate.py`
    가 `gait.step_length_mm`(60)으로 쟀고(`config.yaml` 주석 *"`60` 의 실측 결과가
    104 mm/s"*), `mechdog-02` 의 68.7/83.8 은 `field_measure.py m_drive` 가 `±60` 고정으로
    쟀다. 그래서 `CALIBRATION_STEP_MM` 을 60 으로 박는다 — `step_length_mm` 를 읽으면
    그 설정을 바꾸는 순간 **실측 조건이 아닌 값이 기준이 된다.**
  - **비례는 가정이다.** 60 이외의 step 에서 잰 기록이 없다. 그래도 필요한 이유는
    순찰이 선회 중에 보폭을 줄여 보내기 때문이다(`patrol.steering_for` 의
    `TURN_STEP_REDUCTION`). 시뮬레이션(`simulation.apply_move`)도 같은 비례 모형이다.
    5.4.3 실측(직진·선회 3회)에서 이 가정의 오차가 드러난다.
  - **후진은 `reverse_mm_per_sec` 을 쓴다.** 전진 값으로 대신하지 않는다 —
    `mechdog-01` 은 25% 느리고 `mechdog-02` 는 22% 빠르다. 없으면 오류다
    (`actions.avoid_phases` 는 전진 값으로 되돌아가지만 그것은 이미 기록된 약점이다).
  - **`angle` 은 거리에 넣지 않는다.** 선회 중 전진 속도를 따로 잰 기록이 없다.

## 방향 — IMU yaw 변화량만 쓴다

  - **절대값을 쓰지 않는다.** 펌웨어는 IMU 의 0 이 어디인지 보장하지 않는다 — 절대값을
    쓰면 `odom` 의 방위가 그 임의의 기준에 묶인다. `scan_match.py` 머리말의 버그 ⑥ 과 같은 이유다.
    `odom` 의 yaw 는 **첫 IMU 표본을 0 으로** 두고 변화량만 쌓는다.
  - **0/360 경계**는 변화량을 `[-180, 180)` 으로 접어 넘긴다 (359 → 1 은 +2°).
  - **`turn_rate_curve_deg_s` 는 쓰지 않는다.** 같은 각도가 부팅마다 25% 다르다고
    기록돼 있다 (`mechdog-01.yaml`). 개루프 선회율로 방향을 만들면 그만큼 틀린다.
  - **두 IMU 표본 사이의 이동은 두 yaw 를 선형 보간한 방위로 적분한다.** 방위를
    알고 나서야 적분하므로 IMU 가 끊긴 동안의 이동도 다음 표본이 오면 실측 회전으로
    적분된다. 다만 한 간격에 ±180° 를 넘게 돌면 방향을 구분할 수 없다 — 실측 선회율
    (≤10 도/s)로 18초 이상 끊겨야 일어나는 일이고, 그 전에 자세가 무효가 된다.
  - **`boot_id` 가 바뀌면 그 표본의 변화량은 0 이다.** 재부팅한 IMU 는 다시 0 에서
    시작하므로 차이를 회전으로 읽으면 한 번에 수십 도를 돈 것이 된다.

## 명령의 시간 창

  - `MOVE` 는 **다음 명령까지, 최대 `safety.cmd_timeout_ms` 동안** 유효하다. 로봇은
    그 시간 동안 명령이 없으면 스스로 멈춘다 (FR-1.3) — 호스트가 멈춰도 적분이
    계속되면 안 된다.
  - `STOP` · `ESTOP` · `RESET_SAFE` 는 정지다. **`ESTOP` 뒤에는 `RESET_SAFE` 를 보낼
    때까지 `MOVE` 를 움직임으로 세지 않는다** — 래치가 걸린 로봇은 `MOVE` 를
    차단한다 (PROTOCOL 2절 안전 정지와 해제).
  - 나머지(`STATE`·`LED`·`SOUND`·`POSE` 등)는 이동을 바꾸지 않는다.

## 로봇이 스스로 멈춰 있다고 알려 오면 그 말이 이긴다

보낸 명령으로 추정한 래치는 **호스트의 짐작**이다. 로봇은 저전압이면 `RESET_SAFE` 를
거부하고(`firmware_mechdog_motion/README.md` 3.2.5), 재부팅하면 래치 상태로 켜진다 —
둘 다 명령만 보면 모른다. 그동안 보낸 `MOVE` 를 이동으로 적분하면 **위치가 조용히
앞으로 밀린다.** 그래서 텔레메트리의 `safety_latched` 와 `flags.obstacle`(근거리
정지 — 우선순위가 호스트 명령보다 높다, 같은 README `3.2.5`)을 `note_hold` 로 받아,
로봇이 멈춰 있다고 하는 동안은 `MOVE` 를 0 으로 센다. **명령 추정과 보고 중 어느
쪽이든 정지라 하면 정지다** — 보고는 정지를 넓히기만 하므로, `ESTOP` 직후 도착한
낡은 `false` 가 추정을 덮지 못한다. 구형 펌웨어라 둘 다 없으면(`None`) 명령 추정만
쓴다. 풀렸다는 보고 뒤에도 로봇은 **다음에 받아들인 `MOVE` 부터** 걷는다 — 보고만으로
움직이지 않는다.

한계: 보고가 **흐를 때만** 성립한다. 전이마다 텔레메트리 한 주기(~100ms · ≈1cm)의
지연 오차가 있고, 텔레메트리 공백 동안 보낸 `MOVE` 는 다음 IMU 표본이 오면 적분된다.
단 공백 뒤 `boot_id` 가 바뀌었으면 — 로봇이 재부팅해 SAFE 잠금으로 켜졌다 — 그 사이의
`MOVE` 는 실행되지 않았으므로 버린다.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from host.common.config import ConfigError
from host.common.units import deg_to_rad, mm_to_m, wrap_pi

#: `gait_calibration` 의 전진·후진 속도를 잰 `MOVE.step` 크기 (mm 이름이지만 속도 지령).
#: 근거는 머리말 «step 과 속도».
CALIBRATION_STEP_MM = 60.0

#: 이동을 멈추는 명령. `RESET_SAFE` 도 온보드가 정지 상태로 해제한다.
_STOPPING = frozenset({"STOP", "ESTOP", "RESET_SAFE"})


@dataclass(frozen=True, slots=True)
class OdomParams:
    forward_mm_per_sec: float
    reverse_mm_per_sec: float
    #: `MOVE` 가 이 시간 뒤까지 이어지지 않으면 로봇이 스스로 멈춘다.
    command_timeout_ms: int
    #: IMU 표본이 이보다 오래되면 자세를 무효로 낸다.
    imu_stale_ms: int
    calibration_step_mm: float = CALIBRATION_STEP_MM


@dataclass(frozen=True, slots=True)
class OdomPose:
    """`odom` 프레임의 자세. **`valid` 가 거짓이면 값을 쓰지 않는다.**"""

    x_m: float
    y_m: float
    yaw_rad: float
    #: 이 자세가 가리키는 시각 (Host epoch ms).
    stamp_ms: int
    valid: bool
    #: 무효 사유. 유효하면 빈 문자열.
    reason: str = ""


def odom_params_from_config(config: Mapping[str, Any]) -> OdomParams:
    """설정에서 오도메트리 파라미터를 만든다. **실측이 없으면 `ConfigError`.**

    ⚠️ **다른 기체의 값으로 채우지 않는다.** 서보 비대칭이 개체마다 달라 속도부터
    다르다 (`mechdog-02.yaml` 머리말). 없는 값을 추정으로 채우면 그 기체의 `odom` 이
    조용히 늘어나거나 줄어든다.
    """
    calibration = config.get("gait_calibration")
    if not isinstance(calibration, Mapping):
        raise ConfigError(
            "gait_calibration 이 없다 — 이 기체는 보행 실측 전이라 오도메트리를 만들지 "
            "않는다 (WBS 2.2.3 실측 후 · 다른 기체 값을 복사하지 않는다)"
        )
    speeds = {}
    for name in ("forward_mm_per_sec", "reverse_mm_per_sec"):
        value = calibration.get(name)
        if not isinstance(value, int | float) or isinstance(value, bool) or value <= 0:
            raise ConfigError(f"gait_calibration.{name} 실측값이 필요함 (양수) — 받은 값 {value!r}")
        speeds[name] = float(value)
    lidar = config["lidar"]
    return OdomParams(
        forward_mm_per_sec=speeds["forward_mm_per_sec"],
        reverse_mm_per_sec=speeds["reverse_mm_per_sec"],
        command_timeout_ms=int(config["safety"]["cmd_timeout_ms"]),
        imu_stale_ms=int(lidar["odom_imu_stale_ms"]),
    )


def hold_of_reading(safety_latched: bool | None, obstacle: bool | None) -> bool | None:
    """텔레메트리 한 건에서 «로봇이 스스로 멈춰 있는가» 를 뽑는다.

    래치와 근거리 정지 둘 다 온보드에서 `MOVE` 를 막는다. 어느 하나라도 참이면 정지,
    둘 다 없으면(구형 펌웨어) 모른다 — `None` 을 `note_hold` 에 주면 아무것도 바꾸지 않는다.
    """
    if safety_latched is None and obstacle is None:
        return None
    return bool(safety_latched) or bool(obstacle)


def _arc(travel_m: float, heading_from: float, heading_to: float) -> tuple[float, float]:
    """방위가 `heading_from` → `heading_to` 로 고르게 바뀌며 `travel_m` 을 간 변위.

    구간 중간의 방위 하나로 곧게 가면, 같은 `MOVE` 를 이어 붙인 긴 구간(IMU 가
    끊긴 동안)에서 호가 현으로 바뀌어 거리가 늘어난다. 정확한 호 적분을 쓴다.
    """
    turn = heading_to - heading_from
    if abs(turn) < 1e-9:
        return travel_m * math.cos(heading_from), travel_m * math.sin(heading_from)
    scale = travel_m / turn
    return (
        scale * (math.sin(heading_to) - math.sin(heading_from)),
        scale * (math.cos(heading_from) - math.cos(heading_to)),
    )


class Odometry:
    """보낸 명령과 IMU yaw 로 `odom` 자세를 적분한다."""

    def __init__(self, params: OdomParams) -> None:
        self._params = params
        self._x = 0.0
        self._y = 0.0
        #: `odom` 방위 (rad). 첫 IMU 표본이 0 이다.
        self._yaw = 0.0
        # ── 지금 유효한 이동 ──
        self._speed_m_s = 0.0
        self._segment_from_ms: int | None = None
        self._move_until_ms = 0
        #: 보낸 명령으로 추정한 래치 (`ESTOP` 뒤 · `RESET_SAFE` 전).
        self._latched = False
        #: 로봇이 텔레메트리로 알려 온 온보드 정지. 알면 `_latched` 보다 우선한다 (머리말).
        self._reported_hold: bool | None = None
        #: 아직 방위를 모르는 이동 구간 `(시작 ms, 끝 ms, 속도 m/s)`. 다음 IMU 표본이
        #: 오면 두 표본의 yaw 를 보간해 적분한다.
        self._pending: list[tuple[int, int, float]] = []
        # ── 마지막 IMU 표본 ──
        self._imu_deg: float | None = None
        self._imu_ms: int | None = None
        self._imu_boot: str | None = None

    # ── 입력: 보낸 명령 ─────────────────────────────────────────
    def note_sent(self, lines: Iterable[str], sent_ms: int) -> None:
        """**실제로 보낸** 전문들을 넣는다. 인코더가 만든 한 줄 JSON 이다."""
        for line in lines:
            message = json.loads(line)
            self.note_command(str(message.get("type")), message, sent_ms)

    def note_command(self, type_: str, fields: Mapping[str, Any], sent_ms: int) -> None:
        if type_ == "MOVE":
            speed = 0.0 if self._holding() else self._speed_of(float(fields["step"]))
            self._set_motion(speed, sent_ms)
        elif type_ in _STOPPING:
            self._latched = type_ == "ESTOP" or (self._latched and type_ != "RESET_SAFE")
            self._set_motion(0.0, sent_ms)

    def _holding(self) -> bool:
        """지금 `MOVE` 가 차단되는가 — 명령 추정과 로봇 보고 중 **어느 쪽이든** 정지라 하면 정지.

        보고는 정지를 넓히기만 한다. `ESTOP` 을 보낸 직후 그 전에 만들어진
        `safety_latched=false` 가 도착해도 호스트의 래치 추정을 덮지 못한다.
        """
        return self._latched or self._reported_hold is True

    def _speed_of(self, step: float) -> float:
        """step → 속도 (m/s, 부호 있음). 비례의 근거는 머리말."""
        params = self._params
        rate = params.forward_mm_per_sec if step > 0 else params.reverse_mm_per_sec
        return math.copysign(mm_to_m(rate) * abs(step) / params.calibration_step_mm, step)

    def _set_motion(self, speed_m_s: float, at_ms: int) -> None:
        self._close_segment(at_ms)
        self._speed_m_s = speed_m_s
        self._segment_from_ms = at_ms
        self._move_until_ms = at_ms + self._params.command_timeout_ms

    def _close_segment(self, at_ms: int) -> None:
        """지금까지의 이동을 방위 미정 구간으로 넘긴다."""
        start = self._segment_from_ms
        if start is None:
            return
        end = min(at_ms, self._move_until_ms)
        if self._speed_m_s != 0.0 and end > start:
            last = self._pending[-1] if self._pending else None
            if last is not None and last[1] == start and last[2] == self._speed_m_s:
                # 10Hz 로 같은 `MOVE` 가 반복되므로 이어 붙인다 — IMU 가 끊긴 동안 쌓이는 양이 준다.
                self._pending[-1] = (last[0], end, last[2])
            else:
                self._pending.append((start, end, self._speed_m_s))
        self._segment_from_ms = max(start, at_ms)

    # ── 입력: 온보드 정지 ──────────────────────────────────────
    def note_hold(self, held: bool | None, received_ms: int) -> None:
        """로봇이 알려 온 «스스로 멈춰 있음» (`hold_of_reading`) 을 넣는다.

        참이면 진행 중인 이동을 그 시각에 끊고, 이후 `MOVE` 는 0 으로 센다. 거짓이면
        다음 `MOVE` 부터 다시 센다 — 이 호출만으로 움직임을 만들지 않는다. `None` 은
        구형 펌웨어라 아무것도 바꾸지 않는다.
        """
        if held is None:
            return
        self._reported_hold = held
        if held and self._speed_m_s != 0.0:
            self._set_motion(0.0, received_ms)

    # ── 입력: IMU ──────────────────────────────────────────────
    def note_imu(self, yaw_deg: float, received_ms: int, boot_id: str = "") -> None:
        """텔레메트리 `imu.yaw`(0~360 deg)와 수신 시각을 넣는다."""
        self._close_segment(received_ms)
        if self._imu_deg is None or self._imu_ms is None:
            # 첫 표본 — 이전 이동은 방위 기준이 없어 적분하지 않는다. `odom` 의 원점과
            # 방위는 여기서 정해지므로 잃는 것은 좌표가 아니라 기동 전 몇 걸음뿐이다.
            self._pending.clear()
            delta = 0.0
        elif boot_id != self._imu_boot:
            # 재부팅한 IMU 는 0 에서 다시 시작한다 (머리말). 공백 동안 보낸 `MOVE` 도
            # 버린다 — 로봇은 SAFE 잠금으로 켜져 그것을 실행하지 않았다 (머리말 «로봇이
            # 스스로 멈춰 있다고 알려 오면»). 순찰기는 두절 뒤에도 최대
            # `link_loss_failsafe_ms` 동안 `MOVE` 를 계속 보내므로 적분하면 수십 cm 가 붙는다.
            self._pending.clear()
            delta = 0.0
        else:
            delta = deg_to_rad((yaw_deg - self._imu_deg + 180.0) % 360.0 - 180.0)
        start_ms = self._imu_ms if self._imu_ms is not None else received_ms
        span = received_ms - start_ms

        def heading_at(t_ms: int) -> float:
            fraction = min(max((t_ms - start_ms) / span, 0.0), 1.0) if span > 0 else 1.0
            return self._yaw + delta * fraction

        for seg_from, seg_to, speed in self._pending:
            dx, dy = _arc(
                speed * (seg_to - seg_from) / 1000.0, heading_at(seg_from), heading_at(seg_to)
            )
            self._x += dx
            self._y += dy
        self._pending.clear()
        self._yaw = wrap_pi(self._yaw + delta)
        self._imu_deg = float(yaw_deg)
        self._imu_ms = received_ms
        self._imu_boot = boot_id

    # ── 출력 ───────────────────────────────────────────────────
    def pose(self, now_ms: int) -> OdomPose:
        """`now_ms` 의 자세. 마지막 IMU 뒤의 이동은 마지막 방위로 외삽한다.

        외삽은 최대 `imu_stale_ms` 까지다 — 그보다 오래되면 무효로 낸다.
        """
        if self._imu_ms is None:
            return OdomPose(self._x, self._y, self._yaw, now_ms, False, "IMU 표본 없음")
        age = now_ms - self._imu_ms
        x, y = self._x, self._y
        cos_y, sin_y = math.cos(self._yaw), math.sin(self._yaw)
        segments = list(self._pending)
        start = self._segment_from_ms
        if start is not None and self._speed_m_s != 0.0:
            end = min(now_ms, self._move_until_ms)
            if end > start:
                segments.append((start, end, self._speed_m_s))
        for seg_from, seg_to, speed in segments:
            travel = speed * (seg_to - seg_from) / 1000.0
            x += travel * cos_y
            y += travel * sin_y
        if age > self._params.imu_stale_ms:
            return OdomPose(x, y, self._yaw, now_ms, False, f"IMU {age}ms 미갱신")
        return OdomPose(x, y, self._yaw, now_ms, True)
