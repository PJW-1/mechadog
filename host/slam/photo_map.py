"""Attach fresh camera observations to accepted LiDAR map poses.

Photos mark the *camera pose*, not a detected object's world position. Camera
intrinsics/extrinsics have not been measured, so projecting boxes would invent
coordinates. This module sends no robot commands.
"""

from __future__ import annotations

import html
import json
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from host.common.protocol import system_clock_ms
from host.vision.stream_client import Frame, FrameQueue, StreamEndpoints, StreamReader


@dataclass(frozen=True)
class PhotoPose:
    step: int
    x_m: float
    y_m: float
    yaw_rad: float
    scan_received_ms: int
    frame_received_ms: int
    skew_ms: int
    photo: str


class PhotoRecorder:
    """Receive only the newest JPEG in a bounded background thread."""

    def __init__(self, config: dict[str, Any], url: str, *, max_skew_ms: int = 500) -> None:
        # Reuse the camera check's URL/redirect policy; the stream is read-only.
        from tools.camera_link_check import open_stream, validate_url

        url = validate_url(url)
        self._queue = FrameQueue(capacity=1)
        self._reader = StreamReader(
            config,
            endpoints=StreamEndpoints(control="", stream=url),
            opener=lambda target, timeout: open_stream(target, timeout=timeout),
        )
        self._max_skew_ms = max_skew_ms
        self._thread: threading.Thread | None = None
        self._latest: Frame | None = None
        self._records: list[PhotoPose] = []

    def start(self) -> None:
        self._thread = threading.Thread(target=self._receive, name="map-camera", daemon=True)
        self._thread.start()

    def _receive(self) -> None:
        for frame in self._reader.frames():
            self._queue.put(frame)

    def sample(self, scan_received_ms: int) -> Frame | None:
        """Choose a frame *before* scan matching can delay the mapping loop."""
        frame = self._queue.latest(now_ms=system_clock_ms())
        if frame is not None:
            self._latest = frame
        frame = self._latest
        if frame is None or not frame.looks_like_jpeg:
            return None
        return frame if abs(frame.received_ms - scan_received_ms) <= self._max_skew_ms else None

    def capture(
        self,
        directory: Path,
        step: int,
        pose: tuple[float, float, float],
        scan_received_ms: int,
        frame: Frame,
    ) -> None:
        skew_ms = abs(frame.received_ms - scan_received_ms)
        photos = directory / "photos"
        photos.mkdir(parents=True, exist_ok=True)
        relative = f"photos/step-{step:04d}.jpg"
        (directory / relative).write_bytes(frame.payload)
        self._records.append(
            PhotoPose(step, *pose, scan_received_ms, frame.received_ms, skew_ms, relative)
        )

    def close(self) -> None:
        self._reader.stop()
        if self._thread is not None:
            self._thread.join(timeout=3.0)

    def save(
        self,
        directory: Path,
        extent: list[float],
        *,
        has_png: bool,
        image_name: str = "slam_map.png",
    ) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "photo_poses.json").write_text(
            json.dumps(
                {
                    "kind": "camera_pose_photos_not_object_locations",
                    "max_skew_ms": self._max_skew_ms,
                    "records": [asdict(record) for record in self._records],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        if not has_png:
            return
        left, right, bottom, top = extent
        ratio = (right - left) / (top - bottom)
        pins = []
        for item in self._records:
            x = 100 * (item.x_m - left) / (right - left)
            y = 100 * (top - item.y_m) / (top - bottom)
            photo = html.escape(item.photo, quote=True)
            pins.append(
                f'<a href="{photo}" target="_blank" style="left:{x:.4f}%;top:{y:.4f}%" '
                f'title="촬영 위치 {item.step}, 시각 차이 {item.skew_ms}ms">{item.step}</a>'
            )
        page = (
            '<!doctype html><html lang="ko"><meta charset="utf-8"><title>LiDAR 사진 지도</title>'
            "<style>body{font:16px sans-serif;max-width:900px;margin:auto;padding:1rem;background:#181c24;color:#fff}"
            f".map{{position:relative;width:min(90vw,750px);aspect-ratio:{ratio:.5f};}}"
            ".map img{width:100%;height:100%;image-rendering:pixelated}"
            ".map a{position:absolute;transform:translate(-50%,-50%);background:#ed6c35;color:white;"
            "border-radius:50%;padding:.5rem;text-decoration:none}</style>"
            "<h1>LiDAR 지도와 촬영 위치</h1><p>번호를 누르면 해당 위치에서 받은 사진이 열립니다. "
            "사진 속 물체의 지도 좌표를 뜻하지 않습니다. 회색은 미관측 영역입니다.</p>"
            f'<div class="map"><img src="{html.escape(image_name, quote=True)}" alt="LiDAR 점유격자 지도">'
            + "".join(pins)
            + "</div></html>"
        )
        (directory / "photo_map.html").write_text(page, encoding="utf-8")
