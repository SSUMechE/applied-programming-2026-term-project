"""Export measured Genesis motion for the optional offline student viewer.

Reads a saved CPU plan and an existing full/empty review. No Genesis import,
physics integration, IK, planning, retiming, scoring or network upload occurs.
Visual samples retain recorded robot, cup and bead states. They are not a new
collision or retention certificate. Keep the source files and original logs.
"""
from __future__ import annotations

import argparse
import copy
import json
import math
from pathlib import Path
import sys
import time

import export_student_view as nominal

ROOT = Path(__file__).resolve().parent
DT = .0025
MAX_FRAMES = 30000
require, read, sha = nominal.require, nominal.read, nominal.sha


def sampled_indices(count, fps):
    """Select genuine POST samples, including both endpoints, without fitting."""
    require(type(count) is int and count > 0, "No actual recorded samples")
    require(fps in (15, 30, 60), "Choose display FPS 15, 30 or 60")
    indices = {0, count - 1}
    indices.update(min(count - 1, round(i / fps / DT))
                   for i in range(math.floor((count - 1) * DT * fps) + 1))
    return sorted(indices)


def checked_artifact(binding, directory, source_hashes):
    """Accept a preserved review directory after moving it, never another file."""
    require(type(binding) is dict and type(binding.get("sha256")) is str,
            "Missing recorded artifact hash")
    recorded = binding.get("file", binding.get("path"))
    require(type(recorded) is str, "Missing recorded artifact filename")
    name = recorded.replace("\\", "/").rsplit("/", 1)[-1]
    require(name not in ("", ".", ".."), "Invalid recorded artifact name")
    path = (directory / name).resolve(strict=True)
    require(path.parent == directory.resolve(), "Artifact escaped the review directory")
    require(sha(path) == binding["sha256"], "Recorded artifact changed: " + name)
    source_hashes[str(path)] = binding["sha256"]
    return path


def actual_frame(engine, indices, arm, q_by_arm, body, bead_positions, *,
                 time_s, global_time_s, native_step, inside):
    """FK on measured all-nine-DOF poses, with the independent measured cup."""
    import numpy as np
    import mujoco
    from supplied_support.cpu_geometry import _quat
    from physics.carried_body import WorldPose
    q = np.asarray([q_by_arm[n] for n in ("arm_0", "arm_1", "arm_2")])
    beads = np.asarray(bead_positions, dtype=float).reshape((-1, 3))
    require(q.shape == (3, 9) and np.isfinite(q).all() and np.isfinite(beads).all(),
            "Measured robot/bead coordinates are invalid")
    pose = WorldPose(body["position_m"], body["quaternion_wxyz"])
    # Direct read-only FK avoids constructing a collision-query oracle for each
    # display sample. The measured cup remains independent of robot FK.
    for name, address in engine.arm_addresses.items():
        engine.data.qpos[address] = q_by_arm[name]
    engine._put_body("vessel", pose)
    mujoco.mj_kinematics(engine.model, engine.data)
    poses = [dict(geometry_id=engine.geom_names[index],
                  position_m=engine.data.geom_xpos[index].tolist(),
                  quaternion_wxyz=_quat(engine.data.geom_xmat[index].reshape(3, 3)).tolist())
             for index in indices]
    return dict(time_s=float(time_s), global_sim_time_s=float(global_time_s),
                native_step=int(native_step), q_rad=list(q_by_arm[arm][:7]),
                poses=poses, cup_position_m=list(pose.position_m),
                cup_quaternion_wxyz=list(body["quaternion_wxyz"]),
                bead_positions_m=beads.tolist(), bead_inside_carrier=list(inside))


