"""PPE gate, clipping and time-window decisions without an ONNX model."""

import numpy as np

from host.vision.detector import Detection
from host.vision.ppe_detector import OK, UNDETERMINED, VIOLATION, PpeDetector
from host.vision.tracker import Track


class FakeModel:
    def __init__(self, found):
        self.found = found
        self.calls = 0

    def detect(self, _crop):
        self.calls += 1
        return self.found


def test_ppe_requires_a_visible_unclipped_person(cfg):
    model = FakeModel([Detection("helmet", 0.9, (1, 12, 12, 24))])
    detector = PpeDetector(cfg, model)
    image = np.zeros((480, 640, 3), dtype=np.uint8)
    assert detector.observe(image, (), 0) is None
    clipped = (Track(1, (100, 0, 300, 470), 0.9, 0),)
    verdict = detector.observe(image, clipped, 40)
    assert verdict.state == UNDETERMINED and verdict.clipped
    assert model.calls == 0


def test_ppe_confirms_three_hits_in_the_configured_window_per_track(cfg):
    model = FakeModel(
        [
            Detection("no_helmet", 0.9, (1, 20, 20, 40)),
            Detection("vest", 0.9, (1, 60, 20, 100)),
        ]
    )
    detector = PpeDetector(cfg, model)
    image = np.zeros((480, 640, 3), dtype=np.uint8)
    track = (Track(1, (100, 20, 300, 470), 0.9, 0),)
    assert not detector.observe(image, track, 0).confirmed
    assert not detector.observe(image, track, 1000).confirmed
    assert detector.observe(image, track, 1500).confirmed
    other = (Track(2, (100, 20, 300, 470), 0.9, 1600),)
    assert not detector.observe(image, other, 1600).confirmed


def test_ppe_needs_both_body_regions_and_accepts_complete_gear(cfg):
    model = FakeModel([Detection("helmet", 0.9, (1, 20, 20, 40))])
    detector = PpeDetector(cfg, model)
    image = np.zeros((480, 640, 3), dtype=np.uint8)
    track = (Track(1, (100, 20, 300, 470), 0.9, 0),)
    assert detector.observe(image, track, 0).state == UNDETERMINED
    model.found.append(Detection("vest", 0.9, (1, 60, 20, 100)))
    assert detector.observe(image, track, 40).state == OK
    model.found[1] = Detection("no_vest", 0.9, (1, 60, 20, 100))
    assert detector.observe(image, track, 80).state == VIOLATION
