"""행동 상태 머신의 입력 사건 — `host.behavior.fsm` 전이표가 쓰는 이름.

텔레메트리 수신(`host.telemetry.receiver`)도 로봇 보고를 이 사건으로 바꾸므로 행동 계층이 아니라
여기에 둔다. 아래 계층은 `host.behavior` 를 가져오지 않는다 (`tests/test_layering.py`).
"""

from __future__ import annotations

from enum import StrEnum


class Event(StrEnum):
    """전이를 일으키는 입력 — «무엇이 일어났다» 이지 지시가 아니다.

    `ONBOARD_FAILSAFE` 는 로봇이 이미 멈췄다는 보고다 (Tier 1 우선 · 아키텍처 1.2).
    """

    # ── 순찰 ──
    START_PATROL = "START_PATROL"  # 대시보드에서 순찰 시작을 눌렀다
    SCAN_DUE = "SCAN_DUE"  # 순찰 타이머 10s 만료 (FR-2.4)
    GOAL_UNREACHABLE = "GOAL_UNREACHABLE"  # Single goto ended without reaching its goal.
    SCAN_DONE = "SCAN_DONE"  # 상체 스캔 3초 완료
    # ── 온보드 반사를 호스트가 따라간다 ──
    ONBOARD_AVOID = "ONBOARD_AVOID"  # 초음파 25cm 반사 정지를 로봇이 보고했다
    AVOID_CLEARED = "AVOID_CLEARED"  # 회피 시퀀스 완료 & 전방 clear
    # ── 사람 대응 ──
    PERSON_FOUND = "PERSON_FOUND"  # 300ms 시간 창 안에 3회 검출 (FR-3.2)
    TARGET_OFF_CENTER = "TARGET_OFF_CENTER"  # x축 편차 > 데드존 (FR-3.5)
    TARGET_CENTERED = "TARGET_CENTERED"  # 중앙 정렬 유지
    TARGET_LOST = "TARGET_LOST"  # 미검출 5s 지속 (FR-3.7)
    PPE_VIOLATION = "PPE_VIOLATION"  # 보호구 미착용 확정 (FR-9.3)
    # 보호구 판정 종료 — 적합과 `PPE_UNDETERMINED` 둘 다다 (FR-11.6). 그래서 `PPE_OK` 가 아니다.
    PPE_SETTLED = "PPE_SETTLED"
    # 쓰러짐 확정 (FR-9 · ADR-42 결정 2). 전이표에 없다 — 단계만 L3 로 올린다.
    PERSON_DOWN = "PERSON_DOWN"
    # 공장 쓰러짐 의심 — 누움 후보나 VLM `person_down` «예» 한 번.
    # 순찰·구역 점검을 멈추고 사람 대응(`ALERT`)으로 든다. 이미 `ALERT`·`TRACK` 이면 사건이 없다.
    FALL_SUSPECTED = "FALL_SUSPECTED"
    # 의심 제한 시간 초과 · 쓰러짐 경보(L3) 확인 → 순찰 복귀. 누운 사람은 스스로
    # 떠나지 않아 대상 상실이 걸리지 않는다 — `ZONE_ALARM_CONFIRMED` 와 같은 이유다.
    FALL_RESOLVED = "FALL_RESOLVED"
    ALARM_CONFIRMED = "ALARM_CONFIRMED"  # 래치 경보 확인 뒤 보류한 임무 재개
    # ── 인증 ──
    AUTH_REQUIRED = "AUTH_REQUIRED"  # 미인증 상태 지속 → L2
    AUTH_OK = "AUTH_OK"  # 사원증 또는 암구호 인증 성공
    AUTH_FAILED = "AUTH_FAILED"  # 2회 실패 또는 30초 초과 → L3
    # ── 수동 ──
    MANUAL_ON = "MANUAL_ON"  # 조작자가 수동 조종을 잡았다
    MANUAL_OFF = "MANUAL_OFF"  # 조작자가 놓았다
    # ── 안전 ──
    ONBOARD_FAILSAFE = "ONBOARD_FAILSAFE"  # 링크 두절·저전압·전도를 로봇이 보고
    LINK_LOST = "LINK_LOST"  # 텔레메트리가 끊겼다
    ESTOP = "ESTOP"  # 사람이 비상정지를 눌렀다
    RESET_CONFIRMED = "RESET_CONFIRMED"  # 원인 해소를 사람이 확인했다
    # ── Phase 2 ──
    HAZARD_ALARM = "HAZARD_ALARM"  # 위험구역 알람 수신
    HAZARD_ARRIVED = "HAZARD_ARRIVED"  # 웨이포인트 목표 도달
    HAZARD_SCAN_DONE = "HAZARD_SCAN_DONE"  # 판독 완료
    POSE_STALE = "POSE_STALE"  # SLAM pose 500ms 미갱신
    POSE_REACQUIRED = "POSE_REACQUIRED"  # 재측위 성공
    ZONE_ARRIVED = "ZONE_ARRIVED"  # 구역 도착
    ZONE_CLEAR = "ZONE_CLEAR"  # 검사 완료 & 변화 없음
    ZONE_CHANGED = "ZONE_CHANGED"  # 구역 위험(넘어짐·무너짐) 확정 (FR-8.4)
    ZONE_ALARM_CONFIRMED = "ZONE_ALARM_CONFIRMED"  # 구역 변화 경보(L3)를 사람이 확인했다
