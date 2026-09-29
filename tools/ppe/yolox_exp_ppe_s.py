"""YOLOX-S PPE 4클래스 학습 설정 (`Exp`). **학습 환경(`C:\\dev\\ppe-train`)에서만 불러온다.**

저장소 기본 python 에는 torch·yolox 가 없다. 이 파일은 YOLOX 의 `tools/train.py` 가
`-f` 로 읽는다.

    cd C:\\dev\\ppe-train\\YOLOX
    $env:PPE_DATA_DIR = "<저장소>\\datasets\\ppe\\build\\ppe4_cs_v1"
    ..\\.venv\\Scripts\\python tools\\train.py -f <저장소>\\tools\\ppe\\yolox_exp_ppe_s.py `
        -d 1 -b 16 --fp16 -c ..\\weights\\yolox_s.pth
    # 평가 (공개 test) — 표를 파일로 남겨 export_ppe.py --eval-result 로 넘긴다
    ..\\.venv\\Scripts\\python tools\\eval.py -f <저장소>\\tools\\ppe\\yolox_exp_ppe_s.py `
        -c YOLOX_outputs\\yolox_exp_ppe_s\\best_ckpt.pth -b 8 -d 1 --conf 0.001 --test

환경 변수 (YOLOX 의 `train.py ... data_dir <경로>` 식 덮어쓰기도 된다)
    PPE_DATA_DIR    `rf100_prepare.py` 출력 폴더 (필수)
    PPE_TRAIN_ANN   기본 instances_train.json   (XIAO 섞기: instances_train_mix.json)
    PPE_VAL_ANN     기본 instances_val.json     (XIAO 섞기: instances_val_mix.json)
    PPE_TEST_ANN    기본 instances_test.json

⚠️ **이미지 폴더는 train2017/val2017/test2017 이다.** YOLOX `COCODataset` 의 기본 이름이라
`name` 을 넘기지 않아도 된다 (`rf100_prepare.IMAGE_DIRS`).
"""

from __future__ import annotations

import os
import random
from pathlib import Path

import numpy as np
import yolox.layers
from pycocotools.cocoeval import COCOeval
from yolox.data import COCODataset
from yolox.data.data_augment import augment_hsv
from yolox.exp import Exp as BaseExp

# ⚠️ 왜: YOLOX 평가기는 `COCOeval_opt`(C++ 즉석 빌드)를 먼저 쓰는데, 빌드가 MSVC `cl` 을
# 찾다 `CalledProcessError` 로 죽는다 — 평가기의 `except ImportError` 폴백에 걸리지 않아
# 2026-09-28 첫 학습이 5 에폭 뒤 첫 평가에서 멈췄다. 결과가 같은 표준 COCOeval 로 바꾼다.
yolox.layers.COCOeval_opt = COCOeval

#: 학습 길이와 재현 시드. ⚠️ 바꾸면 export_ppe.py 메타데이터에 그대로 남는다.
MAX_EPOCH = 100
NO_AUG_EPOCHS = 20
SEED = 20260928

#: HSV 증강 게인 (OpenCV 단위: 색상 0~180, 채도·명도 0~255).
#: ⚠️ **색 단서(주황 안전모·형광 조끼)가 판정 근거다** (MODEL_PLAN 1.3). YOLOX 기본
#: (5, 30, 30)으로 흔들면 특징이 지워진다. 색상은 ±3(≈±6°)로 주황·연두가 서로 넘어가지
#: 않게, 채도는 이전 학습(v3)처럼 30→15 로 줄이고, 명도는 조명 차이를 배우도록 둔다.
HSV_GAINS = (3, 15, 30)
HSV_PROB = 1.0

CLASSES = ("helmet", "no_helmet", "vest", "no_vest")


