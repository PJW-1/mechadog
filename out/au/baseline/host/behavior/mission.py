"""운용 모드 — 경비 · 공장 (FR-11 · ADR-33).

전이표는 하나이고, 모드는 어떤 사건이 생길 수 있는지만 정한다 (아키텍처 3절 설계 규칙 ⑥).
FSM 과 직교하는 축이며 사건이 지나는 단일 지점(`runtime._apply`)에서 한 번 걸러진다.

    모드      사람을 보면        인증   변화감지   PPE
    guard    침입자 후보         ○      ✕        ✕
    factory  작업자              ✕      ○        ○

- 모드는 Tier 2 판단만 고른다 (FR-11.8) — 명령 타임아웃·온보드 근거리 정지·페일세이프는 호스트
  사건이 아니라 이 표를 지나지 않는다.
- 모드 전환은 L3·F 를 풀지 않는다 (FR-11.4 · ADR-33 규칙 3) — 이 모듈은 `Escalation` 을 만지지 않는다.
"""

from __future__ import annotations

import importlib.util
from collections.abc import Mapping
from typing import Any

from host.common.config import ConfigError
from host.common.logging_setup import event_logger

LOG = event_logger("mechadog.mission")

__all__ = [
    "ENABLED",
    "FEATURES",
    "MODES",
    "REQUIRES",
    "Mission",
    "ModeError",
    "available_modes",
]

#: 운용 모드. 기본은 `guard` 다 (FR-11.2 · ADR-33 «공장 모드의 범위»).
MODES: tuple[str, ...] = ("guard", "factory")

DEFAULT: str = "guard"

#: 기능 하나가 만드는 사건 묶음 — FR-11.1 표의 행이다. `fsm` 순환 import 를 피해 이름 문자열로 둔다.
FEATURES: dict[str, frozenset[str]] = {
    # FR-10 사원증·암구호. 진입(`AUTH_REQUIRED`)뿐 아니라 판정 사건도 함께 막는다.
    "auth": frozenset({"AUTH_REQUIRED", "AUTH_OK", "AUTH_FAILED"}),
    # FR-8 구역 변화 감지. 도착·해제까지 막는다 — `ZONE_INSPECT` 에 들어가 나올 사건이 없게 되지 않게.
    "change_detect": frozenset(
        {"ZONE_ARRIVED", "ZONE_CLEAR", "ZONE_CHANGED", "ZONE_ALARM_CONFIRMED"}
    ),
    # FR-9 보호구 판정. 위반과 **판정 종료** 둘 다 공장 모드의 사건이다 (FR-11.6).
    "ppe": frozenset({"PPE_VIOLATION", "PPE_SETTLED"}),
    # FR-9 쓰러짐 — 의심·확정·해제 사건은 공장 모드에서만 난다 (ADR-42 결정 2).
    "fallen": frozenset({"PERSON_DOWN", "FALL_SUSPECTED", "FALL_RESOLVED"}),
    # FR-3.5 선회 추종. `PERSON_FOUND`(멈춰 바라보기)는 모든 모드에 공통이라 여기 없다.
    "track": frozenset({"TARGET_OFF_CENTER"}),
}

#: 모드가 켜는 기능 (FR-11.1).
ENABLED: dict[str, frozenset[str]] = {
    "guard": frozenset({"auth", "track"}),
    "factory": frozenset({"change_detect", "ppe", "fallen", "track"}),
}

#: 그 모드를 켜려면 import 가능해야 하는 모듈 (FR-11.7 · ADR-33 규칙 7). 설정 플래그가 아니다.
REQUIRES: dict[str, tuple[str, ...]] = {
    "guard": (),
    # `ppe_detector` 위반 판정 + 게이팅 + 클리핑 검사 · `vlm_reader` 상황 판독
    "factory": ("host.vision.ppe_detector", "host.vision.vlm_reader"),
}

#: 전환을 받는 상태 (FR-11.3 · ADR-33 규칙 2). `fsm.STANDBY` 와 값이 같을 뿐 뜻이 달라 묶지 않는다.
SWITCHABLE: frozenset[str] = frozenset({"IDLE", "MANUAL"})


