"""VLM 미세조정용 프레임 자동 수집 — LiDAR 순찰 판정을 라벨로 쓴다 (WBS 4.8.7).

«통로를 막은 것이 무너진 물건인가» 를 LoRA 로 가르칠 사진을 순찰 중에 모은다.

- **blocked** — `PatrolController` 가 새 장애물을 연속 확정해 `path_blocked`(source `lidar`)가
  나는 프레임. 확정마다 남긴다.
- **clear** — 순찰 중 이동 경로 앞에 확정·확인 중인 장애물이 없고 근거리 반사 정지도 걸리지
  않은 프레임. `clear_every_ms` 간격으로만 남기고, blocked 직후 `clear_holdoff_ms` 동안은
  같은 장애물이 아직 보일 수 있어 남기지 않는다. 보류 시간은 막힘 사건 시점부터 재므로,
  blocked 사진을 남기지 못한 사건(프레임 없음·낡음·저장 실패)도 보류 시간을 건다. 순찰 밖
  (구역 점검 등)에서 확정된 막힘도 사진은 남기지 않지만 보류 시간은 건다 — 순찰로 돌아온
  직후 장애물이 아직 보이는 프레임이 clear 로 들어가지 않게 한다.

비전이 끊기면 마지막 결과가 계속 남아 있으므로, 받은 지 `max_frame_age_ms` 가 지난 프레임은
라벨을 붙이지 않는다. manifest 에는 프레임 수신 시각(`frame_ms`)과 번호(`frame_seq`)를 남겨
같은 프레임이 두 번 들어간 것을 나중에 걸러낼 수 있게 한다.

⚠️ **사진에는 얼굴이 찍힌다.** 수집 폴더는 저장소 밖이어야 하며(`config.validate_base_config`),
기본은 꺼짐(`root: null`)이다. 저장 실패는 제어 루프를 죽이지 않고 기록만 남긴다.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from host.common.config import repo_path
from host.common.logging_setup import event_logger

LOG = event_logger("mechadog.vision")

SOURCE = "lidar"

# 수집기가 쓰는 배치 `<YYYYMMDD>/{blocked,clear}/*.jpg` 만 상한에 센다.
LAYOUT_GLOBS = tuple(f"{'[0-9]' * 8}/{label}/*.jpg" for label in ("blocked", "clear"))


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
        max_frame_age_ms: int = 1000,
    ) -> None:
        self._root = Path(root) if root else None
        self._device_id = device_id
        self.clear_every_ms = clear_every_ms
        self.clear_holdoff_ms = clear_holdoff_ms
        self.max_files = max_files
        self.max_frame_age_ms = max_frame_age_ms
        self._last_clear_ms: int | None = None
        self._last_blocked_ms: int | None = None
        self._seq = 0
        self._warned = False
        # 재기동해도 상한이 이어지도록 이미 쌓인 사진을 센다.
        self._count = self._count_existing()

    @property
    def root(self) -> Path | None:
        return self._root

    @property
    def enabled(self) -> bool:
        return self._root is not None

    @property
    def full(self) -> bool:
        return self._count >= self.max_files

    def note_blocked(
        self,
        now_ms: int,
        jpeg: bytes | None,
        hit: tuple[float, float],
        target: str | None,
        state: str,
        *,
        frame_ms: int | None = None,
        frame_seq: int | None = None,
    ) -> bool:
        """LiDAR 막힘 확정 프레임. 저장했으면 True.

        저장 여부와 관계없이 막힘 사건이면 clear 보류 시간을 건다. 사진은 순찰 중일 때만
        남긴다 (추적·경보 중에는 앞에 선 사람이 신규 장애물로 확정된다).
        """
        if self._root is None:
            return False
        self._last_blocked_ms = now_ms
        if state != "PATROL":
            return False
        if jpeg is None or self._stale(now_ms, frame_ms):
            reason = "no_frame" if jpeg is None else "stale"
            LOG.info("frame_collect_skipped", label="blocked", reason=reason)
            return False
        extra = {"x": round(hit[0], 2), "y": round(hit[1], 2), "target": target}
        return self._save(now_ms, jpeg, "blocked", state, extra, frame_ms, frame_seq)

    def note_clear(
        self,
        now_ms: int,
        jpeg: bytes | None,
        *,
        state: str,
        obstacle_active: bool,
        pending: bool,
        frame_ms: int | None = None,
        frame_seq: int | None = None,
    ) -> bool:
        """«막힘 없음» 후보 프레임. 간격·보류 시간·상태·프레임 나이를 통과해야 저장한다."""
        if self._root is None or state != "PATROL" or obstacle_active or pending:
            return False
        if self._last_blocked_ms is not None and now_ms - self._last_blocked_ms < (
            self.clear_holdoff_ms
        ):
            return False
        if self._last_clear_ms is not None and now_ms - self._last_clear_ms < self.clear_every_ms:
            return False
        if jpeg is None or self._stale(now_ms, frame_ms):
            return False
        saved = self._save(now_ms, jpeg, "clear", state, {}, frame_ms, frame_seq)
        if saved:
            self._last_clear_ms = now_ms
        return saved

    def _stale(self, now_ms: int, frame_ms: int | None) -> bool:
        """받은 지 `max_frame_age_ms` 가 지난 프레임은 판정 시점의 장면이 아니다."""
        return frame_ms is not None and now_ms - frame_ms > self.max_frame_age_ms

    def _count_existing(self) -> int:
        if self._root is None:
            return 0
        try:
            if not self._root.is_dir():
                return 0
            return sum(1 for pattern in LAYOUT_GLOBS for _ in self._root.glob(pattern))
        except OSError as exc:
            LOG.warning("frame_collect_count_failed", error=f"{type(exc).__name__}: {exc}")
            return 0

    def _save(
        self,
        now_ms: int,
        jpeg: bytes,
        label: str,
        state: str,
        extra: dict[str, Any],
        frame_ms: int | None,
        frame_seq: int | None,
    ) -> bool:
        assert self._root is not None
        if self.full:
            if not self._warned:
                self._warned = True
                LOG.warning("frame_collect_full", max_files=self.max_files)
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
        if frame_ms is not None:
            entry["frame_ms"] = frame_ms
        if frame_seq is not None:
            entry["frame_seq"] = frame_seq
        try:
            (day_dir / label).mkdir(parents=True, exist_ok=True)
            (day_dir / rel).write_bytes(jpeg)
            self._count += 1  # manifest 쓰기가 실패해도 남은 jpg 는 센다
            with (day_dir / "manifest.jsonl").open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError as exc:
            LOG.error("frame_collect_failed", error=f"{type(exc).__name__}: {exc}")
            return False
        return True


def collector_from_config(config: Mapping[str, Any], device_id: str) -> FrameCollector:
    """`vision.collect` 절로 수집기를 만든다. 절이 없거나 `root` 가 비면 꺼진 수집기다.

    상대 `root` 는 설정 검증과 같이 CWD 가 아니라 저장소 루트 기준으로 푼다.
    """
    spec = config.get("vision", {}).get("collect") or {}
    root = spec.get("root")
    return FrameCollector(
        repo_path(root) if root else None,
        device_id,
        clear_every_ms=spec.get("clear_every_ms", 5000),
        clear_holdoff_ms=spec.get("clear_holdoff_ms", 10000),
        max_files=spec.get("max_files", 5000),
        max_frame_age_ms=spec.get("max_frame_age_ms", 1000),
    )
