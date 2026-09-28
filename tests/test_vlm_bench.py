"""VLM 벤치 도구 검증 (WBS 3.6.4 · `tools/vlm_bench.py`).

**모델도 GPU 도 카메라도 없이 닫힌다.** 사진은 임시 폴더에 쓴 작은 가짜 JPEG 바이트이고,
판독 세션은 그 바이트에 적어 둔 답을 돌려주는 대역이다 — 실제 사진은 쓰지 않는다.

    ① 폴더 구조가 틀리면 틀린 곳을 모두 말하고 멈춘다
    ② 혼동 행렬과 비율이 맞다 — 판독 불가는 «예» 가 아니되 따로 센다
    ③ 운용의 판독기로 그 폴더의 질문 하나만 묻는다
    ④ 장면은 번호 순으로 이웃한 N 프레임이 모두 «예» 여야 «예» 다
    ⑤ 캡처 파일 이름이 이어지는 번호이고, 측정이 같은 장면으로 읽는다
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from host.vision.vlm_reader import QUESTIONS
from tools import vlm_bench as bench

JPEG_HEAD = b"\xff\xd8\xff\xe0"
JPEG_TAIL = b"\xff\xd9"
PROMPT = {question.key: question.prompt for question in QUESTIONS}


def _jpeg(answer: str) -> bytes:
    """가짜 JPEG. 대역 세션이 돌려줄 답을 가운데 적어 둔다."""
    return JPEG_HEAD + answer.encode("utf-8") + JPEG_TAIL


class FakeVlmSession:
    """판독 세션 대역. **모델을 흉내 내지 않고** 사진 바이트에 적힌 답만 돌려준다."""

    def __init__(self) -> None:
        self.asked: list[str] = []
        self.closed = 0

    def ask(self, image: object, prompt: str) -> str:
        assert isinstance(image, bytes), "운용처럼 JPEG 바이트가 그대로 넘어와야 한다"
        self.asked.append(prompt)
        answer = image[len(JPEG_HEAD) : -len(JPEG_TAIL)].decode("utf-8")
        if answer == "boom":
            raise RuntimeError("추론 실패")
        return answer

    def close(self) -> None:
        self.closed += 1


def _tree(root: Path, files: dict[str, str]) -> Path:
    for rel, answer in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(_jpeg(answer))
    return root


def _run(root: Path, repeat: int = 1) -> tuple[FakeVlmSession, list[dict], dict]:
    session = FakeVlmSession()
    records = bench.measure(bench.collect(root), session, budget_ms=3000)
    return session, records, bench.summarize(records, repeat)


# ── ① 폴더 구조 ─────────────────────────────────────────────


def test_collect_reads_question_label_scene_and_frame(tmp_path: Path) -> None:
    _tree(
        tmp_path,
        {
            "fallen_object/yes/소화기_01.jpg": "yes",
            "fallen_object/yes/소화기_02.jpg": "yes",
            "fallen_object/no/빈복도.jpg": "no",
            "blocked_path/yes/상자_10.JPG": "yes",
        },
    )
    got = {(s.key, s.label, s.file, s.scene, s.frame) for s in bench.collect(tmp_path)}
    assert got == {
        ("fallen_object", "yes", "fallen_object/yes/소화기_01.jpg", "소화기", 1),
        ("fallen_object", "yes", "fallen_object/yes/소화기_02.jpg", "소화기", 2),
        ("fallen_object", "no", "fallen_object/no/빈복도.jpg", "빈복도", 0),
        ("blocked_path", "yes", "blocked_path/yes/상자_10.JPG", "상자", 10),
    }


def test_collect_names_every_problem_at_once(tmp_path: Path) -> None:
    """⚠️ 하나씩 말하면 사진을 다시 정리할 때마다 한 번씩 돌려야 한다."""
    _tree(
        tmp_path,
        {
            "fallen/yes/a_01.jpg": "yes",
            "fallen_object/maybe/a_01.jpg": "yes",
            "fallen_object/yes/a_01.png": "yes",
            "fallen_object/yes/a_02.jpg": "yes",
            "fallen_object/yes/sub/a_03.jpg": "yes",
        },
    )
    (tmp_path / "notes.txt").write_text("메모", encoding="utf-8")
    (tmp_path / "blocked_path").mkdir()

    with pytest.raises(bench.LayoutError) as caught:
        bench.collect(tmp_path)
    problems = "\n".join(caught.value.problems)
    for expected in (
        "fallen: 질문키 폴더가 아니다",
        "notes.txt: 질문키 폴더가 아니다",
        "fallen_object/maybe: yes 나 no 폴더가 아니다",
        "fallen_object/yes/a_01.png: .jpg 사진이 아니다",
        "fallen_object/yes/sub: .jpg 사진이 아니다",
        "blocked_path: 사진이 없다",
    ):
        assert expected in problems
    assert "a_02" not in problems, "맞는 사진까지 틀렸다고 하면 안 된다"


@pytest.mark.parametrize("make", ["missing", "empty"])
def test_collect_refuses_a_missing_or_empty_root(tmp_path: Path, make: str) -> None:
    root = tmp_path / "root"
    if make == "empty":
        root.mkdir()
    with pytest.raises(bench.LayoutError):
        bench.collect(root)


# ── ② 채점 ─────────────────────────────────────────────────


def test_score_counts_the_confusion_matrix_and_rates() -> None:
    pairs = [(True, True)] * 3 + [(True, False)] + [(False, True)] * 2 + [(False, False)] * 6
    assert bench.score(pairs) == {
        "tp": 3,
        "fn": 1,
        "fp": 2,
        "tn": 6,
        "recall": 0.75,
        "false_alarm": 0.25,
    }


def test_score_has_no_rate_without_a_denominator() -> None:
    """정상 사진이 없는데 오경보율 0% 라고 하면 잰 적 없는 것을 잰 것처럼 보인다."""
    got = bench.score([(True, True)])
    assert got["recall"] == 1.0
    assert got["false_alarm"] is None


def test_unreadable_is_counted_and_never_raises_the_alarm(tmp_path: Path) -> None:
    """⚠️ 판독 불가는 운용에서 경보가 울리지 않는다 — «예» 로 세면 적중률이 부푼다."""
    _tree(
        tmp_path,
        {
            "fallen_object/yes/a.jpg": "boom",  # 세션 예외 → 기능 저하
            "fallen_object/yes/b.jpg": "maybe",  # 답은 왔으나 못 읽음
            "fallen_object/yes/c.jpg": "Yes, a chair fell over.",
            "fallen_object/no/d.jpg": "I think so",
            "fallen_object/no/e.jpg": "no",
        },
    )
    _, records, summary = _run(tmp_path)

    by_file = {Path(r["file"]).name: r for r in records}
    assert by_file["a.jpg"]["degraded"] is True
    assert by_file["a.jpg"]["reason"] == "ask_failed"
    assert by_file["a.jpg"]["value"] is None
    assert by_file["a.jpg"]["latency_ms"] is None
    assert by_file["b.jpg"]["degraded"] is False, "파싱 실패는 기능 저하가 아니다"
    assert by_file["b.jpg"]["value"] is None
    assert by_file["b.jpg"]["raw"] == "maybe", "원문을 남겨야 왜 못 읽었는지 본다"

    entry = summary["questions"]["fallen_object"]
    assert entry["unreadable"] == {"yes": 2, "no": 1}
    frames = entry["frames"]
    assert (frames["tp"], frames["fn"], frames["fp"], frames["tn"]) == (1, 2, 0, 2)
    assert frames["recall"] == pytest.approx(1 / 3)
    assert frames["false_alarm"] == 0.0


def test_latency_stats_leave_out_the_warmup() -> None:
    """⚠️ 첫 질문(약 0.89초)을 넣으면 적은 표본의 p95·최대가 워밍업 하나로 정해진다."""
    records = [
        {
            "key": "fallen_object",
            "label": "no",
            "scene": f"s{i}",
            "frame": 0,
            "value": False,
            "latency_ms": ms,
            "warmup": i == 0,
        }
        for i, ms in enumerate([890, 200, 240, 210, 220])
    ]
    summary = bench.summarize(records, repeat=1)
    assert summary["warmup_ms"] == 890
    assert summary["questions"]["fallen_object"]["latency_ms"] == {
        "n": 4,
        "p50": 210,
        "p95": 240,
        "max": 240,
    }


# ── ③ 운용의 판독기로 묻는다 ─────────────────────────────────


def test_measure_asks_only_the_folder_question_through_the_runtime_reader(
    tmp_path: Path,
) -> None:
    _tree(
        tmp_path,
        {
            "fallen_object/yes/a.jpg": "yes",
            "fallen_object/no/b.jpg": "No.",
            "blocked_path/yes/c.jpg": "yes",
        },
    )
    session, records, _ = _run(tmp_path)
    expected = [PROMPT["blocked_path"], PROMPT["fallen_object"], PROMPT["fallen_object"]]
    assert session.asked == expected, "그 폴더의 질문 하나만, 런타임 문장 그대로"
    assert [r["warmup"] for r in records] == [True, False, False]
    assert {r["file"]: r["value"] for r in records} == {
        "blocked_path/yes/c.jpg": True,
        "fallen_object/no/b.jpg": False,
        "fallen_object/yes/a.jpg": True,
    }


# ── ④ 장면 묶음 (--repeat) ──────────────────────────────────


@pytest.mark.parametrize(
    ("values", "repeat", "expected"),
    [
        ([True, None, True], 2, False),  # 판독 불가가 연속을 끊는다
        ([False, True, True], 2, True),
        ([True, False, True], 2, False),
        ([True], 2, False),  # 프레임이 모자라면 «예» 가 될 수 없다
        ([True], 1, True),
        ([True, True, False, True], 3, False),
    ],
)
def test_scene_needs_n_neighbouring_yes_frames(
    values: list[bool | None], repeat: int, expected: bool
) -> None:
    assert bench.hits(values, repeat) is expected


def test_repeat_scores_scenes_in_frame_number_order(tmp_path: Path) -> None:
    _tree(
        tmp_path,
        {
            # 번호 순이면 예·아니오·예 → 연속 없음. 글자 순(1·10·2)이면 예·예로 잘못 맞는다.
            "fallen_object/yes/a_1.jpg": "yes",
            "fallen_object/yes/a_2.jpg": "no",
            "fallen_object/yes/a_10.jpg": "yes",
            "fallen_object/yes/b_01.jpg": "yes",
            "fallen_object/yes/b_02.jpg": "yes",
            # 이름이 같아도 no 폴더의 b 는 다른 장면이다
            "fallen_object/no/b_01.jpg": "yes",
            "fallen_object/no/b_02.jpg": "no",
            "fallen_object/no/d_01.jpg": "yes",
            "fallen_object/no/d_02.jpg": "yes",
            "fallen_object/no/e.jpg": "yes",  # 한 장뿐인 장면
        },
    )
    _, _, summary = _run(tmp_path, repeat=2)
    entry = summary["questions"]["fallen_object"]
    scenes = entry["scenes"]
    assert (scenes["tp"], scenes["fn"], scenes["fp"], scenes["tn"]) == (1, 1, 1, 2)
    assert scenes["short"] == 1
    assert scenes["recall"] == 0.5
    assert scenes["false_alarm"] == pytest.approx(1 / 3)
    # 프레임 채점은 그대로 함께 나온다
    frames = entry["frames"]
    assert (frames["tp"], frames["fn"], frames["fp"], frames["tn"]) == (4, 1, 4, 1)


def test_scenes_are_scored_only_when_repeat_is_asked(tmp_path: Path) -> None:
    _tree(tmp_path, {"fallen_object/yes/a_01.jpg": "yes"})
    _, _, summary = _run(tmp_path, repeat=1)
    assert "scenes" not in summary["questions"]["fallen_object"]


# ── ⑤ 캡처 파일 이름 ────────────────────────────────────────


def test_next_frame_path_continues_the_scene_numbering(tmp_path: Path) -> None:
    folder = tmp_path / "fallen_object" / "yes"
    first = bench.next_frame_path(tmp_path, "fallen_object", "yes", "소화기_눕힘")
    assert first == folder / "소화기_눕힘_01.jpg"

    _tree(
        tmp_path,
        {
            "fallen_object/yes/소화기_눕힘_01.jpg": "yes",
            "fallen_object/yes/소화기_눕힘_07.jpg": "yes",
            "fallen_object/yes/소화기_눕힘_99.png": "yes",  # 사진이 아니면 세지 않는다
            "fallen_object/yes/소화기_20.jpg": "yes",  # 다른 장면
            "fallen_object/no/소화기_눕힘_30.jpg": "no",  # 다른 라벨
        },
    )
    got = bench.next_frame_path(tmp_path, "fallen_object", "yes", "소화기_눕힘")
    assert got == folder / "소화기_눕힘_08.jpg"


def test_captured_names_read_back_as_one_scene(tmp_path: Path) -> None:
    """⚠️ 캡처가 붙인 이름을 측정이 다른 장면으로 읽으면 `--repeat` 채점이 무너진다."""
    for _ in range(3):
        path = bench.next_frame_path(tmp_path, "blocked_path", "no", "복도_3")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(_jpeg("no"))
    samples = bench.collect(tmp_path)
    assert [(s.scene, s.frame) for s in samples] == [("복도_3", 1), ("복도_3", 2), ("복도_3", 3)]


@pytest.mark.parametrize(
    ("key", "label", "scene"),
    [
        ("fallen", "yes", "a"),
        ("fallen_object", "maybe", "a"),
        ("fallen_object", "yes", ""),
        ("fallen_object", "yes", " a"),
        ("fallen_object", "yes", ".."),
        ("fallen_object", "yes", "../a"),
        ("fallen_object", "yes", "a\\b"),
        ("fallen_object", "yes", "a:b"),
    ],
)
def test_next_frame_path_refuses_names_that_leave_the_layout(
    tmp_path: Path, key: str, label: str, scene: str
) -> None:
    with pytest.raises(ValueError):
        bench.next_frame_path(tmp_path, key, label, scene)


# ── 명령줄 ─────────────────────────────────────────────────


def test_measure_cli_prints_tables_and_saves_raw_answers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _tree(
        tmp_path / "photos",
        {
            "fallen_object/yes/a_01.jpg": "yes",
            "fallen_object/yes/a_02.jpg": "yes",
            "fallen_object/no/b_01.jpg": "no",
        },
    )
    session = FakeVlmSession()
    monkeypatch.setattr(bench, "build_session_factory", lambda _config: lambda: session)
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")  # 도구가 세우는 값이 시험 밖으로 새지 않게
    out = tmp_path / "bench.json"

    assert bench.main(["measure", str(root), "--repeat", "2", "--out", str(out)]) == 0

    printed = capsys.readouterr().out
    assert "## 프레임 단위" in printed
    assert "## 장면 단위" in printed
    assert "`fallen_object`" in printed
    assert session.closed == 1, "측정이 끝나면 VRAM 을 놓아야 한다"
    saved = json.loads(out.read_text(encoding="utf-8"))
    assert saved["repeat"] == 2
    assert [r["file"] for r in saved["records"]] == [
        "fallen_object/no/b_01.jpg",
        "fallen_object/yes/a_01.jpg",
        "fallen_object/yes/a_02.jpg",
    ]
    assert all(r["raw"] in {"yes", "no"} for r in saved["records"])
    assert saved["summary"]["questions"]["fallen_object"]["scenes"]["tp"] == 1


def test_measure_cli_stops_on_a_bad_layout_before_loading_the_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def never(_config: object) -> None:
        raise AssertionError("구조가 틀렸는데 모델(14.5초)부터 올렸다")

    monkeypatch.setattr(bench, "build_session_factory", never)
    assert bench.main(["measure", str(tmp_path / "missing")]) == 2
    assert "폴더 구조가 틀렸다" in capsys.readouterr().err


def test_measure_cli_says_which_python_when_vlm_packages_are_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _tree(tmp_path, {"fallen_object/yes/a.jpg": "yes"})
    monkeypatch.setattr(bench, "build_session_factory", lambda _config: None)
    assert bench.main(["measure", str(root)]) == 1
    assert ".venv-mechdog-vlm" in capsys.readouterr().err


def test_capture_cli_checks_the_path_before_touching_the_camera(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def never(_args: object) -> int:
        raise AssertionError("잘못된 장면 이름인데 카메라부터 열었다")

    monkeypatch.setattr(bench, "capture", never)
    argv = ["capture", "--camera-ip", "192.0.2.1", "--root", str(tmp_path)]
    with pytest.raises(SystemExit) as caught:
        bench.main([*argv, "--question", "fallen_object", "--label", "yes", "--scene", "../a"])
    assert caught.value.code == 2
