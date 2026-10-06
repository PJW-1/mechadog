"""다중 인원 추적 검증 (WBS 3.3.4 · FR-3.6).

**두 사람이 있을 때가 본론이다.** 한 사람만 있으면 어떤 결합 규칙도 통과하므로,
이 파일의 시험은 대부분 **두 사람을 서로 바꿔 놓으려는 시도**다 — 스쳐 지나기,
가려지기, 한 명만 사라지기.

시간은 전부 주입한다. 소실 버퍼 1초를 실제로 기다리지 않는다.
"""

from __future__ import annotations

import time

import pytest

from host.vision.detector import Detection
from host.vision.tracker import PersonTracker, Track, iou

T0 = 1_000_000
#: 추론 주기 (25fps). 프레임 간 간격은 이 값이다.
STEP = 40


@pytest.fixture
def tracker(cfg: dict) -> PersonTracker:
    return PersonTracker(cfg)


def _person(
    x: float, *, width: float = 60.0, height: float = 200.0, score: float = 0.9
) -> Detection:
    """바닥에 서 있는 사람 하나. `x` 는 좌상단 x 다."""
    return Detection(label="person", score=score, box=(x, 100.0, x + width, 100.0 + height))


def _chair(x: float = 400.0) -> Detection:
    return Detection(label="chair", score=0.8, box=(x, 300.0, x + 50.0, 350.0))


def _ids(tracks: tuple[Track, ...]) -> list[int]:
    return [t.track_id for t in tracks]


# ── 겹침 계산 ──────────────────────────────────────────────
def test_iou_of_identical_boxes_is_one() -> None:
    assert iou((0.0, 0.0, 10.0, 10.0), (0.0, 0.0, 10.0, 10.0)) == pytest.approx(1.0)


def test_iou_of_disjoint_boxes_is_zero() -> None:
    assert iou((0.0, 0.0, 10.0, 10.0), (20.0, 20.0, 30.0, 30.0)) == 0.0


def test_iou_of_touching_boxes_is_zero() -> None:
    """변이 닿기만 한 것은 겹친 것이 아니다 — 경계에서 ID 가 붙으면 안 된다."""
    assert iou((0.0, 0.0, 10.0, 10.0), (10.0, 0.0, 20.0, 10.0)) == 0.0


def test_iou_ignores_degenerate_boxes() -> None:
    """면적 0 인 박스는 0 나눗셈이 된다 — 겹치지 않은 것으로 본다."""
    assert iou((5.0, 5.0, 5.0, 5.0), (0.0, 0.0, 10.0, 10.0)) == 0.0


# ── 기본 ──────────────────────────────────────────────────
def test_no_detections_yields_no_tracks(tracker: PersonTracker) -> None:
    assert tracker.update([], T0) == ()
    assert tracker.tracked == 0


def test_non_person_detections_are_ignored(tracker: PersonTracker) -> None:
    """FR-8 변화 감지 대상까지 사람으로 추적하면 상한이 의자로 채워진다."""
    assert tracker.update([_chair(), _chair(500)], T0) == ()
    assert tracker.tracked == 0


def test_same_person_keeps_the_id_across_frames(tracker: PersonTracker) -> None:
    first = tracker.update([_person(100)], T0)
    assert _ids(first) == [1]
    for step in range(1, 25):  # 1초 동안 조금씩 걸어간다
        moved = tracker.update([_person(100 + step * 4)], T0 + step * STEP)
        assert _ids(moved) == [1], f"{step}번째 프레임에서 ID 가 바뀌었다"


def test_track_carries_the_latest_box_and_score(tracker: PersonTracker) -> None:
    tracker.update([_person(100, score=0.6)], T0)
    (track,) = tracker.update([_person(140, score=0.95)], T0 + STEP)
    assert track.box[0] == 140.0
    assert track.score == pytest.approx(0.95)
    assert track.last_seen_ms == T0 + STEP


def test_height_is_the_distance_proxy(tracker: PersonTracker) -> None:
    """주 대상 선정과 상한이 쓰는 단일 기준 (FR-3.8.2 · FR-3.8.5)."""
    (near,) = tracker.update([_person(100, height=300.0)], T0)
    assert near.height == pytest.approx(300.0)


# ── 두 사람 (DoD) ──────────────────────────────────────────
def test_two_people_get_distinct_stable_ids(tracker: PersonTracker) -> None:
    """**WBS 3.3.4 DoD** — 2인 이상 동시 등장 시 ID 유지."""
    left, right = _person(100), _person(400)
    assert _ids(tracker.update([left, right], T0)) == [1, 2]
    for step in range(1, 20):
        tracks = tracker.update(
            [_person(100 + step * 2), _person(400 - step * 2)], T0 + step * STEP
        )
        assert _ids(tracks) == [1, 2], f"{step}번째 프레임에서 ID 가 흔들렸다"


