"""이벤트 당시의 JPEG·판단 근거·텔레메트리를 함께 보관한다 (WBS 4.4.3).

이 모듈은 **디스크 저장과 조회만** 맡는다. 어떤 사건을 기록할지 결정하는 런타임
연결과 대시보드 전송은 호출부의 책임이다. 저장 도중 프로세스가 끝나더라도 완성된
``meta.json`` 이 없는 디렉터리는 피드에서 보이지 않게 하여 부분 기록을 공개하지
않는다.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from itertools import count
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from host.common.config import repo_path
from host.common.logging_setup import event_logger

if TYPE_CHECKING:
    from host.vision.detector import Detection
    from host.vision.tracker import Track

LOG = event_logger("mechadog.blackbox")
_SAFE_NAME = re.compile(r"[^A-Za-z0-9_.-]+")


@dataclass(frozen=True, slots=True)
class BlackboxEntry:
    """한 사건의 파일 위치와 JSON으로 직렬화 가능한 메타데이터."""

    ts_ms: int
    event_type: str
    state: str
    escalation: str
    tracks: list[dict[str, Any]]
    detections: list[dict[str, Any]]
    telemetry: dict[str, Any]
    jpeg_path: Path | None
    meta_path: Path


def _atomic_write(path: Path, payload: bytes) -> None:
    """같은 디렉터리의 임시 파일을 완성한 뒤 최종 이름으로 교체한다."""
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_bytes(payload)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


class EventBlackbox:
    """사건과 당시 상태를 디스크에 저장하고 시간순으로 조회한다."""

    def __init__(self, config: Mapping[str, Any]) -> None:
        logging_config = config.get("logging")
        if not isinstance(logging_config, Mapping):
            raise ValueError("logging 설정이 필요함")
        directory = logging_config.get("blackbox_dir")
        if not isinstance(directory, str) or not directory.strip():
            raise ValueError("logging.blackbox_dir 는 비어 있지 않은 문자열이어야 함")
        self._dir = repo_path(directory)
        self._dir.mkdir(parents=True, exist_ok=True)

    def _new_entry_dir(self, now_ms: int, event_type: str) -> Path:
        safe_event = (_SAFE_NAME.sub("_", event_type).strip("._") or "event")[:80]
        base = f"{now_ms}_{safe_event}"
        for suffix in count():
            candidate = self._dir / (base if suffix == 0 else f"{base}_{suffix}")
            try:
                candidate.mkdir()
            except FileExistsError:
                continue
            return candidate
        raise AssertionError("unreachable")

    def record(
        self,
        event_type: str,
        *,
        jpeg: bytes | None = None,
        tracks: Sequence[Track] = (),
        detections: Sequence[Detection] = (),
        telemetry: Mapping[str, Any] | None = None,
        state: str = "",
        escalation: str = "",
        now_ms: int,
    ) -> BlackboxEntry:
        """JPEG를 재인코딩하지 않고 사건의 모든 관측값과 함께 기록한다."""
        if not isinstance(event_type, str) or not event_type.strip():
            raise ValueError("event_type 은 비어 있지 않은 문자열이어야 함")
        if not isinstance(now_ms, int) or isinstance(now_ms, bool) or now_ms < 0:
            raise ValueError("now_ms 는 0 이상의 정수여야 함")

        tracks_data = [
            {"track_id": item.track_id, "box": list(item.box), "score": item.score}
            for item in tracks
        ]
        detections_data = [
            {"label": item.label, "score": item.score, "box": list(item.box)} for item in detections
        ]
        telemetry_data = deepcopy(dict(telemetry or {}))
        metadata: dict[str, Any] = {
            "ts_ms": now_ms,
            "event": event_type,
            "state": state,
            "escalation": escalation,
            "tracks": tracks_data,
            "detections": detections_data,
            "telemetry": telemetry_data,
        }

        entry_dir = self._new_entry_dir(now_ms, event_type)
        jpeg_path = entry_dir / "snapshot.jpg" if jpeg is not None else None
        meta_path = entry_dir / "meta.json"
        if jpeg_path is not None:
            _atomic_write(jpeg_path, jpeg)
        # meta.json을 마지막에 게시한다. 조회자는 이 파일이 없는 부분 기록을 무시한다.
        encoded = (json.dumps(metadata, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        _atomic_write(meta_path, encoded)

        LOG.info(
            "blackbox_recorded",
            event_type=event_type,
            ts_ms=now_ms,
            meta_path=str(meta_path),
        )
        return BlackboxEntry(
            ts_ms=now_ms,
            event_type=event_type,
            state=state,
            escalation=escalation,
            tracks=tracks_data,
            detections=detections_data,
            telemetry=telemetry_data,
            jpeg_path=jpeg_path,
            meta_path=meta_path,
        )

    def feed(self, since_ms: int = 0) -> list[BlackboxEntry]:
        """``since_ms``보다 뒤에 완성된 기록만 시간순으로 반환한다."""
        entries: list[BlackboxEntry] = []
        for directory in self._dir.iterdir():
            if not directory.is_dir():
                continue
            meta_path = directory / "meta.json"
            try:
                metadata = json.loads(meta_path.read_text(encoding="utf-8"))
            except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
                continue
            if not isinstance(metadata, dict):
                continue

            ts_ms = metadata.get("ts_ms")
            event_type = metadata.get("event")
            state = metadata.get("state")
            escalation = metadata.get("escalation")
            tracks = metadata.get("tracks")
            detections = metadata.get("detections")
            telemetry = metadata.get("telemetry")
            if (
                not isinstance(ts_ms, int)
                or isinstance(ts_ms, bool)
                or ts_ms <= since_ms
                or not isinstance(event_type, str)
                or not isinstance(state, str)
                or not isinstance(escalation, str)
                or not isinstance(tracks, list)
                or not isinstance(detections, list)
                or not isinstance(telemetry, dict)
            ):
                continue

            jpeg_path = directory / "snapshot.jpg"
            entries.append(
                BlackboxEntry(
                    ts_ms=ts_ms,
                    event_type=event_type,
                    state=state,
                    escalation=escalation,
                    tracks=tracks,
                    detections=detections,
                    telemetry=telemetry,
                    jpeg_path=jpeg_path if jpeg_path.is_file() else None,
                    meta_path=meta_path,
                )
            )
        entries.sort(key=lambda entry: (entry.ts_ms, str(entry.meta_path)))
        return entries

    @property
    def latest(self) -> BlackboxEntry | None:
        """가장 최근에 완성된 기록. 기록이 없으면 ``None``."""
        entries = self.feed()
        return entries[-1] if entries else None
