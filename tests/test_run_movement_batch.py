"""run_movement_batch 의 케이스 선택과 기록 저장 경로를 검증한다."""

import json
from pathlib import Path

from tools import field_plan as plan
from tools import run_movement_batch as batch


def _cases():
    return plan.catalog()["cases"]


def test_select_cases_id_filter_allows_stationary_items():
    all_cases = _cases()
    picked = batch.select_cases(all_cases, {"HW-004"})
    assert [c["id"] for c in picked] == ["HW-004"]


def test_select_cases_without_ids_keeps_movement_filter():
    all_cases = _cases()
    picked = batch.select_cases(all_cases, set())
    expected = {c["id"] for c in batch.sessions.movement_cases(all_cases)}
    assert {c["id"] for c in picked} == expected
    assert picked


def test_save_case_writes_record_and_marks_summary(tmp_path, monkeypatch):
    monkeypatch.setattr(batch, "OUT", tmp_path)
    monkeypatch.setitem(batch.ENV, "host_revision", "test-rev")
    batch.payload = {"steps": [{"summary": "표본 3회 수집"}]}
    batch.path = tmp_path / "session.json"
    batch.path.write_text(json.dumps({"schema": 1}), encoding="utf-8")
    case = {"id": "HW-006"}

    batch.save_case(case, 0)

    record_path = (
        plan.record_path(tmp_path, batch.DEVICE, "HW-006") if hasattr(plan, "record_path") else None
    )
    saved = [p for p in tmp_path.rglob("*.json") if p.name != "session.json"]
    assert saved, "기록 파일이 만들어지지 않았다"
    record = json.loads(saved[0].read_text(encoding="utf-8"))
    text = json.dumps(record, ensure_ascii=False)
    assert "표본 3회 수집" in text
    assert record_path is None or record_path.exists()
