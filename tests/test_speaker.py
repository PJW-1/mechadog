"""말하기 경로 단독 검증 — `host.behavior.speaker.Speaker` (Runtime 분할 9단계).

사건 → 문장·트랙 시나리오는 `test_runtime.py`·`test_demo_flow_20261006.py` 가 `Runtime` 으로 본다.
여기서는 런타임 없이 트랙 고르기·반복·ACK 대기·재전송을 본다.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

import host.behavior.speaker as speaker_module
from host.behavior.commander import Commander
from host.behavior.speaker import SOUND_ACK_TIMEOUT_MS, SOUND_RETRIES, Speaker
from host.common.protocol import CommandEncoder


class Clock:
    def __init__(self) -> None:
        self.now = 0

    def __call__(self) -> int:
        return self.now


def _speaker(
    tracks: dict[str, int],
    *,
    repeat_after_ms: int = 0,
    announcer: Any = None,
    latest: Any = None,
) -> tuple[Speaker, Commander, Clock]:
    clock = Clock()
    commander = Commander(CommandEncoder(clock=clock), period_ms=100, clock=clock)
    config = {"robot_sound": {"tracks": tracks, "repeat_after_ms": repeat_after_ms}}
    speaker = Speaker(
        config,
        commander=commander,
        clock=clock,
        announcer=announcer,
        latest=latest or (lambda: None),
    )
    return speaker, commander, clock


def _sounds(lines: list[str]) -> list[dict[str, Any]]:
    return [m for m in map(json.loads, lines) if m["type"] == "SOUND"]


def test_play_sends_known_track_and_skips_unknown_or_out_of_range() -> None:
    speaker, commander, _ = _speaker({"person_fallen": 7, "bad": 0, "huge": 3001})
    speaker.play("nothing")
    speaker.play("bad")
    speaker.play("huge")
    assert not commander.has_pending("SOUND")
    speaker.play("person_fallen")
    assert [m["track"] for m in _sounds(commander.tick(0))] == [7]


def test_warning_repeats_once_but_guidance_does_not() -> None:
    speaker, commander, clock = _speaker(
        {"ppe_violation_warning": 5, "route_started": 6}, repeat_after_ms=1000
    )
    speaker.play("ppe_violation_warning")
    commander.tick(0)
    speaker.repeat(999)
    assert not commander.has_pending("SOUND")
    speaker.repeat(1000)
    assert [m["track"] for m in _sounds(commander.tick(1000))] == [5]
    speaker.repeat(5000)
    assert not commander.has_pending("SOUND"), "반복은 한 번뿐이다"

    clock.now = 2000
    speaker.play("route_started")
    commander.tick(2000)
    speaker.repeat(9000)
    assert not commander.has_pending("SOUND"), "안내 문장은 반복하지 않는다"


def test_unacked_sound_is_resent_with_new_seq_then_given_up() -> None:
    speaker, commander, _ = _speaker({"person_fallen": 7})
    speaker.play("person_fallen")
    first = commander.tick(0)
    speaker.watch(first, 0)
    seqs = [_sounds(first)[0]["seq"]]
    now = 0
    for _ in range(SOUND_RETRIES):
        now += SOUND_ACK_TIMEOUT_MS
        speaker.resend(now)
        lines = commander.tick(now)
        speaker.watch(lines, now)
        seqs.append(_sounds(lines)[0]["seq"])
    assert len(set(seqs)) == SOUND_RETRIES + 1
    speaker.resend(now + SOUND_ACK_TIMEOUT_MS)
    assert not commander.has_pending("SOUND"), "재전송을 다 쓰면 더 싣지 않는다"


def test_ack_clears_the_wait() -> None:
    speaker, commander, _ = _speaker({"person_fallen": 7})
    speaker.play("person_fallen")
    lines = commander.tick(0)
    speaker.watch(lines, 0)
    speaker.note_ack(json.dumps({"type": "ACK", "seq": _sounds(lines)[0]["seq"]}))
    speaker.resend(SOUND_ACK_TIMEOUT_MS)
    assert not commander.has_pending("SOUND")


def test_ppe_warning_key_names_the_single_missing_item() -> None:
    def verdict(*labels: tuple[str, str]) -> SimpleNamespace:
        regions = [SimpleNamespace(item=item, label=label) for item, label in labels]
        return SimpleNamespace(ppe=SimpleNamespace(regions=regions))

    latest: list[Any] = [verdict(("helmet", "no_helmet"), ("vest", "vest"))]
    speaker, _, _ = _speaker({"ppe_violation_helmet_warning": 9}, latest=lambda: latest[0])
    assert speaker.ppe_warning_key("ppe_violation_warning") == "ppe_violation_helmet_warning"
    latest[0] = verdict(("helmet", "no_helmet"), ("vest", "no_vest"))
    assert speaker.ppe_warning_key("ppe_violation_warning") == "ppe_violation_warning"
    latest[0] = None
    assert speaker.ppe_warning_key("ppe_violation_warning") == "ppe_violation_warning"


def test_announce_survives_broken_announcer_and_picks_detector_item_track(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(speaker_module, "describe", lambda kind, _j: f"{kind} 문장")

    def broken(_sentence: str) -> None:
        raise RuntimeError("boom")

    speaker, commander, _ = _speaker({"hazard_notice_lighter": 4}, announcer=broken)
    judgement = {"source": "detector", "items": ["lighter", "lighter"]}
    assert speaker.announce("hazard_notice", judgement) == "hazard_notice 문장"
    assert [m["track"] for m in _sounds(commander.tick(0))] == [4]


def test_route_edges_speak_start_and_completion_only() -> None:
    speaker, commander, _ = _speaker({"route_started": 1, "route_finished": 2})
    nav = SimpleNamespace(route_active=True, route_status=lambda: {"status": "stopped"})
    speaker.route_edges(nav)  # type: ignore[arg-type]
    assert [m["track"] for m in _sounds(commander.tick(0))] == [1]
    nav.route_active = False
    speaker.route_edges(nav)  # type: ignore[arg-type]
    assert not commander.has_pending("SOUND"), "정지는 말하지 않는다"
    nav.route_active = True
    speaker.route_edges(nav)  # type: ignore[arg-type]
    commander.tick(100)
    nav.route_active = False
    nav.route_status = lambda: {"status": "completed"}
    speaker.route_edges(nav)  # type: ignore[arg-type]
    assert [m["track"] for m in _sounds(commander.tick(200))] == [2]
