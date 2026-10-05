"""VLM LoRA 도구 검증 (`tools/vlm_lora/` · ADR-43 «재검토» 향후 방향).

**GPU 도 모델도 torch·peft 도 없이 닫힌다.** 사진은 임시 폴더의 가짜 JPEG 바이트이고,
프로세서·토크나이저·판독 세션은 대역이다.

    ① 두 형식(연출 촬영 폴더 · 자동 수집 manifest)을 하나의 목록으로 읽는다
    ② 자동 수집 라벨은 질문키별로 쓸 수 있는 것만 쓴다 — 모르는 것은 «검토» 로 뺀다
    ③ 학습/보류는 날짜·장면 묶음 단위로 나뉘고 같은 묶음이 양쪽에 들어가지 않는다
    ④ 프롬프트는 운용과 같은 문장·채팅 형식이고, 손실은 정답 토큰에만 걸린다
    ⑤ LoRA 는 언어 모델 투영에만 붙고 비전 인코더에는 붙지 않는다
    ⑥ 평가는 운용의 리더·`parse_answer` 로 두 모델을 같은 표에 낸다
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

from host.vision import vlm_reader, vlm_session
from host.vision.frame_collector import FrameCollector
from host.vision.vlm_reader import QUESTIONS, Question
from tools.probe import vlm_bench as bench
from tools.vlm_lora import dataset as ds
from tools.vlm_lora import evaluate as ev
from tools.vlm_lora import merge as mg
from tools.vlm_lora import train as tr

JPEG_HEAD = b"\xff\xd8\xff\xe0"
JPEG_TAIL = b"\xff\xd9"
FALLEN_PROMPT = (
    "Is the walkway blocked by an object that has fallen over or collapsed? "
    "Answer with yes or no only."
)
#: 운용 질문 셋에 `blocked_by_fallen` 을 더한 가짜. 도구는 문장을 복사해 두지 않고
#: 실행할 때 `vlm_reader.QUESTIONS` 에서 읽으므로, 시험은 이것을 주입해 확인한다.
FAKE_QUESTIONS = tuple(q for q in QUESTIONS if q.key != "blocked_by_fallen") + (
    Question(key="blocked_by_fallen", prompt=FALLEN_PROMPT, intent="시험용"),
)


@pytest.fixture(autouse=True)
def _questions(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(vlm_reader, "QUESTIONS", FAKE_QUESTIONS)


def _jpeg(answer: str = "yes") -> bytes:
    return JPEG_HEAD + answer.encode("utf-8") + JPEG_TAIL


def _write(path: Path, data: bytes = b"") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data or _jpeg())
    return path


def _set_date(path: Path, yyyymmdd: str) -> None:
    import datetime as dt

    stamp = dt.datetime.strptime(yyyymmdd + " 12", "%Y%m%d %H").timestamp()
    os.utime(path, (stamp, stamp))


def _auto_day(root: Path, date: str, rows: list[dict]) -> Path:
    day = root / date
    for row in rows:
        _write(day / row["file"])
    day.mkdir(parents=True, exist_ok=True)
    (day / "manifest.jsonl").write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8"
    )
    return day


def _entry(**kw) -> ds.Entry:
    base = {
        "image": "x.jpg",
        "key": "blocked_by_fallen",
        "answer": "no",
        "source": "auto",
        "date": "20261001",
        "group": "auto/20261001",
        "origin": "clear",
    }
    base.update(kw)
    return ds.Entry(**base)


# ── ① 질문 ─────────────────────────────────────────────────


def test_questions_are_read_from_the_runtime_set_at_call_time() -> None:
    """⚠️ 문장이 한 글자라도 다르면 운용과 다른 질문으로 학습한다 — 복사본을 두지 않는다."""
    for question in FAKE_QUESTIONS:
        assert ds.question_for(question.key) is question
    assert ds.question_for("blocked_by_fallen").prompt == FALLEN_PROMPT
    assert ds.question_keys() == tuple(q.key for q in FAKE_QUESTIONS)


def test_a_key_missing_from_the_runtime_set_stops_clearly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    without = tuple(q for q in QUESTIONS if q.key != "blocked_by_fallen")
    monkeypatch.setattr(vlm_reader, "QUESTIONS", without)
    with pytest.raises(
        ds.MissingQuestionError,
        match="질문 키 blocked_by_fallen 이 vlm_reader.QUESTIONS 에 없다",
    ):
        ds.question_for("blocked_by_fallen")
    assert "blocked_by_fallen" not in ds.question_keys()


def test_train_and_evaluate_stop_before_the_gpu_when_the_question_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "list.jsonl"
    image = str(_write(tmp_path / "a.jpg"))
    ds.write_jsonl(
        [_entry(image=image, split="train"), _entry(image=image, group="g2", split="holdout")],
        path,
    )
    monkeypatch.setattr(
        vlm_reader, "QUESTIONS", tuple(q for q in QUESTIONS if q.key != "blocked_by_fallen")
    )
    touched: list = []
    monkeypatch.setattr(tr, "train", lambda *a: touched.append(a))
    monkeypatch.setattr(ev, "build_session_factory", lambda c: touched.append(c))
    assert tr.main([str(path), "--out", str(tmp_path / "adapter")]) == 2
    assert ev.main([str(path), "--base-only"]) == 2
    assert touched == []
    assert capsys.readouterr().err.count("vlm_reader.QUESTIONS 에 없다") == 2


# ── ② 목록 만들기 · 라벨 규칙 ──────────────────────────────


def test_staged_folders_become_entries_grouped_by_scene(tmp_path: Path) -> None:
    root = tmp_path / "staged"
    a = _write(root / "blocked_by_fallen/yes/상자_01.jpg")
    b = _write(root / "blocked_by_fallen/no/상자_02.jpg")
    c = _write(root / "person_down/no/빈복도.jpg")
    for path in (a, b, c):
        _set_date(path, "20261003")
    got = sorted(ds.read_staged(root), key=lambda e: e.image)
    assert [(e.key, e.answer, e.source, e.date, e.group) for e in got] == [
        ("blocked_by_fallen", "no", "staged", "20261003", "staged/상자"),
        ("blocked_by_fallen", "yes", "staged", "20261003", "staged/상자"),
        ("person_down", "no", "staged", "20261003", "staged/빈복도"),
    ]
    assert all(Path(e.image).is_absolute() for e in got)


def test_auto_rules_are_fixed_per_question() -> None:
    """`clear` 는 막힘이 없으니 «무너진 물건으로 막힘» 도 확실히 아니오다.

    `blocked` 는 막힌 것은 맞지만 무너진 물건인지는 모른다 — 사람 확인 전에는 미상이다.
    """
    assert ds.auto_answer("clear", "blocked_by_fallen") == "no"
    assert ds.auto_answer("clear", "blocked_path") == "no"
    assert ds.auto_answer("blocked", "blocked_path") == "yes"
    assert ds.auto_answer("blocked", "blocked_by_fallen") is None
    # LiDAR 라벨로 알 수 없는 질문은 목록에 넣지도 않는다.
    for key in ("person_down", "fallen_object", "hazard_item"):
        assert key not in ds.AUTO_RULES["clear"]
        assert key not in ds.AUTO_RULES["blocked"]


def test_auto_manifest_becomes_entries_with_review_marks(tmp_path: Path) -> None:
    root = tmp_path / "auto"
    _auto_day(
        root,
        "20261004",
        [
            {"file": "blocked/a.jpg", "ts_ms": 1, "label": "blocked", "source": "lidar"},
            {"file": "clear/b.jpg", "ts_ms": 2, "label": "clear", "source": "lidar"},
        ],
    )
    got = {(Path(e.image).name, e.key): e for e in ds.read_auto(root)}
    assert set(got) == {
        ("a.jpg", "blocked_path"),
        ("a.jpg", "blocked_by_fallen"),
        ("b.jpg", "blocked_path"),
        ("b.jpg", "blocked_by_fallen"),
    }
    review = got["a.jpg", "blocked_by_fallen"]
    assert review.answer is None
    assert got["b.jpg", "blocked_by_fallen"].answer == "no"
    assert got["a.jpg", "blocked_path"].answer == "yes"
    assert {e.date for e in got.values()} == {"20261004"}
    assert {e.group for e in got.values()} == {"auto/20261004"}
    assert review.origin == "blocked"


def test_auto_reader_reads_what_the_runtime_collector_writes(tmp_path: Path) -> None:
    """운용 수집기(`FrameCollector`, 4.8.7)가 실제로 쓰는 폴더·manifest 를 그대로 읽는다."""
    import datetime as dt

    root = tmp_path / "auto"
    collector = FrameCollector(root, "mechdog-02", clear_every_ms=0, clear_holdoff_ms=0)
    t0 = int(dt.datetime(2026, 10, 4, 14, 3, 5).timestamp() * 1000) + 123
    assert collector.note_blocked(t0, _jpeg(), (1.234, -0.5), "B", "PATROL")
    assert collector.note_clear(
        t0 + 1000, _jpeg(), state="PATROL", obstacle_active=False, pending=False
    )
    rows = [
        json.loads(line)
        for line in (root / "20261004" / "manifest.jsonl").read_text("utf-8").splitlines()
    ]
    assert rows[0]["file"] == "blocked/140305123_0001.jpg"
    assert {"at_ms", "device_id", "source", "x", "y", "target", "state"} <= set(rows[0])

    got = {(Path(e.image).name, e.key): e for e in ds.read_auto(root)}
    assert {name for name, _ in got} == {"140305123_0001.jpg", "140306123_0002.jpg"}
    assert got["140305123_0001.jpg", "blocked_path"].answer == "yes"
    assert got["140305123_0001.jpg", "blocked_by_fallen"].answer is None
    assert got["140306123_0002.jpg", "blocked_by_fallen"].answer == "no"
    assert {(e.date, e.group, e.source) for e in got.values()} == {
        ("20261004", "auto/20261004", "auto")
    }


def test_auto_manifest_accepts_a_bare_file_name(tmp_path: Path) -> None:
    root = tmp_path / "auto"
    day = root / "20261004"
    _write(day / "clear/b.jpg")
    (day / "manifest.jsonl").write_text(
        json.dumps({"file": "b.jpg", "label": "clear"}) + "\n", encoding="utf-8"
    )
    assert {Path(e.image).name for e in ds.read_auto(root)} == {"b.jpg"}


def test_auto_manifest_names_every_problem(tmp_path: Path) -> None:
    root = tmp_path / "auto"
    day = _auto_day(root, "20261004", [{"file": "clear/ok.jpg", "label": "clear"}])
    _write(day / "blocked/wrong.jpg")
    with (day / "manifest.jsonl").open("a", encoding="utf-8") as out:
        out.write(json.dumps({"file": "clear/missing.jpg", "label": "clear"}) + "\n")
        out.write(json.dumps({"file": "blocked/wrong.jpg", "label": "clear"}) + "\n")
        out.write(json.dumps({"file": "clear/ok.jpg", "label": "maybe"}) + "\n")
        out.write("{깨진 줄\n")
    (root / "notes").mkdir()
    _write(root / "20261005" / "clear" / "x.jpg")  # manifest 없는 날짜
    with pytest.raises(bench.LayoutError) as caught:
        ds.read_auto(root)
    problems = "\n".join(caught.value.problems)
    for expected in (
        "clear/missing.jpg: 사진이 없다",
        "blocked/wrong.jpg: 폴더(blocked)와 라벨(clear)이 다르다",
        "라벨이 blocked·clear 가 아니다",
        "JSON 이 아니다",
        "notes: 날짜(YYYYMMDD) 폴더가 아니다",
        "20261005: manifest.jsonl 이 없다",
    ):
        assert expected in problems


def test_auto_root_must_exist(tmp_path: Path) -> None:
    with pytest.raises(bench.LayoutError):
        ds.read_auto(tmp_path / "none")


def test_jsonl_round_trip_and_hash(tmp_path: Path) -> None:
    entries = [_entry(split="train"), _entry(answer=None, split="review")]
    path = tmp_path / "list.jsonl"
    ds.write_jsonl(entries, path)
    assert ds.read_jsonl(path) == entries
    first = ds.file_sha256(path)
    assert re.fullmatch(r"[0-9a-f]{64}", first)
    ds.write_jsonl(entries[:1], path)
    assert ds.file_sha256(path) != first


# ── ③ 분할 ─────────────────────────────────────────────────


def _mixed() -> list[ds.Entry]:
    return [
        _entry(image="a1", date="20261001", group="auto/20261001"),
        _entry(image="a2", date="20261002", group="auto/20261002", answer="yes"),
        _entry(image="s1", date="20261001", group="staged/상자", source="staged"),
        # 같은 장면이 이틀에 걸쳐 찍혔다 — 묶음째 한쪽으로 가야 한다.
        _entry(image="s2", date="20261002", group="staged/상자", source="staged"),
        _entry(image="s3", date="20261001", group="staged/복도", source="staged"),
        _entry(image="r1", date="20261002", group="auto/20261002", answer=None),
    ]


def test_default_holdout_is_the_latest_date_and_whole_groups_move() -> None:
    got = {e.image: e.split for e in ds.split_entries(_mixed())}
    assert got == {
        "a1": "train",
        "a2": "holdout",
        "s1": "holdout",  # 같은 장면 s2 가 보류 날짜에 있다
        "s2": "holdout",
        "s3": "train",
        "r1": "review",  # 정답 미상은 어느 쪽에도 넣지 않는다
    }


def test_explicit_holdout_group() -> None:
    got = {e.image: e.split for e in ds.split_entries(_mixed(), holdout_groups=["staged/복도"])}
    assert got["s3"] == "holdout"
    assert {got[k] for k in ("a1", "a2", "s1", "s2")} == {"train"}


def test_explicit_holdout_date() -> None:
    got = {e.image: e.split for e in ds.split_entries(_mixed(), holdout_dates=["20261001"])}
    assert got["a1"] == got["s3"] == got["s1"] == got["s2"] == "holdout"
    assert got["a2"] == "train"


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"holdout_dates": ["20991231"]}, "목록에 없는 날짜"),
        ({"holdout_groups": ["staged/없음"]}, "목록에 없는 묶음"),
        ({"holdout_dates": ["20261001", "20261002"]}, "학습 세트가 비었다"),
    ],
)
def test_split_refuses_typos_and_empty_sides(kwargs: dict, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        ds.split_entries(_mixed(), **kwargs)


def test_split_needs_two_dates_unless_told() -> None:
    """다른 날 보류가 기본이다 — 하루치뿐이면 사람이 묶음을 골라야 한다."""
    one_day = [e for e in _mixed() if e.date == "20261001"]
    with pytest.raises(ValueError, match="날짜가 하나뿐"):
        ds.split_entries(one_day)


def test_leak_check_catches_a_group_or_image_on_both_sides() -> None:
    ok = [_entry(image="a", split="train"), _entry(image="b", group="g2", split="holdout")]
    ds.check_no_leak(ok)
    with pytest.raises(ValueError, match="묶음"):
        ds.check_no_leak([_entry(image="a", split="train"), _entry(image="b", split="holdout")])
    with pytest.raises(ValueError, match="사진"):
        ds.check_no_leak(
            [_entry(image="a", split="train"), _entry(image="a", group="g2", split="holdout")]
        )


def test_dataset_cli_writes_one_list(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    auto = tmp_path / "auto"
    _auto_day(auto, "20261003", [{"file": "clear/a.jpg", "label": "clear"}])
    _auto_day(auto, "20261004", [{"file": "blocked/b.jpg", "label": "blocked"}])
    staged = tmp_path / "staged"
    _set_date(_write(staged / "blocked_by_fallen/yes/상자_01.jpg"), "20261003")
    out = tmp_path / "list.jsonl"
    code = ds.main(["--auto", str(auto), "--staged", str(staged), "--out", str(out)])
    assert code == 0
    entries = ds.read_jsonl(out)
    assert {e.split for e in entries} == {"train", "holdout", "review"}
    assert "holdout" in capsys.readouterr().out


def test_dataset_cli_reports_layout_and_split_problems(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "list.jsonl"
    assert ds.main(["--auto", str(tmp_path / "none"), "--out", str(out)]) == 2
    auto = tmp_path / "auto"
    _auto_day(auto, "20261003", [{"file": "clear/a.jpg", "label": "clear"}])
    assert ds.main(["--auto", str(auto), "--out", str(out)]) == 2
    err = capsys.readouterr().err
    assert "폴더가 없다" in err
    assert "날짜가 하나뿐" in err
    assert not out.exists()


def test_dataset_cli_needs_a_source(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        ds.main(["--out", str(tmp_path / "x.jsonl")])


# ── ④ 프롬프트 · 정답 토큰 마스크 ─────────────────────────


def test_training_prompt_uses_the_runtime_chat_format() -> None:
    assert vlm_session.chat_messages("Q?") == [
        {"role": "user", "content": [{"type": "image"}, {"type": "text", "text": "Q?"}]}
    ]


class FakeTokenizer:
    """글자 하나 = 토큰 하나."""

    def __call__(self, text: str, add_special_tokens: bool = True) -> dict:
        assert add_special_tokens is False
        return {"input_ids": [ord(c) for c in text]}


class FakeProcessor:
    def __init__(self) -> None:
        self.tokenizer = FakeTokenizer()
        self.templated: list = []

    def apply_chat_template(self, messages, tokenize: bool, add_generation_prompt: bool) -> str:
        assert tokenize is False
        assert add_generation_prompt is True
        self.templated.append(messages)
        return "<U>" + messages[0]["content"][1]["text"] + "<A>"

    def __call__(self, text, images, return_tensors):
        assert return_tensors == "pt"
        assert len(images) == 1
        return {"input_ids": [[ord(c) for c in text[0]]], "pixel_values": "px"}


def test_loss_is_only_on_the_answer_tokens() -> None:
    assert tr.answer_labels([1, 2, 3, 7, 8], [7, 8]) == [-100, -100, -100, 7, 8]
    with pytest.raises(ValueError, match="정답 토큰"):
        tr.answer_labels([1, 2, 3], [7])
    with pytest.raises(ValueError, match="정답 토큰"):
        tr.answer_labels([7], [7])  # 프롬프트 없이 정답만 있으면 뜻이 없다


def test_build_example_appends_yes_or_no_after_the_runtime_prompt() -> None:
    processor = FakeProcessor()
    entry = _entry(answer="yes")
    inputs, labels = tr.build_example(processor, entry, image="IMG")
    text = "".join(chr(t) for t in inputs["input_ids"][0])
    assert text == f"<U>{FALLEN_PROMPT}<A>Yes"
    assert processor.templated[0] == vlm_session.chat_messages(FALLEN_PROMPT)
    assert labels[-3:] == [ord("Y"), ord("e"), ord("s")]
    assert set(labels[:-3]) == {-100}
    _, labels = tr.build_example(processor, _entry(answer="no"), image="IMG")
    assert [chr(t) for t in labels if t != -100] == ["N", "o"]


def test_build_example_refuses_an_unknown_answer() -> None:
    with pytest.raises(ValueError, match="정답"):
        tr.build_example(FakeProcessor(), _entry(answer=None), image="IMG")


def test_answers_parse_back_through_the_runtime_parser() -> None:
    from host.vision.vlm_reader import parse_answer

    assert parse_answer(tr.ANSWER_TEXT["yes"]) is True
    assert parse_answer(tr.ANSWER_TEXT["no"]) is False


# ── ⑤ LoRA 대상 ────────────────────────────────────────────

LM = "model.language_model.layers.3.self_attn.{}"
VISION = "model.visual.blocks.0.attn.{}"


@pytest.mark.parametrize("mlp", [False, True])
def test_lora_targets_only_language_model_projections(mlp: bool) -> None:
    pattern = re.compile(tr.target_modules(mlp=mlp))
    for name in ("q_proj", "k_proj", "v_proj", "o_proj"):
        assert pattern.fullmatch(LM.format(name))
    for mlp_name in ("gate_proj", "up_proj", "down_proj"):
        hit = pattern.fullmatch(f"model.language_model.layers.3.mlp.{mlp_name}")
        assert bool(hit) is mlp
    for frozen in ("qkv", "proj", "q_proj"):
        assert not pattern.fullmatch(VISION.format(frozen)), "비전 인코더에 LoRA 가 붙었다"
    assert not pattern.fullmatch("model.visual.merger.mlp.0")
    assert not pattern.fullmatch("lm_head")


def test_training_entries_take_only_labelled_train_rows() -> None:
    entries = [
        _entry(image="a", split="train"),
        _entry(image="b", split="holdout"),
        _entry(image="c", split="review", answer=None),
        _entry(image="d", split="train", key="person_down"),
    ]
    assert [e.image for e in tr.training_entries(entries, keys=None)] == ["a", "d"]
    assert [e.image for e in tr.training_entries(entries, keys=["person_down"])] == ["d"]


def test_train_cli_validates_before_touching_the_gpu(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "list.jsonl"
    ds.write_jsonl([_entry(image="a", split="holdout")], path)
    called: list = []
    monkeypatch.setattr(tr, "train", lambda *a: called.append(a))
    out = tmp_path / "adapter"
    assert tr.main([str(path), "--out", str(out)]) == 2
    assert "학습할 사진이 없다" in capsys.readouterr().err
    assert tr.main([str(path), "--out", str(out), "--keys", "nope"]) == 2
    assert "질문 키 nope 이 vlm_reader.QUESTIONS 에 없다" in capsys.readouterr().err

    ds.write_jsonl([_entry(image="a", split="train")], path)
    assert tr.main([str(path), "--out", str(out), "--epochs", "2", "--mlp"]) == 0
    (args, entries, config) = called[0]
    assert args.out == out
    assert [e.image for e in entries] == ["a"]
    assert config["base_model"] == "Qwen/Qwen2-VL-2B-Instruct"
    assert config["dataset_sha256"] == ds.file_sha256(path)
    assert config["lora"]["target_modules"] == tr.target_modules(mlp=True)
    assert config["epochs"] == 2
    assert config["max_pixels"] == 640 * 480
    assert config["examples"] == {"blocked_by_fallen": {"yes": 0, "no": 1}}


def test_train_cli_refuses_a_used_output_folder(tmp_path: Path) -> None:
    path = tmp_path / "list.jsonl"
    ds.write_jsonl([_entry(image="a", split="train")], path)
    out = tmp_path / "adapter"
    _write(out / "old.bin")
    assert tr.main([str(path), "--out", str(out)]) == 2


# ── 병합 ───────────────────────────────────────────────────


def test_merge_record_carries_train_config_and_dataset_hash(tmp_path: Path) -> None:
    adapter = tmp_path / "adapter"
    adapter.mkdir()
    config = {"base_model": "Qwen/Qwen2-VL-2B-Instruct", "dataset_sha256": "ab" * 32}
    (adapter / tr.CONFIG_NAME).write_text(json.dumps(config), encoding="utf-8")
    record = mg.merge_record(adapter)
    assert record["base_model"] == config["base_model"]
    assert record["dataset_sha256"] == config["dataset_sha256"]
    assert record["train_config"] == config
    assert record["dtype"] == "bfloat16"
    with pytest.raises(FileNotFoundError):
        mg.merge_record(tmp_path / "none")


def test_merge_cli_writes_the_record_next_to_the_weights(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = tmp_path / "adapter"
    adapter.mkdir()
    (adapter / tr.CONFIG_NAME).write_text(
        json.dumps({"base_model": "b", "dataset_sha256": "c"}), encoding="utf-8"
    )
    merged: list = []

    def fake_merge(base: str, adapter_dir: Path, out: Path) -> None:
        merged.append((base, adapter_dir))
        out.mkdir(parents=True)

    monkeypatch.setattr(mg, "merge", fake_merge)
    out = tmp_path / "merged"
    assert mg.main([str(adapter), "--out", str(out)]) == 0
    assert merged == [("b", adapter)]
    record = json.loads((out / mg.RECORD_NAME).read_text(encoding="utf-8"))
    assert record["dataset_sha256"] == "c"
    assert mg.main([str(adapter), "--out", str(out)]) == 2, "있는 폴더를 덮지 않는다"
    assert mg.main([str(tmp_path / "none"), "--out", str(tmp_path / "m2")]) == 2


# ── ⑥ 평가 ─────────────────────────────────────────────────


class FakeSession:
    """사진 바이트에 적힌 답을 돌려준다."""

    def __init__(self, model: str) -> None:
        self.model = model
        self.closed = 0

    def ask(self, image: object, prompt: str) -> str:  # noqa: ARG002
        assert isinstance(image, bytes)
        answer = image[len(JPEG_HEAD) : -len(JPEG_TAIL)].decode("utf-8")
        # 기본 모델은 늘 «예» 라고 하고, 병합 모델은 사진에 적힌 답을 한다.
        return "Yes" if self.model == "base" else answer

    def close(self) -> None:
        self.closed += 1


def _holdout(tmp_path: Path) -> list[ds.Entry]:
    rows = []
    for name, answer, group in (
        ("y1", "yes", "g1"),
        ("y2", "yes", "g1"),
        ("n1", "no", "g2"),
        ("u1", "maybe", "g2"),
    ):
        path = _write(tmp_path / f"{name}.jpg", _jpeg(answer))
        label = "no" if answer == "maybe" else answer
        rows.append(_entry(image=str(path), answer=label, group=group, split="holdout"))
    rows.append(_entry(image=str(tmp_path / "t.jpg"), split="train"))
    rows.append(_entry(image=str(tmp_path / "r.jpg"), answer=None, split="review"))
    return rows


def test_holdout_samples_keep_only_labelled_holdout_rows(tmp_path: Path) -> None:
    samples = ev.holdout_samples(_holdout(tmp_path), keys=None)
    assert [s.path.name for s in samples] == ["y1.jpg", "y2.jpg", "n1.jpg", "u1.jpg"]
    assert [(s.scene, s.frame) for s in samples] == [("g1", 0), ("g1", 1), ("g2", 0), ("g2", 1)]
    assert ev.holdout_samples(_holdout(tmp_path), keys=["person_down"]) == []


def test_evaluate_uses_the_runtime_reader_and_parser(tmp_path: Path) -> None:
    samples = ev.holdout_samples(_holdout(tmp_path), keys=None)
    sessions: list[FakeSession] = []
    seen_configs: list = []

    def build(config):
        seen_configs.append(config)
        model = config["vision"]["vlm"]["model_id"]

        def factory() -> FakeSession:
            sessions.append(FakeSession(model))
            return sessions[-1]

        return factory

    vlm = {"model_id": "ignored", "budget_ms": 3000, "max_new_tokens": 8}
    summaries = {
        name: ev.evaluate_model(name, samples, vlm, build_factory=build)[0]
        for name in ("base", "merged")
    }
    assert [c["vision"]["vlm"]["model_id"] for c in seen_configs] == ["base", "merged"]
    assert seen_configs[0]["vision"]["vlm"]["max_new_tokens"] == 8
    assert all(s.closed == 1 for s in sessions), "모델을 내리지 않았다"
    base = summaries["base"]["questions"]["blocked_by_fallen"]
    merged = summaries["merged"]["questions"]["blocked_by_fallen"]
    assert base["frames"]["false_alarm"] == 1.0
    assert merged["frames"]["recall"] == 1.0
    assert merged["frames"]["false_alarm"] == 0.0
    assert merged["unreadable"] == {"yes": 0, "no": 1}, "«maybe» 는 판독 불가로 센다"

    table = ev.compare(summaries)
    assert "| `blocked_by_fallen` | base | 2/2 | 100.0% | 100.0% | 0/0 |" in table
    assert "| `blocked_by_fallen` | merged | 2/2 | 100.0% | 0.0% | 0/1 |" in table


def test_evaluate_refuses_without_a_session_factory(tmp_path: Path) -> None:
    samples = ev.holdout_samples(_holdout(tmp_path), keys=None)
    with pytest.raises(RuntimeError, match="VLM 의존성"):
        ev.evaluate_model(
            "m", samples, {"model_id": "x", "budget_ms": 1}, build_factory=lambda _c: None
        )


def test_evaluate_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "list.jsonl"
    ds.write_jsonl(_holdout(tmp_path), path)
    merged = tmp_path / "merged"
    merged.mkdir()

    def build(config):
        model = config["vision"]["vlm"]["model_id"]
        return lambda: FakeSession("base" if model == "Qwen/Qwen2-VL-2B-Instruct" else "merged")

    monkeypatch.setattr(ev, "build_session_factory", build)
    out = tmp_path / "eval.json"
    assert ev.main([str(path), "--merged", str(merged), "--out", str(out)]) == 0
    printed = capsys.readouterr().out
    assert "| `blocked_by_fallen` | Qwen/Qwen2-VL-2B-Instruct |" in printed
    saved = json.loads(out.read_text(encoding="utf-8"))
    assert set(saved["models"]) == {"Qwen/Qwen2-VL-2B-Instruct", str(merged)}
    assert saved["dataset_sha256"] == ds.file_sha256(path)

    assert ev.main([str(path), "--merged", str(tmp_path / "none")]) == 2
    assert ev.main([str(path), "--merged", str(merged), "--keys", "person_down"]) == 2
    assert "보류 사진이 없다" in capsys.readouterr().err


def test_evaluate_cli_says_which_python_when_packages_are_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "list.jsonl"
    ds.write_jsonl(_holdout(tmp_path), path)
    monkeypatch.setattr(ev, "build_session_factory", lambda _config: None)
    monkeypatch.setattr(ev, "missing_packages", lambda: ("transformers",))
    assert ev.main([str(path), "--base-only"]) == 1
    assert "venv-mechdog-vlm" in capsys.readouterr().err


# ── 운용 런타임이 병합 폴더를 읽는다 ──────────────────────


def test_runtime_factory_passes_a_local_model_folder_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`from_pretrained` 는 모델 ID 자리에 로컬 폴더도 받는다 — 경로를 바꾸지 않고 넘긴다."""
    monkeypatch.setattr(vlm_session, "missing_packages", lambda: ())
    made: list[str] = []

    class Spy:
        def __init__(self, model_id: str, *, max_new_tokens: int = 32) -> None:  # noqa: ARG002
            made.append(model_id)

    monkeypatch.setattr(vlm_session, "QwenVlSession", Spy)
    folder = str(tmp_path / "merged")
    factory = vlm_session.build_session_factory({"vision": {"vlm": {"model_id": folder}}})
    assert factory is not None
    factory()
    assert made == [folder]
