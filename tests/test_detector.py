"""검출기 검증 (WBS 3.3.1 · FR-3.1.2).

**가중치도 GPU 도 없이 전수 검증된다.** 전·후처리는 배열만 다루는 순수 함수이고,
세션은 주입받는다. 파서를 소켓에서 떼어 놓은 것(`4.3.3`)과 같은 이유다.

핵심은 **역변환**이다. letterbox 를 되돌리지 않으면 박스가 화면 왼쪽 위에 몰리는데,
그게 "검출은 되고 있다"처럼 보여서 늦게 발견된다. 그래서 좌표를 왕복시켜 검사한다.
"""

from __future__ import annotations

import numpy as np
import pytest

from host.common.config import load_config
from host.vision.coco_labels import COCO_CLASSES
from host.vision.detector import (
    ADAPTERS,
    YOLOX_PAD_VALUE,
    YOLOX_STRIDES,
    Detector,
    ModelMissingError,
    YoloxAdapter,
    build_adapter,
    nms,
)

INPUT = 640
#: 640 입력에서 나오는 후보 수 — 80²+40²+20².
ANCHORS = sum((INPUT // s) ** 2 for s in YOLOX_STRIDES)


@pytest.fixture
def cfg() -> dict:
    return load_config("mechdog-01")


# ── 어댑터 등록 ─────────────────────────────────────────────
def test_selected_family_is_registered(cfg: dict) -> None:
    """설정에 적힌 계열이 실제로 존재해야 한다 (OI-15 = YOLOX)."""
    assert cfg["vision"]["coco"]["model_family"] in ADAPTERS


def test_unknown_family_is_refused() -> None:
    """⚠️ **조용히 기본값으로 넘어가면 설정을 고쳐도 아무 일이 안 일어난다.**

    해상도 정본이 둘이어서 겪은 그 형태다 (`4.3.3`).
    """
    with pytest.raises(ValueError, match="model_family"):
        build_adapter("yolov8", INPUT)


def test_input_size_must_match_the_grid() -> None:
    """stride 32 의 배수가 아니면 격자와 출력이 어긋난다."""
    with pytest.raises(ValueError):
        YoloxAdapter(600)


# ── 전처리 ──────────────────────────────────────────────────
def test_preprocess_letterboxes_without_distorting() -> None:
    """가로세로 비가 유지되어야 한다 — 늘리면 박스도 같이 늘어난다."""
    adapter = YoloxAdapter(INPUT)
    prep = adapter.preprocess(np.zeros((480, 640, 3), dtype=np.uint8))
    assert prep.tensor.shape == (1, 3, INPUT, INPUT)
    assert prep.ratio == pytest.approx(1.0)


def test_preprocess_pads_with_114_not_black() -> None:
    """⚠️ **여백은 114 다.** 0(검정)으로 채우면 여백 자체가 물체처럼 보인다."""
    adapter = YoloxAdapter(INPUT)
    prep = adapter.preprocess(np.full((320, 640, 3), 200, dtype=np.uint8))
    # 아래쪽 절반이 여백이다 (좌상단 정렬).
    assert prep.tensor[0, :, INPUT - 1, INPUT - 1] == pytest.approx(YOLOX_PAD_VALUE)


def test_preprocess_anchors_top_left_not_center() -> None:
    """⚠️ **가운데 정렬이 아니다.** 가운데로 맞추면 좌표 복원이 여백 절반만큼 밀린다."""
    adapter = YoloxAdapter(INPUT)
    prep = adapter.preprocess(np.full((320, 640, 3), 200, dtype=np.uint8))
    assert prep.tensor[0, 0, 0, 0] == pytest.approx(200.0), "좌상단은 영상이어야 한다"


def test_preprocess_matches_the_reference_rounding() -> None:
    """⚠️ **원본은 버림이다.** `yolox/data/data_augment.py` 의 `int(shape * r)`.

    VGA·QVGA 는 비율이 정수라 차이가 없지만, 비정수 비율에서 반올림하면 1픽셀
    어긋나 **모델이 학습 때 본 것과 다른 그림**이 된다. 처음 반올림으로 썼다가
    원본을 대조해서 고쳤다.
    """
    adapter = YoloxAdapter(INPUT)
    # 480x700 → ratio = 640/700. 높이 480*ratio = 438.857 이라 **버림 438 / 반올림 439**
    # 로 갈린다. 폭은 정확히 640 이 되어 갈리지 않으므로 높이로 판별한다.
    prep = adapter.preprocess(np.full((480, 700, 3), 200, dtype=np.uint8))
    ratio = 640 / 700
    truncated, rounded = int(480 * ratio), round(480 * ratio)
    assert truncated != rounded, "이 치수는 두 방식이 갈려야 시험이 성립한다"

    column = prep.tensor[0, 0, :, 0]  # 첫 열 — 여백이 시작되는 행이 축소된 높이다
    filled = int((column != YOLOX_PAD_VALUE).sum())
    assert filled == truncated, f"버림 {truncated} 이어야 함 (반올림이면 {rounded})"


def test_preprocess_does_not_normalize() -> None:
    """⚠️ **YOLOX 는 0~255 를 그대로 받는다.**

    `/255` 를 습관으로 넣으면 검출이 전부 사라지는데, 에러가 아니라 **빈 결과**로
    나오므로 원인을 찾기 어렵다.
    """
    adapter = YoloxAdapter(INPUT)
    prep = adapter.preprocess(np.full((640, 640, 3), 255, dtype=np.uint8))
    assert prep.tensor.max() == pytest.approx(255.0)


def test_preprocess_refuses_non_bgr() -> None:
    with pytest.raises(ValueError):
        YoloxAdapter(INPUT).preprocess(np.zeros((640, 640), dtype=np.uint8))


# ── 디코딩 ──────────────────────────────────────────────────
def _raw_with_one_box(
    *,
    index: int,
    class_id: int,
    objectness: float = 1.0,
    class_score: float = 1.0,
) -> np.ndarray:
    """후보 하나만 켜진 원시 출력. 크기는 stride 와 같게(로그 0) 둔다."""
    raw = np.zeros((1, ANCHORS, 5 + len(COCO_CLASSES)), dtype=np.float32)
    raw[0, index, 4] = objectness
    raw[0, index, 5 + class_id] = class_score
    return raw


def test_decode_places_box_on_its_grid_cell() -> None:
    """격자 좌표 × stride 로 중심이 결정된다."""
    adapter = YoloxAdapter(INPUT)
    # stride 8 격자의 (1, 0) 번째 = 인덱스 1 → 중심 x = (0+1)*8 = 8
    boxes, scores, ids = adapter.decode([_raw_with_one_box(index=1, class_id=0)])
    assert boxes.shape == (ANCHORS, 4)
    center_x = (boxes[1, 0] + boxes[1, 2]) / 2
    assert center_x == pytest.approx(8.0)
    assert scores[1] == pytest.approx(1.0)
    assert ids[1] == 0


def test_decode_multiplies_objectness_by_class_score() -> None:
    """⚠️ **둘 중 하나만 쓰면 배경이 통과한다.**"""
    adapter = YoloxAdapter(INPUT)
    _, scores, _ = adapter.decode(
        [_raw_with_one_box(index=5, class_id=0, objectness=0.5, class_score=0.4)]
    )
    assert scores[5] == pytest.approx(0.2)


def test_decode_refuses_grid_mismatch() -> None:
    """input_size 가 모델과 어긋난 상태를 조용히 넘기면 좌표가 전부 틀린다."""
    adapter = YoloxAdapter(INPUT)
    with pytest.raises(ValueError, match="격자"):
        adapter.decode([np.zeros((1, 100, 85), dtype=np.float32)])


# ── NMS ─────────────────────────────────────────────────────
def test_nms_keeps_the_best_of_overlapping_boxes() -> None:
    boxes = np.array([[0, 0, 10, 10], [1, 1, 11, 11], [50, 50, 60, 60]], dtype=np.float32)
    scores = np.array([0.9, 0.8, 0.7], dtype=np.float32)
    assert nms(boxes, scores, 0.45) == [0, 2]


def test_nms_returns_score_order() -> None:
    boxes = np.array([[0, 0, 10, 10], [50, 50, 60, 60]], dtype=np.float32)
    scores = np.array([0.3, 0.9], dtype=np.float32)
    assert nms(boxes, scores, 0.45) == [1, 0]


def test_nms_survives_zero_area_boxes() -> None:
    """0 나눗셈을 만들지 않는다 — 면적 0 은 겹치지 않은 것으로 본다."""
    boxes = np.array([[5, 5, 5, 5], [0, 0, 10, 10]], dtype=np.float32)
    scores = np.array([0.5, 0.9], dtype=np.float32)
    assert nms(boxes, scores, 0.45) == [1, 0]


def test_nms_on_empty_input() -> None:
    assert nms(np.zeros((0, 4), dtype=np.float32), np.zeros((0,), dtype=np.float32), 0.5) == []


# ── 파이프라인 (세션 주입) ──────────────────────────────────
class _FakeSession:
    """`run()` 이 정해진 배열을 돌려주는 가짜 세션.

    ⚠️ **입력 이름을 실제처럼 물어보게 둔다.** 이름을 코드에 박으면 모델을 바꿀 때
    조용히 깨진다.
    """

    def __init__(self, raw: np.ndarray) -> None:
        self._raw = raw
        self.fed: dict[str, np.ndarray] = {}

    def get_inputs(self) -> list:
        class _Input:
            name = "images"

        return [_Input()]

    def run(self, _outputs, feed: dict[str, np.ndarray]) -> list[np.ndarray]:
        self.fed = feed
        return [self._raw]


def _detector(cfg: dict, raw: np.ndarray) -> tuple[Detector, _FakeSession]:
    session = _FakeSession(raw)
    det = Detector(cfg, labels=COCO_CLASSES, session_factory=lambda *_: session)
    return det, session


def test_open_warms_up_through_preprocess_not_just_the_session(cfg: dict) -> None:
    """⚠️ **첫 프레임 비용의 대부분이 추론이 아니라 전처리였다.**

    실측 — 첫 프레임 122ms = **전처리 78.8ms + 추론 5.2ms**. OpenCV 의 첫 호출
    초기화가 지배한다. 그래서 세션만 미리 돌리면 절반만 데워지고, 실제로 그렇게
    만들어서 첫 프레임이 108ms 로 거의 그대로였다.

    전처리를 통과했는지 확인하는 방법 — 워밍업 입력은 **`YOLOX_PAD_VALUE` 로 채운
    여백이 없는 정사각**이므로, 세션에 들어온 텐서가 0 이면 전처리를 지난 것이다
    (전처리를 건너뛰고 텐서를 직접 만들어도 0 이므로, 값이 아니라 **호출 여부**를
    본다).
    """
    det, session = _detector(cfg, _raw_with_one_box(index=1, class_id=0))
    calls: list[tuple[int, int]] = []
    original = det.adapter.preprocess

    def spy(image):
        calls.append(image.shape[:2])
        return original(image)

    det._adapter.preprocess = spy  # type: ignore[method-assign]
    det.open()

    assert calls == [(INPUT, INPUT)], "전처리를 지나야 OpenCV 초기화가 끝난다"
    assert session.fed["images"].shape == (1, 3, INPUT, INPUT)


def test_detect_returns_original_image_coordinates(cfg: dict) -> None:
    """⚠️ **letterbox 를 되돌린다.**

    VGA(640x480)는 640 입력에 비율 1.0 으로 들어가고 아래쪽이 여백이 된다. 되돌리지
    않으면 박스가 여백 쪽으로 밀린 채 찍히는데, 그래도 "검출은 되는" 것처럼 보인다.
    """
    det, session = _detector(cfg, _raw_with_one_box(index=1, class_id=0))
    found = det.detect(np.zeros((480, 640, 3), dtype=np.uint8))
    assert len(found) == 1
    assert found[0].label == "person"
    x1, y1, x2, y2 = found[0].box
    assert 0 <= x1 <= x2 <= 640
    assert 0 <= y1 <= y2 <= 480, "원본 높이를 넘으면 역변환·클리핑이 빠진 것이다"
    assert session.fed["images"].shape == (1, 3, INPUT, INPUT)


def test_detect_drops_below_confidence(cfg: dict) -> None:
    """임계값 미달은 버린다 (FR-3.2 는 그 위에서 시간 창 안의 횟수를 본다)."""
    raw = _raw_with_one_box(index=1, class_id=0, objectness=0.3, class_score=0.3)
    det, _ = _detector(cfg, raw)
    assert det.detect(np.zeros((480, 640, 3), dtype=np.uint8)) == []


def test_detect_suppresses_per_class_not_globally(cfg: dict) -> None:
    """⚠️ **클래스별로 억제한다.**

    전체를 한 번에 억제하면 겹쳐 선 사람과 의자가 하나로 합쳐진다 — 변화 감지(FR-8)
    에서 물체가 사라진 것처럼 보인다.
    """
    # 이웃 격자 두 칸에 **크게** 그려 서로 90% 이상 겹치게 만든다. 전역 억제라면
    # 점수 낮은 쪽이 사라지고, 클래스별 억제라면 둘 다 남는다.
    raw = _raw_with_one_box(index=0, class_id=COCO_CLASSES.index("person"))
    raw[0, 1, 4] = 1.0
    raw[0, 1, 5 + COCO_CLASSES.index("chair")] = 0.9
    raw[0, [0, 1], 2:4] = np.log(200.0 / YOLOX_STRIDES[0])  # 변 200px

    det, _ = _detector(cfg, raw)
    found = det.detect(np.zeros((480, 640, 3), dtype=np.uint8))
    assert {d.label for d in found} == {"person", "chair"}


def test_detect_sorts_by_score(cfg: dict) -> None:
    raw = _raw_with_one_box(index=1, class_id=0, class_score=0.6)
    raw[0, 4000, 4] = 1.0
    raw[0, 4000, 5 + COCO_CLASSES.index("bottle")] = 0.95
    det, _ = _detector(cfg, raw)
    found = det.detect(np.zeros((480, 640, 3), dtype=np.uint8))
    assert [d.label for d in found] == ["bottle", "person"]


def test_missing_model_file_stops_with_the_procedure(cfg: dict, tmp_path) -> None:
    """⚠️ **없는 가중치를 조용히 넘기지 않는다.**

    벤더 라이브러리를 `#error` 가드로 처리한 것(ADR-20)과 같은 원칙이다. 빈 결과를
    내면 "사람이 없는 것"과 구별되지 않는다.

    ⚠️ **없는 경로를 명시한다.** 처음에는 `models/` 가 비어 있다는 로컬 상태에
    기대어 통과했고, 가중치를 실제로 받은 순간 깨졌다. **환경에 기대어 통과하는
    시험은 통과해도 아무것도 증명하지 않는다.**
    """
    from copy import deepcopy

    broken = deepcopy(cfg)
    broken["vision"]["coco"]["model_path"] = str(tmp_path / "없는파일.onnx")
    det = Detector(broken, labels=COCO_CLASSES)  # 실제 팩토리
    with pytest.raises(ModelMissingError, match="models/README.md"):
        det.open()


def test_model_is_found_from_any_working_directory(
    cfg: dict, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """⚠️ **`models/coco.onnx` 는 저장소 기준이다** — 실행 위치(CWD) 기준이면 못 찾는다."""
    from host.common.config import ROOT

    monkeypatch.chdir(tmp_path)
    opened: list = []

    def factory(path, _providers):
        opened.append(path)
        return _FakeSession(_raw_with_one_box(index=1, class_id=0))

    Detector(cfg, labels=COCO_CLASSES, session_factory=factory).open()
    assert opened == [ROOT / cfg["vision"]["coco"]["model_path"]]


def test_model_family_must_be_recorded(cfg: dict) -> None:
    """재현성 — 계열이 비면 몇 달 뒤 같은 결과를 못 만든다 (FR-9.1.2)."""
    from copy import deepcopy

    broken = deepcopy(cfg)
    broken["vision"]["coco"]["model_family"] = None
    with pytest.raises(ValueError, match="model_family"):
        Detector(broken, labels=COCO_CLASSES)


# ── 라벨 ────────────────────────────────────────────────────
def test_label_order_starts_with_person() -> None:
    """⚠️ **순서가 모델과의 계약이다.** 어긋나면 사람을 자전거로 부르면서도 조용하다."""
    assert COCO_CLASSES[0] == "person"
    assert len(COCO_CLASSES) == 80
    assert len(set(COCO_CLASSES)) == 80


def test_watched_change_classes_exist_in_vocabulary(cfg: dict) -> None:
    """FR-8 시연 소품이 COCO 어휘 안에 있어야 한다 — 개방 어휘를 안 쓰기로 한 전제다."""
    unknown = set(cfg["vision"]["coco"]["change_watch_classes"]) - set(COCO_CLASSES)
    assert not unknown, f"COCO 80 에 없는 클래스: {sorted(unknown)}"


def test_person_class_is_in_vocabulary(cfg: dict) -> None:
    assert cfg["vision"]["coco"]["person_class"] in COCO_CLASSES


@pytest.mark.parametrize("size", [32, 640])
def test_repeated_decode_preserves_grid_coordinates_and_input(size: int) -> None:
    adapter = YoloxAdapter(size)
    cells = [(x, y, s) for s in YOLOX_STRIDES for y in range(size // s) for x in range(size // s)]
    raw = np.zeros((1, len(cells), 7), dtype=np.float32)
    raw[0, :, :2] = 0.25
    raw[0, :, 4:] = [0.8, 0.25, 0.75]
    saved = raw.copy()
    expected = np.array(
        [[(x - 0.25) * s, (y - 0.25) * s, (x + 0.75) * s, (y + 0.75) * s] for x, y, s in cells]
    )
    for _ in range(3):
        boxes, scores, ids = adapter.decode([raw])
        np.testing.assert_array_equal(boxes, expected)
        np.testing.assert_allclose(scores, 0.6)
        np.testing.assert_array_equal(ids, 1)
        np.testing.assert_array_equal(raw, saved)
