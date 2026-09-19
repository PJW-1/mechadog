"""운용 모드 — 경비 · 공장 · 현장지원 (WBS 3.4.4 · FR-11 · ADR-33 · ADR-34).

⚠️ **전이표를 모드별로 복제하지 않는다.** 표는 하나이고 모드는 **어떤 사건이
생길 수 있는지**만 정한다 (아키텍처 3절 설계 규칙 ⑥). 복제하면 상태 13개 ×
모드 3개를 사람이 손으로 맞춰야 하고, 그 순간 `test_fsm.py` 의 문서 대조가
지키던 불변식이 사라진다.

그래서 이 축은 `escalation.py` 와 같은 모양이다 — FSM 과 **직교**하고, 표를
데이터로 두며, 사건이 지나는 단일 지점(`runtime._apply`)에서 한 번 걸러진다.

    모드      사람을 보면        인증   변화감지   PPE   정보안내
    guard    침입자 후보         ○      ✕        ✕      ✕
    factory  작업자              ✕      ○        ○      ✕
    assist   질의 사용자         ✕      ✕        ✕      ○

⚠️ **모드는 Tier 2 판단만 고른다** (FR-11.8). 명령 타임아웃 300ms · 온보드
근거리 정지 · 페일세이프는 모드와 무관하다 — 그것들은 애초에 호스트 사건이
아니라서 이 표를 지나지 않는다. 그 사실이 곧 불간섭의 구현이다.

⚠️ **모드 전환은 L3·F 를 풀지 않는다** (FR-11.4). 이 모듈이 `Escalation` 을
아예 만지지 않는 것이 그 구현이다 — 푸는 코드가 없으면 풀리지 않는다.
비상정지로 경보를 지우지 못하게 한 것(ADR-26)과 같은 이유이며, 모드 전환이
확인 없는 해제 경로가 되면 *"경보가 뜨면 모드를 바꾼다"* 가 요령이 된다.
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

#: 운용 모드 셋. **기본은 `guard`** 다 (FR-11.2).
#:
#: ⚠️ `safety` 가 아니다. 처음 이름은 «안전점검»이었지만 보호구만 가리켜 범위를
#: 좁게 만들었고, 2026-09-17 ADR-33 개정이 **쓰러진 사람·무너진 물건·없어진
#: 물건까지 포함하는 «공장»** 으로 넓혔다.
MODES: tuple[str, ...] = ("guard", "factory", "assist")

DEFAULT: str = "guard"

#: 기능 하나가 만드는 사건 묶음. **FR-11.1 표의 행이 그대로 여기 온다.**
#:
#: 사건을 `Event` 가 아니라 이름 문자열로 두는 이유 — `fsm` 을 import 하면 순환
#: 참조가 되고, 그러면 이 축을 FSM 없이 단독으로 시험할 수 없다 (`escalation.py`
#: 의 `RAISED_BY` 와 같은 판단이다).
FEATURES: dict[str, frozenset[str]] = {
    # FR-10 사원증·암구호. `AUTH_WAIT` 로 들어가는 문이 `AUTH_REQUIRED` 하나뿐이라
    # 그것만 막아도 30초 타이머가 돌지 않지만, **나머지 둘도 함께 막는다** — 문을
    # 잠그는 것과 방을 비우는 것은 다른 일이고, 판정기가 사건을 직접 낼 수도 있다.
    "auth": frozenset({"AUTH_REQUIRED", "AUTH_OK", "AUTH_FAILED"}),
    # FR-8 구역 변화 감지. ⚠️ **도착·해제까지 막는다** — `ZONE_CHANGED` 만 막으면
    # 로봇이 구역 마커 앞에서 `ZONE_INSPECT` 로 들어가 놓고 나올 사건이 없어진다.
    "change_detect": frozenset({"ZONE_ARRIVED", "ZONE_CLEAR", "ZONE_CHANGED"}),
    # FR-9 보호구 판정. 위반과 **판정 종료** 둘 다 공장 모드의 사건이다 (FR-11.6).
    "ppe": frozenset({"PPE_VIOLATION", "PPE_SETTLED"}),
    # FR-3.5 선회 추종. ⚠️ **FR-11.1 표에 «추종» 행은 없다** — 표는 *"사람을 보면"*
    # 셀에 적었고(`assist` 는 *"자동 인증·PPE 판정·추종 없음"*), ADR-34 규칙 2 가
    # 같은 말을 반복한다. 게이트는 사건 단위라 여기서 한 줄로 세운다.
    #
    # ⚠️ **`PERSON_FOUND` 는 여기 없다.** `assist` 에서도 사람을 보면 멈춰 서서
    # 바라보는 것(`ALERT`)까지는 한다 — FR-11.1 이 끄기로 적은 것은 인증·PPE·추종
    # 셋이고 관찰이 아니다. 질의 사용자를 등지고 순찰을 계속하는 쪽이 오히려 틀렸다.
    "track": frozenset({"TARGET_OFF_CENTER"}),
}

#: 모드가 켜는 기능. **FR-11.1 표가 정본이다.**
#:
#: ⚠️ `assist` 가 비어 있는 것은 누락이 아니다. 현장지원은 **읽기 전용 정보
#: 안내**이며 로봇이 스스로 대응을 시작하지 않는다 (ADR-34 규칙 1·2). 정보 안내
#: 자체(FR-12)는 음성 경로가 맡고 FSM 사건을 만들지 않으므로 이 표에 행이 없다.
ENABLED: dict[str, frozenset[str]] = {
    "guard": frozenset({"auth", "track"}),
    "factory": frozenset({"change_detect", "ppe", "track"}),
    "assist": frozenset(),
}

#: 그 모드를 켜려면 있어야 하는 구현 (FR-11.7 · ADR-33 운용규칙 7).
#:
#: **모듈이 있느냐로 묻는다.** 설정 플래그를 따로 두면 기능 없이 `true` 를 적는
#: 길이 생기고, 그것이 바로 이 규칙이 막으려던 *"이름만 있는 모드"* 다. 판정기가
#: 저장소에 들어오는 순간 별도 조치 없이 열린다.
REQUIRES: dict[str, tuple[str, ...]] = {
    "guard": (),
    # `3.7.3` 위반 판정 + 게이팅 + 클리핑 검사 · `4.8.0` 상황 판독
    "factory": ("host.vision.ppe_detector", "host.vision.vlm_reader"),
    # `4.7.15` 운영 데이터 원본 · `4.7.16` 질의 라우터 · `4.7.17` 신선도 계약
    "assist": ("host.factory_ops.service", "host.factory_ops.router"),
}

#: 전환을 받는 상태 (FR-11.3). **`fsm.STANDBY` 와 같은 집합이지만 뜻이 다르다** —
#: 저쪽은 *"대응 단계를 올리지 않는다"* 이고 이쪽은 *"판정 규칙을 바꿔도 되는
#: 자리"* 다. 우연히 같으므로 import 해서 묶지 않는다. 한쪽이 바뀔 때 다른 쪽이
#: 딸려 가면 그것이 버그다.
SWITCHABLE: frozenset[str] = frozenset({"IDLE", "MANUAL"})


class ModeError(ConfigError):
    """모르는 모드이거나 선행 기능이 없어 기동할 수 없음.

    ⚠️ **`ConfigError` 를 물려받는다.** 기동 거부의 뜻이 *"이 설정으로는 켤 수
    없다"* 로 같고, 그래야 `runtime.main()` 의 설정 오류 경로가 **새 except 절
    없이** 이것도 잡는다 — 잡는 곳을 늘리면 언젠가 한 곳을 빠뜨린다.
    """


def _present(module: str) -> bool:
    """그 모듈이 import 가능한가. **부모 패키지가 없어도 예외를 내지 않는다.**

    `find_spec` 은 부모를 먼저 import 하므로 `host.factory_ops` 가 아직 없으면
    `ModuleNotFoundError` 를 던진다. 없다는 것이 답이지 오류가 아니다.
    """
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def missing_requirements(mode: str) -> tuple[str, ...]:
    """그 모드에 모자란 구현. 비어 있으면 켤 수 있다."""
    return tuple(name for name in REQUIRES.get(mode, ()) if not _present(name))


def available_modes() -> tuple[str, ...]:
    """지금 이 저장소에서 고를 수 있는 모드 (FR-11.7).

    `GET /health` 의 `modes` 가 이 값이다. ⚠️ **화면은 아직 읽지 않는다** — 모드
    버튼을 늘 띄워 두고 누르면 서버가 사유를 돌려주는 쪽을 골랐다(2026-09-19).
    """
    return tuple(mode for mode in MODES if not missing_requirements(mode))


class Mission:
    """지금 어떤 모드로 순찰하는가. **사건을 거르고 전환을 지킨다.**

    시계를 만들지 않는다 — `now_ms` 를 받는다 (`Escalation` 과 같은 규칙).
    """

    def __init__(self, config: Mapping[str, Any], *, mode: str | None = None) -> None:
        """설정의 `mission.mode` 를 읽되 인자가 있으면 그것이 이긴다 (`--mode`).

        ⚠️ **모르는 값이면 기동을 거부한다** (WBS 3.4.4 ①). 기본값으로 떨어지면
        오타 하나가 *"경비 모드로 잘 도는 것처럼 보이는 공장 순찰"* 이 된다.
        """
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
        """이 모드가 켠 기능. 관제 화면이 *"무엇이 돌고 있나"* 를 보이는 데 쓴다."""
        return ENABLED[self._mode]

    def enables(self, feature: str) -> bool:
        """그 기능이 이 모드에서 도는가. **사건을 만들기 전에 묻는 자리다.**

        `allows()` 가 사건 하나를 거르는 마지막 방어선이라면 이쪽은 **판정 자체를
        돌리지 않기 위한** 물음이다. 둘 다 필요하다 — 판정기는 사건 말고도 상태를
        바꾸고(인증 세션·기준 스냅샷), 그것은 `_apply` 를 지나지 않는다.

        ⚠️ **모르는 기능 이름은 예외다.** 조용히 `False` 를 내면 오타 한 글자가
        기능을 통째로 끄고, 그것은 *"잘 도는 것처럼 보이는 정지"* 가 된다.
        """
        if feature not in FEATURES:
            raise KeyError(f"모르는 기능: {feature!r} (있는 것: {', '.join(sorted(FEATURES))})")
        return feature in ENABLED[self._mode]

    def allows(self, event: str) -> bool:
        """이 사건이 이 모드에서 생길 수 있나.

        ⚠️ **어느 기능에도 속하지 않는 사건은 언제나 통과한다.** 순찰·스캔·회피·
        수동·안전은 세 모드에 공통이고(FR-11.1 *"세 모드에 공통"*), 표에 없는 것을
        막으면 모드를 늘릴 때마다 공통 사건을 하나씩 잃는다.
        """
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
        """전환을 거절할 이유. 받아도 되면 `None`.

        **사유를 문자열로 돌려주는 것이 요구사항이다** (FR-11.3 *"거부하고 사유를
        돌려준다"*). 조용히 무시하면 조작자는 버튼이 고장 난 줄 안다.
        """
        if target not in ENABLED:
            return f"모르는 운용 모드: {target!r}"
        lacking = missing_requirements(target)
        if lacking:
            return f"{target} 모드의 선행 기능이 없다: {', '.join(lacking)}"
        if state not in SWITCHABLE:
            return f"{state} 에서는 모드를 바꾸지 않는다 — 멈춰 있을 때만 받는다"
        return None

    def switch(self, target: str, *, state: str) -> str | None:
        """모드를 바꾼다. 성공이면 `None`, 거절이면 사유.

        ⚠️ **대응 단계를 건드리지 않는다** (FR-11.4). L3·F 는 그대로 남고, 전환이
        경보를 지우는 길은 만들지 않는다.

        ⚠️ **`now_ms` 를 받지 않는다.** 이 축에는 시간으로 정해지는 것이 하나도 없다
        — 타이머도 유예도 없고 전환은 즉시다. 시계를 받아 두면 *"언젠가 쓰겠지"* 가
        되고, 쓰이지 않는 인자는 호출부마다 틀린 값을 넘길 자리가 된다.
        """
        reason = self.refuse_reason(target, state=state)
        if reason is not None:
            LOG.warning("mission_mode_refused", requested=target, state=state, reason=reason)
            return reason
        if target == self._mode:
            return None
        previous, self._mode = self._mode, target
        LOG.info("mission_mode", **{"from": previous, "to": target, "state": state})
        return None
