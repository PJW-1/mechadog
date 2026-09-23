"""robotlink.play_track 시험 — 로봇 스피커 트랙 재생 경로 (WBS 4.7.21 ⑤). 서버·로봇 불요."""

import io
import unittest
import urllib.error
from unittest import mock

import robotlink


class PlayTrackTests(unittest.TestCase):
    def test_posts_only_the_track_number(self):
        with mock.patch.object(robotlink, "_post", return_value={"accepted": True}) as post:
            ok, _detail = robotlink.play_track(17)
        self.assertTrue(ok)
        post.assert_called_once_with(robotlink.DEFAULT_BASE, "/api/command/sound", {"track": 17})

    def test_refusal_returns_the_runtime_reason(self):
        refused = {"accepted": False, "detail": "트랙 번호 범위 밖: 3001 (0~3000)"}
        with mock.patch.object(robotlink, "_post", return_value=refused):
            ok, detail = robotlink.play_track(3001)
        self.assertFalse(ok)
        self.assertIn("범위", detail)

    def test_bad_request_is_not_reported_as_a_lost_link(self):
        """400 도 `OSError` 계열이다 — «연결할 수 없다» 로 말하면 원인을 잘못 짚는다."""
        error = urllib.error.HTTPError("u", 400, "Bad Request", {}, io.BytesIO(b""))
        with mock.patch.object(robotlink, "_post", side_effect=error):
            ok, detail = robotlink.play_track(True)
        self.assertFalse(ok)
        self.assertIn("400", detail)

    def test_lost_link_does_not_raise(self):
        """말하기가 실패했다고 음성 루프가 멈추면 안 된다."""
        with mock.patch.object(robotlink, "_post", side_effect=OSError("refused")):
            ok, detail = robotlink.play_track(17)
        self.assertFalse(ok)
        self.assertIn("연결할 수 없습니다", detail)

    def test_non_json_reply_does_not_raise(self):
        with mock.patch.object(robotlink, "_post", side_effect=ValueError("Expecting value")):
            ok, _detail = robotlink.play_track(17)
        self.assertFalse(ok)

    def test_is_not_reachable_from_user_speech(self):
        """사람의 발화로는 트랙을 틀 수 없다 — 화이트리스트 명령표에 없다."""
        names = {name for name, _ack in robotlink.ACTIONS.values()}
        self.assertNotIn("sound", names)
        self.assertNotIn("/api/command/sound", robotlink._ENDPOINTS.values())
        with mock.patch.object(robotlink, "_post") as post:
            self.assertFalse(robotlink.run_action("sound")[0])
        post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
