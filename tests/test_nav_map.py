"""항법 지도(`NavigationMap`) 단위 시험 — 마스크·실시간 장애물·비용·국소 창을 순찰기 하나로 본다."""

from __future__ import annotations

import numpy as np
from test_live_nav import ready


def test_initial_masks_match_the_grid_and_have_no_live_obstacles() -> None:
    c = ready()
    navmap = c.navmap
    assert navmap.static is not None and navmap.body is not None and navmap.dynamic is not None
    assert navmap.static.shape == navmap.body.shape == c.grid.cells.shape
    assert navmap.frame == (
        c.grid.meta.resolution,
        c.grid.meta.origin_x,
        c.grid.meta.origin_y,
    )
    assert np.array_equal(c.blocked, navmap.static | navmap.dynamic)
    assert np.array_equal(c.body_blocked, navmap.body | navmap.dynamic)


def test_rebuild_marks_live_obstacles_and_resets_the_inflate_counter() -> None:
    c = ready()
    navmap = c.navmap
    navmap.updates_since_inflate = 3
    navmap.obstacles = [(3.0, 3.0)]
    navmap.rebuild_masks()
    assert navmap.dynamic is not None and navmap.dynamic[c.grid.to_cell(3.0, 3.0)]
    assert navmap.updates_since_inflate == 0
    assert c.obstacles == ((3.0, 3.0),)


def test_refresh_forgets_obstacles_after_their_ttl() -> None:
    c = ready()
    navmap = c.navmap
    old, fresh = c.grid.to_cell(3.0, 3.0), c.grid.to_cell(3.5, 3.0)
    navmap.dynamic_seen = {old: (3.0, 3.0, 0), fresh: (3.5, 3.0, 1000)}
    navmap.refresh(c.nav_params.dynamic_ttl_ms)
    assert set(navmap.dynamic_seen) == {fresh}
    assert navmap.obstacles == [(3.5, 3.0)]
    assert navmap.dynamic is not None and navmap.dynamic[fresh] and not navmap.dynamic[old]


def test_costs_are_cached_until_the_blocked_mask_changes() -> None:
    c = ready()
    first = c.navigation_costs
    assert c.navmap.costs is first
    c.navmap.obstacles = [(3.0, 3.0)]
    c.navmap.rebuild_masks()
    assert c.navmap.costs is not first


def test_local_window_and_path_clear_follow_the_body_mask() -> None:
    c = ready()
    window = c.local_costmap
    assert window == c.navmap.local_window
    assert window["width"] == len(window["blocked"][0]) and window["height"] == len(
        window["blocked"]
    )
    assert c.navmap.path_clear((3.0, 2.0))
    assert c.navmap.dynamic is not None
    c.navmap.dynamic[c.grid.to_cell(2.5, 2.0)] = True
    assert not c.navmap.path_clear((3.0, 2.0))


def test_new_obstacles_and_pending_hit_reach_the_controller() -> None:
    c = ready()
    assert not c.obstacle_pending
    c.navmap.pending_hit = (2.5, 2.0)
    assert c.obstacle_pending
    c.navmap.new_obstacles.append((2.5, 2.0))
    assert c.take_new_obstacles() == ((2.5, 2.0),)
    assert c.take_new_obstacles() == () and c.navmap.new_obstacles == []
