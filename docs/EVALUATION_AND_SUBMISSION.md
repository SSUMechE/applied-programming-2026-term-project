# Test, replay and submit

**2026-02 · Beads only · term_v3**

Design your layout, seven-joint path and speed. Supplied code performs grasping and placement. The exact saved CPU path is reused by empty-cup and bead replay. [Inputs and API](STUDENT_API.md) · [Maps](MAPS_AND_DESIGN.md) · [Scoring](SCORING.md)

The supplied bead workflows have been tested on an RTX 4090. Verify your own layout, path and speed with the steps below. GTX 1660 SUPER qualification is pending. Test history is not an official competition record until instructor activation. Final representative selection opens after that activation. Process liquid and silica sand remain locked.

## 1. Check the environment and your implementation

Use **Windows Anaconda Prompt**, from your project repository root. Copy commands without prompts or printed output. Save the instructor-issued team token outside Git at `%USERPROFILE%\applied-programming-team.token`, then configure it once for this checkout:

```bat
conda activate applied-programming-cuda
python -B project_run.py configure --token-file "%USERPROFILE%\applied-programming-team.token"
python -B project_run.py check --cuda --output output/environment.json
python -B project_run.py test
```

Continue after `REPORTING_CONFIGURED`. The configuration stores only the token-file path and server address in ignored `output/diagnostics_config.json`. Keep the token file in place and do not commit it. Students do not install ngrok or a VPN.

The environment check must print `ENVIRONMENT_CUDA_SMOKE_PASS`. Keep the existing PyTorch environment. A mismatch is reported automatically when connected. Wait for instructor guidance before changing packages. Complete `student/joint_strategy.py` and the selected design JSON before planning. The unfinished starter has two path errors. After implementing it, all three supplied checks should pass.

The supplied `project_run.py` runs the original checks, planner and physical evaluator with your arguments. It preserves their exit status and does not change paths, timing or physics. Failed `check`, `test`, `plan` and `review` commands queue a short diagnostic for the instructor, separate from competition records.

## 2. Select your complexity and output folder

Copy one block. These variables last only in the current prompt window.

Lower complexity:

```bat
set "LEVEL=easy"
set "MAP=easy_open_v1"
set "RUN=output/lower_01"
```

Moderate complexity:

```bat
set "LEVEL=medium"
set "MAP=medium_staggered_v1"
set "RUN=output/moderate_01"
```

Higher complexity:

```bat
set "LEVEL=hard"
set "MAP=hard_industrial_v1"
set "RUN=output/higher_01"
```

For another attempt, choose a fresh `RUN`, such as `output/higher_02`. Preserve earlier results.

## 3. Save a CPU-checked path

```bat
python -B project_run.py plan --scope tier-mission --difficulty %LEVEL% --material beads --task cases/difficulty_20261106/%MAP% --design student/%LEVEL%_design.json --strategy student.joint_strategy --output "%RUN%/cpu"
```

Continue after `CPU_GEOMETRIC_TIER_MISSION_PLANNED`. `%RUN%/cpu/saved_tier_mission.json` contains exact joints, edge durations, layout and source identities. CPU geometry and analytic timing checks do not establish actual velocity/acceleration compliance, bead retention or completion within the simulated deadline. To use your explicit timing function, set JSON speed scale to `1.0` and append `--edge-timing-hook`.

Do not edit planner, design or supplied files between export and replay. After an edit, plan again in a fresh output folder. Default CPU search has no wall timeout. The query budget remains finite.

**Optional: inspect your own saved motion.** After a complete CPU export, create a viewer file:

```bat
python -B export_student_view.py --saved-tier "%RUN%/cpu/saved_tier_mission.json" --output "%RUN%/motion_preview.json"
```

Open `docs/map_viewer.html` from your cloned repository in a browser. Keep `motion_preview.js` beside it. Select the same complexity as your plan and choose **Preview your saved motion**, then load `motion_preview.json`. Use **Stage**, **Frame** or **Play** to inspect the arm, cup, seven joint angles, planned path and chosen handoff coordinates. Use **Preview your design JSON** to inspect base and handoff choices before planning. The preview reads your saved plan without replanning, retiming or starting Genesis. Its stage command time is not the competition completion time. It shows planned collision geometry, not measured physical tracking, bead motion or a new collision certificate. Continue with empty-cup or bead replay for physical verification.

**Optional: diagnose one CPU stage.** If a CPU attempt failed after saving its input records, you may edit `student/joint_strategy.py` and retry just its next stage with the supplied diagnostic tool:

```bat
python -B debug_cpu_stage.py --cpu-dir "%RUN%/cpu" --difficulty %LEVEL% --stage next --plan --output "%RUN%/debug_stage_01"
```

