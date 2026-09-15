"""사원증 마커 읽기 (WBS 3.8.1 · FR-10.1).

**픽셀만 다룬다.** 승인 여부·세션·시도 횟수는 `behavior/auth.py` 가 판정한다 —
`detector.py`(픽셀)와 `person.py`(판정)를 나눈 것과 같은 선이다. 그 선이 있어야
`worker` 가 `behavior` 를 import 하지 않는다(방향이 거꾸로가 된다).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class Marker:
    """프레임에서 읽은 마커 하나. **판정은 들어 있지 않다.**"""

    marker_id: int
    #: 마커 중심. 어느 사람의 것인지 정하는 데 쓴다.
    center: tuple[float, float]


class BadgeReader:
    """프레임에서 ArUco 마커를 찾는다. **승인 여부는 판정하지 않는다.**

    ⚠️ **사전(dictionary)을 바꾸면 인쇄한 사원증도 바꿔야 한다.** 그래서 설정에
    두고 모르는 이름은 거부한다 — 검출기의 `model_family` 와 같은 방식이다.

    `DICT_4X4_50` 이 기본인 이유: 비트가 적을수록 작게·멀리서도 읽힌다. 사원증은
    1~3m 에서 VGA 로 읽어야 하고 필요한 ID 는 팀 규모만큼이다.
    """

    def __init__(self, config: Mapping[str, Any]) -> None:
        import cv2  # 지연 import — 세션 관리 시험이 OpenCV 를 요구하지 않게 한다

        name = str(config["auth"]["badge_dictionary"])
        code = getattr(cv2.aruco, name, None)
        if code is None or not isinstance(code, int):
            raise ValueError(f"auth.badge_dictionary 를 cv2.aruco 에서 찾을 수 없다: {name!r}")
        self._detector = cv2.aruco.ArucoDetector(
            cv2.aruco.getPredefinedDictionary(code),
            cv2.aruco.DetectorParameters(),
        )

    def read(self, image: Any) -> tuple[Marker, ...]:
        """마커를 찾는다. 없으면 빈 튜플.

        ⚠️ **호출부가 게이팅한다** — 추적 대상이 없는 프레임에서는 부르지 않는다
        (FR-3.1.1 과 같은 원칙). 마커 없는 VGA 프레임에 0.75ms 가 든다.
        """
        corners, ids, _rejected = self._detector.detectMarkers(image)
        if ids is None or len(ids) == 0:
            return ()
        markers: list[Marker] = []
        for marker_id, quad in zip(ids.flatten().tolist(), corners, strict=True):
            points = quad.reshape(-1, 2)
            markers.append(
                Marker(
                    marker_id=int(marker_id),
                    center=(float(points[:, 0].mean()), float(points[:, 1].mean())),
                )
            )
        return tuple(markers)
