# Evidence map tools

These offline tools read local input files. They do not connect to devices, start
servers, commit changes, or select a deployment map.

```powershell
python -m tools.maps.build_house_map --evidence <local-spec.json> --output <new-folder> --diagnostics <new-diagnostics-folder>
python -m tools.maps.compare_localization --baseline <reference-folder> --candidate <candidate-folder> --sessions <session-folders> --every 100 --max-samples 0 --output <new-report.json>
python -m tools.maps.audit_clearance --maps <candidate-folder> --output <new-clearance-report.json> --tracking-error 0.05
python -m pytest tools/maps/test_map_geometry.py tools/maps/test_clearance_geometry.py -q
```

The local spec supplies source folders, navigation floor rectangles, wall and
keepout polygons, provisional furniture rectangles, symbolic zone ids and room
anchor rectangles. Geometry and household identifiers belong in that local
spec, not this directory. Source grid values are log odds, not probabilities.
The target grid and pose frame come from the supplied baseline.

`recent_patches` must have identical grid metadata and pose frames. Actual free
rays can fill previously unknown navigation cells only when
`fill_navigation_unknown` is explicitly true; navigation occupied observations
remain occupied inside the navigation vicinity. Localization snapshots replace
observed cells and record both occupancy/free conflict masks. Such conflicts
are evidence to audit, not automatic proof that either source is correct.

`retain_measured_returns_outside_navigation` permits sensor reference returns
outside navigable room boundaries, while candidate pose origins and zone labels
remain inside the supplied floor. `anchor_inset_m` limits anchors to room
interiors. Provisional furniture and drawing walls are navigation keepouts;
they are not synthetic localization returns. No unknown/free interpolation is
performed. A labelled floor cell is not automatically a body-safe robot pose.

The builder saves input hashes and `navigation_ready: false`. A reviewer must
assess routes, body masks, localization replay and simulation events before
runtime integration. The local comparator uses recorded algorithm poses rather
than independent truth. Use `--max-samples 0` to assess late valid references;
a small selected-scan limit can miss them. Separate training indices from replay
indices and disclose dependence on the reference map and recorded poses.

Optional `navigation_observation_keepouts` entries supply `mask`,
`endpoint_support`, `map_meta` and `pose_frame` files, with
`minimum_distinct_endpoint_scans` at least two. Matching grid/frame and repeated
registered endpoints are required. Only eligible unknown navigation cells become
conservative binary keepouts; known free cells are never changed by this step.
Repeated endpoints with conflicting free rays do not establish physical truth.
This retains the existing body safety policy and may change planned routes.

The clearance auditor measures continuous segment distances to closed cell
rectangles and the unsafe map exterior. It requires both body radius plus
tracking error from nonfree cells and tracking error from the existing inflated
body mask. The body radius must not be added again to that mask. These are static
map clearances, not independent physical measurements or a sensor-loop patrol
success. Existing output files are rejected rather than overwritten.

## Stationary marker refinement

`refine_from_markers` reads saved runtime `events.jsonl` scan records (`kind`,
`t` in epoch milliseconds, `raw` as the original wire string). It never polls a
live API. Supply independently measured **LiDAR-centre** positions in the
**patrol** coordinate frame, in metres with yaw in **radians**. Verify physical
standstill, levelness and laser extrinsics before choosing the windows; a frozen
localization estimate cannot establish them. Trim moving/settling boundaries.

Example truth schema (synthetic values, inclusive, ordered nonoverlapping windows):

```json
{
  "frame_id": "patrol",
  "pose_origin": "lidar_centre",
  "yaw_unit": "radians",
  "truth_source": "measured_markers",
  "intervals": [
    {"start_ms": 1800000000000, "end_ms": 1800000006000, "x": 0, "y": 0, "yaw": 0},
    {"start_ms": 1800000010000, "end_ms": 1800000016000, "x": 0.3, "y": 0, "yaw": 0}
  ]
}
```

Rectangles JSON is a nonempty list of `[min_x, min_y, max_x, max_y]` in patrol
metres, or `{"frame_id":"patrol","allowed_rectangles_m":[...]}`. Only cells
wholly contained in a rectangle can change. Boundary-crossing cells and all
exterior cells keep their exact original values. An expanded accumulator is
cropped by world coordinates to the original extent and metadata. The tool does
**not** pass `allowed_rectangles` to `MapRefinement`, because that API makes the
exterior occupied. Each interval starts a fresh batch; scan sequence checks
persist across intervals. Equal receive milliseconds are permitted for distinct
packets; reversed times and replayed sequences are not used as new evidence.

```powershell
python -m tools.maps.refine_from_markers refine --session <recording-folder> --maps <original-maps> --truth <measured-truth.json> --rectangles <rectangles.json> --lidar-device <recorded-device-id> --output <new-map-folder> --validate
python -m tools.maps.refine_from_markers validate --session <evaluation-recording> --baseline <original-maps> --candidate <new-map-folder> --truth <evaluation-truth.json> --lidar-device <recorded-device-id> --output <new-evaluation.json>
```

The map bundle must contain `map_meta.json`, `slam_map.npy` and
`slam_map_loc.npy`. The output folder must be new and outside input folders.
All source files are copied; only `slam_map_loc.npy` changes and
`refinement_report.json` is added/replaced. Navigation bytes, metadata and
ancillary files remain unchanged. Copied previews and previous reports are not
regenerated; use the new report for this refinement. No accepted full revolutions
means failure with no output. Missing observations in individual windows are
reported explicitly.

The report records accepted revolutions per interval, value/sign changes,
rectangles, input hashes, configuration, and packet provenance. `--validate`
compares identical full revolutions in the original and refined grids using
local search from truth, local search from truth +0.5 m in X, and global search.
Rows contain position/yaw errors, fit fractions and global `peers`; local peers
and inactive full-scan diagnostics are `null`, not claims of no ambiguity.
The table is printed and rows are saved in JSON. Defaults select at most three
revolutions per interval (`--max-revolutions`). The local window is ±0.7 m at
0.05 m steps; global coarse search uses 0.1 m/15° and the configured free-space
threshold. Global matching can take time on large maps.

Reusing training packets adds an explicit **circular validation** warning.
Separate evaluation can be supplied to `refine --validate` using both
`--validation-session` and `--validation-truth`; evaluation hashes and truth
are recorded separately. Missing provenance also warns about possible reuse.
Disjoint packets alone do not establish independent ground truth. Outputs always
retain `navigation_ready: false`; these diagnostics do not activate a map.

Helpers only process saved files:

```powershell
python -m tools.maps.refine_from_markers detect --input <nav_poll.log-or-events.jsonl> --output <new-candidates.json>
python -m tools.maps.refine_from_markers line --intervals <reviewed-candidates.json> --origin 0 0 --direction-deg 90 --yaw-deg 0 --spacing-m 0.30 --output <new-line-truth.json>
python -m pytest tests/test_refine_from_markers.py tests/test_marker_observations.py
```

Detection uses a fixed pose anchor with 3 cm/5° tolerance for at least 5 seconds,
and breaks at gaps over 1 second or invalid/stale/LOST/unupdated observations.
Polling input uses epoch **seconds** and yaw **degrees**; runtime localization
events use epoch **milliseconds** and **radians**. Candidate output cannot be
used directly as measured truth. The line helper keeps interval times only and
generates 30 cm spacing from an independently measured origin, line direction
and common robot yaw (CLI angles are degrees). Review its physical assumptions;
it does not fit a line to the localization estimates.
