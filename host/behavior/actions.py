"""상태별 모션 시퀀스 — PATROL / AVOID (FR-2.1/2.3).

상태에 머무는 동안 매 틱 무엇을 보낼지 정한다. 등록되지 않은 `SEQUENCE` 상태는 `Behavior`
가 정지로 처리한다.

호스트는 거리를 보고 회피를 시작하지 않는다 — 로봇이 `AVOID` 를 보고했을 때만 «멈춘 뒤
빠져나오는 법» 을 연다 (아키텍처 1.2). 후진 거리·선회각을 시간으로 바꾸려면
`gait_calibration` 실측 속도가 필요하므로, 실측 전에는 회피 시퀀스를 등록하지 않는다.
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
    """회피 시퀀스의 한 구간. `step_mm` 은 보폭(음수면 후진), `duration_ms` 는 유지 시간이다."""

    name: str
    step_mm: float
    angle_deg: float
    duration_ms: int


class PatrolSequence:
    """순찰 중 계속 전진한다. 주기 스캔·회피는 전이가 처리한다.

    직진 명령에는 개체별 실측 `straight_bias_deg`(순 회전이 0 이 되는 각도)를 얹는다 — 각도
    0 이면 요가 누적돼 휜다. 개루프 보정이라 바닥·배터리가 바뀌면 다시 재야 한다
    (절차: `docs/measurements/2026-09-18-turn-rate-curve.md` 1절).
    """

    def __init__(self, step_mm: float, bias_deg: float = 0.0) -> None:
        self._step_mm = float(step_mm)
        self._bias_deg = float(bias_deg)

    def __call__(self, commander: Commander, now_ms: int) -> None:  # noqa: ARG002
        commander.drive(self._step_mm, self._bias_deg)


class PostureSequence:
    """상태에 `hold_ms` 이상 머문 뒤 자세를 한 번 보내고, 보냈을 때만 이탈 시 중립으로 되돌린다
    (FR-3.3).

    매 틱 시퀀스로 불리고, `restart` 는 진입 훅, `release` 는 이탈 훅에 건다. `ALERT` 는 조준
    중 `TRACK` 과 짧게 왕복하므로 머문 뒤에 보내 보간이 끊기지 않게 한다 (ADR-40). `POSE` 는
    `dur` 동안 보간되므로 틱마다 다시 보내지 않는다. pitch 는 설정값 그대로 싣는다(음수가
    고개를 든다). 안전 래치 중에는 로봇이 `POSE` 를 거절해 중립 복귀가 닿지 않을 수 있다.
    """

    def __init__(
        self, commander: Commander, pitch_deg: float, dur_ms: int, hold_ms: int = 0,
        roll_deg: float = 0.0,
    ) -> None:
        if dur_ms <= 0:
            raise ValueError("dur_ms 는 0 보다 커야 함")
        if hold_ms < 0:
            raise ValueError("hold_ms 는 0 이상이어야 함")
        self._commander = commander
        self._pitch_deg = float(pitch_deg)
        self._dur_ms = int(dur_ms)
        self._hold_ms = int(hold_ms)
        # `posture.roll_offset_deg` — IMU 상시 roll 편향 상쇄. «중립 복귀» 시에도 0 이
        # 아니라 이 오프셋으로 돌려야 보정이 보행 중에도 유지된다.
        self._roll_deg = float(roll_deg)
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
        """진입 훅. 체류 시각은 첫 틱에서 잡는다(훅은 시각을 받지 않는다)."""
        self._since_ms = None
        self._sent = False

    def release(self, _previous: str = "", _target: str = "") -> None:
        """이탈 훅. 자세를 보냈을 때만 중립으로 되돌린다."""
        if not self._sent:
            return
        self._send(0.0)
        self._sent = False

    def _send(self, pitch_deg: float) -> None:
        self._commander.once("POSE", pitch=pitch_deg, roll=self._roll_deg, height=0.0, dur=self._dur_ms)

    def __call__(self, commander: Commander, now_ms: int) -> None:  # noqa: ARG002
        if self._since_ms is None:
            self._since_ms = now_ms
        if not self._sent and now_ms - self._since_ms >= self._hold_ms:
            self._send(self._pitch_deg)
            self._sent = True
        # 자세를 잡는 동안에도 그 뒤에도 제자리다 — 조준·경계는 정지 상태의 행동이다.
        self._commander.drive(0.0, 0.0)


class TrackSequence:
    """가장 최근 추종 지시를 명령 주기(10Hz)로 `MOVE` 로 옮긴다 (FR-3.5).

    지시는 운용 루프가 새 검출마다(`LockOnTracker` 계산) `note` 로 넣는다. ⚠️ 지시가 없거나
    `max_age_ms`(`fsm.track_coast_ms`)보다 낡으면 정지를 보낸다 — 아무것도 안 보내면 로봇이
    직전 명령대로 계속 돈다. 이 나이는 «관측이 신선한가» 이며 `safety.cmd_timeout_ms`(링크
    생존)와 별개다 — 짧은 검출 공백은 이어 가고 긴 공백만 정지로 떨어뜨린다.
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
        """진입 훅 — 지난 추종의 지시를 지운다. `(previous, target)` 은 쓰지 않는다."""
        self._command = None
        self._at_ms = None

    def stale(self, now_ms: int) -> bool:
        if self._command is None or self._at_ms is None:
            return True
        return now_ms - self._at_ms > self._max_age_ms

    def __call__(self, commander: Commander, now_ms: int) -> None:
        if self.stale(now_ms):
            # 지시가 없거나 낡았다 — 직전 명령을 유지시키지 않는다.
            commander.drive(0.0, 0.0)
            return
        step_mm, angle_deg = self._command  # type: ignore[misc]
        commander.drive(step_mm, angle_deg)


