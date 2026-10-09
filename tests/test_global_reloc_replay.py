"""Offline reports must not count missing references or rejected candidates as fixes."""

import json
import math
from collections import Counter

import pytest

from host.slam.scan_match import MatchResult
from tools.lidar.global_reloc_replay import (
    agreement,
    classify,
    evaluate,
    iter_samples,
    reference_pose,
    rejection,
    summarize,
)


@pytest.mark.parametrize(
    "changes",
    [
        {"updated": False},
        {"lost": True},
        {"lost": None},
        {"pose": [0, 0, float("nan")]},
        {"pose": [False, 0, 0]},
        {"pose": None},
    ],
)
def test_reference_excludes_stale_default_and_nonfinite_poses(changes):
    event = {"updated": True, "lost": False, "pose": [1, 2, 3]}
    event.update(changes)
    assert reference_pose(event) is None


def test_agreement_requires_both_translation_and_wrapped_heading():
    reference = (0.0, 0.0, math.radians(179))
    assert agreement((0.3, 0.0, math.radians(-171)), reference) is True
    assert agreement((0.301, 0.0, reference[2]), reference) is False
    assert agreement((0.0, 0.0, math.radians(-170)), reference) is False
    assert agreement(reference, None) is None


def test_report_separates_false_fixes_and_missing_references():
    rows = [
        {
            "after": {
                "eligible": True,
                "confirmed": True,
                "agrees_with_local": value,
                "late": False,
                "decision": "eligible",
            }
        }
        for value in (True, False, None)
    ]
    summary = summarize(rows, "after")
    assert summary["vote_confirmations"] == 3
    assert summary["assessed_confirmations"] == 2
    assert summary["false_fixes_vs_local"] == 1
    assert summary["unassessed_confirmations"] == 1
    assert summarize([], "after")["confirmation_rate"] is None


class Voter:
    def __init__(self):
        self.global_votes = [object()]
        self.calls = Counter()

    def vote_global(self, _pose, _peers):
        self.calls["vote"] += 1
        return True


@pytest.mark.parametrize("diagnostic", [{"search_complete": False}, {"competing_peaks": 1}])
def test_unresolved_and_late_results_do_not_vote(diagnostic):
    voter = Voter()
    result = MatchResult((0, 0, 0), 60, **diagnostic)
    entry = evaluate(result, [None] * 60, None, {}, voter, 0.1)
    assert not entry["eligible"] and not entry["confirmed"]
    assert not voter.global_votes and not voter.calls
    entry = evaluate(MatchResult((0, 0, 0), 60), [None] * 60, None, {}, voter, 3.1)
    assert entry["eligible"] and entry["late"] and not entry["confirmed"]
    assert not voter.calls


def test_classification_does_not_call_unfinished_search_resolved():
    assert classify("peers", None) == "sampling_or_score_scale_rejection"
    assert classify("peers", "unfinished_search") == "unresolved_budget"
    assert classify("peers", "competing_full_scan_peaks") == "geometric_ambiguity_evidence"
    assert rejection(MatchResult((0, 0, 0), 1), 60, {}) == "weak_score"


def test_replay_pairs_exact_scan_and_excludes_lost_reference(tmp_path):
    manifest = {"mount_yaw_deg": 0, "angle_direction": 1, "lidar_device": "lidar-a"}
    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    events = []
    for seq in (1, 2):
        raw = {
            "device_id": "lidar-a",
            "boot_id": "boot-a",
            "type": "SCAN",
            "seq": seq,
            "ts": seq * 100,
            "points": [[angle, 1000] for angle in range(0, 360, 5)],
        }
        events.append({"kind": "scan", "t": seq * 100, "raw": json.dumps(raw)})
    for seq, lost in ((99, False), (1, False), (2, True)):
        events.append(
            {
                "kind": "localization",
                "t": 300,
                "scan_seq": seq,
                "scan_boot": "boot-a",
                "points": 72,
                "updated": not lost,
                "lost": lost,
                "pose": [1, 2, 0],
            }
        )
    (tmp_path / "events.jsonl").write_text(
        "\n".join(json.dumps(event) for event in events), encoding="utf-8"
    )
    stats = Counter()
    samples = list(iter_samples(tmp_path, 1, stats))
    assert samples[0][2] is None, "an unrelated event must not consume a pending scan"
    assert samples[1][2].seq == 1
    assert samples[1][3] == (1.0, 2.0, 0.0)
    assert samples[2][2].seq == 2 and samples[2][3] is None
    assert stats["paired"] == 2 and stats["unpaired"] == 1
    assert stats["reference_available"] == 1
