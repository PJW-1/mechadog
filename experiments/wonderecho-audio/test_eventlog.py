"""일자별 이벤트 저널과 일일 리포트 검증 (WBS 4.7.12)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import daily_report
import eventlog
import robotlink


class JournalTest(unittest.TestCase):
    def test_record_appends_one_jsonl_line(self):
        with tempfile.TemporaryDirectory() as d:
            journal = eventlog.EventJournal(d)
            journal.record("user", "메카독 배터리 어때")
            journal.record("robot", "현재 상태입니다")
            journal.close()
            files = list(Path(d).glob("voice-*.jsonl"))
            self.assertEqual(len(files), 1)
            events = eventlog.load_events(files[0])
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]["role"], "user")
        self.assertEqual(events[0]["text"], "메카독 배터리 어때")
        self.assertIn("ts", events[0])

    def test_korean_text_survives_round_trip(self):
        with tempfile.TemporaryDirectory() as d:
            journal = eventlog.EventJournal(d)
            journal.record("system", "경비모드: 김민수 확인됨")
            journal.close()
            path = list(Path(d).glob("voice-*.jsonl"))[0]
            raw = path.read_text(encoding="utf-8")
            self.assertIn("김민수", raw)
            self.assertEqual(eventlog.load_events(path)[0]["text"], "경비모드: 김민수 확인됨")

    def test_load_skips_malformed_lines(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "voice-2026-09-15.jsonl"
            path.write_text(
                '{"ts":"09:00:00","role":"user","text":"정상"}\n'
                '{"ts":"09:00:01","role":'  # 크래시로 반쪽 난 줄
                "not json at all\n"
                '{"ts":"09:00:02","role":"robot","text":"응답"}\n',
                encoding="utf-8",
            )
            events = eventlog.load_events(path)
        self.assertEqual(len(events), 2)
        self.assertEqual(events[1]["text"], "응답")

    def test_load_missing_file_returns_empty(self):
        self.assertEqual(eventlog.load_events("no/such/file.jsonl"), [])

    def test_hub_forwards_events_to_journal(self):
        import voice_pipeline

        with tempfile.TemporaryDirectory() as d:
            journal = eventlog.EventJournal(d)
            hub = voice_pipeline.Hub("mechadog-01", journal=journal)
            hub.event("user", "안녕")
            journal.close()
            events = eventlog.load_events(list(Path(d).glob("voice-*.jsonl"))[0])
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["text"], "안녕")

    def test_hub_without_journal_still_works(self):
        import voice_pipeline

        hub = voice_pipeline.Hub("mechadog-01")
        hub.event("user", "안녕")
        self.assertEqual(len(hub.events), 1)
        self.assertIsNone(hub.report_today())

    def test_poll_robot_events_merges_into_journal(self):
        """로봇 측 사건이 음성 저널에 robot_evt 역할로 합쳐진다 (4.7.12)."""
        import voice_pipeline

        with tempfile.TemporaryDirectory() as d:
            journal = eventlog.EventJournal(d)
            hub = voice_pipeline.Hub("mechadog-01", journal=journal)
            payload = (
                [{"event": "person_found", "state": "PATROL", "escalation": "L1", "seq": 7}],
                0,
                7,
            )
            with mock.patch.object(robotlink, "fetch_events", return_value=payload):
                hub.poll_robot_events("http://x", interval=0)
            journal.close()
            events = eventlog.load_events(list(Path(d).glob("voice-*.jsonl"))[0])
        self.assertEqual(events[0]["role"], "robot_evt")
        self.assertIn("person_found", events[0]["text"])
        self.assertIn("state=PATROL", events[0]["text"])
        self.assertEqual(hub.robot_cursor, 7)

    def test_poll_robot_events_announces_gap(self):
        """버퍼에서 밀린 사건은 건수를 남긴다 — 조용한 공백은 거짓 안심이다."""
        import voice_pipeline

        hub = voice_pipeline.Hub("mechadog-01")
        with mock.patch.object(robotlink, "fetch_events", return_value=([], 3, 10)):
            hub.poll_robot_events("http://x", interval=0)
        self.assertEqual(hub.events[0]["text"], "event_gap (dropped=3)")
        self.assertEqual(hub.robot_cursor, 10)

    def test_poll_robot_events_survives_dashboard_outage(self):
        """관제가 꺼져 있어도 음성 루프는 산다 — 실패는 조용히 넘긴다."""
        import voice_pipeline

        hub = voice_pipeline.Hub("mechadog-01")
        with mock.patch.object(robotlink, "fetch_events", return_value=None):
            hub.poll_robot_events("http://x", interval=0)
        self.assertEqual(len(hub.events), 0)
        self.assertEqual(hub.robot_cursor, 0)

    def test_poll_robot_events_respects_interval(self):
        import voice_pipeline

        hub = voice_pipeline.Hub("mechadog-01")
        with mock.patch.object(robotlink, "fetch_events", return_value=([], 0, 5)) as fetch:
            hub.poll_robot_events("http://x", interval=60)
            hub.poll_robot_events("http://x", interval=60)  # 간격 안 — 안 부른다
        self.assertEqual(fetch.call_count, 1)


class ReportTest(unittest.TestCase):
    def _events(self):
        return [
            {"ts": "09:00:00", "role": "user", "text": "메카독 배터리 어때"},
            {"ts": "09:00:01", "role": "robot", "text": "현재 상태입니다"},
            {"ts": "09:10:00", "role": "system", "text": "시나리오 실행: fire_evac"},
            {"ts": "09:10:05", "role": "system", "text": "시나리오 실행: fire_evac"},
            {"ts": "09:11:00", "role": "system", "text": "시나리오 실패: guard_mode: timeout"},
            {"ts": "09:20:00", "role": "system", "text": "명령 estop: 성공"},
            {"ts": "09:20:30", "role": "system", "text": "명령 patrol_start: 로봇 거부"},
            {"ts": "09:30:00", "role": "system", "text": "비상: 도와줘"},
            {"ts": "09:40:00", "role": "system", "text": "안전모 미착용 경고"},
            {"ts": "09:50:00", "role": "admin", "text": "전체 공지입니다"},
        ]

    def test_summarize_aggregates(self):
        s = daily_report.summarize(self._events(), date="2026-09-15")
        self.assertEqual(s["total"], 10)
        self.assertEqual(s["conversations"], 1)
        self.assertEqual(s["scenario_runs"], {"fire_evac": 2})
        self.assertEqual(s["scenario_failures"][0]["scenario"], "guard_mode")
        self.assertEqual(s["commands"], {"estop": 1, "patrol_start": 1})
        self.assertEqual(len(s["emergencies"]), 1)
        self.assertEqual(len(s["warnings"]), 1)
        self.assertEqual(s["first_ts"], "09:00:00")
        self.assertEqual(s["last_ts"], "09:50:00")

    def test_empty_day_is_not_an_error(self):
        s = daily_report.summarize([], date="2026-09-15")
        self.assertEqual(s["total"], 0)
        md = daily_report.render_markdown(s)
        self.assertIn("기록된 이벤트가 없습니다", md)

    def test_render_markdown_sections(self):
        md = daily_report.render_markdown(daily_report.summarize(self._events(), "2026-09-15"))
        self.assertIn("# 음성·순찰 일일 리포트 — 2026-09-15", md)
        self.assertIn("fire_evac: 2회", md)
        self.assertIn("비상 접수", md)
        self.assertIn("도와줘", md)
        self.assertIn("안전모 미착용 경고", md)

    def test_robot_events_aggregated(self):
        evts = self._events() + [
            {"ts": "10:00:00", "role": "robot_evt", "text": "person_found (state=PATROL 단계=L1)"},
            {"ts": "10:05:00", "role": "robot_evt", "text": "person_found (state=PATROL 단계=L2)"},
            {"ts": "10:10:00", "role": "robot_evt", "text": "event_gap (dropped=2)"},
        ]
        s = daily_report.summarize(evts, date="2026-09-15")
        self.assertEqual(s["robot_events"], {"person_found": 2, "event_gap": 1})
        md = daily_report.render_markdown(s)
        self.assertIn("로봇 사건", md)
        self.assertIn("person_found: 2건", md)

    def test_main_writes_report_file(self):
        with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as out:
            Path(d, "voice-2026-09-15.jsonl").write_text(
                json.dumps({"ts": "09:00:00", "role": "user", "text": "안녕"}, ensure_ascii=False)
                + "\n",
                encoding="utf-8",
            )
            rc = daily_report.main(["--date", "2026-09-15", "--dir", d, "--out", out])
            self.assertEqual(rc, 0)
            report = Path(out, "voice-report-2026-09-15.md")
            self.assertTrue(report.exists())
            self.assertIn("2026-09-15", report.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