class ModeError(ConfigError):
    """모르는 모드이거나 선행 기능이 없어 기동할 수 없음 (`ConfigError` 경로로 잡힌다)."""


def _present(module: str) -> bool:
    """그 모듈이 import 가능한가. 부모 패키지가 없어도 예외 없이 `False` 다."""
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def missing_requirements(mode: str) -> tuple[str, ...]:
    """그 모드에 모자란 구현. 비어 있으면 켤 수 있다."""
    return tuple(name for name in REQUIRES.get(mode, ()) if not _present(name))


def available_modes() -> tuple[str, ...]:
    """지금 이 저장소에서 고를 수 있는 모드 (FR-11.7).

    `GET /health` 의 `modes` 가 이 값이다.
    """
    return tuple(mode for mode in MODES if not missing_requirements(mode))


class Mission:
    """지금 어떤 모드로 순찰하는가 — 사건을 거르고 전환 조건을 지킨다. 시간과 무관하다."""

    def __init__(self, config: Mapping[str, Any], *, mode: str | None = None) -> None:
        """설정의 `mission.mode` 를 읽되 인자(`--mode`)가 이긴다. 모르는 값이면 `ModeError`."""
        section = config.get("mission") or {}
        chosen = mode if mode is not None else str(section.get("mode", DEFAULT))
        if chosen not in ENABLED:
            raise ModeError(f"모르는 운용 모드: {chosen!r} (고를 수 있는 것: {', '.join(MODES)})")
        lacking = missing_requirements(chosen)
        if lacking:
            raise ModeError(f"{chosen} 모드의 선행 기능이 없다: {', '.join(lacking)}")
        self._mode = chosen

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def features(self) -> frozenset[str]:
        """이 모드가 켠 기능 (관제 화면 표시용)."""
        return ENABLED[self._mode]

    def enables(self, feature: str) -> bool:
        """그 기능이 이 모드에서 도는가 — 판정 자체를 돌리기 전에 묻는다. 모르는 이름은 `KeyError`.

        판정기는 사건 말고도 상태(인증 세션·기준 스냅샷)를 바꾸므로 `allows()` 와 별도로 필요하다.
        """
        if feature not in FEATURES:
            raise KeyError(f"모르는 기능: {feature!r} (있는 것: {', '.join(sorted(FEATURES))})")
        return feature in ENABLED[self._mode]

    def allows(self, event: str) -> bool:
        """이 사건이 이 모드에서 생길 수 있나. 어느 기능에도 속하지 않는 공통 사건은 늘 통과한다."""
        enabled = ENABLED[self._mode]
        for feature, events in FEATURES.items():
            if event in events:
                return feature in enabled
        return True

    def blocked(self) -> frozenset[str]:
        """이 모드가 만들지 않는 사건 전부. 시험과 화면 설명에 쓴다."""
        return frozenset(
            event
            for feature, events in FEATURES.items()
            if feature not in ENABLED[self._mode]
            for event in events
        )

    # ── 전환 (FR-11.3) ──────────────────────────────────────
    def refuse_reason(self, target: str, *, state: str) -> str | None:
        """전환을 거절할 사유 문자열 (FR-11.3). 받아도 되면 `None`."""
        if target not in ENABLED:
            return f"모르는 운용 모드: {target!r}"
        lacking = missing_requirements(target)
        if lacking:
            return f"{target} 모드의 선행 기능이 없다: {', '.join(lacking)}"
        if state not in SWITCHABLE:
            return f"{state} 에서는 모드를 바꾸지 않는다 — 멈춰 있을 때만 받는다"
        return None

    def switch(self, target: str, *, state: str) -> str | None:
        """모드를 즉시 바꾼다. 성공이면 `None`, 거절이면 사유. 대응 단계는 건드리지 않는다 (FR-11.4)."""
        reason = self.refuse_reason(target, state=state)
        if reason is not None:
            LOG.warning("mission_mode_refused", requested=target, state=state, reason=reason)
            return reason
        if target == self._mode:
            return None
        previous, self._mode = self._mode, target
        LOG.info("mission_mode", **{"from": previous, "to": target, "state": state})
        return None
