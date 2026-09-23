"""TF 카드 문장 표 시험 (WBS 4.7.21) — Piper·카드 없이 돈다."""

import io
import shutil
import tempfile
import unittest
import wave
from pathlib import Path

import tf_tracks as tf


def _table_file(tmp, body):
    path = Path(tmp) / "t.tsv"
    path.write_text(body, encoding="utf-8")
    return path


class LookupTests(unittest.TestCase):
    def test_every_spoken_line_has_a_track(self):
        # `python tf_tracks.py --check` 와 같은 판정 — 실패하면 `--add` 뒤 카드를 다시 만든다
        self.assertEqual(tf.missing(tf.load_table()), {})

    def test_every_escalation_warning_has_a_track(self):
        warnings = tf.warning_lines()
        self.assertIn("l3_warning", warnings)
        self.assertIn("person_down_warning", warnings)
        for key, text in warnings.items():
            self.assertIsNotNone(tf.track_for(text), key)

    def test_whitespace_and_unicode_form_are_ignored(self):
        text = "경보가 발령되었습니다. 관리자에게 통보되었습니다."
        n = tf.track_for(text)
        self.assertIsNotNone(n)
        self.assertEqual(tf.track_for(f"  {text.replace(' ', chr(10) + ' ')}\t"), n)
        import unicodedata

        self.assertEqual(tf.track_for(unicodedata.normalize("NFD", text)), n)

    def test_punctuation_and_unknown_lines_do_not_match(self):
        self.assertIsNone(tf.track_for("경보가 발령되었습니다 관리자에게 통보되었습니다"))
        self.assertIsNone(tf.track_for("김민수 님, 확인되었습니다."))
        self.assertIsNone(tf.track_for(""))

    def test_rejects_duplicate_or_out_of_range_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            for body in ("0001\t가\n0001\t나\n", "0001\t가\n0002\t 가\n", "3001\t가\n", "0001가\n"):
                with self.assertRaises(ValueError, msg=body):
                    tf.load_table(_table_file(tmp, body))


class AddTests(unittest.TestCase):
    def test_new_lines_get_next_numbers_and_old_numbers_stay(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _table_file(
                tmp,
                "# 머리\n0007\t옛 문장\n0003\t경보가 발령되었습니다. 관리자에게 통보되었습니다.\n",
            )
            before = tf.load_table(path)
            rows = tf.add_missing(path)
            after = tf.load_table(path)
        self.assertEqual(rows[0][0], 8)  # 빈 번호를 메우지 않고 가장 큰 번호 다음부터
        self.assertEqual({n: after[n] for n in before}, before)
        self.assertEqual(tf.missing(after), {})


class SaySiteTests(unittest.TestCase):
    def test_dynamic_say_sites_are_known(self):
        # f-문자열로 말하는 자리가 늘면 여기서 걸린다 — 표로 덮을 수 있는지 먼저 정한다.
        # 지식 본문 시나리오는 knowledge_lines() 가 덮는다. voice_pipeline.main 의
        # 「인증 결과를 전달하지 못했습니다. {err}」 는 서버 사유가 붙어 덮지 못한다.
        knowledge = {
            "sc_delivery_guide",
            "sc_general_notice",
            "sc_inspection_notice",
            "sc_meal_notice",
            "sc_safety_check",
            "sc_safety_reminder",
            "sc_shift_notice",
            "sc_visitor_guide",
        }
        expected = {f"scenarios.py:{name}" for name in knowledge} | {"voice_pipeline.py:main"}
        self.assertEqual(tf.dynamic_sites(), expected)

    def test_fixed_rewrites_of_name_and_location_lines_are_collected(self):
        lines = tf.collect_lines()
        self.assertIn("말씀하신 위치로 접수했습니다. 담당자가 출발합니다.", lines)
        self.assertIn("인증되지 않은 사람이 확인되었습니다. 암구호를 말씀해 주십시오.", lines)
        self.assertIn("로봇 관제 서버에 연결할 수 없습니다", lines)

    def test_warnings_come_first(self):
        first = list(tf.collect_lines().values())[: len(tf.warning_lines())]
        self.assertTrue(all(src.startswith("config escalation.sound.") for src in first))


class BuildTests(unittest.TestCase):
    TABLE = {1: "경보가 발령되었습니다.", 12: "3번 구역 [공지] 안내"}

    def test_writes_numbered_files_under_mp3(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "card"
            paths = tf.build(
                out, lambda text: text.encode(), self.TABLE, lambda wav, p: p.write_bytes(wav)
            )
            names = sorted(p.name for p in (out / "MP3").iterdir())
            self.assertEqual(paths[0].read_bytes(), "경보가 발령되었습니다.".encode())
        self.assertEqual(names, ["0001경보가발령되었습니다.mp3", "0012번구역공지안내.mp3"])

    def test_refuses_non_empty_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "MP3").mkdir()
            (Path(tmp) / "MP3" / "0017江南Style.mp3").write_bytes(b"")
            with self.assertRaises(ValueError):
                tf.build(Path(tmp), bytes, self.TABLE, lambda *_: None)

    def test_refuses_drive_root(self):
        with self.assertRaises(ValueError):
            tf.build(Path(Path.cwd().anchor), bytes, self.TABLE, lambda *_: None)

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg 없음")
    def test_ffmpeg_encodes_wav_to_mp3(self):
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(22050)
            w.writeframes(b"\x00\x00" * 2205)
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "0001무음.mp3"
            tf.ffmpeg_mp3(buf.getvalue(), out)
            head = out.read_bytes()[:3]
        self.assertTrue(head == b"ID3" or head[0] == 0xFF, head)


if __name__ == "__main__":
    unittest.main()
