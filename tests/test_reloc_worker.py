"""전역 탐색 워커(`GlobalMatchWorker`) 단위 시험 — 탐색 함수는 가짜로 넣는다."""

from __future__ import annotations

import threading
import time

import numpy as np

from host.behavior.reloc_worker import GlobalMatchWorker
from host.common.lidar_link import Scan
from host.slam.occupancy import MapMeta, OccupancyGrid
from host.slam.scan_match import MatchResult

GRID = OccupancyGrid(MapMeta(0.05, 0.0, 0.0, 4, 4), np.zeros((4, 4), dtype=np.float32))
SCAN = Scan("dev", "boot", 1, 0, ((0.0, 1.0),))
POINTS = np.zeros((3, 2))


def wait_for_result(worker: GlobalMatchWorker, timeout_s: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_s
    while worker.result is None and time.monotonic() < deadline:
        time.sleep(0.01)


def test_take_is_empty_until_a_result_arrives() -> None:
    worker = GlobalMatchWorker(lambda *_: None, lambda *_: MatchResult((0.0, 0.0, 0.0), 1))
    worker.inflight = True
    assert worker.take() == (None, None)
    assert worker.inflight is True, "결과가 없으면 진행 중 표시를 풀지 않는다"


def test_submit_runs_the_search_and_restore_on_the_worker_thread() -> None:
    seen: list[tuple[str, object, object, object]] = []

    def search(grid: OccupancyGrid, points: np.ndarray, allowed: object) -> MatchResult:
        seen.append((threading.current_thread().name, grid, points, allowed))
        return MatchResult((1.0, 2.0, 0.5), 7, peers=3)

    def restore(grid: OccupancyGrid, points: np.ndarray, prior: object) -> MatchResult:
        seen.append((threading.current_thread().name, grid, points, prior))
        return MatchResult((1.1, 2.0, 0.5), 6)

    def allowed(xs: np.ndarray, _ys: np.ndarray) -> np.ndarray:
        return np.ones_like(xs, dtype=bool)

    worker = GlobalMatchWorker(search, restore)
    worker.submit(
        ("reloc", POINTS, SCAN, (0.0, 0.0, 0.0), GRID), (None, 4, 2, 1000), (1.0, 2.0, 0.5), allowed
    )
    assert worker.inflight is True
    assert worker.context == (None, 4, 2, 1000)
    wait_for_result(worker)
    done, prior_result = worker.take()
    assert done is not None
    kind, result, points, scan, asked = done
    assert (kind, scan, asked) == ("reloc", SCAN, (0.0, 0.0, 0.0))
    assert points is POINTS
    assert result == MatchResult((1.0, 2.0, 0.5), 7, peers=3)
    assert prior_result == MatchResult((1.1, 2.0, 0.5), 6)
    assert worker.inflight is False
    assert [name for name, *_ in seen] == ["patrol-global-match"] * 2
    assert seen[0][1] is GRID and seen[0][2] is POINTS and seen[0][3] is allowed
    assert seen[1][1] is GRID and seen[1][3] == (1.0, 2.0, 0.5)
    assert (
        worker.request is None and worker.request_prior is None and worker.request_allowed is None
    )


def test_search_failure_still_posts_a_result(caplog) -> None:
    def broken(*_: object) -> MatchResult:
        raise ValueError("boom")

    restored: list[object] = []
    worker = GlobalMatchWorker(broken, lambda *args: restored.append(args))
    worker.submit(
        ("verify", POINTS, SCAN, (0.0, 0.0, 0.0), GRID), (None, 0, 0, 0), (0.0, 0.0, 0.0), None
    )
    wait_for_result(worker)
    done, prior_result = worker.take()
    assert done is not None and done[1] is None, "결과를 두지 않으면 inflight 가 영영 안 풀린다"
    assert prior_result is None
    assert restored == [], "전역 결과가 없으면 창 안 정합도 하지 않는다"
    assert "global_match_failed" in caplog.text


def test_restore_failure_keeps_the_global_result(caplog) -> None:
    def broken(*_: object) -> MatchResult:
        raise RuntimeError("restore")

    worker = GlobalMatchWorker(lambda *_: MatchResult((0.0, 0.0, 0.0), 5), broken)
    worker.submit(
        ("reloc", POINTS, SCAN, (0.0, 0.0, 0.0), GRID), (None, 0, 0, 0), (0.0, 0.0, 0.0), None
    )
    wait_for_result(worker)
    done, prior_result = worker.take()
    assert done is not None and done[1] == MatchResult((0.0, 0.0, 0.0), 5)
    assert prior_result is None
    assert "restore_match_failed" in caplog.text


def test_worker_thread_is_started_once() -> None:
    worker = GlobalMatchWorker(lambda *_: None, lambda *_: MatchResult((0.0, 0.0, 0.0), 1))
    worker.ensure_started()
    first = worker.thread
    worker.ensure_started()
    assert worker.thread is first and first is not None and first.daemon