def build_preview(saved_path, review_dir, *, fps=15):
    import numpy as np
    from transport_challenge.contracts import digest
    from supplied_support.cpu_geometry import CpuGeometry

    require(type(fps) is int and fps in (15, 30, 60), "Choose display FPS 15, 30 or 60")
    saved_path = Path(saved_path).resolve(strict=True)
    review_dir = Path(review_dir).resolve(strict=True)
    # The existing exporter validates the immutable map, layout, plan, supplied
    # code, actual installed Panda assets and exact saved source bindings.
    metadata = nominal.build_preview(saved_path, frames_per_stage=2)
    identity = read(saved_path.parent / "input_identity.json")
    result_path = review_dir / "result.json"
    result_hash = sha(result_path)
    result = read(result_path)
    execution = result.get("device_tier_execution")
    require(type(execution) is dict
            and execution.get("schema_version") == "provided-device-tier-mission-result-v1",
            "This review has no recorded native tier motion")
    require(result.get("device_tier_execution_sha256") == digest(execution),
            "Recorded execution digest differs")
    require(execution.get("saved_plan_file_sha256") == metadata["saved_plan_sha256"]
            and execution.get("saved_bundle_sha256") == metadata["bundle_sha256"]
            and result.get("saved_plan_file_sha256") == metadata["saved_plan_sha256"],
            "Review and saved CPU plan differ")
    require(result.get("selected_design_sha256") == metadata["design_sha256"]
            and execution["task_contract"]["difficulty"] == metadata["difficulty"],
            "Review layout or difficulty differs")
    contents = execution.get("contents", "beads")
    identities = execution["original_sample_ids"]
    require(contents in ("beads", "empty") and len(identities) == (4 if contents == "beads" else 0),
            "Only actual bead or empty-cup reviews are supported")
    bead_ids = [item["id"] for item in identities]
    require(len(set(bead_ids)) == len(bead_ids), "Duplicate recorded bead IDs")
    radii = {item["radius_m"] for item in identities}
    require(not identities or len(radii) == 1, "Recorded bead radii differ")
    engine = CpuGeometry(identity["world"], identity["scenario"])
    geometry, indices = nominal._catalog(engine)
    require(geometry == metadata["geometry"], "Measured preview geometry differs")
    native_dir = review_dir / "device_tier_execution"
    source_hashes = {str(result_path): result_hash}
    phases = copy.deepcopy(execution.get("phases", []))
    failed = execution.get("failed_phase_prefix")
    if failed is not None:
        require(type(failed) is dict and type(failed.get("checks")) is dict,
                "Failed physical prefix has no original checks")
        item = copy.deepcopy(failed)
        item.update(phase_id=f"phase_{len(phases):03d}_failed", failed_prefix=True,
                    control=item["checks"]["checks"]["control"], payload=item["checks"]["checks"]["payload"])
        phases.append(item)
    require(phases and len(phases) <= 100, "No bounded actual phase history available")
    returns = {}
    for entry in execution.get("donor_returns", []):
        receipt = entry["receipt"]
        require(receipt.get("physical_steps") == 0 and receipt.get("simulation_duration_s") == 0,
                "Donor-return clock contract differs")
        require(digest(receipt["recorded_frames"]) == receipt["recorded_frames_sha256"],
                "Recorded donor-return frames changed")
        checked_artifact(receipt["native_transcript"], native_dir, source_hashes)
        require(receipt["native_scene_step"] not in returns, "Duplicate donor return clock")
        returns[receipt["native_scene_step"]] = receipt
    stages = []
    previous_end = 0
    total_frames = 0
    for phase in phases:
        start, end = phase["start_native_step"], phase["end_native_step"]
        require(type(start) is int and type(end) is int and start == previous_end and end > start,
                "Recorded physical phases are not continuous")
        previous_end = end
        path = checked_artifact(phase["raw_history"], native_dir, source_hashes)
        control, payload = phase["control"], phase["payload"]
        names, channels = control["arm_order"], control["history_channels"]
        require(names == ["arm_0", "arm_1", "arm_2"] and channels.count("post_q") == 1,
                "Actual control identity/layout differs")
        require(payload["original_bead_ids"] == identities, "Bead identities changed in a phase")
        count = end - start
        with np.load(path, allow_pickle=False) as archive:
            c, p = archive["control"], archive["payload"]
            require(c.shape == (count, 27, len(channels)) and p.shape == (count, 14 + 8*len(identities))
                    and np.isfinite(c).all() and np.isfinite(p).all(),
                    "Actual control/payload history shape or finite-state error")
            arm = phase["active_arm_id"]
            require(arm in names, "Unknown recorded active arm")
            frames = []
            for i in sampled_indices(count, fps):
                q_by_arm = {name:c[i, 9*j:9*j+9, channels.index("post_q")].tolist()
                            for j, name in enumerate(names)}
                bead_positions = [p[i, 14+8*j:17+8*j].tolist() for j in range(len(identities))]
                frames.append(actual_frame(engine, indices, arm, q_by_arm,
                    dict(position_m=p[i, :3].tolist(), quaternion_wxyz=p[i, 3:7].tolist()),
                    bead_positions, time_s=i*DT, global_time_s=(start+i+1)*DT,
                    native_step=start+i+1, inside=[bool(p[i, 20+8*j]) for j in range(len(identities))]))
        stages.append(dict(stage_id=phase["phase_id"]+"__"+phase["purpose"], kind="physical",
                           active_arm_id=arm, phase=phase["phase"], purpose=phase["purpose"],
                           duration_s=(count-1)*DT, physical_duration_known=True,
                           simulation_duration_s=count*DT, start_native_step=start, end_native_step=end,
                           failed_prefix=bool(phase.get("failed_prefix")), source_history_sha256=sha(path),
                           frames=frames))
        total_frames += len(frames)
        if end in returns:
            receipt = returns.pop(end)
            arm = receipt["arm_id"]
            source_frames = receipt["recorded_frames"]
            require(source_frames and source_frames[0]["display_s"] == 0., "Missing actual return start")
            frames = []
            for frame in source_frames:
                require(frame["step"] == end and frame["simulation_s"] == end*DT
                        and [m["id"] for m in frame["materials"]] == bead_ids,
                        "Donor return changed the native clock or bead IDs")
                frames.append(actual_frame(engine, indices, arm,
                    {name:frame["arms"][name]["q"] for name in names}, frame["body"],
                    [m["position_m"] for m in frame["materials"]], time_s=frame["display_s"],
                    global_time_s=end*DT, native_step=end,
                    inside=[m["inside_carrier"] for m in frame["materials"]]))
            stages.append(dict(stage_id=arm+"__recorded_kinematic_return", kind="physical",
                               active_arm_id=arm, phase="kinematic_return", purpose="Recorded zero-physics donor return",
                               duration_s=frames[-1]["time_s"], simulation_duration_s=0.,
                               physical_duration_known=True, zero_physics_return=True,
                               recorded_fps=receipt["recorded_frames_fps"], frames=frames))
            total_frames += len(frames)
        require(total_frames <= MAX_FRAMES, "Preview exceeds 30,000 display frames")
    require(not returns, "A recorded donor return has no matching physical phase")
    require(len(stages) <= 100, "Preview exceeds 100 stages")
    metadata.update(schema_version="ap-student-physical-preview-v1", geometry=geometry, stages=stages,
        result_sha256=result_hash, bead_ids=bead_ids, bead_radius_m=next(iter(radii), .012),
        contents=contents, display_fps=fps, recorded_physics_dt_s=DT,
        original_result_status=result.get("status"), original_full_mission_passed=result.get("full_mission_passed") is True,
        failed_prefix_included=failed is not None, total_display_frames=total_frames,
        source_record_sha256={str(Path(k).relative_to(review_dir)):v for k,v in source_hashes.items()},
        scope=dict(geometry_only=False, actual_recorded_states=True, genesis_imported=False,
                   physical_steps=0, ik_calls=0, planner_calls=0, source_commands_and_times_unchanged=True,
                   collision_or_retention_certificate=False, no_state_interpolation=True,
                   note="Recorded Genesis robot/cup/bead states reconstructed with supplied collision geometry. Display sampling is not physics resolution. Zero-physics donor returns have a separate display clock. This export does not rerun, rescore or upload the review."))
    metadata.pop("frames_per_stage", None)
    for path, expected in source_hashes.items():
        require(sha(path) == expected, "Recorded source changed during preview export")
    json.dumps(metadata, allow_nan=False)
    return metadata


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--saved-tier", type=Path, required=True)
    parser.add_argument("--review-dir", type=Path, required=True)
    parser.add_argument("--fps", type=int, choices=(15, 30, 60), default=15,
                        help="Visual samples per second only. Physics settings remain unchanged.")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    output = args.output.resolve()
    if output.exists() or not output.is_relative_to((ROOT/"output").resolve()):
        parser.error("Use a fresh preview JSON file under this repository's output directory")
    blocker = nominal.NoGenesis()
    sys.meta_path.insert(0, blocker)
    started = time.perf_counter()
    try:
        require(not any(n == "genesis" or n.startswith("genesis.") for n in sys.modules),
                "Use a fresh CPU-only export process")
        result = build_preview(args.saved_tier, args.review_dir, fps=args.fps)
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("x", encoding="utf-8") as stream:
            json.dump(result, stream, separators=(",", ":"), allow_nan=False)
            stream.write("\n")
        print(json.dumps(dict(status="RECORDED_PHYSICS_PREVIEW_EXPORTED", output=str(output),
                              frames=result["total_display_frames"], physical_steps=0,
                              wall_s=time.perf_counter()-started)))
    except (ValueError, OSError, KeyError, TypeError, RuntimeError) as exc:
        parser.exit(2, f"Preview export failed: {exc}\n")
    finally:
        sys.meta_path.remove(blocker)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
