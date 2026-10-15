"""YOLOX-S 위험물 2클래스 — 로봇 시점 합성 데이터 (2026-10-06 · hazard-v2 후보)."""

import os

from yolox.exp import Exp as BaseExp


class Exp(BaseExp):
    def __init__(self):
        super().__init__()
        self.depth, self.width = 0.33, 0.50
        self.num_classes = 2
        self.data_dir = os.environ.get(
            "HZ_DATA_DIR", str(__import__("pathlib").Path(__file__).parent / "dataset")
        )
        self.train_ann = "instances_train2017.json"
        self.val_ann = "instances_val2017.json"
        self.input_size = (640, 640)
        self.test_size = (640, 640)
        self.max_epoch = int(os.environ.get("HZ_EPOCHS", "30"))
        self.no_aug_epochs = 5
        self.eval_interval = 5
        self.print_interval = 50
        self.data_num_workers = 4
        self.mosaic_prob = 0.5
        self.mixup_prob = 0.0
        self.hsv_prob = 0.5
        self.flip_prob = 0.5
        self.mosaic_scale = (0.5, 1.5)  # 작은 물체가 더 작아지지 않게 확대 위주
        self.basic_lr_per_img = 0.01 / 64.0
        self.warmup_epochs = 2
        self.seed = 20261006
        self.exp_name = "hazard_synth_s"
