"""Short-check duration, evidence preservation and startup failure cleanup."""

import argparse
import json

import pytest

from tools import vision_link_check as check


@pytest.mark.parametrize("value", ["0", "-1", "61", "nan", "inf"])
def test_refuses_long_or_invalid_duration(value):
    with pytest.raises(argparse.ArgumentTypeError):
        check.short_seconds(value)


def test_existing_output_is_not_overwritten(tmp_path):
    marker = tmp_path / "original.txt"
    marker.write_text("preserve", encoding="utf-8")
    with pytest.raises(FileExistsError):
        check.run("mechdog-01", "127.0.0.1", 1, tmp_path)
    assert marker.read_text(encoding="utf-8") == "preserve"


def test_failed_model_start_is_recorded_without_opening_camera(monkeypatch, tmp_path):
    def fail(_self):
        raise RuntimeError("model initialization failed")

    def unexpected(*_args, **_kwargs):
        pytest.fail("Camera must not open before model initialization")

    monkeypatch.setattr(check.Detector, "open", fail)
    monkeypatch.setattr(check.StreamReader, "frames", unexpected)
    output = tmp_path / "new"
    summary = check.run("mechdog-01", "127.0.0.1", 1, output)
    assert summary["observed_results"] == 0
    assert summary["stream"]["connects"] == 0
    assert summary["threads_still_alive"] == []
    assert "model initialization failed" in summary["error"]
    assert json.loads((output / "summary.json").read_text(encoding="utf-8")) == summary