def test_detection_order_does_not_decide_identity(tracker: PersonTracker) -> None:
    """⚠️ 검출기는 **점수 내림차순**으로 준다 — 점수가 뒤바뀌면 순서도 뒤바뀐다.

    순서로 ID 를 붙이면 그 순간 두 사람이 서로 바뀐다. 겹침으로 잇는 이유다.
    """
    tracker.update([_person(100, score=0.95), _person(400, score=0.6)], T0)
    swapped = tracker.update([_person(402, score=0.95), _person(102, score=0.6)], T0 + STEP)
    by_id = {t.track_id: t.box[0] for t in swapped}
    assert by_id[1] == pytest.approx(102.0), "왼쪽 사람이 1번을 유지해야 한다"
    assert by_id[2] == pytest.approx(402.0)


def test_approaching_each_other_does_not_swap_ids(tracker: PersonTracker) -> None:
    """서로에게 다가간다 — 매 프레임 겹침이 가장 큰 짝을 고르므로 유지된다.

    ⚠️ **완전히 겹치는 순간까지는 시험하지 않는다.** 두 박스가 같아지면 어느 쪽에
    어느 ID 를 주어도 옳다고 말할 수 없고, 그 상황을 단정하는 시험은 구현을
    베끼는 것이 된다. 여기서는 **구분이 가능한 동안 유지되는지**만 본다.
    """
    tracker.update([_person(100), _person(300)], T0)
    for step in range(1, 25):  # 4px/프레임씩. step 24 에서 196 대 204 로 아직 구분된다
        left, right = 100 + step * 4, 300 - step * 4
        tracks = tracker.update([_person(left), _person(right)], T0 + step * STEP)
        assert len(tracks) == 2
        by_id = {t.track_id: t.box[0] for t in tracks}
        assert by_id[1] == pytest.approx(left), f"{step}번째에서 좌우가 뒤바뀌었다"
        assert by_id[2] == pytest.approx(right)


def test_best_overlap_wins_not_the_first_acceptable_one(tracker: PersonTracker) -> None:
    """⚠️ **이 시험이 결합 규칙 자체를 가른다.**

    두 사람이 가까이 서 있으면 **교차 짝도 임계를 넘는다.** 트랙 순서대로 훑으며
    *임계를 넘는 첫 검출*을 집으면 두 사람이 서로 바뀐다. 전 짝을 겹침 내림차순으로
    정렬해 **가장 겹치는 짝부터** 확정해야 한다.

        트랙 A(100) ↔ 검출 A'(104) = 0.88   ← 정답
        트랙 A(100) ↔ 검출 B'(124) = 0.43   ← 임계(0.3) 를 넘는다
    """
    tracker.update([_person(100), _person(120)], T0)
    # 검출 순서를 뒤집어 준다 — 점수가 뒤바뀌면 검출기가 실제로 이렇게 준다.
    tracks = tracker.update([_person(124), _person(104)], T0 + STEP)
    by_id = {t.track_id: t.box[0] for t in tracks}
    assert by_id[1] == pytest.approx(104.0), "왼쪽 사람이 1번을 유지해야 한다"
    assert by_id[2] == pytest.approx(124.0)


def test_one_person_leaving_does_not_disturb_the_other(tracker: PersonTracker) -> None:
    tracker.update([_person(100), _person(400)], T0)
    (staying,) = tracker.update([_person(402)], T0 + STEP)
    assert staying.track_id == 2
    # 떠난 사람은 소실 버퍼에 남아 있다 — 결과에는 없지만 아직 추적 중이다.
    assert tracker.tracked == 2


# ── 공백과 소실 ────────────────────────────────────────────
def test_short_gap_keeps_the_id(tracker: PersonTracker) -> None:
    """⚠️ **실기 통과율이 52% 였다** — 한 프레임 공백은 예외가 아니라 일상이다."""
    tracker.update([_person(100)], T0)
    tracker.update([], T0 + STEP)  # 한 번 놓친다
    tracker.update([], T0 + STEP * 2)
    (again,) = tracker.update([_person(112)], T0 + STEP * 3)
    assert again.track_id == 1


def test_long_gap_yields_a_new_id(tracker: PersonTracker, cfg: dict) -> None:
    """**ID 재발급은 재인증을 뜻하고 그것이 안전측이다** (FR-3.6.3)."""
    lost_ms = cfg["vision"]["tracker"]["track_lost_ms"]
    tracker.update([_person(100)], T0)
    (again,) = tracker.update([_person(100)], T0 + lost_ms)
    assert again.track_id == 2
    assert tracker.tracked == 1, "만료된 대상이 남아 있으면 상한을 잠식한다"


def test_expiry_happens_before_association(tracker: PersonTracker, cfg: dict) -> None:
    """만료가 뒤에 오면 **이미 죽었어야 할 대상이 새 검출을 가로챈다.**"""
    lost_ms = cfg["vision"]["tracker"]["track_lost_ms"]
    tracker.update([_person(100)], T0)
    # 같은 자리에 나타나므로 겹침은 완벽하다 — 그래도 시간이 지났으면 다른 사람이다.
    (again,) = tracker.update([_person(100)], T0 + lost_ms + 500)
    assert again.track_id == 2


