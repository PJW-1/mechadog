"""실기 측정 하네스의 판정·기록 검증 (WBS 2.2.3 · 3.2.1 · 3.3.3 측정 경로).

**로봇 없이 검증한다.** 이 도구가 실기를 구동하긴 하지만, 틀리면 아픈 곳은
구동이 아니라 **판정과 기록**이다 — 계산이 틀려도 표는 그럴듯하게 찍히고,
그 숫자가 `config/devices/*.yaml` 에 캘리브레이션으로 들어간다. 그러면 로봇이
비뚤게 걷고, 원인을 펌웨어에서 찾게 된다.

그래서 여기서는 UDP 를 쓰지 않는다. 표본을 직접 넣고 판정만 본다 —
루프백 UDP 에 판정을 얹으면 시험이 러너 부하에 흔들린다(`test_motion_probe`).
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from host.telemetry.receiver import Reading
from tools.field_measure import Result, Sample, TelemetryTap, save


def reading(*, pitch: float | None = 0.0, roll: float | None = 0.0) -> Reading:
    """IMU 만 의미 있는 최소 레코드. 나머지는 판정에 쓰이지 않는다."""
    return Reading(
        device_id="mechdog-01",
        boot_id="b1",
        seq=1,
        state="IDLE",
        batt_v=8.0,
        dist_cm=None,
        tipped=False,
        lowbatt=False,
        link_ok=True,
        pitch=pitch,
        roll=roll,
        yaw=0.0,
    )


@pytest.fixture
def tap() -> TelemetryTap:
    """포트 0 = 임시 포트. 소켓은 열되 **패킷은 주고받지 않는다.**"""
    made = TelemetryTap(port=0, device_id="mechdog-01")
    yield made
    made.close()


def feed(tap: TelemetryTap, pairs: list[tuple[float | None, float | None]]) -> None:
    """표본을 창 안쪽 시각으로 직접 넣는다."""
    now = time.perf_counter()
    for index, (pitch, roll) in enumerate(pairs):
        tap.samples.append(Sample(now - 0.001 * index, reading(pitch=pitch, roll=roll)))


# ── 정지 판정 (`motion`) ──────────────────────────────────────


def test_too_few_samples_answer_unknown_not_still(tap: TelemetryTap) -> None:
    """⚠️ **모르는 것을 '멈춤' 으로 읽으면 안 된다.**

    1-7(명령 두절 → 다리 정지)은 "멈췄다" 를 보고 통과를 준다. 표본이 없을 때
    `False`(안 움직임)를 돌려주면 **텔레메트리가 끊긴 것을 정지로 읽고 가짜
    통과**를 만든다. 로봇은 계속 걷고 있을 수도 있다.
    """
    feed(tap, [(0.0, 0.0), (0.1, 0.1)])
    assert tap.motion() is None


def test_standing_noise_is_not_motion(tap: TelemetryTap) -> None:
    """선 상태의 IMU 노이즈(~0.1°)는 임계값 0.4° 아래다."""
    feed(tap, [(0.00, 0.00), (0.05, -0.04), (-0.05, 0.03), (0.02, 0.01)])
    assert tap.motion() is False


def test_trot_shake_is_motion(tap: TelemetryTap) -> None:
    """트롯은 수 °씩 흔들린다 — 임계값 위로 확실히 벌어진다."""
    feed(tap, [(0.0, 0.0), (3.2, 0.1), (-2.8, -0.2), (2.5, 0.1)])
    assert tap.motion() is True


def test_roll_only_shake_is_motion(tap: TelemetryTap) -> None:
    """**pitch 가 잠잠해도 roll 이 흔들리면 움직이는 것이다.**

    판정이 두 축의 `max` 인 이유다. 한 축만 보면 옆으로 흔들리는 보행을
    '멈춤' 으로 읽는다.
    """
    feed(tap, [(0.0, 0.0), (0.0, 3.1), (0.0, -2.9), (0.0, 2.4)])
    assert tap.motion() is True


def test_missing_imu_counts_as_unknown(tap: TelemetryTap) -> None:
    """IMU 가 빠진 구형 펌웨어는 판정 대상이 아니다 — `None` 이지 `False` 가 아니다."""
    feed(tap, [(None, 0.0), (None, 3.0), (None, -3.0), (None, 2.0)])
    assert tap.motion() is None


# ── 시간 창 (`window`) ────────────────────────────────────────


def test_window_drops_samples_older_than_the_cut(tap: TelemetryTap) -> None:
    """창 밖 표본이 섞이면 방금 멈춘 로봇이 계속 '움직임' 으로 읽힌다."""
    now = time.perf_counter()
    tap.samples.append(Sample(now - 5.0, reading(pitch=9.0)))
    tap.samples.append(Sample(now - 0.01, reading(pitch=0.0)))
    assert len(tap.window(0.4)) == 1
    assert len(tap.window(10.0)) == 2


# ── 기록 (`save`) ─────────────────────────────────────────────


def results() -> list[Result]:
    return [
        Result("링크 기저선", "-", "pass", "10.0Hz", {"hz": 10.0}),
        Result("명령 두절 정지", "3.2.1", "fail", "2100ms", {"stop_ms": 2100}),
        Result("전진 이동량", "2.2.3", "measured", "104.0 mm/s", {"mm_s": 104.0}),
        Result("텔레옵 방향", "2-4", "skipped", "텔레메트리 없음"),
    ]


def test_json_keeps_every_result_and_its_data(tmp_path: Path) -> None:
    """**요약(md)은 사람이 보고 원본(json)은 나중에 다시 계산한다.**

    데이터가 빠지면 그 측정은 다시 해야 한다 — 실기를 한 번 더 세운다는 뜻이다.
    """
    path = save(results(), tmp_path, "mechdog-01")
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert payload["tool"] == "field_measure"
    assert payload["device"] == "mechdog-01"
    assert [r["wbs"] for r in payload["results"]] == ["-", "3.2.1", "2.2.3", "2-4"]
    assert payload["results"][2]["data"] == {"mm_s": 104.0}
    assert payload["results"][3]["data"] == {}


def test_markdown_marks_each_verdict_distinctly(tmp_path: Path) -> None:
    """⚠️ **실패와 건너뜀이 같은 기호면 안 된다.**

    건너뛴 항목은 '아직 안 쟀다' 이고 실패는 '재 봤더니 기준 미달' 이다. 표에서
    둘이 섞이면 WBS 완료 판정이 틀어진다.
    """
    path = save(results(), tmp_path, "mechdog-01")
    table = path.with_suffix(".md").read_text(encoding="utf-8")

    assert "✅ pass" in table
    assert "❌ fail" in table
    assert "📏 measured" in table
    assert "⏭️ skipped" in table
    # 네 항목 + 헤더 2줄
    assert len([line for line in table.splitlines() if line.startswith("|")]) == 6


def test_unknown_verdict_is_flagged_not_dropped(tmp_path: Path) -> None:
    """모르는 판정값은 조용히 사라지지 말고 `?` 로 남아야 눈에 띈다."""
    path = save([Result("오타난 항목", "9.9", "passed", "")], tmp_path, "mechdog-01")
    assert "? passed" in path.with_suffix(".md").read_text(encoding="utf-8")


def test_save_creates_the_output_directory(tmp_path: Path) -> None:
    """`--out` 에 없는 경로를 줘도 측정 결과를 잃지 않는다."""
    target = tmp_path / "새폴더" / "안쪽"
    path = save(results(), target, "mechdog-02")
    assert path.parent == target
    assert path.exists() and path.with_suffix(".md").exists()


# ── 사람에게 묻는 입력 ────────────────────────────────────────


def test_blank_answer_is_not_yes(monkeypatch: pytest.MonkeyPatch) -> None:
    """⚠️ **엔터만 친 것을 'y' 로 읽으면 안 된다.**

    자동 판정이 아닌 항목(8번 텔레옵)은 사람이 눈으로 보고 답한다. 무심코 누른
    엔터가 통과가 되면 **아무도 안 본 항목이 통과로 기록된다.**
    """
    from tools import field_measure

    monkeypatch.setattr(field_measure, "_ask", lambda _p: "")
    assert field_measure._ask_yn("앞으로 갔나") is False


@pytest.mark.parametrize(("answer", "expected"), [("y", True), ("ㅛ", True), ("n", False)])
def test_yes_no_accepts_the_hangul_key(
    monkeypatch: pytest.MonkeyPatch, answer: str, expected: bool
) -> None:
    """한글 자판에서 `y` 를 누르면 `ㅛ` 가 들어온다 — 실기 중에 자주 난다."""
    from tools import field_measure

    monkeypatch.setattr(field_measure, "_ask", lambda _p: answer)
    assert field_measure._ask_yn("갔나") is expected


def test_number_rejects_negative_and_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    """줄자 값은 음수가 될 수 없다. 오타를 그대로 받으면 캘리브레이션이 뒤집힌다."""
    from tools import field_measure

    answers = iter(["-5", "여든", "820"])
    monkeypatch.setattr(field_measure, "_ask", lambda _p: next(answers))
    assert field_measure._ask_number("이동 거리 mm: ") == 820.0


def test_blank_number_means_not_measured(monkeypatch: pytest.MonkeyPatch) -> None:
    """빈 입력은 0 이 아니라 '안 쟀다' 다 — 0 으로 읽으면 못 걷는 로봇이 된다."""
    from tools import field_measure

    monkeypatch.setattr(field_measure, "_ask", lambda _p: "")
    assert field_measure._ask_number("이동 거리 mm: ") is None


# ── 0 은 빈 값이 아니라 관측값이다 ────────────────────────────


class _FakeTap:
    """`m_turn` 이 보는 최소 표면 — yaw 가 변하지 않는(= 안 도는) 기체."""

    def __init__(self) -> None:
        self.latest = reading()  # yaw=0.0 고정

    def wait_flag(self, _pred: object, **_kw: object) -> tuple[bool, float]:
        # `_require_telemetry` 가 `timeout_s=` 를 키워드로 넘긴다 — 이름을 지켜야 한다.
        return True, 0.0


def _ctx(tap: object = None):
    from tools.field_measure import Ctx

    return Ctx(
        sock=None, peer=("127.0.0.1", 5001), commander=None, tap=tap, device="d", host="127.0.0.1"
    )


def _stub_a_clean_run(monkeypatch: pytest.MonkeyPatch, measured: float) -> None:
    """3회 모두 성공하고 줄자/각도 입력이 `measured` 인 시행을 흉내낸다."""
    from tools import field_measure as fm
    from tools.gait_calibrate import Acks

    monkeypatch.setattr(fm, "_ask", lambda _p: "")  # 엔터 = 실행
    monkeypatch.setattr(fm, "_ask_number", lambda _p, **_k: measured)
    monkeypatch.setattr(fm, "drive_window", lambda *_a, **_k: (3.0, 30, Acks(total=30, applied=30)))
    monkeypatch.setattr(fm.time, "sleep", lambda _s: None)


def test_zero_distance_is_recorded_not_blanked(monkeypatch: pytest.MonkeyPatch) -> None:
    """⚠️ **안 움직이는 기체가 측정의 가장 중요한 결과다.**

    `if mean` 은 `0.0` 을 빈 값으로 읽는다. 그러면 판정은 `measured` 인데 요약은
    "유효 시행 없음" 이고 값은 `None` 이 되어 **세 기록이 서로 모순된다.** 사람은
    도구가 고장난 줄 알고 로봇을 다시 세운다.
    """
    from tools.field_measure import m_drive

    _stub_a_clean_run(monkeypatch, 0.0)
    result = m_drive(_ctx(), "forward")

    assert result.verdict == "measured"
    assert result.data["mm_per_s"] == 0.0
    assert "0.0 mm/s" in result.summary
    assert "유효 시행 없음" not in result.summary


def test_zero_turn_rate_is_recorded_not_blanked(monkeypatch: pytest.MonkeyPatch) -> None:
    """선회도 같다 — yaw 가 안 변한 것은 '못 쟀다' 가 아니라 '안 돌았다' 다."""
    from tools.field_measure import m_turn

    _stub_a_clean_run(monkeypatch, 0.0)
    result = m_turn(_ctx(_FakeTap()), "turn_left")

    assert result.verdict == "measured"
    assert result.data["deg_per_s"] == 0.0
    assert "+0.0 °/s" in result.summary


def test_real_distance_still_reports_mean_and_spread(monkeypatch: pytest.MonkeyPatch) -> None:
    """0 을 살리면서 정상 경로가 상하지 않았는지 — 312mm/3s = 104.0 mm/s."""
    from tools.field_measure import m_drive

    _stub_a_clean_run(monkeypatch, 312.0)
    result = m_drive(_ctx(), "forward")

    assert result.data["mm_per_s"] == 104.0
    assert "104.0 mm/s (n=3)" in result.summary
    assert "퍼짐 0%" in result.summary, "세 시행이 같으면 퍼짐은 0 이다"


def test_no_trials_still_says_nothing_was_measured(monkeypatch: pytest.MonkeyPatch) -> None:
    """**진짜로 못 잰 경우는 그대로 '유효 시행 없음' 이어야 한다.**

    0 을 살리느라 빈 결과까지 값으로 만들면 반대쪽으로 거짓말한다.
    """
    from tools import field_measure as fm

    _stub_a_clean_run(monkeypatch, 0.0)
    monkeypatch.setattr(fm, "_ask", lambda _p: "s")  # 3회 모두 건너뜀
    result = fm.m_drive(_ctx(), "forward")

    assert result.verdict == "fail"
    assert result.data["mm_per_s"] is None
    assert result.summary == "유효 시행 없음"


# ── 선회 명령 부호 (`TURN_COMMANDS`) ─────────────────────────


def test_turn_commands_match_gait_calibrate_signs() -> None:
    """⚠️ **같은 모드명은 같은 방향을 보내야 한다.**

    2026-09-21 에 `field_measure` 의 `reverse_turn` 이 -20(후진+우선회)을 보내고
    있었다 — `gait_calibrate` 와 회피 시퀀스는 +20(후진+좌선회)다. 두 도구가
    갈라지면 회피가 쓰는 구동의 **반대 방향**을 재서 설정에 적게 된다.
    """
    from tools.field_measure import TURN_COMMANDS
    from tools.gait_calibrate import command_for

    gait = {"step_length_mm": 60.0, "turn_angle_deg": 20.0}
    for mode in ("turn_left", "turn_right", "reverse_turn"):
        assert TURN_COMMANDS[mode] == command_for(mode, gait), mode


def test_reverse_turn_is_the_direction_the_avoid_sequence_sends() -> None:
    """회피는 `Phase("reverse_turn", -step, +turn_deg)` — 후진+좌선회다 (ADR-29)."""
    from tools.field_measure import TURN_COMMANDS

    step, angle = TURN_COMMANDS["reverse_turn"]
    assert step < 0, "후진이어야 한다"
    assert angle > 0, "angle 양수 = 반시계 = 좌선회 (PROTOCOL 부호 규약)"
