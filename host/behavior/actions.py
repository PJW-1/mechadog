"""상태별 모션 시퀀스 — PATROL / AVOID (WBS 3.5.1 · FR-2.1/2.3).

FSM 은 *어디로 갈지*를 알고 송신기는 *무엇을 보낼지*를 안다. **그 사이에서 "그 상태에
있는 동안 무엇을 할지" 를 정하는 것이 여기다.** 지금까지 이 자리가 비어 있어서
`SEQUENCE` 상태들이 모두 안전측 기본값(정지)으로 처리됐다.

⚠️ **호스트는 거리를 보고 회피를 시작하지 않는다.** 로봇이 `AVOID` 를 보고했을 때만
시퀀스를 연다 (아키텍처 1.2). 여기 있는 것은 *"멈춘 뒤 어떻게 빠져나오는가"* 뿐이고
*"멈출지 말지"* 는 온보드(`3.2.6`)의 판단이다.

⚠️ **후진 거리·선회 각도를 시간으로 바꾸려면 실측 속도가 필요하다.** 보폭(`step`)은
한 걸음의 길이이고 이동 거리가 아니므로, 200mm 를 물러나는 데 몇 초가 걸리는지는
`gait_calibration` 없이 알 수 없다. 그래서 **실측 전에는 회피 시퀀스를 만들지 않고**
등록도 하지 않는다 — 등록되지 않은 `SEQUENCE` 상태는 `Behavior` 가 정지로 처리하므로
추정값으로 걷는 것보다 안전하다.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from host.behavior.commander import Commander
from host.behavior.fsm import Behavior
from host.common.logging_setup import EdgeTrigger, event_logger

LOG = event_logger("mechadog.actions")


@dataclass(frozen=True, slots=True)
class Phase:
    """회피 시퀀스의 한 구간. **표이며 분기문이 아니다.**

    `step_mm` 은 한 걸음의 보폭이고 음수면 후진이다. `duration_ms` 는 그 구간을
    유지할 시간이며, 거리·각도를 실측 속도로 나눠 만든다.
    """

    name: str
    step_mm: float
    angle_deg: float
    duration_ms: int


class PatrolSequence:
    """전진만 한다. **주기 스캔·회피는 전이가 처리하므로 여기 없다.**

    10초 타이머는 `3.4.3` 이 `SCAN` 으로 보내고, 장애물은 로봇이 `AVOID` 를 보고해
    빠져나간다. 그래서 이 시퀀스는 *"순찰 중일 때 계속 앞으로"* 하나만 맡는다.

    ⚠️ **각도 0 을 보내면 똑바로 가지 않는다.** `mechdog-01` 은 직진 명령에서
    **좌로 1.87 도/s** 씩 요가 돈다(2026-09-18 IMU 실측 · 5m 직선이면 90도).
    방향이 누적되므로 실측한 보정 각도를 **직진 명령에 얹는다.**

    ⚠️ **보정값은 «순 회전이 0 이 되는 각도» 이며 실측이어야 한다.** `mechdog-01` 은
    `-5.0` 이다(2026-09-18 · 세 점 `0 → +1.87` · `-5 → -0.19` · `-8 → -1.40`).
    예전 `-8.0` 은 두 점 눈대중 보간이었고 그 값으로 걸으면 **반대쪽으로** 1.40 도/s
    휜다 — 8초에 11도라 **눈으로는 일직선으로 보이지만** 5m 직선이면 67도다.
    절차는 `docs/measurements/2026-09-18-turn-rate-curve.md` 1절.

    ⚠️ **이것은 개루프 보정이라 바닥·배터리가 바뀌면 다시 틀린다.** 제대로 된
    해법은 요를 보고 닫는 것이며(측위의 `patrol.steering_for` 는 이미 폐루프,
    펌웨어 텔레메트리 `4.1.4` 가 오면 IMU 요로 직접), 이 보정은 **측위 없는
    Phase 1 순찰이 벽으로 휘지 않게 하는 임시 수단**이다.
    """

    def __init__(self, step_mm: float, bias_deg: float = 0.0) -> None:
        self._step_mm = float(step_mm)
        self._bias_deg = float(bias_deg)

    def __call__(self, commander: Commander, now_ms: int) -> None:  # noqa: ARG002
        commander.drive(self._step_mm, self._bias_deg)


class HoldSequence:
    """제자리에 선다 (`ZONE_INSPECT` · FR-8).

    ⚠️ **아무것도 보내지 않는 것과 다르다.** 안 보내면 로봇은 `cmd_timeout_ms`
    까지 직전 명령을 유지하므로, 순찰에서 막 들어왔으면 **걸어 들어가면서**
    구역을 본다. 기준 스냅샷은 같은 자리에서 찍혀야 비교가 성립한다.
    """

    def __call__(self, commander: Commander, now_ms: int) -> None:  # noqa: ARG002
        commander.drive(0.0, 0.0)


class PostureSequence:
    """상태에 **머문 뒤** 자세를 잡고, **잡았을 때만** 되돌린다 (WBS 3.5.2 · 3.5.3 · FR-3.3).

    시퀀스이자 두 전이 훅이다 — `register_sequence` 로 매 틱 불리고, `restart` 를
    진입 훅에, `release` 를 이탈 훅에 건다. 셋이 한 객체여야 *"보냈는가"* 를 기억할
    수 있다.

    ⚠️ **진입 즉시 보내면 안 된다 — 2026-09-18 실기에서 그렇게 만들었다가 잡았다.**
    `ALERT` 는 조준 중에 `TRACK` 과 왕복하는데 그 체류가 **0.2~0.6초**였다(실측 10회).
    자세 보간(`dur` = 500ms)이 끝나기 전에 중립이 오므로 **고개가 올라가려다 멈추고
    다시 올라가려 한다** — 운용자 관찰 *"고개를 들려다가 움직이고 들려다가 움직이고"*.
    그래서 `hold_ms` 이상 머문 뒤에 잡는다.

    ⚠️ **이 왕복은 히스테리시스로 못 막는다.** 진입·이탈 임계를 갈라 둔 것(50/40px)은
    **경계 떨림**을 막는 장치이고, 여기서 일어나는 것은 **검출 깜빡임으로 bbox 가 실제로
    크게 움직이는 것**이다. 값이 진짜로 임계를 넘나든다.

    ⚠️ **의미도 그쪽이 맞다.** 경계 자세는 *"자리를 잡았을 때"* 취하는 것이지 조준하는
    중에 드는 것이 아니다. 그래서 왕복 구간에서는 `POSE` 가 **한 장도 나가지 않는다** —
    잡지 않았으면 되돌릴 것도 없기 때문이다.

    ⚠️ **`SCAN` 은 `hold_ms=0` 이다.** 타이머가 3초만 주는데 기다리면 자세 시간이 줄고,
    `SCAN` 은 타이머가 열고 닫으므로 왕복하지 않는다.

    ⚠️ **틱마다 보내지 않는다.** 벤더 `set_pose` 는 `dur` 동안 보간해 움직이므로 매 틱
    다시 보내면 보간이 계속 처음부터 시작해 **자세가 영원히 도착하지 않는다.**

    ⚠️ **부호 주의 — 음수가 «고개를 드는» 쪽이다** (2026-09-15 IMU 실측). 여기서 부호를
    만들지 않고 설정값을 그대로 싣는다 — 코드가 뒤집으면 설정을 고쳐도 동작이 안 바뀐다.

    ⚠️ **안전 래치 중에는 로봇이 `POSE` 를 거절한다**(펌웨어 `safe_latched` 가드).
    `ALERT → FAILSAFE` 의 중립 복귀는 닿지 않고 고개를 든 채로 남는다 — 서 있는 상태라
    위험하지 않고, 래치를 풀고 다시 자세 상태에 들어가면 복귀한다. 걷는 `PATROL` 에
    `POSE` 를 끼워 넣지 **않는** 이유는 반대다(트롯 중 자세 보간이 겹치면 걸음이 흔들린다).
    """

    def __init__(
        self, commander: Commander, pitch_deg: float, dur_ms: int, hold_ms: int = 0
    ) -> None:
        if dur_ms <= 0:
            raise ValueError("dur_ms 는 0 보다 커야 함")
        if hold_ms < 0:
            raise ValueError("hold_ms 는 0 이상이어야 함")
        self._commander = commander
        self._pitch_deg = float(pitch_deg)
        self._dur_ms = int(dur_ms)
        self._hold_ms = int(hold_ms)
        self._since_ms: int | None = None
        self._sent = False

    @property
    def pitch_deg(self) -> float:
        return self._pitch_deg

    @property
    def hold_ms(self) -> int:
        return self._hold_ms

    @property
    def sent(self) -> bool:
        """자세를 실제로 보냈는가. 이탈 훅이 이 값으로 중립 복귀를 가른다."""
        return self._sent

    def restart(self, _previous: str = "", _target: str = "") -> None:
        """진입 훅. **체류 시각은 첫 틱에서 잡는다** — 훅은 시각을 받지 않는다."""
        self._since_ms = None
        self._sent = False

    def release(self, _previous: str = "", _target: str = "") -> None:
        """이탈 훅. **자세를 잡았을 때만** 중립으로 되돌린다."""
        if not self._sent:
            return
        self._send(0.0)
        self._sent = False

    def _send(self, pitch_deg: float) -> None:
        self._commander.once("POSE", pitch=pitch_deg, roll=0.0, height=0.0, dur=self._dur_ms)

    def __call__(self, commander: Commander, now_ms: int) -> None:  # noqa: ARG002
        if self._since_ms is None:
            self._since_ms = now_ms
        if not self._sent and now_ms - self._since_ms >= self._hold_ms:
            self._send(self._pitch_deg)
            self._sent = True
        # 자세를 잡는 동안에도 그 뒤에도 제자리다 — 조준·경계는 정지 상태의 행동이다.
        self._commander.drive(0.0, 0.0)


class TrackSequence:
    """`TRACK` 에서 추종 지시를 `MOVE` 로 내보낸다 (WBS 3.5.4 · FR-3.5).

    ⚠️ **지시를 여기서 만들지 않는다.** 편차→보폭·각도 계산은 `LockOnTracker` 가
    하고, 그것을 부르는 곳은 검출이 들어오는 자리다(`runtime._poll_vision`).
    이 시퀀스는 **가장 최근 지시를 명령 주기로 옮기는 일만** 한다 — 비전은
    25fps 로 오고 명령은 10Hz 로 나가므로 둘을 같은 자리에 두면 하나가 다른
    하나를 끌고 간다.

    ⚠️ **낡은 지시로는 돌지 않는다 — 이 시퀀스의 존재 이유다.** 비전이 죽거나
    대상이 사라져도 마지막 지시가 남아 있으면 로봇은 **그 각도로 계속 돈다.**
    `FR-3.7` 의 5초 대상 상실 타이머가 `TRACK` 을 빠져나가기 전까지 그만큼을
    맴돌게 된다. 그래서 지시에 나이를 매기고, 넘으면 **정지를 보낸다** — 아무것도
    보내지 않는 것과 다르다. 안 보내면 로봇이 `cmd_timeout_ms` 까지 직전 명령을
    유지한다.

    ⚠️ **그 나이는 `fsm.track_coast_ms` 다 — `safety.cmd_timeout_ms` 가 아니다.**
    예전에는 후자를 빌려 썼는데 둘은 다른 관심사다. 하나는 *"링크가 살아 있는가"*,
    하나는 *"관측이 신선한가"* 이고 우연히 같은 300ms 였다. 그래서 검출이 0.3초만
    끊겨도 정지가 나가 **가다말다**가 됐다 — 2026-09-18 실기에서 `TRACK` 구간의
    초당 지시가 중앙값 1/25 까지 떨어졌다. 4족 보행의 흔들림은 물리라 공백을 0 으로
    만들 수 없고, 그렇다면 **짧은 공백은 이어 가고 긴 공백만 정지로 떨어뜨리는 것**이
    맞다. 이어 가는 것에는 회전도 포함한다 — 화면 밖으로 빠진 대상은 그쪽으로 조금
    더 돌아야 쫓을 수 있고(호로만 가능 · DR-11), 위험은 상한이 막는다.
    """

    def __init__(self, max_age_ms: int) -> None:
        if max_age_ms <= 0:
            raise ValueError("max_age_ms 는 0 보다 커야 함")
        self._max_age_ms = int(max_age_ms)
        self._command: tuple[float, float] | None = None
        self._at_ms: int | None = None

    @property
    def max_age_ms(self) -> int:
        return self._max_age_ms

    def note(self, step_mm: float, angle_deg: float, now_ms: int) -> None:
        """새 추종 지시를 받는다. 검출이 들어올 때마다 불린다."""
        self._command = (float(step_mm), float(angle_deg))
        self._at_ms = int(now_ms)

    def forget(self, _previous: str = "", _target: str = "") -> None:
        """`TRACK` 에 들어갈 때 부른다 — 지난 추종의 잔상으로 출발하지 않는다.

        전이 훅으로 걸리므로 `(previous, target)` 을 받는다. 쓰지는 않는다.
        """
        self._command = None
        self._at_ms = None

    def stale(self, now_ms: int) -> bool:
        if self._command is None or self._at_ms is None:
            return True
        return now_ms - self._at_ms > self._max_age_ms

    def __call__(self, commander: Commander, now_ms: int) -> None:
        if self.stale(now_ms):
            # 지시가 없거나 낡았다. **직전 명령을 유지시키지 않는다.**
            commander.drive(0.0, 0.0)
            return
        step_mm, angle_deg = self._command  # type: ignore[misc]
        commander.drive(step_mm, angle_deg)


class AvoidSequence:
    """정지 → 후진 → 선회 → 전방 재확인. 안 풀리면 정해진 횟수만큼 되풀이한다.

    **빠져나왔는지는 이 시퀀스가 판정하지 않는다.** 로봇이 `PATROL` 을 보고하면
    수신기가 `AVOID_CLEARED` 를 만들고 FSM 이 상태를 옮긴다 — 그 순간 이 시퀀스는
    더 불리지 않는다. 여기서 전방이 비었는지 스스로 판단하면 Tier 1 을 호스트로
    옮기는 것이 된다.

    `restart()` 는 `AVOID` **진입 훅**으로 연결한다. 상태를 다시 밟았을 때 이전
    진행을 물려받으면 후진을 건너뛰고 선회부터 시작하는 일이 생긴다.
    """

    def __init__(self, phases: tuple[Phase, ...], *, attempts: int) -> None:
        if not phases:
            raise ValueError("회피 구간이 비어 있음")
        if attempts < 1:
            raise ValueError("attempts 는 1 이상이어야 함")
        self._phases = phases
        self._attempts = attempts
        self._cycle_ms = sum(p.duration_ms for p in phases)
        self._started_ms: int | None = None
        self._edge = EdgeTrigger()
        self._exhausted = False

    @property
    def attempts(self) -> int:
        return self._attempts

    @property
    def cycle_ms(self) -> int:
        """한 시도에 걸리는 시간. 기동 로그와 대시보드가 이 값을 보여준다."""
        return self._cycle_ms

    @property
    def exhausted(self) -> bool:
        """정해진 횟수를 다 쓰고도 못 빠져나왔다. **대시보드가 보여줄 값이다.**"""
        return self._exhausted

    def restart(self, previous: str = "", target: str = "") -> None:  # noqa: ARG002
        self._started_ms = None
        self._exhausted = False
        self._edge.forget("phase")
        self._edge.forget("exhausted")

    def phase_at(self, elapsed_ms: int) -> tuple[Phase | None, int]:
        """경과 시간이 속한 구간과 시도 회차. 다 썼으면 `(None, 회차)`."""
        attempt = elapsed_ms // self._cycle_ms
        if attempt >= self._attempts:
            return None, self._attempts
        offset = elapsed_ms % self._cycle_ms
        for phase in self._phases:
            if offset < phase.duration_ms:
                return phase, int(attempt) + 1
            offset -= phase.duration_ms
        return self._phases[-1], int(attempt) + 1  # 경계 보정

    def __call__(self, commander: Commander, now_ms: int) -> None:
        if self._started_ms is None:
            self._started_ms = now_ms
        phase, attempt = self.phase_at(now_ms - self._started_ms)
        if phase is None:
            # ⚠️ 더 시도하지 않고 멈춘 채로 둔다. 로봇은 갇혀 있고 호스트가 할 수
            # 있는 것이 없다 — 계속 흔들면 기어만 상한다. 사람이 봐야 한다.
            self._exhausted = True
            commander.halt()
            if self._edge.changed("exhausted", True):
                LOG.error("avoid_exhausted", attempts=self._attempts)
            return
        if self._edge.changed("phase", (attempt, phase.name)):
            LOG.info(
                "avoid_phase",
                phase=phase.name,
                attempt=attempt,
                step_mm=phase.step_mm,
                angle_deg=phase.angle_deg,
            )
        commander.drive(phase.step_mm, phase.angle_deg)


def avoid_phases(config: Mapping[str, Any]) -> tuple[Phase, ...] | None:
    """설정에서 회피 구간을 만든다. **실측 속도가 없으면 `None`.**

    거리를 시간으로 바꾸는 데 필요한 값이 `gait_calibration` 이며, 그것은 시연할
    바닥에서 재야 한다 (카펫과 장판에서 다르다 · WBS 2.2). 추정값을 넣어 걷게 하면
    후진이 모자라 같은 장애물에 다시 붙거나 지나치게 물러난다.

    ⚠️ **기체마다 따로 재야 한다 — 다른 기체의 값을 복사하면 안 된다.** 서보
    오프셋이 개체마다 다르고 그 비대칭이 곧 속도·선회율·직진 편향의 차이로
    나온다. 복사한 값으로는 시간이 틀리게 계산되므로 **그 기체의 회피가 장애물에
    더 붙거나 지나치게 물러난다.**
    """
    calibration = config.get("gait_calibration") or {}
    forward = calibration.get("forward_mm_per_sec")
    turn = calibration.get("turn_deg_per_sec")
    if not forward or not turn or forward <= 0 or turn <= 0:
        return None

    gait = config["gait"]
    settle_ms = int(config["localization"]["settle_delay_ms"])
    increment_deg = float(config["localization"]["turn_increment_deg"])
    clearance_mm = float(gait["reverse_distance_mm"])
    step_mm = abs(float(gait["step_length_mm"]))
    turn_deg = float(gait["turn_angle_deg"])
    # 온보드가 이미 멈췄다. 자세가 가라앉기 전에 걷기 시작하면 첫 걸음이 튄다.
    settle = Phase("settle", 0.0, 0.0, settle_ms)
    # 멈춰서 로봇의 다음 보고를 기다린다. 전방이 비면 로봇이 `PATROL` 을 보고한다.
    verify = Phase("verify", 0.0, 0.0, settle_ms)

    # ⚠️ **후진하며 선회한다 — 물러난 뒤 전진하며 돌지 않는다.**
    #
    # 2026-09-11 실측이 원래 설계를 무효로 만들었다. 제자리 회전이 불가하므로
    # (ADR-11) 선회는 이동을 동반하는데, **전진하며 돌면 후진으로 번 여유를 그대로
    # 되돌려 준다.**
    #
    #     후진 200mm 확보 → 30도 선회 중 전진 370mm  →  순 여유 **-170mm**
    #
    # 즉 회피가 장애물에 **더 붙었다.** 후진하며 돌면 같은 선회 속도(6.6 vs 6.8
    # 도/s)로 **여유를 벌면서** 돈다 — 30도에 315mm 후퇴다. 요가 `angle` 단독으로
    # 결정되므로(PROTOCOL 부호 규약) 후진에서도 같은 부호가 같은 방향이다.
    reverse_turn_deg = calibration.get("reverse_turn_deg_per_sec")
    reverse_turn_mm = calibration.get("reverse_turn_mm_per_sec")
    if reverse_turn_deg and reverse_turn_mm and reverse_turn_deg > 0 and reverse_turn_mm > 0:
        # **각도와 여유 둘 다 만족시킨다** — 둘 중 오래 걸리는 쪽을 쓴다. 각도만
        # 보면 여유가 모자랄 수 있고, 여유만 보면 방향 전환이 모자라 장애물을
        # 돌아가지 못한다.
        for_angle_ms = increment_deg / float(reverse_turn_deg) * 1000
        for_clearance_ms = clearance_mm / float(reverse_turn_mm) * 1000
        duration_ms = int(max(for_angle_ms, for_clearance_ms))
        return (
            settle,
            Phase("reverse_turn", -step_mm, turn_deg, duration_ms),
            verify,
        )

    # ⚠️ **후진 선회를 재지 않은 개체는 옛 구간표로 돈다.** 그 개체에서는 위의
    # 여유 문제가 그대로 남아 있으므로 `2.2.3` 을 먼저 재야 한다.
    #
    # 후진 속도도 전진 속도가 아니다 — 실측에서 **25% 느렸다**(103.9 대 78.0).
    # 재지 않았으면 전진 값으로 되돌아가고, 그 개체의 후진량은 그만큼 틀린다.
    reverse_speed = calibration.get("reverse_mm_per_sec") or forward
    reverse_ms = int(clearance_mm / float(reverse_speed) * 1000)
    turn_ms = int(increment_deg / float(turn) * 1000)
    return (
        settle,
        Phase("reverse", -step_mm, 0.0, reverse_ms),
        Phase("turn", step_mm, turn_deg, turn_ms),
        verify,
    )


def register_actions(behavior: Behavior, config: Mapping[str, Any]) -> dict[str, str]:
    """만들 수 있는 시퀀스를 `Behavior` 에 등록하고 결과를 돌려준다.

    돌려주는 값은 `상태 → 등록됨 / 건너뛴 이유` 다. **조용히 건너뛰지 않는다** —
    회피가 없다는 것은 로봇이 장애물 앞에서 멈춘 채로 있는다는 뜻이고, 그것을
    기동 로그에서 알 수 있어야 한다.
    """
    result: dict[str, str] = {}

    # ⚠️ **보정을 재지 않은 개체는 0 이다** — 그 개체의 순찰은 휜다(`2.2.3`).
    # 다른 기체의 값을 기본값으로 두면 **틀린 방향으로 휘게** 만든다.
    bias_deg = float((config.get("gait_calibration") or {}).get("straight_bias_deg") or 0.0)
    behavior.register_sequence("PATROL", PatrolSequence(config["gait"]["step_length_mm"], bias_deg))
    result["PATROL"] = "등록" if bias_deg else "등록 (직진 보정 미실측 — 휜다)"
    if not bias_deg:
        LOG.warning(
            "straight_bias_unmeasured",
            effect="직진 명령이 그대로 나가 순찰이 한쪽으로 휜다",
            remedy="tools/gait_calibrate.py --mode forward --bias-deg <각도> (WBS 2.2.3)",
        )

    behavior.register_sequence("ZONE_INSPECT", HoldSequence())
    result["ZONE_INSPECT"] = "등록"

    # ── 자세 상태 둘 (WBS 3.5.2 SCAN · 3.5.3 ALERT) ────────────────────────
    #
    # **둘은 같은 기전이고 값만 다르다** — 들어갈 때 `POSE` 를 한 번 보내고, 그 동안
    # 제자리에 서 있고, 나갈 때 중립으로 되돌린다. 그래서 하나의 훅 클래스로 덮는다.
    #
    # ⚠️ **요 스캔은 없다.** 다리 2자유도가 모두 앞뒤 평면에 있어 몸통 요가 기하학적으로
    # 불가능하고(DR-11) 규약의 `POSE` 에도 요 필드가 없다. `SCAN` 은 피치만 쓴다.
    #
    # ⚠️ **3초는 여기서 세지 않는다** — `fsm.patrol_scan_interval_s`·`scan_duration_s`
    # 타이머가 이미 `SCAN_DONE` 을 낸다(`3.4.3`). 시퀀스가 시간을 또 재면 두 개의
    # 시계가 같은 일을 하고, 어긋나는 날이 온다.
    commander = behavior.commander
    settle_ms = int(config["posture"]["settle_ms"])
    # `SCAN` 은 즉시, `ALERT` 는 머문 뒤다 — 위 `PostureSequence` 주석의 실측 근거.
    # `AUTH_WAIT` 도 **같은 경계 자세**를 쓴다 — 자세는 상태가 아니라 «사람을 상대하는
    # 구간» 의 것이고, 실측상 로봇이 서 있는 시간의 대부분이 거기다. 왕복하지 않으므로
    # 기다리지 않는다.
    holds = {"SCAN": 0, "ALERT": int(config["posture"]["alert_hold_ms"]), "AUTH_WAIT": 0}
    for state, key in (
        ("SCAN", "scan_pitch_deg"),
        ("ALERT", "alert_pitch_deg"),
        ("AUTH_WAIT", "alert_pitch_deg"),
    ):
        posture = PostureSequence(commander, float(config["fsm"][key]), settle_ms, holds[state])
        behavior.register_sequence(state, posture)
        behavior.fsm.on_enter(state, posture.restart)
        behavior.fsm.on_exit(state, posture.release)
        result[state] = (
            f"등록 (pitch {posture.pitch_deg:+.0f}° · {posture.hold_ms}ms 머문 뒤)"
            if posture.hold_ms
            else f"등록 (pitch {posture.pitch_deg:+.0f}° · 즉시)"
        )

    # 추종 지시의 유효기간. **명령 타임아웃과 다른 값이다** — 위 `TrackSequence` 주석.
    track_max_age = int(config["fsm"]["track_coast_ms"])
    track = TrackSequence(track_max_age)
    behavior.register_sequence("TRACK", track)
    behavior.fsm.on_enter("TRACK", track.forget)
    result["TRACK"] = "등록"

    phases = avoid_phases(config)
    if phases is None:
        result["AVOID"] = "gait_calibration 실측 전 — 정지 유지"
        LOG.warning(
            "sequence_unavailable",
            state="AVOID",
            reason="gait_calibration 미실측",
            effect="장애물 앞에서 정지 유지 (WBS 2.2 실측 후 활성)",
        )
    else:
        sequence = AvoidSequence(phases, attempts=int(config["fsm"]["avoid_attempts"]))
        behavior.register_sequence("AVOID", sequence)
        behavior.fsm.on_enter("AVOID", sequence.restart)
        result["AVOID"] = "등록"
        LOG.info(
            "avoid_ready",
            attempts=sequence.attempts,
            cycle_ms=sequence.cycle_ms,
            phases=[p.name for p in phases],
        )
    return result
