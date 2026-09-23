# PPE 전용 YOLOX-Nano 실험 설정 — docs/MODEL_PLAN.md 1.3절 레시피
# 5클래스: helmet / no_helmet / vest / no_vest / person_down (generator CLASS_IDS 순)
# 실행: C:/dev/yolox-venv/Scripts/python.exe C:/dev/YOLOX/tools/train.py
#   -f ppe_exp.py -d 1 -b 16 --fp16 -o -c C:/dev/yolox-venv/yolox_nano.pth
import os

import torch.nn as nn

from yolox.exp import Exp as MyExp


class Exp(MyExp):
    def __init__(self):
        super().__init__()
        # ── YOLOX-Nano (depthwise) ──
        self.depth = 0.33
        self.width = 0.25
        self.input_size = (640, 640)
        self.random_size = (14, 26)
        self.mosaic_scale = (0.5, 1.5)
        self.test_size = (640, 640)
        self.mosaic_prob = 1.0
        self.enable_mixup = False
        self.exp_name = "ppe_yolox_nano_v3"

        self.num_classes = 5
        self.data_dir = os.path.join(os.path.dirname(__file__), "ppe_coco_v3")
        self.train_ann = "instances_train.json"
        self.val_ann = "instances_val.json"

        # ── MODEL_PLAN 1.3: SGD lr0.01 cosine, warmup 5ep, mosaic 마지막 15ep OFF ──
        self.max_epoch = 100
        self.no_aug_epochs = 15
        self.warmup_epochs = 5
        self.basic_lr_per_img = 0.01 / 64.0
        self.scheduler = "yoloxwarmcos"
        self.min_lr_ratio = 0.05
        self.weight_decay = 5e-4
        self.momentum = 0.9
        # 색 채도 증강 약하게 — 주황 안전모·형광 조끼가 판정 단서다 (MODEL_PLAN)
        self.hsv_prob = 1.0
        self.flip_prob = 0.5
        self.degrees = 10.0
        self.translate = 0.1
        self.shear = 2.0
        self.mixup_prob = 0.0
        self.data_num_workers = 4
        self.eval_interval = 10
        self.print_interval = 10
        self.save_history_ckpt = False

    def get_model(self, sublinear=False):
        def init_yolo(M):
            for m in M.modules():
                if isinstance(m, nn.BatchNorm2d):
                    m.eps = 1e-3
                    m.momentum = 0.03

        if "model" not in self.__dict__:
            from yolox.models import YOLOPAFPN, YOLOX, YOLOXHead
            in_channels = [256, 512, 1024]
            backbone = YOLOPAFPN(
                self.depth, self.width, in_channels=in_channels,
                act=self.act, depthwise=True,
            )
            head = YOLOXHead(
                self.num_classes, self.width, in_channels=in_channels,
                act=self.act, depthwise=True,
            )
            self.model = YOLOX(backbone, head)

        self.model.apply(init_yolo)
        self.model.head.initialize_biases(1e-2)
        return self.model

    def get_dataset(self, cache: bool = False, cache_type: str = "ram"):
        from yolox.data import COCODataset, TrainTransform
        return COCODataset(
            data_dir=self.data_dir,
            json_file=self.train_ann,
            name="train",
            img_size=self.input_size,
            preproc=TrainTransform(
                max_labels=50,
                flip_prob=self.flip_prob,
                hsv_prob=self.hsv_prob,
            ),
            cache=cache,
            cache_type=cache_type,
        )

    def get_eval_dataset(self, **kwargs):
        from yolox.data import COCODataset, ValTransform
        return COCODataset(
            data_dir=self.data_dir,
            json_file=self.val_ann,
            name="val",
            img_size=self.test_size,
            preproc=ValTransform(legacy=kwargs.get("legacy", False)),
        )
