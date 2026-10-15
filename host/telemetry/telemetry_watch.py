"""텔레메트리 진단 — 받은 값에서 각속도·명령 폐기·추종 중 막힘을 읽어 **기록만** 남긴다.

판단(FSM·지시)은 아무것도 바꾸지 않는다. 모든 메서드는 운용 루프 스레드의
`Runtime.ingest` 가 우리 로봇의 텔레메트리를 받아들인 뒤 부른다.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any

from host.common.logging_setup import EdgeTrigger, PeriodicSummary, event_logger

if TYPE_CHECKING:
    from host.behavior.fsm import Behavior

#: 런타임과 같은 로거 이름을 쓴다 — 로그 레코드가 옮기기 전과 같아야 한다.
LOG = event_logger("mechadog.runtime")


class TelemetryWatch:
    """IMU 각속도 요약, 로봇의 명령 폐기 경고, 추종 중 막힘 구간 기록을 맡는다."""

    #: 명령이 무시되는 상태가 이만큼 이어지면 경고한다. 한 번 튄 것으로 떠들지 않는다.
    _link_ignored_warn_ms = 1000

    def __init__(
        self,
        config: Mapping[str, Any],
        *,
        behavior: Behavior,
        summary: PeriodicSummary,
        session_pending: Callable[[], bool],
    ) -> None:
        self._behavior = behavior
        self._summary = summary
        #: 세션 개시 전문이 아직 나가지 않았다 — 그동안은 명령 폐기를 따지지 않는다.
        self._session_pending = session_pending
        self._cmd_timeout_ms = int(config["safety"]["cmd_timeout_ms"])
        self._ignored_since_ms: int | None = None
        # 직전 IMU 방위와 그 시각. 각속도는 차분이라 표본 하나를 들고 있어야 한다.
        self._last_yaw: tuple[float, int] | None = None
        self._blocked_since_ms: int | None = None
        self._edge = EdgeTrigger()
        # 정상값을 미리 심는다 — 첫 관측이 변화로 잡히지 않게.
        self._edge.changed("track_blocked", False)

    def observe_yaw_rate(self, yaw: float | None, now_ms: int) -> None:
        """IMU 방위를 **각속도**로 바꿔 1초 요약에 싣는다 (좌우 대칭 근거).

        ⚠️ **yaw 를 그대로 평균 내면 안 된다.** 0~360 이라 경계에서 뒤집히고
        (359° 다음 1° 가 −358° 로 읽힌다), 좌우 대칭을 보려면 필요한 것은 방위가
        아니라 *"얼마나 빨리 도는가"* 다. 그래서 차분을 ±180 으로 접어 시간으로 나눈다.

        ⚠️ **부호를 살린다.** `track_dev_px` 는 좌우 진동을 감추지 않으려고 절대값을
        쓰지만 여기는 반대다 — **어느 쪽으로 돌았는지가 재려는 것 자체다.** 화면
        편차(px/s)는 대상까지의 거리에 종속돼 같은 런 안에서도 두 배씩 달라진다.
        """
        if yaw is None:
            return
        previous = self._last_yaw
        self._last_yaw = (yaw, now_ms)
        if previous is None:
            return
        prev_yaw, prev_ms = previous
        elapsed_ms = now_ms - prev_ms
        # 끊긴 구간을 가로질러 재지 않는다 — 텔레메트리가 10Hz 이므로 1초를 넘는
        # 간격은 공백이고, 그 사이의 회전은 이 두 표본으로 복원되지 않는다.
        if not 0 < elapsed_ms <= 1000:
            return
        delta_deg = (yaw - prev_yaw + 180.0) % 360.0 - 180.0
        self._summary.observe("yaw_rate_deg_s", delta_deg * 1000.0 / elapsed_ms)

    def watch_command_uptake(self, age_ms: int | None, now_ms: int) -> None:
        """⚠️ **로봇이 우리 명령을 폐기하고 있는지 본다.**

        10Hz 로 보내는데 로봇이 보고하는 *마지막 수락 명령의 나이*가 명령 타임아웃을
        계속 넘으면, 패킷은 나가는데 로봇이 받아들이지 않는 것이다. 호스트 재시작으로
        seq 가 되돌아간 경우가 대표적이며 **조용하다** — 우리 통계에는 송신 성공으로
        잡히고 로봇만 폐기 카운터를 올린다. 그래서 여기서 큰 소리를 낸다.
        """
        if age_ms is None or self._session_pending():
            return
        if age_ms <= self._cmd_timeout_ms:
            self._ignored_since_ms = None
            return
        if self._ignored_since_ms is None:
            self._ignored_since_ms = now_ms
            return
        if now_ms - self._ignored_since_ms >= self._link_ignored_warn_ms:
            self._ignored_since_ms = now_ms
            LOG.warning(
                "commands_ignored",
                last_cmd_age_ms=age_ms,
                hint="세션 개시 실패 가능 (PROTOCOL 4절)",
            )

    def watch_track_blocked(self, reading: Any, now_ms: int) -> None:
        """추종 중에 온보드가 전진을 거부한 구간을 기록한다 (설계 규칙 ④ · FR-2.2).

        ⚠️ **판단은 아무것도 바꾸지 않는다 — 기록만 붙인다.** `AVOID` 는 `PATROL`
        에서만 열리는 것이 의도이고(추종 중에 회피를 돌리면 카메라가 대상을 놓쳐
        회피가 임무를 취소한다), 그래서 추종 중 막힘에는 **대응할 상태가 없다.**
        문제는 대응이 없는 것이 아니라 **일어난 줄도 몰랐다**는 것이다.

        실제로 일어나는 일 — 온보드가 **전진만** 거부하고 호 조향은 전진이
        있어야 돌므로 로봇은 **선 채로** 멈춘다. 호스트는 `TRACK` 을 유지하며 지시를
        계속 보내고, 대상이 계속 보이니 `TARGET_LOST` 도 돌지 않아 **그 자리에
        머문다.** 안전한 정지이지만 «왜 멈췄는지» 가 로그에 없다.

        ⚠️ **실기에서 운용자가 이것을 눈으로 봤다** — 순찰 중 사람을 발견한
        로봇이 **코앞(온보드 정지 거리)까지 와서** 섰다. 원인은 `FR-3.5.2`(거리 유지)가
        **미구현**이라 편차가 데드존 밖이면 거리와 무관하게 최대 보폭으로 전진하고,
        멈추는 것이 초음파 반사뿐이기 때문이다. **Tier 1 안전 반사가 거리 제어를
        대신하고 있다.** 이 로그가 그 빈도와 거리를 남겨 `FR-3.5.2` 의 목표값을 정할
        근거가 된다.

        ⚠️ **변화 때만 낸다.** 막힌 동안 10Hz 로 같은 줄을 쏟으면 그 로그가 런을 덮는다.
        """
        blocked = bool(reading.obstacle) and self._behavior.tracking
        if not self._edge.changed("track_blocked", blocked):
            return
        if blocked:
            self._blocked_since_ms = now_ms
            LOG.warning(
                "track_blocked",
                state=self._behavior.state,
                dist_cm=reading.dist_cm,
            )
            return
        since = self._blocked_since_ms
        self._blocked_since_ms = None
        LOG.info(
            "track_unblocked",
            state=self._behavior.state,
            blocked_ms=None if since is None else now_ms - since,
        )
