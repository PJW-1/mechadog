"""LiDAR 스캔 중계 — 최신 스캔으로 측위하고 그 결과를 구역·대시보드·기록·수집기로 넘긴다.

`--lidar-device` 로 길 찾기가 붙었을 때만 일을 한다. 수신 스레드는 스캔을 칸에 넣고
전방 위험만 즉시 세우며(`host/telemetry/lidar_feed.py`), 이 모듈의 메서드는 모두 운용
루프 스레드에서 부른다 (`Runtime.tick`·`Runtime._poll_vision`).
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any, cast

from host.common.logging_setup import event_logger
from host.vision.frame_collector import collector_from_config

if TYPE_CHECKING:
    from host.behavior.fsm import Behavior
    from host.behavior.path_cause import PathCause
    from host.behavior.patrol import PatrolController
    from host.behavior.zone_inspector import ZoneInspector
    from host.behavior.zone_policy import ZonePpePolicy
    from host.common.lidar_link import Scan
    from host.slam.pose_out import PoseOut
    from host.telemetry.session_recorder import SessionRecorder
    from host.vision.worker import VisionResult

#: 런타임과 같은 로거 이름을 쓴다 — 로그 레코드가 옮기기 전과 같아야 한다.
LOG = event_logger("mechadog.runtime")


class ScanRelay:
    """스캔 측위 · 위치 전파 · 측위 기록 · 새 장애물(막힘 기록·학습 프레임 수집)을 맡는다."""

    def __init__(
        self,
        config: Mapping[str, Any],
        *,
        device_id: str,
        navigator: Callable[[], PatrolController | None],
        behavior: Behavior,
        zone_ppe: ZonePpePolicy,
        zone_inspector: ZoneInspector,
        path_cause: PathCause,
        latest: Callable[[], VisionResult | None],
        pose_out: PoseOut | None,
        recorder: SessionRecorder | None,
    ) -> None:
        # 호출 때 찾는다 — 시험이 만든 뒤에 `runtime._navigator` 를 대역으로 바꾼다.
        self._navigator = navigator
        self._behavior = behavior
        self._zone_ppe = zone_ppe
        self._zone_inspector = zone_inspector
        self._path_cause = path_cause
        self._latest = latest
        #: 대시보드로 가는 측위 포즈 — 지도 폴더의 `pose_frame.json` 이 있을 때만
        #: 만든다 (`PoseOut.of`). 없으면 보내지 않는다.
        self._pose_out = pose_out
        self._recorder = recorder
        self._recorded_nav_phase: str | None = None
        #: 스캔 공급자 — `main()` 이 `LidarFeed.take` 를 붙인다 (`attach`).
        self.take_scan: Callable[[], Scan | None] | None = None
        #: VLM 학습용 프레임 수집기 (4.8.7) — `vision.collect.root` 가 비면 꺼져 있다.
        self.collector = collector_from_config(config, device_id)
        #: 항법 설정 중 여기서 직접 읽는 스위치(`publish_new_obstacles`).
        self._config_nav = dict(config.get("nav") or {})

    def attach(self, take: Callable[[], Scan | None]) -> None:
        """최신 스캔 공급자를 붙인다 (`LidarFeed.take`). 길 찾기가 없으면 쓰지 않는다."""
        self.take_scan = take

    def note_pose(self, pose: tuple[float, float, float], now_ms: int) -> None:
        """측위의 최신 위치 `(x m, y m, yaw rad)` 를 받는다 (`ZoneInspector.note_pose`).

        LiDAR 길 찾기(`--lidar-device`)의 스캔 정합이 `observe` 에서 부른다.
        그것이 없으면 아무도 부르지 않고 구역 도착이 일어나지 않는다.
        """
        self._zone_ppe.note_pose(pose, now_ms)
        self._zone_inspector.note_pose(pose, now_ms)
        if self._pose_out is not None:
            # 대시보드 실시간 위치 — 송신 실패는 순찰을 늦추지 않는다 (`PoseOut.send`).
            self._pose_out.send(
                pose,
                moving=self._behavior.state == "PATROL",
                score_frac=getattr(self._navigator(), "match_frac", None),
                zone=self._zone_ppe.current_zone(now_ms),
                verified=bool(getattr(self._navigator(), "pose_verified", False)),
            )

    def observe(self, now_ms: int) -> None:
        """최신 스캔으로 측위하고, 자세가 갱신됐으면 구역 점검에 넘긴다.

        ⚠️ **루프 스레드에서만 길 찾기 상태를 바꾼다.** 수신 스레드는 스캔을 칸에
        넣고 전방 위험만 즉시 세운다 (`host/telemetry/lidar_feed.py`).
        """
        navigator = self._navigator()
        if navigator is None or self.take_scan is None:
            return
        scan = self.take_scan()
        if scan is not None:
            _obs_started = time.perf_counter()
            navigator.observe_scan(scan, now_ms)
            _obs_ms = (time.perf_counter() - _obs_started) * 1000
            if _obs_ms > 300:
                LOG.warning("observe_scan_slow", ms=round(_obs_ms, 1), points=len(scan.points))
            updated = navigator.pose_ms == now_ms
            if updated:
                self.note_pose(navigator.pose, now_ms)
            elif (
                self._pose_out is not None
                and navigator.pose_ms is not None
                and navigator.pose_stale(now_ms)
            ):
                # 약한 정합을 조용히 삼키지 않는다 — 마지막 자세를 LOST 로 표시해
                # 대시보드가 낡은 위치를 신선한 것처럼 보여주지 않게 한다.
                self._pose_out.send(
                    navigator.pose,
                    moving=False,
                    lost=True,
                    score_frac=getattr(navigator, "match_frac", None),
                    zone=self._zone_ppe.current_zone(now_ms),
                    verified=False,
                )
            if self._recorder is not None:
                x, y, yaw = navigator.pose
                self._recorder.record(
                    "localization",
                    at_ms=now_ms,
                    scan_seq=scan.seq,
                    scan_boot=scan.boot_id,
                    points=len(scan.points),
                    updated=updated,
                    score_frac=round(navigator.match_frac, 3),
                    lost=navigator.pose_stale(now_ms),
                    verified=navigator.pose_verified,
                    zone=self._zone_ppe.current_zone(now_ms),
                    pose=[round(x, 4), round(y, 4), round(yaw, 5)],
                    phase=navigator.phase.value,
                    target=navigator.target,
                    scan_gate=navigator.scan_gate.status,
                )
        if self._recorder is not None and navigator.phase.value != self._recorded_nav_phase:
            self._recorded_nav_phase = navigator.phase.value
            self._recorder.record(
                "navigator_phase",
                at_ms=now_ms,
                phase=self._recorded_nav_phase,
                halt_reason=navigator.halt_reason,
                target=navigator.target,
            )
        # 지도에 없던 새 끝점을 모두 «통로 막힘» 사건으로 내면 시연 중 사람·의자마다 방송이 나간다
        # (현장 10-06). 원인 판독(ADR-45)은 `nav.publish_new_obstacles` 로 켤 때만 낸다.
        publish = bool(self._config_nav.get("publish_new_obstacles", False))
        for hit in navigator.take_new_obstacles():
            self.collect_blocked(hit, now_ms)
            if publish:
                self.record_path_blocked(hit, now_ms)

    def collect_blocked(self, hit: tuple[float, float], now_ms: int) -> None:
        """LiDAR 막힘 확정 프레임을 VLM 학습용으로 모은다 (4.8.7 · 꺼져 있으면 아무 일 없다).

        프레임이 없어도 수집기에 알린다 — 막힘 사건 자체로 clear 보류 시간을 건다.
        """
        if not self.collector.enabled:
            return
        result = self._latest()
        self.collector.note_blocked(
            now_ms,
            result.jpeg if result is not None else None,
            hit,
            cast("PatrolController", self._navigator()).target,
            self._behavior.state,
            frame_ms=result.frame_received_ms if result is not None else None,
            frame_seq=result.frame_seq if result is not None else None,
        )

    def collect_clear(self, result: VisionResult, now_ms: int) -> None:
        """막힘 없는 순찰 프레임을 VLM 학습용으로 모은다. 근거리 반사 정지·장애물 확인 중엔 거른다."""
        navigator = self._navigator()
        if not self.collector.enabled or navigator is None:
            return
        self.collector.note_clear(
            now_ms,
            result.jpeg,
            state=self._behavior.state,
            obstacle_active=navigator.safety.obstacle_active,
            pending=navigator.obstacle_pending,
            frame_ms=result.frame_received_ms,
            frame_seq=result.frame_seq,
        )

    def record_path_blocked(self, hit: tuple[float, float], now_ms: int) -> None:
        """이동 경로가 새 장애물로 막혔다 — **가벼운 경고만** 남기고 순찰은 이어 간다.

        단계·FSM 은 바꾸지 않는다. 재계획(A*)이 돌아갈 길을 찾고, 못 찾으면 컨트롤러가
        재확인 뒤 그 구역을 이번 사이클에서 버린다 (`PatrolController._replan`).

        ⚠️ **순찰 중일 때만 기록한다.** 추적·경보 중에는 앞에 선 사람이 신규 장애물로
        확정되는데, 그것은 «경로가 막혔다» 가 아니다. 표시 자체는 남아 재계획이 피해 간다.

        기록은 `PathCause` 가 그 프레임의 원인 판독(«무너진 물건인가» · ADR-45)을 실어 한 번
        남긴다 — 판독을 걸 수 있으면 답이나 `vision.vlm.path_cause_wait_ms` 까지 미룬다.
        """
        judgement: dict[str, Any] = {
            "x": round(hit[0], 2),
            "y": round(hit[1], 2),
            "target": cast("PatrolController", self._navigator()).target,
            "source": "lidar",
        }
        if self._behavior.state != "PATROL":
            LOG.info("path_obstacle_ignored", state=self._behavior.state, **judgement)
            return
        LOG.warning("path_blocked", **judgement)
        result = self._latest()
        self._path_cause.blocked(judgement, result, now_ms)
