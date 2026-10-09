"""측위 신뢰(`LocalizationTrust`) 단위 시험 — 순찰기 대신 읽는 설정·관측만 흉내 낸다."""

from __future__ import annotations

import math
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

from host.behavior.heading import HeadingTracker
from host.behavior.live_nav import LocalScan, NavParams
from host.behavior.localization import LocalizationTrust
from host.behavior.nav_state import PatrolNavState
from host.behavior.patrol import SafetyView


def trust(yaw_deg: float | None = None, seen_ms: int | None = None, **config: Any) -> Any:
    owner = SimpleNamespace(
        safety=SafetyView(yaw_deg=yaw_deg, last_seen_ms=seen_ms),
        pose=(0.0, 0.0, 0.0),
        pose_seeded=False,
        imu_fresh_ms=300,
        reloc_votes=3,
        reloc_vote_yaw_rad=math.radians(10.0),
        reloc_stationary_max_peers=10,
        reloc_restore_enabled=True,
        reloc_restore_max_age_ms=30000,
        reloc_restore_yaw_rad=math.radians(5.0),
        reloc_restore_tilt_rad=math.radians(8.0),
        zone_hint_ms=60000,
        point_hint_radius_m=0.6,
    )
    for key, value in config.items():
        setattr(owner, key, value)
    owner.heading = HeadingTracker(owner)
    return owner, LocalizationTrust(owner, PatrolNavState(local_scan=LocalScan(NavParams())))


def test_untrusted_only_for_own_localization_without_verify_or_seed() -> None:
    owner, loc = trust()
    assert loc.untrusted is False, "외부 측위는 그쪽이 보증한다"
    loc.own_localization = True
    assert loc.untrusted is True
    owner.pose_seeded = True
    assert loc.untrusted is False
    owner.pose_seeded = False
    loc.verified = True
    assert loc.untrusted is False


def test_new_epoch_and_note_move_invalidate_votes_and_requests() -> None:
    _, loc = trust()
    loc.global_votes.append((0.0, 0.0, 0.0))
    loc.restore_votes.append((0.0, 0.0, 0.0))
    loc.new_epoch()
    assert (loc.loc_epoch, loc.global_votes, loc.restore_votes) == (1, [], [])
    loc.moved_since_vote = False
    loc.note_move()
    assert loc.moved_since_vote is True and loc.move_seq == 1


def test_lifting_the_body_drops_the_restore_anchor() -> None:
    _, loc = trust()
    tilt0 = (0.0, 0.0)
    loc.restore_anchor = ((1.0, 1.0, 0.0), 0.0, 1000, 0, tilt0)
    loc.restore_votes.append((1.0, 1.0, 0.0))
    loc.observe_tilt(5.0, -5.0)
    assert loc.tilt == pytest.approx((math.radians(5), math.radians(-5)))
    assert loc.restore_anchor is not None, "한도 안의 기울기는 기준을 지킨다"
    loc.observe_tilt(0.0, 9.0)
    assert loc.restore_anchor is None and loc.restore_votes == []


def test_votes_need_agreeing_answers_and_independent_scenes() -> None:
    _, loc = trust()
    assert loc.vote_global((1.0, 1.0, 0.0), peers=50) is False
    assert loc.global_votes == [(1.0, 1.0, 0.0)]
    assert loc.vote_global((1.1, 1.0, 0.0), peers=50) is False, "같은 장면의 모호한 답"
    assert len(loc.global_votes) == 1
    assert loc.vote_global((1.1, 1.0, 0.0), peers=5) is False
    assert loc.vote_global((3.0, 1.0, 0.0)) is False, "0.3m 넘게 다르면 다시 센다"
    assert loc.global_votes == [(3.0, 1.0, 0.0)]
    loc.note_move()
    assert loc.vote_global((3.0, 1.1, 0.0), peers=50) is False
    assert loc.vote_global((3.0, 1.0, 0.05), peers=5) is True
    assert loc.global_votes == []


def test_restore_prior_follows_the_imu_until_the_anchor_is_invalid() -> None:
    owner, loc = trust(yaw_deg=3.0, seen_ms=1000)
    loc.restore_anchor = ((1.0, 2.0, 0.5), 0.0, 900, 0, None)
    prior = loc.restore_prior(1100)
    assert prior == pytest.approx((1.0, 2.0, 0.5 + math.radians(3)))
    owner.safety = SafetyView(yaw_deg=10.0, last_seen_ms=1000)
    assert loc.restore_prior(1100) is None, "IMU 가 창 넘게 돌았다"
    assert loc.restore_anchor is None
    loc.restore_anchor = ((1.0, 2.0, 0.5), 0.0, 900, 0, None)
    loc.note_move()
    assert loc.restore_prior(1100) is None, "상실 뒤 이동 명령이 나갔다"


def test_point_hint_limits_the_search_until_it_expires() -> None:
    _, loc = trust()
    loc.point_hint = (1.0, 1.0, 1000)
    allowed = loc.zone_filter(2000)
    assert allowed is not None
    assert allowed(np.array([1.0, 1.5, 2.0]), np.array([1.0, 1.0, 1.0])).tolist() == [
        True,
        True,
        False,
    ]
    assert loc.zone_filter(61001) is None
    assert loc.point_hint is None
