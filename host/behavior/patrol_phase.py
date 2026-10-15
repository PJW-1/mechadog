"""순찰 단계와 계획 라벨 — `PatrolController` 와 그 협력 객체가 함께 쓰는 어휘.

`host.behavior.patrol` 이 그대로 다시 내보내므로 기존 import 는 바뀌지 않는다.
협력 객체(`recovery` 등)는 순환 import 를 피하려고 여기서 직접 가져온다.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum

from host.common.protocol import FSM_STATES

#: 지도에서 사람이 찍은 목표의 계획 라벨 — 구역 id 와 겹치지 않는다(구역 id 는 설정의 짧은 기호).
GOAL_LABEL = "GOAL"
HOME_LABEL = "HOME"


class Phase(StrEnum):
    """순찰 내부 단계. 규약의 상태가 아니며 `FSM_STATE_FOR` 로 사상해 내려보낸다."""

    IDLE = "IDLE"  # 기동 후 순찰 시작 전
    PLANNING = "PLANNING"  # 다음 구역 선정 · 경로 생성
    MOVING = "MOVING"  # 웨이포인트 추종
    AIMING = "AIMING"  # 구역 도착 후 카메라 방위로 제자리 회전
    INSPECT = "INSPECT"  # 구역 도착 — 카메라 훅
    LOST = "LOST"  # 측위 실패
    HALTED = "HALTED"  # 안전 래치 (사람이 풀어야 한다)


#: 내부 단계 → FSM 상태 13종 (PROTOCOL.md 2절 `STATE`). 13종 밖의 이름은 로봇이 폐기한다.
#: `AVOID` 는 내려보내지 않는다 — 반향되면 해제를 알 수 없다 (ADR-22).
FSM_STATE_FOR: Mapping[Phase, str] = {
    Phase.IDLE: "IDLE",
    Phase.PLANNING: "PATROL",
    Phase.MOVING: "PATROL",
    Phase.AIMING: "PATROL",
    Phase.INSPECT: "ZONE_INSPECT",
    Phase.LOST: "LOST",
    Phase.HALTED: "FAILSAFE",
}

# 사상표가 규약과 어긋나면 import 시점에 기동을 막는다.
assert set(FSM_STATE_FOR.values()) <= FSM_STATES, "FSM 13종에 없는 상태를 사상하고 있다"
