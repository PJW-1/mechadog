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
    """

    def __init__(self, step_mm: float) -> None:
        self._step_mm = float(step_mm)

    def __call__(self, commander: Commander, now_ms: int) -> None:  # noqa: ARG002
        commander.drive(self._step_mm, 0.0)


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

    behavior.register_sequence("PATROL", PatrolSequence(config["gait"]["step_length_mm"]))
    result["PATROL"] = "등록"

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
