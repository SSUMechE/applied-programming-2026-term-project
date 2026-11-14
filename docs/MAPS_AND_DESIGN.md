# Design the layout

Open [the 3D viewer](map_viewer.html) at `docs/map_viewer.html` from your cloned repository in Chrome or Edge. Keep the supplied `motion_preview.js` and `viewer_capture.js` beside it. Choose your complexity and inspect the grey mounting rectangles, equipment surfaces, blue start and green finish. All geometry uses world XYZ in metres. The viewer displays map geometry and can load your design or saved motion preview. A placement inside a rectangle still needs reachability and collision checks.

Use this mapping to select the case directory and identify the fixed destination.

| CLI name | Case in `cases/difficulty_20261106/` | Transporting arms / handoffs | Fixed destination |
| --- | --- | ---: | --- |
| `easy` | `easy_open_v1` | 1 / 0 | `relay_1_support` |
| `medium` | `medium_staggered_v1` | 2 / 1 | `relay_2_support` |
| `hard` | `hard_industrial_v1` | 3 / 2 | `goal_support` |

All tasks start at `source_support`. The name `relay_1_support` denotes Lower's final destination. It is not an intermediate handoff in that task.

## Choose bases and handoffs

Edit `student/easy_design.json`, `student/medium_design.json` or `student/hard_design.json`. Replace each editable `null` with your choice.

For an arm, select `mount_region`, then set `base_placement.x_m`, `y_m` and `yaw_rad`. The surface fixes base height, including the supplied 1 mm mounting clearance. You can choose a higher mounting surface but cannot enter an arbitrary Z coordinate. Use radians for yaw.

### Which entries do I fill?

A **base** is a robot's mounting location. Every map contains `arm_0`, `arm_1` and `arm_2`, so fill the mounting choices for all three. The arms not carrying the cup stay parked and still count in collision checks. Lower requires only `arm_0` to transport the cup.

A **relay entry** selects a support surface and an X/Y point on it. Its role depends on your complexity. Use the table below while filling your design JSON. `relays[0]` means the first entry and `relays[1]` the second.

| Complexity | Arms that carry | Arms that stay parked | `relays[0]` | `relays[1]` |
| --- | --- | --- | --- | --- |
| Lower | `arm_0` | `arm_1`, `arm_2` | **Fixed finish.** Keep the supplied `relay_1_support` and X/Y `(1.02, 1.04)` m. | Not used for transport. Choose another valid support and X/Y point. |
| Moderate | `arm_0`, `arm_1` | `arm_2` | **Your handoff** from `arm_0` to `arm_1`. Choose its support and X/Y point. | **Fixed finish.** Keep the supplied `relay_2_support` and X/Y `(2.35, 1.02)` m. |
| Higher | `arm_0`, `arm_1`, `arm_2` | None | **Your first handoff** from `arm_0` to `arm_1`. | **Your second handoff** from `arm_1` to `arm_2`. |

Higher ends at the separate fixed `goal_support`, after both handoffs. Every tier starts at the fixed `source_support`. Do not delete any arm or relay entry, and do not leave editable values as `null`. The current loader requires both relay entries to use valid, distinct support IDs, even though Lower's second entry is not visited. Filling that unused entry does not add a handoff or require another arm to carry.

Select distinct allowed handoff supports. Candidates include the relay and quality support surfaces, except the fixed destination for your tier. A catalogue entry is permission to try it, not a guarantee that adjacent arms can reach it. The published XY bounds account for the complete cup footprint. CPU contact planning also requires the bottom corners to remain at least `context.query_tracking_clearance_m` inside the support edges (currently 0.004 m). Choosing an exact catalogue boundary can therefore fail the endpoint check. Allow this extra margin, then check the selected endpoint with the oracle. No required intermediate joint posture is supplied.

To inspect the exact bounds without starting Genesis, run this from the repository root after setting `MAP` as shown in the run guide:

```bat
python -B -c "import json,os; from pathlib import Path; from transport_challenge.three_arm_design import design_space; w=json.loads((Path('cases/difficulty_20261106')/os.environ['MAP']/'world.json').read_bytes()); print(json.dumps(design_space(w),indent=2))"
```

`mount_regions` lists each ID, XY/yaw bounds, surface height and base height. `relay_regions` lists support IDs, allowable XY bounds and heights. Use those values in your design JSON. Load your design JSON in the viewer to display the selected base origins and exact handoff XY coordinates. The support-surface centre is not a substitute for your chosen landing point.

## Inspect your saved motion

After a successful CPU export, follow the [optional motion-preview command](EVALUATION_AND_SUBMISSION.md#3-save-a-cpu-checked-path). In the viewer opened from your cloned repository, select the same complexity and choose **Preview your saved motion** to load the exported JSON. Use **Stage**, **Frame** or **Play** to inspect your seven joint angles, planned arm/cup geometry and handoff coordinates. **Top XY**, **Front XZ** and **Side YZ** provide comparison views. No completed reference path is bundled. The preview changes no path or timing and does not establish physical success. Its stage command time is not the competition completion time.

## Use geometry in your search

`world.json` contains the complete obstacle AABBs, support boxes and vessel geometry. `initialization.json` supplies initial joints, open fingers and original cup/bead state. Robot bases are bound from your completed design. `manifest.json` fixes the supplied inputs.

Within `plan_path`, use `context.nominal_obstacles` for exact obstacle corners and `kinematics.geometry(q)` for robot, cup, peer and obstacle surfaces at your candidate angles. The [API guide](STUDENT_API.md) defines the returned shapes and their units. Calculate your own distance, clearance or additional constraints from this geometry. Keep the supplied full-robot state and edge checks: tool clearance alone can miss an elbow, cup or other arm.

## What stays fixed

Keep the physical map, robot and cup geometry, gravity, material, original four beads, control limits, 0.0025-second timestep, 16 internal substeps and contact checks unchanged. No particle reset or replacement occurs at a handoff. Grasp and release are automatic once the provided endpoint conditions are met.

After supported placement, an intermediate donor retraces its own path through the provided kinematic return. This display operation adds zero simulation ticks and leaves the cup, beads and other arms unchanged. Its final home pose receives a contact check. The return is not a physical motion or a swept collision certificate. The last carrier does not return. The task clock excludes only the initial 0.5-second preparation and includes subsequent physical approach, transport, holds and final rest. These are fixed conventions, not student timing choices.
