import numpy as np
import pytest

from tools.maps.audit_clearance import rectangle_distance


def test_clearance_uses_cell_edges_not_centres():
    box = np.array([[0.15, -0.025, 0.20, 0.025]])
    assert rectangle_distance((0, 0), (0, 0), box)[0] == pytest.approx(0.15)


def test_segment_crossing_or_touching_obstacle_has_zero_clearance():
    box = np.array([[1, 1, 2, 2]])
    assert rectangle_distance((0, 0), (3, 3), box)[0] == 0
    assert rectangle_distance((0, 1), (3, 1), box)[0] == 0


def test_parallel_segment_and_diagonal_corner_clearance():
    box = np.array([[1, 1, 2, 2]])
    assert rectangle_distance((0, 0), (3, 0), box)[0] == pytest.approx(1)
    assert rectangle_distance((0, 0), (0, 0), box)[0] == pytest.approx(2**0.5)