Read `DIAGNOSTIC.json` in that fresh output folder. `DIAGNOSTIC_STAGE_PLANNED` means only that this stage passed CPU path checks. The tool reuses your preceding saved stages, actual selected supports and peer poses. It rejects a changed design, map or supplied source. It creates no replayable mission and never uploads a result. Run the normal full CPU export again before Genesis, even after diagnostic success.

Remove `--plan` to inspect the reconstructed endpoints and direct-edge diagnostic without calling your planner. For a completed attempt, replace `next` with a saved stage ID, such as `arm_0__carry`. Append `--edge-timing-hook` to test your timing function. The diagnostic has a 120-second default wall limit. Inspect `DIAGNOSTIC_PROCESS.json` and `worker_stdout.log` for timeout or interrupted-worker details. A timeout that prevents the worker report produces `DIAGNOSTIC_TIMEOUT`. Use `--wall-limit-s` to choose another positive limit. Completed stages supply the original seed and budgets. If the first stage failed, supply the original `--seed`, `--max-queries` and `--endpoint-attempts` values. Their normal CPU defaults are `71`, `50000` and `32`. Missing or inconsistent records require a fresh normal CPU run. This diagnostic is optional and adds no submission item.

## 4. Optionally replay with an empty cup

```bat
python -B project_run.py review --saved-tier "%RUN%/cpu/saved_tier_mission.json" --difficulty %LEVEL% --task cases/difficulty_20261106/%MAP% --design student/%LEVEL%_design.json --contents empty --output "%RUN%/empty"
```

This uses the same layout, initial robot/cup poses, paths, times and rigid16 settings, with only the beads removed. It checks physical following, contact, handling and completion. It cannot check bead retention and never uploads a competition record. A passing supervisor reports `review_accepted: true` and `empty_mission_passed: true`. Its `full_mission_passed` remains false.

## 5. Replay the full path with beads

```bat
python -B project_run.py review --saved-tier "%RUN%/cpu/saved_tier_mission.json" --difficulty %LEVEL% --task cases/difficulty_20261106/%MAP% --design student/%LEVEL%_design.json --rule leaderboard/rules/term_v3/ap2026-%LEVEL%-beads-term-v3.json --token-file "%USERPROFILE%\applied-programming-team.token" --output "%RUN%/full"
```

