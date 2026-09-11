"""사원증 ArUco 인증 검증 (WBS 3.8.1 · FR-10.1).

**마커를 직접 그려서 읽는다.** `cv2.aruco.generateImageMarker` 가 인쇄할 이미지를
만들어 주므로 카메라도 사원증도 없이 왕복 검증이 된다 — 생성한 ID 가 그대로
읽히는지, 여러 장이 각자 읽히는지까지.

세션 쪽 시험은 **픽셀을 쓰지 않는다.** 그래서 가상 시간으로 60초 만료를 즉시
검증하고, OpenCV 없이도 돈다.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pytest

from host.behavior.auth import Authenticator, Outcome
from host.vision.badge import BadgeReader, Marker
from host.vision.tracker import Track

T0 = 2_000_000


@pytest.fixture
def auth(cfg: dict) -> Authenticator:
    return Authenticator(cfg)


def _track(track_id: int, *, x: float = 100.0, height: float = 300.0) -> Track:
    """사람 하나. 박스는 (x, 100) ~ (x+120, 100+height) 다."""
    return Track(
        track_id=track_id,
        box=(x, 100.0, x + 120.0, 100.0 + height),
        score=0.9,
        last_seen_ms=T0,
    )


def _marker(marker_id: int, *, at: tuple[float, float] = (160.0, 250.0)) -> Marker:
    return Marker(marker_id=marker_id, center=at)


# ── 마커 읽기 (픽셀) ───────────────────────────────────────
def _frame_with(cfg: dict, *placements: tuple[int, int, int]) -> Any:
    """`(marker_id, x, y)` 들을 그려 넣은 VGA 프레임. **인쇄물의 대역이다.**"""
    import cv2

    name = cfg["auth"]["badge_dictionary"]
    dictionary = cv2.aruco.getPredefinedDictionary(getattr(cv2.aruco, name))
    frame = np.full((480, 640, 3), 200, dtype=np.uint8)
    for marker_id, x, y in placements:
        size = 100
        drawn = cv2.aruco.generateImageMarker(dictionary, marker_id, size)
        frame[y : y + size, x : x + size] = cv2.cvtColor(drawn, cv2.COLOR_GRAY2BGR)
    return frame


def test_reader_finds_the_marker_it_was_given(cfg: dict) -> None:
    reader = BadgeReader(cfg)
    (marker,) = reader.read(_frame_with(cfg, (0, 270, 190)))
    assert marker.marker_id == 0
    # 중심은 그려 넣은 사각형의 가운데다.
    assert marker.center == pytest.approx((320.0, 240.0), abs=2.0)


def test_reader_finds_several_markers(cfg: dict) -> None:
    reader = BadgeReader(cfg)
    markers = reader.read(_frame_with(cfg, (0, 80, 190), (1, 440, 190)))
    assert sorted(m.marker_id for m in markers) == [0, 1]


def test_reader_returns_nothing_on_a_blank_frame(cfg: dict) -> None:
    reader = BadgeReader(cfg)
    assert reader.read(np.full((480, 640, 3), 200, dtype=np.uint8)) == ()


def test_unknown_dictionary_is_refused(cfg: dict) -> None:
    """⚠️ 사전을 바꾸면 인쇄한 사원증도 바꿔야 한다 — 오타를 기동 때 잡는다."""
    broken = dict(cfg)
    broken["auth"] = dict(cfg["auth"], badge_dictionary="DICT_NOPE")
    with pytest.raises(ValueError, match="badge_dictionary"):
        BadgeReader(broken)


# ── 승인·거절 ──────────────────────────────────────────────
def test_registered_badge_is_granted(auth: Authenticator, cfg: dict) -> None:
    tracks = [_track(1)]
    assert auth.observe([_marker(0)], tracks, T0) is Outcome.GRANTED
    assert auth.holder(1, T0) == cfg["auth"]["badge_marker_map"][0]


def test_unregistered_badge_is_rejected(auth: Authenticator) -> None:
    """**대장에 없는 ID 는 미승인이다.** 화이트리스트가 정본이다."""
    assert auth.observe([_marker(42)], [_track(1)], T0) is Outcome.REJECTED
    assert auth.holder(1, T0) is None


def test_nothing_to_judge_without_markers_or_tracks(auth: Authenticator) -> None:
    assert auth.observe([], [_track(1)], T0) is Outcome.NOTHING
    assert auth.observe([_marker(0)], [], T0) is Outcome.NOTHING


# ── 시도 횟수 (FR-10.3) ────────────────────────────────────
def test_the_same_badge_seen_again_is_not_a_new_attempt(auth: Authenticator) -> None:
    """⚠️ **이 규칙이 없으면 `max_attempts: 2` 가 80ms 에 소진된다.**

    25fps 로 도는데 프레임마다 세면 사원증을 한 번 들어 보이는 것이 곧 2회 실패가
    된다. 프레임 수로 세다 실패한 것이 이 저장소에 이미 세 번 있다(결정 22·25·27번).
    """
    tracks = [_track(1)]
    assert auth.observe([_marker(42)], tracks, T0) is Outcome.REJECTED
    for step in range(1, 60):  # 같은 사원증을 2.4초 동안 들고 있다
        assert auth.observe([_marker(42)], tracks, T0 + step * 40) is Outcome.NOTHING
    assert auth.attempts(1) == 1


def test_a_second_different_badge_exhausts_the_attempts(auth: Authenticator, cfg: dict) -> None:
    """다른 사원증을 두 번째로 들어 보이는 것이 두 번째 시도다 (FR-10.3)."""
    assert cfg["auth"]["max_attempts"] == 2
    tracks = [_track(1)]
    assert auth.observe([_marker(42)], tracks, T0) is Outcome.REJECTED
    assert auth.observe([_marker(43)], tracks, T0 + 1000) is Outcome.EXHAUSTED
    assert auth.attempts(1) == 2


def test_attempts_are_counted_per_person(auth: Authenticator) -> None:
    """한 사람의 실패가 옆 사람의 기회를 깎으면 안 된다 (FR-3.6.2)."""
    left, right = _track(1, x=100.0), _track(2, x=400.0)
    auth.observe([_marker(42, at=(160.0, 250.0))], [left, right], T0)
    auth.observe([_marker(43, at=(160.0, 250.0))], [left, right], T0 + 1000)
    assert auth.attempts(1) == 2
    assert auth.attempts(2) == 0


def test_granted_badge_wins_over_an_unknown_one(auth: Authenticator) -> None:
    """승인된 사원증을 든 사람이 있는데 옆의 모르는 마커로 경보가 가면 안 된다."""
    tracks = [_track(1)]
    outcome = auth.observe([_marker(42), _marker(0)], tracks, T0)
    assert outcome is Outcome.GRANTED


# ── 귀속 (FR-3.6.2) ────────────────────────────────────────
def test_marker_binds_to_the_person_holding_it(auth: Authenticator) -> None:
    left, right = _track(1, x=100.0), _track(2, x=400.0)
    # 오른쪽 사람의 박스(400~520) 안에 있는 마커다.
    auth.observe([_marker(0, at=(460.0, 250.0))], [left, right], T0)
    assert auth.holder(1, T0) is None
    assert auth.holder(2, T0) is not None


def test_a_marker_nobody_holds_is_ignored(auth: Authenticator) -> None:
    """⚠️ **벽이나 화면에 떠 있는 마커가 인증을 만들어 내면 안 된다.**"""
    assert auth.observe([_marker(0, at=(620.0, 20.0))], [_track(1)], T0) is Outcome.NOTHING
    assert auth.holder(1, T0) is None


def test_overlapping_boxes_bind_to_the_nearest(auth: Authenticator) -> None:
    """둘 다 포함하면 박스가 큰(가까운) 쪽 — FR-3.8.2 와 같은 단일 기준이다."""
    far = _track(1, x=100.0, height=200.0)
    near = _track(2, x=110.0, height=360.0)
    auth.observe([_marker(0, at=(170.0, 250.0))], [far, near], T0)
    assert auth.holder(2, T0) is not None
    assert auth.holder(1, T0) is None


def test_binding_can_be_turned_off_for_diagnostics(cfg: dict) -> None:
    """추적 없이 운용하는 진단 경로 — 그때는 여러 명을 구분할 수 없다."""
    loose = dict(cfg)
    loose["auth"] = dict(cfg["auth"], bind_to_track_id=False)
    auth = Authenticator(loose)
    # 아무 박스에도 들지 않은 위치인데도 가장 가까운 대상에게 붙는다.
    assert auth.observe([_marker(0, at=(620.0, 20.0))], [_track(1)], T0) is Outcome.GRANTED


# ── 유효 시간 (FR-10.2.4) ──────────────────────────────────
def test_session_expires_after_the_configured_time(auth: Authenticator, cfg: dict) -> None:
    valid_ms = cfg["auth"]["session_valid_s"] * 1000
    auth.observe([_marker(0)], [_track(1)], T0)
    assert auth.holder(1, T0 + valid_ms - 1) is not None
    assert auth.holder(1, T0 + valid_ms) is None, "만료 후 재인증을 요구해야 한다"


def test_re_authentication_is_possible_after_expiry(auth: Authenticator, cfg: dict) -> None:
    """만료된 뒤 같은 사원증을 다시 들면 다시 승인돼야 한다.

    ⚠️ 본 마커 집합을 비우지 않으면 *"같은 사원증을 다시 본 것"* 으로 걸러져
    **영원히 재인증할 수 없다.**
    """
    valid_ms = cfg["auth"]["session_valid_s"] * 1000
    tracks = [_track(1)]
    auth.observe([_marker(0)], tracks, T0)
    later = T0 + valid_ms + 1000
    assert auth.observe([_marker(0)], tracks, later) is Outcome.GRANTED
    assert auth.holder(1, later) is not None


# ── 추적 ID 소실 (FR-3.6.3) ────────────────────────────────
def test_session_dies_with_the_track(auth: Authenticator) -> None:
    """⚠️ 세션을 남기면 같은 번호를 받은 사람이 물려받는다."""
    auth.observe([_marker(0)], [_track(1)], T0)
    assert auth.holder(1, T0) is not None
    auth.note_tracks([])
    assert auth.holder(1, T0 + 100) is None


def test_a_surviving_track_keeps_its_session(auth: Authenticator) -> None:
    left, right = _track(1, x=100.0), _track(2, x=400.0)
    auth.observe([_marker(0, at=(160.0, 250.0))], [left, right], T0)
    auth.note_tracks([left])
    assert auth.holder(1, T0 + 100) is not None


def test_attempts_reset_with_the_track(auth: Authenticator) -> None:
    """새 사람이 앞 사람의 실패 횟수를 물려받으면 즉시 경보가 된다."""
    auth.observe([_marker(42)], [_track(1)], T0)
    assert auth.attempts(1) == 1
    auth.note_tracks([])
    assert auth.attempts(1) == 0


# ── 전원 인증 (FR-3.8.1) ───────────────────────────────────
def test_everyone_must_be_authenticated(auth: Authenticator) -> None:
    """⚠️ **한 명이라도 미인증이면 유지한다** — 인증된 사람 뒤에 따라 들어오는 것이
    막아야 하는 상황이다."""
    left, right = _track(1, x=100.0), _track(2, x=400.0)
    auth.observe([_marker(0, at=(160.0, 250.0))], [left, right], T0)
    assert auth.all_authenticated([left], T0) is True
    assert auth.all_authenticated([left, right], T0) is False


def test_nobody_present_is_not_authenticated(auth: Authenticator) -> None:
    """빈 화면을 *"전원 인증됨"* 으로 보면 사람이 없을 때 경비가 풀린다."""
    assert auth.all_authenticated([], T0) is False


# ── 설정이 정본이다 (NFR-3①) ───────────────────────────────
def _rejects(broken: dict, pattern: str) -> None:
    from host.common.config import ConfigError, validate_base_config

    with pytest.raises(ConfigError, match=pattern):
        validate_base_config(broken)


def test_empty_badge_map_is_allowed_but_authenticates_nobody(cfg: dict) -> None:
    """발급 대장이 비어 있는 것은 설정 오류가 아니다 — **아무도 승인되지 않는다.**

    기동을 막지 않는 이유: 사원증을 아직 인쇄하지 않은 상태에서도 순찰·인지는
    돌아야 한다. 다만 그때 인증은 전부 거절이며 그것이 안전측이다.
    """
    from copy import deepcopy

    empty = deepcopy(cfg)
    empty["auth"]["badge_marker_map"] = {}
    from host.common.config import validate_base_config

    validate_base_config(empty)
    auth = Authenticator(empty)
    assert auth.observe([_marker(0)], [_track(1)], T0) is Outcome.REJECTED


def test_badge_map_keys_must_be_marker_ids(cfg: dict) -> None:
    """⚠️ 키는 ArUco ID 다. 사번을 키로 적으면 **영원히 아무도 인증되지 않는다.**"""
    from copy import deepcopy

    broken = deepcopy(cfg)
    broken["auth"]["badge_marker_map"] = {"EMP-001": 0}
    _rejects(broken, "badge_marker_map 키")


def test_badge_dictionary_must_be_named(cfg: dict) -> None:
    from copy import deepcopy

    broken = deepcopy(cfg)
    broken["auth"]["badge_dictionary"] = ""
    _rejects(broken, "badge_dictionary")


def test_auth_section_is_required(cfg: dict) -> None:
    """절 자체의 존재는 `REQUIRED_SECTIONS` 가 본다 — 그 검사를 두 번 만들지 않는다."""
    from copy import deepcopy

    broken = deepcopy(cfg)
    del broken["auth"]
    _rejects(broken, r"필수 설정 섹션 누락.*auth")
