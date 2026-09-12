import json
import tempfile
import unittest
from pathlib import Path

from transcribe_local import claimed_name, validate_capture


class RecognitionGuards(unittest.TestCase):
    def test_explicit_employee_answer(self):
        self.assertEqual(claimed_name("사원 홍길동입니다."), "홍길동")
        self.assertEqual(claimed_name("사원홍길동입니다."), "홍길동")

    def test_ambiguous_and_non_identity_text(self):
        for text in (
            "",
            "하나 둘 셋",
            "사원 홍길동 또는 김철수입니다",
            "홍길동입니다",
            "사원입니다",
            "사원 123입니다",
        ):
            self.assertIsNone(claimed_name(text))

    def test_repeated_same_name_and_conflicting_names(self):
        self.assertEqual(claimed_name("사원 홍길동입니다. 사원 홍길동입니다."), "홍길동")
        self.assertIsNone(claimed_name("사원 홍길동입니다. 사원 김철수입니다."))

    def test_failed_capture_rejected_before_loading_audio(self):
        with tempfile.TemporaryDirectory() as root:
            folder = Path(root)
            (folder / "capture.json").write_text(json.dumps({"success": False, "frames": 250}))
            with self.assertRaisesRegex(ValueError, "successfully completed"):
                validate_capture(folder)

    def test_missing_finish_cannot_pass(self):
        with tempfile.TemporaryDirectory() as root:
            folder = Path(root)
            (folder / "capture.json").write_text(
                json.dumps(
                    {
                        "success": True,
                        "frames": 250,
                        "checksum_errors": 0,
                        "length_errors": 0,
                        "timeouts": 0,
                        "events": [],
                    }
                )
            )
            with self.assertRaisesRegex(ValueError, "successfully completed"):
                validate_capture(folder)


if __name__ == "__main__":
    unittest.main()