class AvoidSequence:
    """회피 구간표를 시간순으로 돌고, 안 풀리면 `attempts` 번까지 되풀이한 뒤 멈춘다.

    빠져나왔는지는 판정하지 않는다 — 로봇의 보고로 `AVOID_CLEARED` 가 나면 FSM 이 상태를
    옮긴다 (ADR-22). `restart()` 는 `AVOID` 진입 훅이다.
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
        """정해진 횟수를 다 쓰고도 못 빠져나왔다 (대시보드 표시용)."""
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
            # 소진 — 더 시도하지 않고 멈춘 채 사람을 기다린다(계속 흔들면 기어만 상한다).
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
    """설정에서 회피 구간을 만든다. 실측 속도(`gait_calibration`)가 없으면 `None`.

    속도는 기체마다, 시연 바닥에서 따로 잰 값이어야 한다 — 다른 기체의 값으로는 후진량이 틀린다.
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

    # 후진하며 선회한다 — 전진 선회는 후진으로 번 여유를 되돌려 준다 (ADR-29).
    # 요는 `angle` 단독으로 정해지므로 후진에서도 같은 부호가 같은 방향이다.
    reverse_turn_deg = calibration.get("reverse_turn_deg_per_sec")
    reverse_turn_mm = calibration.get("reverse_turn_mm_per_sec")
    if reverse_turn_deg and reverse_turn_mm and reverse_turn_deg > 0 and reverse_turn_mm > 0:
        # 각도 목표와 여유 목표 중 오래 걸리는 쪽을 쓴다 (ADR-29).
        for_angle_ms = increment_deg / float(reverse_turn_deg) * 1000
        for_clearance_ms = clearance_mm / float(reverse_turn_mm) * 1000
        duration_ms = int(max(for_angle_ms, for_clearance_ms))
        return (
            settle,
            Phase("reverse_turn", -step_mm, turn_deg, duration_ms),
            verify,
        )

    # 후진 선회를 재지 않은 개체는 «후진 → 전진 선회» 구간표로 돈다 — 여유가 음수로
    # 남으므로 후진 선회 값을 먼저 재야 한다 (ADR-29). 후진 속도를 안 쟀으면 전진 값을 쓴다.
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
    """만들 수 있는 시퀀스를 `Behavior` 에 등록하고 `상태 → 등록됨 / 건너뛴 이유` 를 돌려준다.

    건너뛴 것은 기동 로그에 경고로 남긴다.
    """
    result: dict[str, str] = {}

    # 재지 않은 개체는 0 이다 — 다른 기체의 값을 기본값으로 두지 않는다.
    bias_deg = float((config.get("gait_calibration") or {}).get("straight_bias_deg") or 0.0)
    behavior.register_sequence("PATROL", PatrolSequence(config["gait"]["step_length_mm"], bias_deg))
    result["PATROL"] = "등록" if bias_deg else "등록 (직진 보정 미실측 — 휜다)"
    if not bias_deg:
        LOG.warning(
            "straight_bias_unmeasured",
            effect="직진 명령이 그대로 나가 순찰이 한쪽으로 휜다",
            remedy="tools/probe/gait_calibrate.py --mode forward --bias-deg <각도> (WBS 2.2.3)",
        )

    # 구역 점검은 제자리에서 앵커 방향으로 돈 뒤 선다 (FR-8 · ADR-40 과 같은 예외).
    # 회전 지시는 `ZoneInspector._align` 이 넣고, 낡으면 `TrackSequence` 가 정지를 보낸다.
    inspect = TrackSequence(int(config["fsm"]["track_coast_ms"]))
    behavior.register_sequence("ZONE_INSPECT", inspect)
    behavior.fsm.on_enter("ZONE_INSPECT", inspect.forget)
    result["ZONE_INSPECT"] = "등록"

    # ── 자세 상태 (SCAN · ALERT · AUTH_WAIT) ────────────────
    #
    # 같은 기전에 값만 다르다. `POSE` 에 요 필드가 없어 `SCAN` 은 피치만 쓰고 (ADR-11),
    # 스캔 시간은 FSM 타이머(`SCAN_DONE`)가 잰다.
    commander = behavior.commander
    settle_ms = int(config["posture"]["settle_ms"])
    # `ALERT` 만 머문 뒤에 보낸다. `AUTH_WAIT` 는 같은 경계 자세를 쓰며 왕복하지 않는다.
    holds = {"SCAN": 0, "ALERT": int(config["posture"]["alert_hold_ms"]), "AUTH_WAIT": 0}
    for state, key in (
        ("SCAN", "scan_pitch_deg"),
        ("ALERT", "alert_pitch_deg"),
        ("AUTH_WAIT", "alert_pitch_deg"),
    ):
        posture = PostureSequence(
            commander, float(config["fsm"][key]), settle_ms, holds[state],
            roll_deg=float(config["posture"].get("roll_offset_deg", 0.0)),
        )
        behavior.register_sequence(state, posture)
        behavior.fsm.on_enter(state, posture.restart)
        behavior.fsm.on_exit(state, posture.release)
        result[state] = (
            f"등록 (pitch {posture.pitch_deg:+.0f}° · {posture.hold_ms}ms 머문 뒤)"
            if posture.hold_ms
            else f"등록 (pitch {posture.pitch_deg:+.0f}° · 즉시)"
        )

    # 추종 지시의 유효기간 — 명령 타임아웃과 다른 값이다 (`TrackSequence`).
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
