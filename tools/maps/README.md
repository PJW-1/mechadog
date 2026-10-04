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
