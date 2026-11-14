# Design the path, layout and speed

**2026-02 · Four glass beads · term_v3**

**Edit only the student targets below.** Process liquid and sand are locked.

- `student/joint_strategy.py::plan_path`: implement path search. `choose_edge_durations_s` is optional. You may add your own helper functions in this file or helper modules inside `student/`.
- Your selected `student/easy_design.json`, `student/medium_design.json` or `student/hard_design.json`: choose `arms[].mount_region`, `arms[].base_placement.x_m`, `y_m`, `yaw_rad`, permitted `handoffs[].support_id`, `x_m`, `y_m`, and `joint_speed_scale`. Keep `schema_version`, `difficulty`, arm IDs and entry counts unchanged. Lower's `handoffs` stays `[]`.

**Do not manually edit other supplied files. Unauthorized changes to supplied code or fixed data, or fabricated results, may receive ZERO for the project.** Keep supplied planners, models, maps, limits, physics, interpolation, checkers, tests, scoring rules and registration/recording code unchanged. Do not edit generated plans, logs or result receipts. Instructor-announced updates through the official updater are allowed.

Use the [per-tier entry table](MAPS_AND_DESIGN.md#which-entries-do-i-fill) while filling the JSON's `null` choices. A base is a carrying robot's mounting location. A handoff selects a support surface and X/Y point between two arms. Lower has one arm entry and no handoffs. Moderate has two arm entries and one handoff. Higher has three arm entries and two handoffs. The supplied adapter fixes the source, finish and unused-arm parking. Do not add those entries or edit the generated internal design.

## Implement the search

Complete `plan_path(request, oracle, *, kinematics=None, context=None)` in `student/joint_strategy.py`. Use RRT, bidirectional RRT, A*, or another suitable taught search. Each request supplies one arm's start and goal. Generate all intermediate configurations yourself. Provided endpoint IK handles grasp and placement poses, without supplying a transit route.

Use these interfaces to construct and check candidate connections.

| Interface | Input, return and units |
| --- | --- |
| `request.start`, `request.goal` | Exact seven-angle tuples in `joint1` to `joint7` order, rad |
| `request.bounds` | Seven inclusive joint intervals, rad |
| `request.seed`, `request.max_queries` | Search seed and shared state/edge query budget |
| `oracle.state_valid(q)` | Boolean validity against robot, cup, peers and map |
| `oracle.edge_valid(a, b)` | Boolean sampled validity of a straight joint-space connection |
| `oracle.queries_used`, `oracle.remaining_queries` | Public validity-call counts |
| `kinematics.fk(q)` | `ToolPose` with world `position_m` and unit `quaternion_wxyz` |
| `kinematics.ik(position_m, quaternion_wxyz, seed_q)` | Seven candidate angles or `None` |
| `kinematics.geometry(q)` | Immutable world collision surfaces, described below |
| `context` | Selected base, current arm/stage, endpoint poses, limits and nominal map geometry |

Return a list or tuple of seven-angle tuples. The first and last must equal the requested endpoints exactly. Use finite bounded angles without clipping or wrapping. Do not repeat consecutive states. For equal valid endpoints, return `(request.start,)`. Return `None` if search fails. Every public state/edge call consumes one query, including rejection. Exhaustion raises `PlanningBudgetExceeded`. Supplied final validation checks the returned states and edges separately. Return at most **10,000 states**. This is the supplied final-path storage/validation cap, separate from your search-tree size and query budget.

Valid endpoints do not establish a valid connection. FK and IK do not check collisions. Recheck tree joins and proposed shortcuts with the oracle. Supplied `distance(a, b)` and `steer(a, b, step)` operate in joint-space radians. Physical obstacle clearance is measured in metres.

## Compute clearance and additional constraints

`context.nominal_obstacles` provides `obstacle_id`, `lower_m` and `upper_m` for world boxes. `active_arm_id`, `motion_kind`, `source_support_id` and `destination_support_id` identify this request. The context contains no intermediate joint posture.

Call `surfaces = kinematics.geometry(q)` to compute your clearance estimate or additional constraint. Each frozen `WorldGeometry` record contains:

| Fields | Meaning |
| --- | --- |
| `geometry_id`, `owner_kind`, `entity_id`, `link_id` | Geometry and owning robot link, cup, bead or map object |
| `shape`, `center_m`, `quaternion_wxyz` | Shape and nominal world pose |
| `vertices_m`, `triangles` | World mesh/box vertices and vertex-index triangles |
| `half_extents_m`, `radius_m` | Box half sizes or exact sphere radius, when applicable |
| `surface_points_m` | Surface samples. Sphere samples do not represent its entire curved surface |
| `query_aabb_m` | Separate padded map bounds used by the oracle, when applicable |

Robot records use installed collision meshes. Map boxes expose nominal surfaces. During carrying, the cup follows the provided grasp transform. Peers retain their current stage poses. A plane has infinite extent and a local +Z normal. The geometry query performs no physical step and leaves scene state unchanged.

Each geometry query consumes one shared FK call. Check `kinematics.remaining_fk_calls` and `remaining_ik_calls` before optional queries. Surface samples, FK and IK are not collision certificates. Tool-only distances may miss elbow or cup contact, so retain full-robot state and edge checks.

`context.query_tracking_clearance_m` and `edge_max_joint_delta_rad` describe the oracle's margin and resolution. You may use finer sampling or stricter constraints. Returning fewer waypoints cannot weaken the final check. Sampled geometry does not prove continuous collision freedom or bead retention.

After rejection, `oracle.last_query_diagnostic()` returns that call's detached reason/contact record without another query. Read `reason` and `witness_status` before interpreting reported contacts. A successful query clears previous failed-contact information.

## Design speed

The supplied rest-to-rest quintic interpolator automatically accelerates from rest and decelerates to rest on every path edge. Commanded velocity and acceleration are zero at each waypoint. Choose paths and edge times in `student/joint_strategy.py` and speed scale in the selected `student/*_design.json`. You do not implement a separate braking routine or low-level controller. Actual joints follow through physics and can lag behind the command, including near a waypoint or support contact.

Set `joint_speed_scale` in your JSON to a value in `(0, 1]`. The interpolator divides each edge's analytic minimum duration by this scale. A smaller scale slows the same path and reduces its commanded acceleration. Physics remains at `0.0025 s` with 16 internal substeps.

For independent edge times, complete `choose_edge_durations_s(path, limits, *, context)` in `student/joint_strategy.py`. Its supplied `return None` retains JSON scaling. For explicit times, set the scale to `1.0` and add `--edge-timing-hook` to CPU planning. Return `len(path)-1` finite positive durations in simulated seconds. Each must be at least `minimum_edge_duration_s(a, b, limits)`. A one-state path uses `()`. Invalid durations reject instead of being stretched.

Use the following fixed task limits when choosing times. They are instructor-chosen constraints shared by Lower, Moderate and Higher. Joint numbers follow the seven-angle order in your path. `limits` also provides the inclusive joint-position bounds in radians.

| Arm joints | Maximum absolute velocity (rad/s) | Maximum absolute acceleration (rad/s²) |
| --- | ---: | ---: |
| 1-4 | 1.0 | 2.0 |
| 5-7 | 1.4 | 2.8 |

The physical review checks actual joint velocity and acceleration against these limits. Actual acceleration is `(v_after - v_before) / 0.0025` over one outer physics interval. Its absolute value is checked, so both acceleration and deceleration can reject a run. Actual joint position must also track its command within `0.03 rad`.

The CPU minimum-duration check guarantees the interpolated command limits only. A legal command can still produce `control:acceleration_limit` in physical replay. On the same edge, doubling its duration halves peak commanded velocity and quarters peak commanded acceleration. This scaling does not guarantee the same change in actual motion or a physical pass. Unnecessary waypoints add stops and can increase completion time. Use the [failure records](EVALUATION_AND_SUBMISSION.md#resolve-failures) to revise the relevant edge times or path, then export again. No fixed slowdown factor is required. Forbidden contact, tracking error and actual bead loss also reject a run. There is no separate 8-degree spill gate.

## Test and interpret results

After [first-use team sign-in](EVALUATION_AND_SUBMISSION.md#1-register-your-team-and-check-the-environment) and implementing `plan_path`, run from the repository root in the existing Anaconda Prompt:

```bat
python -B project_run.py test
```

The unfinished starter has two path errors and one passing default timing check. After implementation, all three should pass. They check endpoint and timing contracts in simple joint space, not workcell feasibility.

Follow [CPU plan, optional empty cup and full beads](EVALUATION_AND_SUBMISSION.md). CPU export freezes exact paths, times and source identities. Both replay modes use the same export without new RRT, online IK or hidden timing changes. After editing planner or design inputs, create a fresh CPU output before replaying.

CPU search defaults to no wall timeout (`--wall-limit-s 0`) and 50,000 validity queries per stage. `--seed`, `--max-queries` and `--endpoint-attempts` control local search, not the simulated deadline. `--max-stages 1` produces a first-approach diagnostic with `CPU_PLAN_PREFIX_ONLY`. A prefix is not a complete replayable mission and never enters competition history. Failed supplied-runner commands are reported privately to the instructor.

Use the failed phase to revise a mounting location, relay, clearance strategy or edge time. Compare under stated conditions and interpret the supplied records in your required presentation. No separate student verification report is required.

For visual inspection, the supplied [animation exporters and viewer](EVALUATION_AND_SUBMISSION.md#inspect-and-save-animations-optional) show either your saved CPU plan or actual recorded Genesis motion. CPU previews contain no simulated beads. Recorded previews retain individual bead IDs and positions, with cup-follow and transparent-cup views. Visual quality and export frame rate affect display only. They never change your path, timing, physics settings or evaluator result.

## Optional planning review

MIT's [RRT exercise](https://manipulation.csail.mit.edu/Fall2020/pset5/rrt_planner.html) covers single-tree RRT, bidirectional RRT and shortcut smoothing. Its [Motion Planning chapter](https://manipulation.csail.mit.edu/trajectories.html) explains configuration-space search and collision checking. These are optional explanations, not extra assignments. No MIT Docker/Drake installation is needed. Apply the ideas through this repository's API.
