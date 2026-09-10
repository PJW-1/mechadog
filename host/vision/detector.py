"""범용 객체 검출 (WBS 3.3.1 · FR-3.1.2 · DR-12/13).

**검출기는 갈아 끼울 수 있어야 한다.** 그래서 이 모듈은 세 겹으로 나뉜다.

    ① `DetectorAdapter`  모델 고유 부분 — 전처리 규약과 출력 디코딩
    ② `nms` · `Detector`  모델 무관 부분 — 세션 관리, 억제, 좌표 복원, 게이팅
    ③ `ADAPTERS`          `vision.coco.model_family` 로 ①을 고른다

지금 채택한 것은 **YOLOX-S**(Apache-2.0, Megvii)다. 나중에 정확도가 더 필요해지면
`DetectorAdapter` 하나를 더 쓰면 되고, ②는 손대지 않는다 — 라이선스나 성능 때문에
계열을 갈아탈 가능성이 실재하므로(ADR-24 재검토 조항) 그 비용을 미리 낮춰 둔다.

⚠️ **이 모듈은 파일도 GPU 도 만지지 않는다.** 세션을 만드는 일은 주입받은
`session_factory` 가 하고, 전·후처리는 배열만 다루는 순수 함수다. 파서를 소켓에서
떼어 놓은 것(`4.3.3`)과 같은 이유다 — 모델 가중치 없이 전수 검증된다.

⚠️ **가중치는 저장소에 없다.** `models/` 는 gitignore 이며, 파일이 없으면 조용히
빈 결과를 내는 대신 **획득 절차를 담은 오류로 즉시 멈춘다.** 벤더 라이브러리를
`#error` 가드로 처리한 것(ADR-20)과 같은 원칙이다 — 없는 것을 있는 척하지 않는다.
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import numpy as np

from host.common.logging_setup import event_logger
from host.vision.providers import log_selection, select_providers

LOG = event_logger("mechadog.vision")


class ModelMissingError(RuntimeError):
    """가중치 파일이 없음. **추정으로 돌리지 않고 멈춘다.**"""


@dataclass(frozen=True, slots=True)
class Detection:
    """검출 하나. **좌표는 원본 프레임 기준**이다 (letterbox 를 이미 되돌렸다)."""

    label: str
    score: float
    #: (x1, y1, x2, y2) — 원본 픽셀 좌표
    box: tuple[float, float, float, float]


@dataclass(frozen=True, slots=True)
class Preprocessed:
    """전처리 결과와 **되돌리는 데 필요한 정보**.

    ⚠️ 비율을 함께 들고 다니는 이유 — 이것 없이는 모델 좌표를 원본으로 못 되돌린다.
    되돌리기를 잊으면 박스가 화면 왼쪽 위에 몰려 찍히는데, 그게 "검출이 되긴 된다"
    처럼 보여서 늦게 발견된다.
    """

    tensor: np.ndarray
    ratio: float


class DetectorAdapter(Protocol):
    """모델 고유 부분. **여기만 갈아 끼운다.**"""

    #: 모델이 받는 정사각 입력 변의 길이
    input_size: int

    def preprocess(self, image: np.ndarray) -> Preprocessed:
        """BGR 이미지를 모델 입력 텐서로. 정규화 규약은 모델마다 다르다."""
        ...

    def decode(self, outputs: Sequence[np.ndarray]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """원시 출력을 `(boxes_xyxy, scores, class_ids)` 로. **모델 좌표계 그대로** 낸다."""
        ...


# ── ① YOLOX 고유 부분 ───────────────────────────────────────
#: YOLOX 는 letterbox 여백을 이 값으로 채운다. 0(검정)으로 채우면 여백이 물체처럼 보인다.
YOLOX_PAD_VALUE = 114

#: 출력 격자의 stride. 640 입력이면 80²+40²+20² = 8400 개 후보가 나온다.
YOLOX_STRIDES = (8, 16, 32)


class YoloxAdapter:
    """YOLOX 계열(Apache-2.0, Megvii) 어댑터.

    ⚠️ **YOLOX 는 0~1 정규화도 평균·표준편차 보정도 하지 않는다.** 0~255 를 그대로
    넣는다. 다른 계열의 관례(`/255`)를 습관으로 적용하면 검출이 전부 사라지는데,
    에러가 아니라 **빈 결과**로 나오므로 원인을 찾기 어렵다.

    ⚠️ **letterbox 가 가운데 정렬이 아니라 좌상단 정렬이다.** 가운데로 맞추면
    좌표 복원에 여백 절반만큼 편차가 생겨 박스가 일정하게 밀린다.
    """

    def __init__(self, input_size: int) -> None:
        if input_size <= 0 or input_size % max(YOLOX_STRIDES) != 0:
            raise ValueError(
                f"input_size 는 {max(YOLOX_STRIDES)} 의 양의 배수여야 함: {input_size}"
            )
        self.input_size = int(input_size)

    def preprocess(self, image: np.ndarray) -> Preprocessed:
        if image.ndim != 3 or image.shape[2] != 3:
            raise ValueError(f"HxWx3 BGR 이미지가 필요함: shape={image.shape}")
        import cv2

        size = self.input_size
        height, width = image.shape[:2]
        ratio = min(size / height, size / width)
        # ⚠️ **버림이다. 반올림이 아니다.** 원본 `preproc` 이 `int(shape * r)` 를 쓴다
        # (`yolox/data/data_augment.py`). VGA·QVGA 는 비율이 정수라 차이가 없지만,
        # 다른 해상도에서 1픽셀 어긋나면 **모델이 학습 때 본 것과 다른 그림**이 된다.
        resized = cv2.resize(
            image,
            (int(width * ratio), int(height * ratio)),
            interpolation=cv2.INTER_LINEAR,
        )
        canvas = np.full((size, size, 3), YOLOX_PAD_VALUE, dtype=np.uint8)
        canvas[: resized.shape[0], : resized.shape[1]] = resized
        # HWC(BGR) → CHW, float32. 스케일 변환은 하지 않는다 (위 경고 참조).
        tensor = np.ascontiguousarray(canvas.transpose(2, 0, 1)[None], dtype=np.float32)
        return Preprocessed(tensor=tensor, ratio=ratio)

    def _grid(self) -> tuple[np.ndarray, np.ndarray]:
        """격자 좌표와 stride. 출력 순서(8→16→32)에 맞춰 이어 붙인다."""
        grids: list[np.ndarray] = []
        strides: list[np.ndarray] = []
        for stride in YOLOX_STRIDES:
            side = self.input_size // stride
            xv, yv = np.meshgrid(np.arange(side), np.arange(side))
            grid = np.stack((xv, yv), axis=2).reshape(-1, 2)
            grids.append(grid)
            strides.append(np.full((grid.shape[0], 1), stride))
        return np.concatenate(grids, axis=0), np.concatenate(strides, axis=0)

    def decode(self, outputs: Sequence[np.ndarray]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """`[1, 8400, 85]` → 박스·점수·클래스.

        85 = 중심 오프셋 2 + 크기 로그 2 + objectness 1 + 클래스 80.
        **점수는 objectness × 클래스 확률**이다 — 둘 중 하나만 쓰면 배경이 통과한다.
        """
        raw = np.asarray(outputs[0], dtype=np.float32)
        if raw.ndim == 3:
            raw = raw[0]
        if raw.ndim != 2 or raw.shape[1] < 6:
            raise ValueError(f"[N, 5+클래스] 출력이 필요함: shape={raw.shape}")

        grid, stride = self._grid()
        if raw.shape[0] != grid.shape[0]:
            raise ValueError(
                f"후보 수가 격자와 다름 — input_size 가 모델과 어긋났다: "
                f"출력={raw.shape[0]} 격자={grid.shape[0]}"
            )

        centers = (raw[:, 0:2] + grid) * stride
        sizes = np.exp(raw[:, 2:4]) * stride
        half = sizes / 2.0
        boxes = np.concatenate([centers - half, centers + half], axis=1)

        class_scores = raw[:, 5:]
        class_ids = class_scores.argmax(axis=1)
        scores = raw[:, 4] * class_scores[np.arange(class_scores.shape[0]), class_ids]
        return boxes, scores, class_ids


#: `vision.coco.model_family` 값 → 어댑터. 새 계열은 여기 한 줄만 늘린다.
ADAPTERS: dict[str, type] = {"yolox": YoloxAdapter}


def build_adapter(family: str, input_size: int) -> DetectorAdapter:
    """설정값으로 어댑터를 만든다. **모르는 이름은 거부한다.**

    조용히 기본값으로 넘어가면 설정을 고쳐도 아무 일이 안 일어난다 — 해상도 정본이
    둘이어서 겪은 그 형태다(`4.3.3`).
    """
    try:
        adapter_type = ADAPTERS[family]
    except KeyError:
        raise ValueError(f"모르는 model_family: {family!r}. 가능한 값={sorted(ADAPTERS)}") from None
    return adapter_type(input_size)


# ── ② 모델 무관 부분 ────────────────────────────────────────
def nms(
    boxes: np.ndarray,
    scores: np.ndarray,
    iou_threshold: float,
) -> list[int]:
    """겹치는 박스를 억제하고 **남길 순서(점수 내림차순)** 를 낸다.

    ⚠️ **모델이 아니라 우리가 쥐는 부분이다.** RT-DETR 처럼 NMS 가 필요 없는 계열로
    갈아타면 어댑터가 이 단계를 건너뛰게 두면 된다(임계값 1.0 이 아니라 호출을 뺀다).
    """
    if boxes.shape[0] == 0:
        return []
    x1, y1, x2, y2 = boxes[:, 0], boxes[:, 1], boxes[:, 2], boxes[:, 3]
    areas = np.clip(x2 - x1, 0, None) * np.clip(y2 - y1, 0, None)
    order = scores.argsort()[::-1]

    keep: list[int] = []
    while order.size > 0:
        best = int(order[0])
        keep.append(best)
        if order.size == 1:
            break
        rest = order[1:]
        inter_w = np.clip(np.minimum(x2[best], x2[rest]) - np.maximum(x1[best], x1[rest]), 0, None)
        inter_h = np.clip(np.minimum(y2[best], y2[rest]) - np.maximum(y1[best], y1[rest]), 0, None)
        inter = inter_w * inter_h
        union = areas[best] + areas[rest] - inter
        # 면적 0 인 박스가 섞이면 0 나눗셈이 된다 — 그런 박스는 겹치지 않은 것으로 본다.
        iou = np.where(union > 0, inter / np.maximum(union, 1e-9), 0.0)
        order = rest[iou <= iou_threshold]
    return keep


class Detector:
    """세션 하나를 들고 프레임을 검출로 바꾼다. **모델 계열을 모른다.**

    `session_factory` 를 주입받는 이유 — 이 클래스를 시험하려고 35MB 가중치와 GPU 를
    요구하면 안 된다. CI 에는 둘 다 없고, 그러면 정작 게이팅·억제·좌표 복원을
    검증할 수 없다.
    """

    def __init__(
        self,
        config: Mapping[str, Any],
        *,
        section: str = "coco",
        labels: Sequence[str],
        session_factory: Any | None = None,
    ) -> None:
        vision = config["vision"]
        spec = vision[section]
        family = spec.get("model_family")
        if not family:
            raise ValueError(
                f"vision.{section}.model_family 가 비어 있다 — 재현을 위해 계열을 기록한다"
            )
        self._adapter = build_adapter(str(family), int(spec["input_size"]))
        self._labels = tuple(labels)
        self._conf = float(spec["conf_threshold"])
        self._iou = float(spec.get("iou_threshold", 0.45))
        self._model_path = Path(spec["model_path"])
        self._preferred = list(vision["providers"])
        self._factory = session_factory or _make_onnx_session
        self._session: Any | None = None

    @property
    def adapter(self) -> DetectorAdapter:
        return self._adapter

    def open(self) -> None:
        """세션을 만들고 **파이프라인 전체를 한 번 미리 흘린다.**

        ⚠️ **첫 프레임은 느리다 — 그리고 원인이 추론이 아니었다.** 실측에서 첫
        프레임이 122ms 였고 둘째부터 9~10ms 였다. 구간을 쪼개 보니 **전처리 78.8ms
        + 추론 5.2ms** 였다. 즉 대부분이 **OpenCV 의 첫 호출 초기화**이고 GPU 커널
        준비는 그중 일부다.

        그래서 세션만 미리 돌리면 **절반만 데워진다** — 실제로 그렇게 만들어서
        첫 프레임이 108ms 로 거의 그대로였다. 전처리까지 함께 흘려야 한다.

        기동 직후 사람이 서 있으면 그 판정 하나가 예산(25ms)의 4배를 쓰므로, 비용을
        프레임이 아니라 기동 시점에 지불한다.
        """
        if self._session is not None:
            return
        self._session = self._factory(self._model_path, self._preferred)
        self._warm_up()

    def _warm_up(self) -> None:
        """빈 프레임으로 **전처리 → 추론**을 한 번 지나간다. 결과는 쓰지 않는다."""
        assert self._session is not None
        size = self._adapter.input_size
        started = time.perf_counter()
        # ⚠️ 텐서를 직접 만들지 않고 **어댑터의 전처리를 통과시킨다.** 그래야
        # OpenCV 초기화가 여기서 끝난다 — 그것이 첫 프레임 비용의 대부분이었다.
        prep = self._adapter.preprocess(np.zeros((size, size, 3), dtype=np.uint8))
        self._session.run(None, {self._input_name(): prep.tensor})
        LOG.info("detector_warmed_up", ms=round((time.perf_counter() - started) * 1000, 1))

    def detect(self, image: np.ndarray) -> list[Detection]:
        """프레임 하나 → 검출 목록. 임계값 미달과 겹침은 여기서 걸러진다."""
        self.open()
        assert self._session is not None
        prep = self._adapter.preprocess(image)
        outputs = self._session.run(None, {self._input_name(): prep.tensor})
        boxes, scores, class_ids = self._adapter.decode(outputs)
        return self._finalize(boxes, scores, class_ids, prep, image.shape[:2])

    def _input_name(self) -> str:
        assert self._session is not None
        return self._session.get_inputs()[0].name

    def _finalize(
        self,
        boxes: np.ndarray,
        scores: np.ndarray,
        class_ids: np.ndarray,
        prep: Preprocessed,
        shape: tuple[int, int],
    ) -> list[Detection]:
        above = scores >= self._conf
        boxes, scores, class_ids = boxes[above], scores[above], class_ids[above]
        if boxes.shape[0] == 0:
            return []

        # letterbox 되돌리기 → 원본 좌표. 그다음에 화면 밖을 잘라낸다.
        boxes = boxes / prep.ratio
        height, width = shape
        boxes[:, 0::2] = np.clip(boxes[:, 0::2], 0, width)
        boxes[:, 1::2] = np.clip(boxes[:, 1::2], 0, height)

        results: list[Detection] = []
        # ⚠️ **클래스별로 억제한다.** 전체를 한 번에 억제하면 겹쳐 선 사람과 의자처럼
        # 서로 다른 물체가 하나로 합쳐진다.
        for class_id in np.unique(class_ids):
            mask = class_ids == class_id
            picked = nms(boxes[mask], scores[mask], self._iou)
            sub_boxes, sub_scores = boxes[mask], scores[mask]
            for index in picked:
                results.append(
                    Detection(
                        label=self._label_of(int(class_id)),
                        score=float(sub_scores[index]),
                        box=tuple(float(v) for v in sub_boxes[index]),  # type: ignore[arg-type]
                    )
                )
        results.sort(key=lambda d: d.score, reverse=True)
        return results

    def _label_of(self, class_id: int) -> str:
        if 0 <= class_id < len(self._labels):
            return self._labels[class_id]
        return f"class_{class_id}"


def _make_onnx_session(path: Path, preferred: Sequence[str]) -> Any:
    """실제 onnxruntime 세션. **파일이 없으면 획득 절차를 담아 멈춘다.**"""
    if not path.is_file():
        raise ModelMissingError(
            f"모델 파일 없음: {path}\n"
            f"가중치는 저장소에 없다(용량·라이선스). `models/README.md` 의 획득 절차를 따른다."
        )
    import onnxruntime as ort

    available = list(ort.get_available_providers())
    chosen = select_providers(preferred, available)
    log_selection(chosen, available)
    return ort.InferenceSession(str(path), providers=chosen)
