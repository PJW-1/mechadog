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
    #: 운용 모드 (FR-11.5). **같은 `person` 검출이 모드에 따라 다른 결과를
    #: 낳으므로 모드 없이는 판단 근거를 되짚을 수 없다.**
    mode: str
    tracks: list[dict[str, Any]]
    detections: list[dict[str, Any]]
    telemetry: dict[str, Any]
    #: 그릴 수 없는 판단 근거 — 쓰러짐 판정(`4.8.3`)·VLM 판독(`4.8.0`).
    #: 박스는 사진 위에 그리면 보이지만 이것들은 읽어야만 알 수 있다.
    judgement: dict[str, Any]
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
        mode: str = "",
        judgement: Mapping[str, Any] | None = None,
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
        # ⚠️ **판단 근거를 사진과 같은 자리에 둔다.** 검출 박스는 그려 보면 알지만
        # 쓰러짐 판정이나 VLM 답은 **그릴 것이 없어서** 숫자와 문장으로만 남는다.
        # 사진 옆에 없으면 나중에 *"왜 그렇게 판정했나"* 를 되짚을 수 없다.
        judgement_data = deepcopy(dict(judgement or {}))
        metadata: dict[str, Any] = {
            "ts_ms": now_ms,
            "event": event_type,
            "state": state,
            "escalation": escalation,
            "mode": mode,
            "tracks": tracks_data,
            "detections": detections_data,
            "telemetry": telemetry_data,
            "judgement": judgement_data,
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
            mode=mode,
            tracks=tracks_data,
            detections=detections_data,
            telemetry=telemetry_data,
            judgement=judgement_data,
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
            # ⚠️ **옛 기록에는 없다.** `3.4.4` 이전에 쌓인 것을 읽을 수 없게 만들면
            # 모드를 넣은 대가로 과거 사건을 잃는다 — 빈 문자열로 두고 «모르는 모드»
            # 로 읽히게 한다. 필수로 요구하는 쪽은 새로 쓰는 자리다.
            mode = metadata.get("mode", "")
            tracks = metadata.get("tracks")
            detections = metadata.get("detections")
            telemetry = metadata.get("telemetry")
            # ⚠️ **옛 기록에는 없다** — `mode` 와 같은 이유로 빈 것으로 읽는다.
            judgement = metadata.get("judgement")
            if (
                not isinstance(ts_ms, int)
                or isinstance(ts_ms, bool)
                or ts_ms <= since_ms
                or not isinstance(event_type, str)
                or not isinstance(state, str)
                or not isinstance(escalation, str)
                or not isinstance(mode, str)
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
                    mode=mode,
                    tracks=tracks,
                    detections=detections,
                    telemetry=telemetry,
                    # ⚠️ **없으면 빈 것으로 읽는다.** `judgement` 가 생기기 전에 남은
                    # 기록이 이미 디스크에 있고, 그것들을 버리면 과거가 사라진다.
                    judgement=judgement if isinstance(judgement, dict) else {},
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

    def snapshot_bytes(self, entry: str) -> bytes | None:
        """기록 디렉터리 이름 하나로 그 사건의 JPEG 를 읽는다. 없으면 ``None``.

        ⚠️ **이름은 브라우저에서 온다.** 사건 전문에는 절대 경로 대신 디렉터리 이름만
        싣는데(`4.4.3`), 화면이 그림을 보려면 그 이름으로 되돌아 찾아야 한다. 즉 이
        함수의 입력은 **바깥에서 오는 문자열**이므로 경로로 쓰기 전에 잘라야 한다.

        ⚠️ **검증을 부르는 쪽에 두지 않는다.** 저장 구조를 아는 것은 이 클래스뿐이고,
        서버가 경로를 조립하게 하면 규칙이 두 곳에 생긴다 — 한쪽만 고쳐지는 순간
        디렉터리 밖 파일이 열린다.

        막는 것 셋 — ① 경로 구분자와 `..` 가 든 이름 ② 빈 이름·숨김 이름
        ③ 심볼릭 링크 등으로 기록 폴더 **밖을 가리키게 된 결과 경로**.
        """
        if not entry or entry.startswith(".") or entry != Path(entry).name:
            return None
        target = (self._dir / entry / "snapshot.jpg").resolve()
        try:
            target.relative_to(self._dir.resolve())
        except ValueError:
            return None
        if not target.is_file():
            return None
        return target.read_bytes()
