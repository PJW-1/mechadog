"""LiDAR 막힘 확정 프레임의 원인 판독 — «통로를 막은 것이 무너진 물건인가» (ADR-45).

감지와 우회는 LiDAR 가 맡는다(`PatrolController._check_new_obstacle`). 여기서는 막힘을 확정한
그 프레임에 VLM 질문 하나(`blocked_by_fallen`)를 걸고, 답을 `path_blocked` 판정 근거의 `fallen`
으로 싣는다. VLM 은 감지·방향을 정하지 않는다 (ADR-43 결정 3).

- 막지 않는다 — 걸어 두고 다음 틱에서 줍는다(`FallMonitor`·`ZoneInspector` 와 같다).
- 답을 받거나 `vision.vlm.path_cause_wait_ms` 에 닿으면 `path_blocked` 를 **한 번만** 남긴다.
  로봇은 재계획 때문에 어차피 서 있으므로 알림이 그만큼 늦는 것은 괜찮다.
- 워커의 결과 슬롯은 하나다. 쓰러짐·구역 판독이 걸려 있으면 상한 안에서 빌 때를 기다려
  같은 프레임을 걸고, 끝내 못 걸면 `fallen: null` 이다.
- 답은 스위치(`change_detect.vlm_path_cause`)와 상관없이 늘 남긴다 — 평가·학습 데이터다.
  방송 문장이 «무너진 물건» 을 말하는지는 판정 근거의 `vlm_path_cause` 를 보고
  `host/report/situation.py` 가 정한다 (ADR-41 `vlm_hazards` 와 같은 순서: 벤치 통과 → 스위치).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from host.behavior.mission import Mission
from host.common.logging_setup import event_logger
from host.vision.vlm_reader import Reading
from host.vision.vlm_worker import VlmWorker

#: 런타임과 같은 로거 이름을 쓴다.
LOG = event_logger("mechadog.runtime")

#: LiDAR 막힘 확정 프레임에 묻는 단 하나의 판독 항목 (`vlm_reader.QUESTIONS`).
PATH_CAUSE_KEY = "blocked_by_fallen"


class PathCause:
    """막힘 한 건의 원인 판독을 걸고 결과를 실어 `path_blocked` 를 남긴다.

    운용 루프 스레드에서만 부른다. `others_waiting` 이 참이면(쓰러짐·구역 판독이 걸려 있으면)
    걸지 않고, 이것이 기다리는 동안(`waiting`)에는 그쪽도 걸지 않는다.
    """

    def __init__(
        self,
        config: Mapping[str, Any],
        *,
        mission: Mission,
        vlm: VlmWorker,
        record: Callable[[str, Any, dict[str, Any]], None],
        announce: Callable[[str, dict[str, Any]], Any],
        others_waiting: Callable[[], bool],
    ) -> None:
        self._mission = mission
        self._vlm = vlm
        self._record = record
        self._announce = announce
        self._others_waiting = others_waiting
        self._wait_ms = int(config["vision"]["vlm"]["path_cause_wait_ms"])
        if self._wait_ms <= 0:
            raise ValueError(f"vision.vlm.path_cause_wait_ms 는 0 보다 커야 함: {self._wait_ms}")
        #: 받은 지 이보다 오래된 프레임은 막힘 확정 때의 장면이 아니다 — 비전이 끊겨도
        #: `VisionWorker.latest()` 는 마지막 결과를 돌려준다.
        self._max_frame_age_ms = int(config["vision"]["vlm"]["path_cause_max_frame_age_ms"])
        if self._max_frame_age_ms <= 0:
            raise ValueError(
                "vision.vlm.path_cause_max_frame_age_ms 는 0 보다 커야 함: "
                f"{self._max_frame_age_ms}"
            )
        #: 스위치. 판정 근거에 실어 **그때 문장이 원인을 말할 수 있었는지** 를 남긴다.
        self._announce_cause = bool(config["change_detect"]["vlm_path_cause"])
        #: 기다리는 막힘 `[판정 근거, 프레임, 확정 시각, 걸었나]`. `None` 이면 없다.
        self._pending: list[Any] | None = None
        #: 상한을 넘겨 먼저 남긴 뒤에도 돌고 있는 판독. 끝나면 주워 슬롯을 비운다 —
        #: 그대로 두면 다음에 건 쪽이 이 결과를 제 것으로 읽는다.
        self._draining = False

    @property
    def waiting(self) -> bool:
        """원인 판독을 걸었거나 걸려고 기다리는 중인가. 참이면 다른 판독은 걸지 않는다."""
        return self._pending is not None or self._draining

    def blocked(self, judgement: dict[str, Any], frame: Any, now_ms: int) -> None:
        """순찰 중 LiDAR 막힘 확정. 물을 수 없으면 바로 남기고, 물을 수 있으면 걸어 둔다."""
        judgement = {**judgement, "vlm_path_cause": self._announce_cause}
        if frame is None:
            reason = "no_frame"
        elif now_ms - frame.frame_received_ms > self._max_frame_age_ms:
            # 낡은 장면으로 묻지도, 그 사진을 남기지도 않는다 — `no_frame` 처럼 방송만 한다.
            reason = "stale_frame"
            frame = None
        elif not self._mission.enables("change_detect"):
            reason = "mission"  # 공장 모드에서만 VLM 을 묻는다
        elif not self._vlm.available:
            reason = "not_loaded"
        elif self._pending is not None:
            reason = "busy"  # 앞 막힘의 판독을 기다리는 중이다 — 이 건은 묻지 않는다
        else:
            self._pending = [judgement, frame, now_ms, False]
            self._submit(now_ms)
            return
        self._finish(judgement, frame, now_ms, now_ms, reading=None, reason=reason)

    def poll(self, now_ms: int) -> None:
        """틱마다 부른다 — 못 건 판독을 다시 걸고, 끝난 판독을 줍고, 상한을 본다."""
        if self._draining and not self._vlm.busy:
            self._draining = False
            late = self._vlm.take()
            LOG.info(
                "path_cause_late",
                fallen=late.get(PATH_CAUSE_KEY) if late is not None else None,
            )
        if self._pending is None:
            return
        judgement, frame, since, submitted = self._pending
        if not submitted and self._submit(now_ms):
            return
        if submitted and not self._vlm.busy:
            self._pending = None
            reading = self._vlm.take()
            reason = None if reading is not None else "worker_failed"
            self._finish(judgement, frame, since, now_ms, reading=reading, reason=reason)
            return
        if now_ms - since >= self._wait_ms:
            self._pending = None
            # 건 판독이 아직 돌면 끝날 때까지 슬롯을 쥔다(`_draining`). ⚠️ **못 건 막힘은 앞
            # 막힘의 배수를 지우지 않는다** — 지우면 그 낡은 답이 다음 막힘의 것으로 읽힌다.
            if submitted:
                self._draining = True
            reason = "timeout" if submitted else "busy"
            LOG.warning("path_cause_timeout", reason=reason, wait_ms=self._wait_ms)
            self._finish(judgement, frame, since, now_ms, reading=None, reason=reason)

    def _submit(self, now_ms: int) -> bool:
        pending = self._pending
        assert pending is not None
        # ⚠️ **앞 막힘의 늦은 판독을 비우기 전에는 걸지 않는다.** 워커는 끝난 스레드면 새 판독을
        # 받으므로, 걸면 새 답을 `poll` 이 늦은 판독으로 주워 버린다.
        if self._draining or self._others_waiting():
            return False
        if not self._vlm.submit(pending[1].jpeg, now_ms=now_ms, keys=(PATH_CAUSE_KEY,)):
            return False
        pending[3] = True
        LOG.info("path_cause_requested")
        return True

    def _finish(
        self,
        judgement: dict[str, Any],
        frame: Any,
        since: int,
        now_ms: int,
        *,
        reading: Reading | None,
        reason: str | None,
    ) -> None:
        """판정 근거에 원인을 싣고 `path_blocked` 를 한 번 남긴다."""
        answer = None
        if reading is not None:
            reason = reading.reason
            answer = next((a for a in reading.answers if a.key == PATH_CAUSE_KEY), None)
        judgement = {
            **judgement,
            # 참·거짓·모름(`None`)의 3값 — 모름을 거짓으로 접지 않는다.
            "fallen": answer.value if answer is not None else None,
            "vlm_reason": reason,
            # ⚠️ **원문을 함께 남긴다** — 판독이 이상할 때 사람이 볼 것은 모델이 뱉은 글이다.
            "raw": answer.raw if answer is not None else None,
            "latency_ms": answer.latency_ms if answer is not None else None,
            #: 막힘 확정부터 기록까지 — 알림이 이만큼 늦었다.
            "wait_ms": now_ms - since,
        }
        LOG.info(
            "path_cause",
            fallen=judgement["fallen"],
            reason=reason,
            wait_ms=judgement["wait_ms"],
        )
        if frame is not None:
            self._record("path_blocked", frame, judgement)
            return
        # 프레임이 없으면 사진 기록은 못 남기지만 관제가 들어야 할 경고는 그대로 낸다.
        self._announce("path_blocked", judgement)


__all__ = ["PATH_CAUSE_KEY", "PathCause"]