Genesis replays the saved path without new RRT or online IK. Lower completes one carry, Moderate two and Higher three, using the same cup and four bead IDs. Actual joint velocity/acceleration violations, tracking failure, prohibited contact, bead loss or an unmet final support/rest condition reject the run. The [speed contract](STUDENT_API.md#design-speed) gives the fixed limits and explains automatic acceleration/deceleration. Actual loss stops the run. A lost bead is not regenerated, restored or converted into a competitive partial result.

Inspect `%RUN%/full/REVIEW_SUPERVISOR.json`. Successful full transport requires `review_accepted`, `physical_transport_accepted` and `full_mission_passed` all true. Inspect `%RUN%/full/LEADERBOARD_STATUS.json` separately for upload state.

Every completed full bead review automatically records success or failure. `status: uploaded` means the server acknowledged that attempt. It is not by itself a physics pass. `status: pending` means the local record is queued. `scoring_enabled: false` identifies staging rather than an active competition rule. CPU, empty-cup and short practice runs never enter competition history. Failures from the supplied runner are shared as private diagnostics.

## 6. Retry an upload without repeating Genesis

If the server or token was unavailable, keep your output and run:

```bat
python -B project_run.py retry
```

This resends pending diagnostics and leaderboard records using the configured token. Inspect each returned status. An acknowledged duplicate does not create an extra record. This command launches no physics. If a local automatic competition record was not created, recover it from the completed review:

```bat
python -B -m leaderboard.auto_record record --rule leaderboard/rules/term_v3/ap2026-%LEVEL%-beads-term-v3.json --review-dir "%RUN%/full" --token-file "%USERPROFILE%\applied-programming-team.token"
```

Open the [class leaderboard](https://phoniness-reenact-veggie.ngrok-free.dev), or inspect your team's history:

```bat
python -B -m leaderboard.auto_record history --rule-id ap2026-%LEVEL%-beads-term-v3 --token-file "%USERPROFILE%\applied-programming-team.token"
```

The live comparison uses each team's latest successful run. A later failure stays in history and does not replace that success. The official comparison uses the successful run explicitly selected by each team.

## 7. Select the final representative and submit

After the instructor activates the verified rule, choose one successful acknowledged run. Commit the evaluated student files using your usual Git workflow, then obtain its full ID:

```bat
git rev-parse HEAD
```

Replace the placeholder with those exact 40 characters. Set `RUN` to the selected run's folder before this command.

```bat
python -B -m leaderboard.auto_record select --review-dir "%RUN%/full" --commit YOUR_FULL_40_CHARACTER_COMMIT_ID --token-file "%USERPROFILE%\applied-programming-team.token"
```

Confirm `status: selected`, the intended `run_id` and your full `commit_id` in the response. The command checks every `.py` and `.json` file under `student/`, including helper modules and unused tier designs, against the evaluated snapshot. A changed planner or design cannot be attached to an older result. The instructor freezes official selections for assessment. The schedule remains Week 15, with the exact closure announced by the instructor.

Use one public team repository and individual contributor accounts. Submit the repository URL and full 40-character commit ID in the LMS. Prepare one team PowerPoint with **2–3 common slides plus 3 slides per member**, excluding the title slide. Include the repository reference, settings, run instructions and relevant execution evidence. Each member explains their own named contribution with commit or pull-request links. See [assessment details](SCORING.md). Leaderboard registration does not replace these deliverables.

## Interpret time and cost

Use the simulated deadline to evaluate transport and the computer guard to diagnose resource cost.

| Complexity | Simulated deadline | Default computer review guard |
| --- | ---: | ---: |
| Lower | 40 s | 1,800 s |
| Moderate | 85 s | 3,600 s |
| Higher | 105 s | 5,400 s |

The simulated clock excludes the initial 200 preparation ticks (0.5 s), then includes approach, carrying, handling, waiting and required settling. Deadlines are inclusive. Supplied donor returns reverse their own paths kinematically with zero physics ticks. Only the return's final home pose receives a contact check. This display operation does not certify swept collision freedom or simulate physical return motion. The final carrier does not return.

Computer cost is measured separately. CPU records distinguish total elapsed time, student search callback time and remaining validation/export attribution. The review guard includes the attributed CPU work, GPU review, monitoring, export and cleanup. Search time remains reported but is excluded from that guard. A guard is not a runtime estimate. Missing memory measurements do not count as zero. Sampled process VRAM must stay within 5,000,000,000 bytes. GTX 1660 SUPER costs remain unqualified until measured on that hardware.

A measured **Lower** example on the instructor's **RTX 4090** took **20 min 17.289 s** for configuration, CUDA check, three tests, fresh CPU planning, full bead review and HTTPS registration. CPU planning took **22.308 s** and the native review took **19 min 40.741 s**. This is one completed design, not a guaranteed duration or a GTX 1660 estimate. Different paths, speeds, initialization/cache states and hardware change the cost. The native review includes preparation, physics, checks and recording. It is not a pure GPU-kernel measurement.

## Resolve failures

Use the first reported failure to choose an action. The supplied runner automatically sends a bounded error summary, execution stage, command settings, environment versions and source hashes to the instructor's private issue view. It separates design failures from program or environment errors. It does not upload student source code, credentials or full logs. Reporting does not change the result or score.

`Diagnostic uploaded` confirms that the report reached the instructor. `Diagnostic pending` means it remains in `output/diagnostics_queue`. The instructor can see it after upload, not while the computer is offline. The next supplied-runner command retries pending reports. You can also use `python -B project_run.py retry` after reconnecting. Keep the original local records for detailed debugging. There is no separate bug-report form.

| Result | Next action |
| --- | --- |
| `NotImplementedError` or unset design | Complete the named student function or JSON choice |
| Fresh-directory error | Choose a new `RUN` or output subfolder |
| CPU search or endpoint failure | Inspect the stage, layout and query diagnostics |
| Source mismatch | Restore supplied files and re-export your edited student design |
| `control:acceleration_limit` | Locate the failed phase and tick below. Increase relevant edge durations or revise the path, then create a fresh CPU export |
| Contact, loss or tracking failure | Revise the path, placement or speed and re-export |
| Pending upload | Retry the queued record without rerunning Genesis |
| Final selection mismatch | Select a run whose evaluated student files match the submitted commit |

For a control rejection during an approach or carry, `%RUN%/full/REVIEW_SUPERVISOR.json` gives `worker_failure.message`. Open `%RUN%/full/device_tier_execution/TIER_DEVICE_RESULT.json` and read `failed_phase_prefix.purpose` for the stage. Inside that prefix, `checks.checks.control.flags` identifies the failed condition and `checks.checks.control.first_failed_step` gives its native tick. The stage elapsed time is `(first_failed_step - stage_start_tick) * 0.0025`. Use the prefix's `start_native_step` as `stage_start_tick`, unless earlier entries in `phases` have the same `purpose`. For a stage split into chunks, use the first such entry's `start_native_step`. Compare the elapsed time with the matching saved stage's `trajectory.waypoint_times_s` to locate the commanded edge. These JSON fields require no new analysis script.

Deceleration is already supplied. Changing the relevant duration in `choose_edge_durations_s`, reducing JSON speed scale, or revising the path changes the saved command. A longer duration is a design choice, not a guaranteed cure for physical contact or control failure. No prescribed multiplier or hidden replay retiming applies. Keep the fixed limits and re-export after each student-input edit.

Retain original records, including failed attempts. Supplied checks and source matching establish reproducibility boundaries, not remote attestation of a student-controlled computer.