def test_ids_are_never_reused(tracker: PersonTracker, cfg: dict) -> None:
    """⚠️ **재사용하면 만료된 인증 세션을 다음 사람이 물려받는다** (FR-3.6.2)."""
    lost_ms = cfg["vision"]["tracker"]["track_lost_ms"]
    seen: list[int] = []
    for round_index in range(4):
        at = T0 + round_index * (lost_ms + STEP)
        (track,) = tracker.update([_person(100)], at)
        seen.append(track.track_id)
    assert seen == [1, 2, 3, 4]
    assert len(set(seen)) == len(seen)


# ── 겹침 임계 ──────────────────────────────────────────────
def test_a_jump_larger_than_the_box_starts_a_new_track(tracker: PersonTracker) -> None:
    """겹침이 임계 미만이면 잇지 않는다 — 순간이동한 박스는 다른 사람이다."""
    tracker.update([_person(100)], T0)
    (jumped,) = tracker.update([_person(400)], T0 + STEP)
    assert jumped.track_id == 2


def test_threshold_comes_from_config_not_code(cfg: dict) -> None:
    """설정을 고치면 동작이 바뀌어야 한다 (NFR-3①)."""
    loose = dict(cfg)
    loose["vision"] = dict(
        cfg["vision"], tracker={"iou_match_threshold": 0.05, "track_lost_ms": 1000}
    )
    tracker = PersonTracker(loose)
    tracker.update([_person(100)], T0)
    # 60px 폭 박스를 50px 옮기면 겹침이 약 0.09 다 — 0.3 에서는 끊기고 0.05 에서는 이어진다.
    (moved,) = tracker.update([_person(150)], T0 + STEP)
    assert moved.track_id == 1

    strict = PersonTracker(cfg)
    strict.update([_person(100)], T0)
    (broken,) = strict.update([_person(150)], T0 + STEP)
    assert broken.track_id == 2, "기본 임계에서는 같은 이동이 끊긴다"


# ── 상한 (FR-3.8.5) ────────────────────────────────────────
def test_capacity_keeps_the_nearest(tracker: PersonTracker, cfg: dict) -> None:
    """초과 시 **박스 큰 순으로 유지**한다 — 큰 것이 가까운 것이다."""
    cap = cfg["vision"]["max_tracked_persons"]
    crowd = [_person(index * 70, height=100.0 + index * 10) for index in range(cap + 2)]
    tracks = tracker.update(crowd, T0)
    assert tracker.tracked == cap
    assert len(tracks) == cap, "버려진 대상을 결과로 내보내면 죽은 ID 를 쥐게 된다"
    heights = sorted((t.height for t in tracks), reverse=True)
    assert heights == sorted((d.box[3] - d.box[1] for d in crowd), reverse=True)[:cap]


def test_a_visible_person_outranks_a_lost_one(tracker: PersonTracker, cfg: dict) -> None:
    """⚠️ 크기만으로 정렬하면 **방금 사라진 큰 사람**을 들고 **지금 앞에 있는
    작은 사람**을 버린다."""
    cap = cfg["vision"]["max_tracked_persons"]
    big = [_person(index * 70, height=300.0) for index in range(cap)]
    tracker.update(big, T0)
    assert tracker.tracked == cap

    # 큰 사람들은 전부 사라지고, 작은 사람 하나가 새로 들어온다.
    small = _person(600, height=120.0)
    tracks = tracker.update([small], T0 + STEP)
    assert _ids(tracks) == [cap + 1], "새로 보이는 사람이 결과에 있어야 한다"
    assert tracker.tracked == cap


def test_capacity_warns(tracker: PersonTracker, cfg: dict, caplog) -> None:
    import logging

    cap = cfg["vision"]["max_tracked_persons"]
    with caplog.at_level(logging.WARNING, logger="mechadog.vision"):
        tracker.update([_person(index * 70) for index in range(cap + 3)], T0)
    row = next(
        rec for rec in caplog.records if getattr(rec, "event", "") == "tracker_over_capacity"
    )
    assert row.detail == {"kept": cap, "dropped": 3}


# ── FR-3.6.1 추가 추론 패스가 없다 ─────────────────────────
def test_update_is_post_processing_not_inference(tracker: PersonTracker) -> None:
    """FR-3.6.1 — 신경망이 아니라 후처리다.

    ⚠️ **여유를 크게 잡은 것은 의도다.** CI 러너의 속도를 재는 시험이 아니라
    *추론 패스가 숨어 있지 않다*는 것을 재는 시험이다. 실제 비용은 5명 기준
    갱신당 수십 µs 이며, 여기서는 1ms 를 넘지 않는 것만 본다 — 추론이 끼어
    있었다면 8.2ms 씩 걸려 40배 넘게 초과한다.
    """
    crowd = [_person(index * 70) for index in range(5)]
    started = time.perf_counter()
    for step in range(1000):
        tracker.update(crowd, T0 + step * STEP)
    elapsed_ms = (time.perf_counter() - started) * 1000
    assert elapsed_ms < 1000, f"갱신 1000회에 {elapsed_ms:.0f}ms — 추론이 끼어 있나"
