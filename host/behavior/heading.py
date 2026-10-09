"""IMU 방위 앵커 — 스캔 사이의 지도 방위와 정합의 회전 예측 (순찰 측위 보조).

정합 방위는 LiDAR 바퀴(수백 ms)마다만 갱신된다. 그 사이의 휨은 IMU 가 즉시 보지만
IMU 절대 yaw 는 지도 좌표계와 옵셋이 다르다. 여기서는 두 기준을 쥔다.

- **조향 옵셋** — 정합될 때마다 `IMU − 지도 방위` 를 다시 맞춰, 스캔 사이에는
  `IMU − 옵셋` 을 조향 방위로 쓴다 (`steering_yaw`).
- **회전 앵커** — 지금 `pose` 가 관측된 시점의 IMU. 다음 정합의 회전 예측은
  «지금 IMU − 앵커» 다. 정합이 실패해도 앵커는 그대로라 회전량을 잃지 않는다.

상태 변경은 순찰 루프 스레드에서만 일어난다 (`PatrolController` 와 같은 규칙).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from host.common.units import wrap_pi

if TYPE_CHECKING:
    from host.behavior.patrol import PatrolController


class HeadingTracker:
    """순찰기의 IMU 방위 앵커와 조향 옵셋을 쥔다.

    안전 관측(`safety`)·자세(`pose`)·신선도 한도(`imu_fresh_ms`)는 순찰기의 것을 그때그때
    읽는다 — 텔레메트리마다 `safety` 가 새 객체로 바뀌기 때문이다.
    """

    def __init__(self, patrol: PatrolController) -> None:
        self._patrol = patrol
        #: 마지막 정합 자세와 그때의 IMU yaw 의 차이 — 스캔 사이에 IMU 로 방위를 전파할 때
        #: 지도 좌표계로 되돌리는 옵셋이다. 정합될 때마다 다시 맞춘다.
        self.offset: float | None = None
        #: 지금 `pose` 가 관측된 시점의 IMU yaw — 다음 정합의 회전 예측은 «지금 IMU − 이 값».
        #: 정합이 실패해도 앵커는 그대로라 회전량을 잃지 않고, 전역 채택 때는 그 스캔 시점의
        #: IMU 로 다시 묶어 이중 반영하지 않는다.
        self.anchor: float | None = None
        #: 이번 스캔의 IMU 변화량이 실제 측정인가 (신선한 텔레메트리 기준).
        self.delta_fresh: bool = False

    def imu_is_fresh(self, now_ms: int) -> bool:
        safety = self._patrol.safety
        seen = safety.last_seen_ms
        return (
            safety.yaw_rad is not None
            and seen is not None
            and 0 <= now_ms - seen <= self._patrol.imu_fresh_ms
        )

    def anchor_pose(self, now_ms: int) -> float | None:
        """새 자세를 관측했다 — 앵커를 지금 IMU 로 다시 묶고 그 값을 돌려준다.

        앵커는 **신선한** IMU 일 때만 — 끊긴 IMU 의 옛 값을 묶어 두면 재개 때 그사이 회전
        (LiDAR 가 이미 pose 에 반영한 것)을 다시 더한다.
        """
        self.anchor = self._patrol.safety.yaw_rad if self.imu_is_fresh(now_ms) else None
        return self.anchor

    def align_offset(self) -> None:
        """IMU 절대 yaw 와 지도 방위의 옵셋을 정합 시점에 맞춘다.

        맞춰 두면 다음 스캔 전까지 `imu - offset` 이 지도 방위의 연속 추정치가 된다.
        """
        imu = self._patrol.safety.yaw_rad
        if imu is not None:
            self.offset = wrap_pi(imu - self._patrol.pose[2])

    def adopt(self, request_imu: float | None) -> None:
        """전역 결과로 자세를 바꾼 직후 — 앵커와 조향 옵셋을 **요청 시점** IMU 로 함께 맞춘다.

        `observe_map_pose` 는 현재 IMU 로 옵셋을 만들지만, 자세는 요청 때 스캔의 것이다.
        둘이 다르면 다음 정합 실패 동안 조향 방위가 요청 이후 회전을 빠뜨린다.
        """
        self.anchor = request_imu
        if request_imu is not None:
            self.offset = wrap_pi(request_imu - self._patrol.pose[2])

    def consume_yaw_delta(self, now_ms: int | None = None) -> float:
        """직전 스캔 이후 IMU 가 본 회전량을 소비한다(두 번 반영하지 않는다). 없으면 0.

        `delta_fresh` 는 이 변화량이 신선한 텔레메트리 두 표본의 차일 때만 참이다 —
        참이면 국소 정합이 방위를 IMU 예측 근처(`imu_match_params`)에서만 찾는다.
        """
        safety = self._patrol.safety
        current = safety.yaw_rad
        seen = safety.last_seen_ms
        fresh = (
            current is not None
            and now_ms is not None
            and seen is not None
            and 0 <= now_ms - seen <= self._patrol.imu_fresh_ms
        )
        anchor = self.anchor
        if current is None or anchor is None:
            self.delta_fresh = False
            if self.anchor is None:
                # 아직 앵커가 없으면 지금 값으로 — 다음부터 회전량이 이어진다.
                self.anchor = current
            return 0.0
        # 앵커(지금 `pose` 의 관측 시점) 이후 회전량 — 정합 실패가 이어져도 누적이 남는다.
        self.delta_fresh = bool(fresh)
        return wrap_pi(current - anchor)

    def steering_yaw(self) -> float:
        """조향에 쓸 방위 — 스캔 사이에는 IMU 전파값, 없으면 정합 방위 그대로.

        정합 방위는 바퀴(수백 ms)마다만 갱신되므로 그 사이의 휨을 IMU 가 즉시 본다.
        옵셋은 `observe_map_pose` 에서 정합될 때마다 다시 맞춘다.
        """
        imu = self._patrol.safety.yaw_rad
        if imu is not None and self.offset is not None:
            return wrap_pi(imu - self.offset)
        return self._patrol.pose[2]
