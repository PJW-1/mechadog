"""VLM 미세조정용 프레임 자동 수집 — LiDAR 순찰 판정을 라벨로 쓴다 (WBS 4.8.7).

«통로를 막은 것이 무너진 물건인가» 를 LoRA 로 가르칠 사진을 순찰 중에 모은다.

- **blocked** — `PatrolController` 가 새 장애물을 연속 확정해 `path_blocked`(source `lidar`)가
  나는 프레임. 확정마다 남긴다.
- **clear** — 순찰 중 이동 경로 앞에 확정·확인 중인 장애물이 없고 근거리 반사 정지도 걸리지
  않은 프레임. `clear_every_ms` 간격으로만 남기고, blocked 직후 `clear_holdoff_ms` 동안은
  같은 장애물이 아직 보일 수 있어 남기지 않는다.

⚠️ **사진에는 얼굴이 찍힌다.** 수집 폴더는 저장소 밖이어야 하며(`config.validate_base_config`),
기본은 꺼짐(`root: null`)이다. 저장 실패는 제어 루프를 죽이지 않고 기록만 남긴다.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from host.common.logging_setup import event_logger

LOG = event_logger("mechadog.vision")

SOURCE = "lidar"


class FrameCollector:
    """`<root>/<YYYYMMDD>/<label>/<시각>_<번호>.jpg` 와 같은 날짜 폴더의 `manifest.jsonl`."""

    def __init__(
        self,
        root: Path | str | None,
        device_id: str,
        *,
        clear_every_ms: int = 5000,
        clear_holdoff_ms: int = 10000,
        max_files: int = 5000,
    ) -> None:
        self._root = Path(root) if root else None
        self._device_id = device_id
        self._clear_every_ms = clear_every_ms
        self._clear_holdoff_ms = clear_holdoff_ms
        self._max_files = max_files
        self._last_clear_ms: int | None = None
        self._last_blocked_ms: int | None = None
        self._seq = 0
        self._warned = False
        # 재기동해도 상한이 이어지도록 이미 쌓인 사진을 센다.
        self._count = (
            sum(1 for _ in self._root.rglob("*.jpg"))
            if self._root is not None and self._root.is_dir()
            else 0
        )

    @property
    def enabled(self) -> bool:
        return self._root is not None

    @property
    def full(self) -> bool:
        return self._count >= self._max_files

    def note_blocked(
        self,
        now_ms: int,
        jpeg: bytes,
        hit: tuple[float, float],
        target: str | None,
        state: str,
    ) -> bool:
        """LiDAR 막힘 확정 프레임. 저장했으면 True."""
        if self._root is None or state != "PATROL":
            return False
        extra = {"x": round(hit[0], 2), "y": round(hit[1], 2), "target": target}
        saved = self._save(now_ms, jpeg, "blocked", state, extra)
        if saved:
            self._last_blocked_ms = now_ms
        return saved

    def note_clear(
        self,
        now_ms: int,
        jpeg: bytes,
        *,
        state: str,
        obstacle_active: bool,
        pending: bool,
    ) -> bool:
        """«막힘 없음» 후보 프레임. 간격·보류 시간·상태를 통과해야 저장한다."""
        if self._root is None or state != "PATROL" or obstacle_active or pending:
            return False
        if self._last_blocked_ms is not None and now_ms - self._last_blocked_ms < (
            self._clear_holdoff_ms
        ):
            return False
        if self._last_clear_ms is not None and now_ms - self._last_clear_ms < self._clear_every_ms:
            return False
        saved = self._save(now_ms, jpeg, "clear", state, {})
        if saved:
            self._last_clear_ms = now_ms
        return saved

    def _save(
        self, now_ms: int, jpeg: bytes, label: str, state: str, extra: dict[str, Any]
    ) -> bool:
        assert self._root is not None
        if self.full:
            if not self._warned:
                self._warned = True
                LOG.warning("frame_collect_full", max_files=self._max_files)
            return False
        stamp = datetime.fromtimestamp(now_ms / 1000)
        day_dir = self._root / stamp.strftime("%Y%m%d")
        self._seq += 1
        rel = f"{label}/{stamp.strftime('%H%M%S')}{now_ms % 1000:03d}_{self._seq:04d}.jpg"
        entry = {
            "file": rel,
            "at_ms": now_ms,
            "device_id": self._device_id,
            "label": label,
            "source": SOURCE,
            **extra,
            "state": state,
        }
        try:
            (day_dir / label).mkdir(parents=True, exist_ok=True)
            (day_dir / rel).write_bytes(jpeg)
            with (day_dir / "manifest.jsonl").open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError as exc:
            LOG.error("frame_collect_failed", error=f"{type(exc).__name__}: {exc}")
            return False
        self._count += 1
        return True


def collector_from_config(config: Mapping[str, Any], device_id: str) -> FrameCollector:
    """`vision.collect` 절로 수집기를 만든다. 절이 없거나 `root` 가 비면 꺼진 수집기다."""
    spec = config.get("vision", {}).get("collect") or {}
    return FrameCollector(
        spec.get("root"),
        device_id,
        clear_every_ms=spec.get("clear_every_ms", 5000),
        clear_holdoff_ms=spec.get("clear_holdoff_ms", 10000),
        max_files=spec.get("max_files", 5000),
    )
