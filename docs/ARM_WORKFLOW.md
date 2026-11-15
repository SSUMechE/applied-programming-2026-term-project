# Plan one arm, practice it, then combine

Moderate can be developed as **two arm problems**. Higher can be developed as **three**. You do not need a complete mission or another member's working planner to test your own arm.

Each local problem includes that arm's approach, automatic pickup, carry, automatic placement and rest. The supplied functions provide start and goal. You generate the intermediate seven-joint configurations and choose speed. The shared map, all robot models and their collision checks remain present.

## 1. Agree on one shared design

First follow [README 3.1–3.3](../README.md#31-create-and-clone-your-teams-repository) to create the private team repository, sign in, check the environment and set `LEVEL`, `MAP` and a fresh `RUN`. Run commands in **Windows Anaconda Prompt** with `applied-programming-cuda` active.

Complete the same `student/%LEVEL%_design.json` on every member's checkout. Agree on **all participating base placements, handoff supports, cup X/Y coordinates and speed scale**. The supplied support heights, source and final destination stay fixed. Use the same instructor release.

- Moderate: `arm_0` carries from the source to your handoff. `arm_1` carries from that handoff to the fixed finish.
- Higher: `arm_0` carries to your first handoff. `arm_1` carries between your two handoffs. `arm_2` carries to the fixed finish.

Local practice starts the cup on that arm's source support, with the other robots at the shared home commands. It does not reproduce an earlier physical handoff. The final whole-scene run checks the actual sequence.

## 2. Implement your own arm module

Set your assigned arm. Copy the starter only if that file does not exist:

```bat
set "ARM=arm_0"
if not exist "student\%ARM%_strategy.py" copy "student\joint_strategy.py" "student\%ARM%_strategy.py"
```

Edit **`student/arm_0_strategy.py`** for this example. For another arm, change `ARM` and edit its corresponding file. Implement `plan_path(request, oracle, *, kinematics=None, context=None)` at the marked student edit location. Its interface is identical to `student/joint_strategy.py`. Optional `choose_edge_durations_s` controls edge times. Read [the exact input, output, units and limits](STUDENT_API.md#implement-the-search).

`context.active_arm_id` is your selected arm. `context.motion_kind` identifies `approach` or `carry`. No other arm's planner is called. Helpers imported by your module must also stay under `student/`.

Run the three supplied interface checks for your module:

```bat
python -B project_run.py test-arm --strategy student.%ARM%_strategy --arm %ARM%
```

All three should pass. They use small toy geometry, not the workcell. Do not edit supplied tests, physics, fixed data or generated plans/results. Unauthorized changes or fabricated results may receive zero.

## 3. Plan just your arm on CPU

```bat
python -B project_run.py plan-arm --difficulty %LEVEL% --task cases/difficulty_20261106/%MAP% --design student/%LEVEL%_design.json --arm %ARM% --strategy student.%ARM%_strategy --output "%RUN%/cpu_%ARM%"
```

Continue after `CPU_GEOMETRIC_ARM_MISSION_PLANNED`. Keep the **whole output folder**, including `saved_arm_mission.json` and its sibling identity/provenance files. Only your two motion stages are planned. Other arm strategy files may be absent or unfinished.

For explicit timing, set the shared JSON speed scale to `1.0` and add `--edge-timing-hook`. Otherwise the shared speed scale is used. The query budget remains finite. CPU elapsed time is measured without an automatic time cutoff.

To choose different speeds for different arms, keep that shared scale at `1.0` and return each arm's edge durations from its own timing hook. Do not combine explicit durations with a scale below `1.0`.

Optional geometric preview:

```bat
python -B export_student_view.py --saved-arm "%RUN%/cpu_%ARM%/saved_arm_mission.json" --output "%RUN%/preview_%ARM%.json"
```

Open `docs/map_viewer.html`, select your complexity and load that JSON through **Preview your saved motion**. This displays your own commanded geometry and times. It is not a physical pass.

## 4. Experiment with an empty cup, then beads

```bat
python -B project_run.py practice --saved-arm "%RUN%/cpu_%ARM%/saved_arm_mission.json" --difficulty %LEVEL% --task cases/difficulty_20261106/%MAP% --design student/%LEVEL%_design.json --arm %ARM% --contents empty --output "%RUN%/practice_%ARM%_empty"
python -B project_run.py practice --saved-arm "%RUN%/cpu_%ARM%/saved_arm_mission.json" --difficulty %LEVEL% --task cases/difficulty_20261106/%MAP% --design student/%LEVEL%_design.json --arm %ARM% --contents beads --output "%RUN%/practice_%ARM%_beads"
```

Run the second experiment when you want to test retention. Both reuse the exact saved path and edge times. Empty-cup practice checks robot/cup motion, contact and automatic handling. Bead practice additionally checks the five beads during carrying. Physics resolution and collision checks are unchanged. The supplied handling policy prepares the beads outside carries and never restores a failed carry.

Read `PRACTICE_RESULT.json`. `ARM_PRACTICE_PASS` is an isolated practice pass. On failure, read `failure` and `failed_checks`. These results remain local and cannot enter the leaderboard. Use fresh output names for another attempt. Empty-cup and bead practice measure elapsed time without an automatic time cutoff. Press **Ctrl+C** to stop manually. Keep any written logs and use a fresh output folder when restarting. An interrupted run is incomplete and cannot rank.

Optional animation after a successful local practice:

```bat
python -B export_physics_view.py --saved-arm "%RUN%/cpu_%ARM%/saved_arm_mission.json" --review-dir "%RUN%/practice_%ARM%_beads" --fps 30 --output "%RUN%/physics_%ARM%.json"
```

Load the JSON in `docs/map_viewer.html` to inspect actual recorded arm, cup and bead motion. For an empty-cup practice, change the review directory and output filename. The exporter reads the existing logs without running physics again. This standalone animation export requires a successful local practice. Failed attempts retain their original diagnostic logs. [Viewer controls](EVALUATION_AND_SUBMISSION.md#5-replay-the-full-path-with-beads) include cup interior, transparent cup, PNG and WebM output.

Change a used planner/helper or its times: regenerate that arm's CPU plan. Change the shared design or supplied release: regenerate compatible arm plans before combining. An unrelated teammate's separate planner edit does not invalidate your standalone plan. Once a complete mission is merged, keep all its bound inputs unchanged through full review.

## 5. Combine the saved plans

Commit and push each member's producing modules and helpers to the **same private team repository**, using their own account. Bring those exact source files into the checkout that will run the whole mission. Git does not transfer the ignored output folders. Privately copy each complete arm CPU output folder separately, named `%RUN%/cpu_arm_0`, `%RUN%/cpu_arm_1` and, for Higher, `%RUN%/cpu_arm_2`. A private ZIP transfer is also suitable if you extract the whole folder without editing its contents. These folders are local transfer artifacts, not an extra LMS submission.

For Moderate:

```bat
python -B project_run.py merge-arms --difficulty %LEVEL% --task cases/difficulty_20261106/%MAP% --design student/%LEVEL%_design.json --arm-plans "%RUN%/cpu_arm_0/saved_arm_mission.json" "%RUN%/cpu_arm_1/saved_arm_mission.json" --output "%RUN%/cpu"
```

For Higher:

```bat
python -B project_run.py merge-arms --difficulty %LEVEL% --task cases/difficulty_20261106/%MAP% --design student/%LEVEL%_design.json --arm-plans "%RUN%/cpu_arm_0/saved_arm_mission.json" "%RUN%/cpu_arm_1/saved_arm_mission.json" "%RUN%/cpu_arm_2/saved_arm_mission.json" --output "%RUN%/cpu"
```

The supplied merger checks matching layout, supports, boundaries, source files, joint limits, fixed-resolution collision checks and exact segment times. Missing or duplicate arms, different shared inputs or changed producing code are rejected. It does **not** call RRT, IK or your timing function, move waypoints or change durations.

Success creates `%RUN%/cpu/saved_tier_mission.json`. This is a checked complete CPU plan, not a physical success. Original arm provenance remains recorded. Do not edit files to force an incompatible merge to pass.

## 6. Run one complete bead mission

```bat
python -B project_run.py review --saved-tier "%RUN%/cpu/saved_tier_mission.json" --difficulty %LEVEL% --task cases/difficulty_20261106/%MAP% --design student/%LEVEL%_design.json --contents beads --rule leaderboard/rules/term_v6/ap2026-%LEVEL%-beads-term-v6.json --output "%RUN%/full"
```

This executes the actual handoff sequence in one scene. Separate arm passes are not combined into a score. Inspect `REVIEW_SUPERVISOR.json` and `LEADERBOARD_STATUS.json`, then follow [result confirmation and final submission](../README.md#4-results-leaderboard-and-submission). Only an accepted full bead run can rank. The existing team repository, presentation and LMS requirements remain unchanged.
