"""경비 추종(`TRACK`) — 대표 박스를 조준·접근 지시로 바꾸고 정지선에서 고개를 들게 한다 (FR-3.5 · ADR-40).

편차 → `step`·`angle` 계산은 `LockOnTracker` 가 하고, 여기서는 정지선·제자리 회전·중앙
히스테리시스를 얹어 `TARGET_CENTERED`/`TARGET_OFF_CENTER` 사건과 `TRACK` 시퀀스 지시로 넘긴다.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any, cast

from host.behavior.fsm import Behavior, Event
from host.behavior.mission import Mission
from host.behavior.tracker import LockOnTracker
from host.common.logging_setup import EdgeTrigger, PeriodicSummary, event_logger

if TYPE_CHECKING:
    from host.behavior.actions import TrackSequence
    from host.vision.worker import VisionResult

#: 런타임과 같은 로거 이름을 쓴다 — 로그 레코드가 옮기기 전과 같아야 한다.
LOG = event_logger("mechadog.runtime")


class TrackController:
    """사람 추종의 조준·접근·정지선과 «고개를 들었다»(engaged) 상태를 맡는다.

    모든 메서드는 운용 루프 스레드에서만 부른다(`Runtime.tick`·`ingest`·시퀀스·FSM 훅).
    """

    def __init__(
        self,
        config: Mapping[str, Any],
        *,
        behavior: Behavior,
        mission: Mission,
        summary: PeriodicSummary,
        apply: Callable[[Event, int], bool],
        fall_suspected: Callable[[], bool],
    ) -> None:
        self._behavior = behavior
        self._mission = mission
        self._summary = summary
        self._apply = apply
        self._fall_suspected = fall_suspected
        self._edge = EdgeTrigger()
        # 추종은 **검출이 들어오는 자리에서** 계산한다 — 비전은 25fps 로
        # 오고 명령은 10Hz 로 나가므로, 명령 쪽에서 계산하면 프레임을 버리게 된다.
        # 계산 결과는 `TRACK` 시퀀스에 넘기고 그쪽이 명령 주기로 옮긴다.
        self._tracker = LockOnTracker(config)
        self._sequence = cast("TrackSequence | None", behavior.sequence_for("TRACK"))
        # 직전 추종 지시 시각. 공백 길이를 재는 데 쓴다 (`track_gap_ms`).
        self._last_track_ms: int | None = None
        self._stop_height_px = float(config["fsm"].get("track_target_height_px") or 0) * float(
            config["fsm"]["track_stop_ratio"]
        )
        self._stop_reached = False
        fsm = config["fsm"]
        self._stop_dist_cm = float(fsm["track_stop_dist_cm"])
        self._aim_timeout_ms = int(fsm["track_aim_timeout_ms"])
        self._stop_ms = 0
        self._deadzone_px = float(fsm["track_deadzone_px"])
        self._turn_split_px = float(fsm["track_turn_split_px"])
        self._turn_small_deg = float(fsm["track_turn_small_deg"])
        self._turn_large_deg = float(fsm["track_turn_large_deg"])
        self._dist_cm: float | None = None
        self._centered = False
        # 경비에서 고개를 들었는가 (ADR-40). 든 뒤로는 움직이지 않고, L1 은 여기서 시작한다.
        self._engaged = False

    @property
    def engaged(self) -> bool:
        """경비에서 고개를 들었나 (ADR-40) — 든 뒤로는 추종하지 않는다."""
        return self._engaged

    def engage(self) -> None:
        """고개를 들었다고 적는다 (`ALERT` 시퀀스가 자세를 보낸 뒤)."""
        self._engaged = True

    def rearm(self) -> None:
        """순찰을 시작할 때 정지선 도달과 고개 든 상태를 푼다."""
        self._stop_reached = False
        self._engaged = False

    def note_distance(self, dist_cm: float | None) -> None:
        """텔레메트리의 초음파 거리 — 정지선 판정에 쓴다."""
        self._dist_cm = dist_cm

    def tracks_person(self) -> bool:
        """경비 추종 모드인가. 공장(PPE)·현장지원은 추종하지 않는다 (FR-11.1)."""
        return self._mission.enables("track") and not self._mission.enables("ppe")

    def track(self, result: VisionResult, now_ms: int) -> None:
        """대표 박스의 x편차를 추종 지시로 바꿔 사건과 시퀀스에 넘긴다 (FR-3.5 · ADR-40).

        `ALERT`·`TRACK` 에서만, 추종 모드이거나 쓰러짐 의심일 때만 돈다. 박스가 없으면
        아무 사건도 내지 않는다 — 대상 상실은 FR-3.7 의 5초 타이머 소관이다.
        """
        if not self._behavior.tracking or not (self.tracks_person() or self._fall_suspected()):
            # ⚠️ **추종 구간을 벗어나면 엣지 기억을 지운다.** 남겨 두면 다시 `ALERT` 로
            # 들어왔을 때 첫 판정이 *"변화 없음"* 으로 삼켜져 `TARGET_OFF_CENTER` 가
            # 나가지 않는다. 그러면 시퀀스가 없는 `ALERT` 에
            # 정지한 채 갇힌다 — 실기에서 편차 84px 대상을 앞에 두고
            # 33초를 서 있었다.
            self._edge.forget("track_centered")
            self._centered = False
            self._last_track_ms = None
            return
        if self._engaged:
            # 고개를 든 뒤에는 다시 쫓지 않는다 (ADR-40). 대상이 떠나면 5초 상실이 순찰로 돌린다.
            return
        box = result.sighting.box
        if box is None:
            return
        width = int(getattr(result, "frame_width", 0) or 0)
        if width <= 0:
            return
        center_x = (float(box[0]) + float(box[2])) / 2.0
        box_height = float(box[3]) - float(box[1])
        try:
            command = self._tracker.update(center_x, width, box_height)
        except ValueError as exc:
            # 데드존이 화면 반폭 이상이면 추종이 성립하지 않는다. 설정 오류이며
            # 이 프레임을 버리고 다음으로 간다 — 여기서 죽으면 순찰까지 멈춘다.
            if self._edge.changed("track_config_bad", True):
                LOG.error("track_unusable", reason=str(exc))
            return
        self._edge.changed("track_config_bad", False)
        # 실기 검증 근거 — 1초 요약(`telemetry_summary`)에 추종 지표를 같이 싣는다.
        # ⚠️ **절대값을 넣는다.** 부호를 그대로 평균 내면 좌우로 떠는 것이 0 으로
        # 상쇄돼 미세진동이 감춰진다 — DoD 가 확인하라는 바로 그것이다.
        self._summary.observe("track_dev_px", abs(command.deviation_px))
        # ⚠️ **거리 유지(`FR-3.5.2`)의 목표값을 정하려면 이 숫자부터 있어야 한다.**
        # `fsm.track_target_height_px` 는 화각·장착 높이·사람 키가 섞여 계산으로
        # 세울 수 없다 — 목표 거리(약 1.0m)에 서서 여기 찍히는 값을 읽어 채운다.
        self._summary.observe("track_box_h_px", box_height)
        # ⚠️ **공백의 «길이» 를 남긴다.** 요약은 개수만 세므로 지시가 몇 번 나갔는지는
        # 알아도 **얼마나 오래 비었는지**를 알 수 없었고, 그래서 `track_coast_ms` 를
        # 한 번의 관측(중앙값 956ms)으로 어림해야 했다. 이 값이 쌓이면 상한이 맞는지
        # 숫자로 판정된다 — 보행 흔들림이 만드는 공백은 기체·바닥마다 다르다.
        if self._last_track_ms is not None:
            self._summary.observe("track_gap_ms", float(now_ms - self._last_track_ms))
        self._last_track_ms = now_ms
        if not command.centered:
            self._summary.count("track_off_center")
        # ⚠️ **전이를 먼저, 지시는 그다음이다.** `TRACK` 진입 훅이 지난 추종의
        # 잔상을 지우므로(`TrackSequence.forget`), 순서를 뒤집으면 방금 넣은 지시가
        # 함께 지워져 **추종 첫 주기가 통째로 정지로 나간다.**
        #
        # 같은 판정이 이어지는 동안은 사건을 내지 않는다 — 25fps 로 같은 전이를
        # 수백 번 넣으면 로그가 전이로 뒤덮이고 단계 축도 흔들린다.
        #
        # 조향 정렬과 거리 정지는 다르다. 멀리 있는 중앙 대상에게도 직진하고,
        # 정지선에서는 걸음을 멈춘 채 제자리에서 돌아 중앙을 맞춘 뒤 고개를 든다 (ADR-40).
        if not self._stop_reached and (
            (self._stop_height_px and box_height >= self._stop_height_px)
            # 높이 목표가 없으면 추종기는 중앙 대상 앞에서 걷지 않는다 — 거기가 정지선이다.
            or (command.centered and command.step == 0.0)
            or (self._dist_cm is not None and 0 < self._dist_cm <= self._stop_dist_cm)
        ):
            self._stop_reached = True
            self._stop_ms = now_ms
            # 박스와 초음파 중 무엇이 세웠는지 남긴다 — 1초 평균으로는 순간값이 가려진다.
            LOG.info("track_stop_reached", box_h_px=round(box_height, 1), dist_cm=self._dist_cm)
        deviation = command.deviation_px
        # 정지선에서는, 또는 편차가 크면 걷지 않고 제자리에서 돈다 (ADR-40).
        if self._stop_reached or abs(deviation) > self._turn_split_px:
            step, angle = 0.0, self._spin_angle(deviation)
        else:
            step, angle = command.step, command.angle
        # ⚠️ **한 번 중앙에 들면 분기점(120px)까지는 붙든다** — 히스테리시스. 웅크린 사람은
        # 박스 중심이 프레임마다 ±30px 떨려 데드존(40px) 경계를 넘나든다. 실기에서
        # 2.3초 동안 `ALERT ⇄ TRACK` 14회를 오가며 자세 대기(1초)가 매번 처음부터 다시 돌았다.
        limit = self._turn_split_px if self._centered else self._deadzone_px
        centered = self._stop_reached and (
            abs(deviation) <= limit
            # 상한이 지나면 조준을 끝낸 것으로 본다 — 피하는 대상이 L1 을 영영 막지 못하게.
            or now_ms - self._stop_ms >= self._aim_timeout_ms
        )
        self._centered = centered
        self._summary.observe("track_angle_deg", abs(angle))
        self._summary.observe("track_step_mm", step)
        if self._edge.changed("track_centered", centered):
            LOG.info(
                "track_command",
                centered=centered,
                deviation_px=round(deviation, 1),
                angle=round(angle, 2),
                step=round(step, 1),
            )
            self._apply(Event.TARGET_CENTERED if centered else Event.TARGET_OFF_CENTER, now_ms)
        if self._sequence is not None:
            self._sequence.note(step, angle, now_ms)

    def _spin_angle(self, deviation_px: float) -> float:
        """제자리 회전각. 데드존 안은 0, 그 밖은 두 단계다 — 좌회전 15° 이하가 안 돈다."""
        size = abs(deviation_px)
        if size <= self._deadzone_px:
            return 0.0
        angle = self._turn_small_deg if size <= self._turn_split_px else self._turn_large_deg
        # 편차 부호와 조향 부호는 반대다 — 오른쪽(양수)이면 우회전(음수).
        return -angle if deviation_px > 0 else angle
