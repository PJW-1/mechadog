"""순찰 항법 공유 상태 — 순찰기와 협력자가 함께 읽고 쓰는 시계·목표·재계획 대기·송신 기록.

`PatrolController` 가 협력자보다 먼저 하나 만들어 협력자(`RouteFollower`·`LocalAvoidance` …)에
넘긴다. 협력자는 값을 복사해 두지 않고 매번 여기서 읽는다.

- **시계** — 이번 틱의 시각(`now_ms`)과 지금 처리 중인 스캔의 시각(`scan_now_ms`).
- **목표** — 지도에서 찍은 곳·동선 지점과 그 자리 대기 (`GoalState`).
- **재계획 대기** — 실제 STOP 송신과 안정 스캔을 기다리는 관문 (`ReplanGate`).
- **국소 스캔** — 최신 실측 스캔(`local_scan`)과 그것을 받은 자세(`local_scan_pose`).
- **송신 기록** — 마지막 송신이 이동이었는가, 언제부터 서 있었는가.

순찰 루프 스레드 전용이라 잠금이 없다. 전역 탐색 워커(`reloc_worker.py`)는 이것을 읽지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from host.common.logging_setup import EdgeTrigger

if TYPE_CHECKING:
    from host.behavior.live_nav import LocalScan
    from host.slam.scan_match import Pose


@dataclass(slots=True)
class GoalState:
    """지도에서 찍은 목표(순찰 좌표 m)와 그 자리 대기."""

    #: 있으면 구역 순찰보다 먼저 그리로 간다.
    xy: tuple[float, float] | None = None
    #: 찍은 곳에 도착(또는 못 가게 됨) — 사람이 순찰을 다시 시작할 때까지 그 자리에 선다.
    hold: bool = False
    #: 대기 이유 — «reached»(찍은 곳 도착) | «blocked»(가는 도중 길이 막힘) | «route_…» | None.
    hold_reason: str | None = None

    def set(self, xy: tuple[float, float]) -> None:
        """새 목표로 간다 — 대기를 푼다."""
        self.xy, self.hold, self.hold_reason = xy, False, None

    def hold_at(self, reason: str) -> None:
        """목표를 버리고 그 자리에서 기다린다."""
        self.xy, self.hold, self.hold_reason = None, True, reason

    def clear(self) -> None:
        """목표도 대기도 없다."""
        self.xy, self.hold, self.hold_reason = None, False, None


@dataclass(slots=True)
class ReplanGate:
    """재계획 전 정지 확인 — 실제 STOP 송신과 그 뒤 안정 스캔을 기다린다."""

    required: bool = False
    #: 대기를 시작한 틱 시각 — 상한(`REPLAN_SETTLE_TIMEOUT_MS`)을 여기서 잰다.
    wait_started_ms: int | None = None
    #: 상한으로 대기를 푼 틱 — 그 틱은 정지로 끝낸다.
    timeout_hold_ms: int | None = None

    def require(self, now_ms: int) -> None:
        """정지 확인을 요구한다. 이미 기다리는 중이면 시작 시각을 밀지 않는다."""
        if not self.required or self.wait_started_ms is None:
            self.wait_started_ms = now_ms
        self.required = True

    def clear(self) -> None:
        self.required = False
        self.wait_started_ms = None


@dataclass(slots=True)
class PatrolNavState:
    """순찰기와 협력자가 함께 쓰는 항법 상태. `local_scan` 은 다시 만들지 않는다."""

    local_scan: LocalScan
    #: 바뀔 때만 로그를 남기는 표지 — 순찰기와 협력자가 한 인스턴스를 같이 쓴다
    #: (제자리 회전 «spin» 은 구역 추종과 동선 직접 추종이 같은 키다).
    edge: EdgeTrigger = field(default_factory=EdgeTrigger)
    goal: GoalState = field(default_factory=GoalState)
    replan: ReplanGate = field(default_factory=ReplanGate)
    #: 이번 틱의 Host 시각.
    now_ms: int = 0
    #: 지금 처리 중인 스캔의 Host 시각 (`observe_scan` 이 찍는다).
    scan_now_ms: int = 0
    #: 직전 추종 틱이 제자리 회전이었나 (`steering_for(spinning=)`).
    spinning: bool = False
    #: `local_scan` 을 받았을 때의 (x, y, 조향 방위) — 그 뒤 이동만큼 통로 원점을 옮긴다.
    local_scan_pose: Pose | None = None
    #: 마지막으로 보낸 명령이 이동이었나 — STOP 송신 확인에 쓴다.
    last_sent_moving: bool = False
    #: 실제 정지(STOP 송신·온보드 정지)를 처음 본 시각 — 걷는 중이면 None.
    stopped_since_ms: int | None = None