class WeakHsvCOCODataset(COCODataset):
    """원본 이미지를 꺼낼 때 약한 HSV 를 먼저 건다.

    ⚠️ **왜 여기인가** — YOLOX 의 HSV 는 `get_data_loader` 안에서 새로 만드는
    `TrainTransform` 이 기본 게인으로 건다. 그 함수를 통째로 덮으면 YOLOX 판마다 깨진다.
    그래서 내장 HSV 는 `hsv_prob=0` 으로 끄고, `MosaicDetection` 이 반드시 거치는
    `pull_item` 에서 약하게 건다. 모듈 최상위 클래스라 Windows spawn 워커로도 피클된다.
    """

    def pull_item(self, index):
        img, target, img_info, img_id = super().pull_item(index)
        if random.random() < HSV_PROB:
            img = np.ascontiguousarray(img).copy()  # 캐시 원본을 건드리지 않는다
            augment_hsv(img, *HSV_GAINS)
        return img, target, img_info, img_id


class Exp(BaseExp):
    def __init__(self) -> None:
        super().__init__()
        # ── 모델: yolox-s ──
        self.depth = 0.33
        self.width = 0.50
        self.num_classes = len(CLASSES)
        self.exp_name = Path(__file__).stem

        # ── 데이터 ──
        self.data_dir = os.environ.get("PPE_DATA_DIR")
        self.train_ann = os.environ.get("PPE_TRAIN_ANN", "instances_train.json")
        self.val_ann = os.environ.get("PPE_VAL_ANN", "instances_val.json")
        self.test_ann = os.environ.get("PPE_TEST_ANN", "instances_test.json")
        # ⚠️ 호스트 어댑터가 640 격자로 디코드한다. 바꾸면 출력이 [1, 8400, 9] 가 아니다.
        self.input_size = (640, 640)
        self.test_size = (640, 640)
        # 런타임 입력은 늘 640 이다. 다중 크기 학습은 ±2단계(576~704)만 둔다.
        self.multiscale_range = 2

        # ── 증강 ──
        # ⚠️ mosaic 판단 — 입력은 **사람 한 명 크롭**이고 런타임에는 그 크롭이 letterbox 로
        # 640 을 채운다. YOLOX 기본(mosaic 1.0 · scale 0.1~2 · mixup)은 크롭 네 장을 작은
        # 배율로 이어 붙여 «여러 명이 작게 선 풀프레임» 을 만든다 — 런타임에 없는 분포다.
        # 가장자리에서 잘린 머리·몸통이 조각 라벨로 남는 것도 문제다. 그렇다고 끄면
        # 공개 크롭 수천 장에 과적합하기 쉽다. 그래서 절반만(0.5), 배율은 0.5~1.5 로 좁혀
        # 크기 분포를 런타임 근처에 두고, 마지막 NO_AUG_EPOCHS 는 mosaic 없이 런타임과
        # 같은 letterbox 입력으로 마무리한다.
        self.mosaic_prob = 0.5
        self.mosaic_scale = (0.5, 1.5)
        # ⚠️ mixup 은 끈다 — 두 장을 반투명하게 겹치면 조끼·안전모 색이 섞여 색 단서가 흐려진다.
        self.enable_mixup = False
        self.mixup_prob = 0.0
        self.hsv_prob = 0.0  # 내장 HSV 는 끄고 WeakHsvCOCODataset 이 약하게 건다
        self.flip_prob = 0.5
        self.degrees = 5.0
        self.translate = 0.1
        self.shear = 1.0

        # ── 학습 ──
        self.max_epoch = MAX_EPOCH
        self.no_aug_epochs = NO_AUG_EPOCHS
        self.warmup_epochs = 5
        self.eval_interval = 5
        self.seed = SEED

    def _require_data_dir(self) -> str:
        """⚠️ 비어 있으면 YOLOX 는 조용히 `datasets/COCO` 를 찾는다. 여기서 멈춘다."""
        if not self.data_dir:
            raise RuntimeError("PPE_DATA_DIR 이 비었다 — rf100_prepare.py 출력 폴더를 넘긴다")
        return str(self.data_dir)

    def get_dataset(self, cache: bool = False, cache_type: str = "ram"):
        from yolox.data import TrainTransform

        return WeakHsvCOCODataset(
            data_dir=self._require_data_dir(),
            json_file=self.train_ann,
            img_size=self.input_size,
            preproc=TrainTransform(max_labels=50, flip_prob=self.flip_prob, hsv_prob=0.0),
            cache=cache,
            cache_type=cache_type,
        )

    def get_eval_dataset(self, **kwargs):
        self._require_data_dir()
        return super().get_eval_dataset(**kwargs)
