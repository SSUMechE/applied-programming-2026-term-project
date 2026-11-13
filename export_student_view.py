"""Export an optional CPU geometry preview of your existing saved mission.

This reads the saved plan and its sibling input_identity.json. It never plans,
retimes, runs IK, integrates physics, or modifies the saved inputs. The viewer
shows commanded robot/cup geometry, not a physical success or bead-retention
certificate. Use the original environment and keep the saved source files.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.abc
import json
import math
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parent
MAPS = {"easy": "easy_open_v1", "medium": "medium_staggered_v1", "hard": "hard_industrial_v1"}
MAP_FILES = ("difficulty_target.json", "initialization.json", "manifest.json",
             "map_geometry.json", "physics_settings_reference.json", "world.json")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read(path):
    def reject(value):
        raise ValueError("Nonfinite JSON value: " + value)
    return json.loads(Path(path).read_bytes(), parse_constant=reject)


class NoGenesis(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "genesis" or fullname.startswith("genesis."):
            raise RuntimeError("The optional geometry preview must not import Genesis")
        return None


def _trajectory(record):
    from transport_challenge.joint_planning import JointLimits, JointTrajectory
    result = JointTrajectory(record["waypoints"], record["edge_durations_s"], JointLimits(**record["limits"]))
    require(result.to_record() == record, "Saved trajectory or timing differs")
    return result


def sample_times(trajectory, limit):
    """Bound display work without changing a single saved edge or duration."""
    duration = trajectory.duration_s
    if duration == 0:
        return [0.0]
    # Python's compensated sum and incremental waypoint addition can differ by
    # a few ulps. Keep the authoritative duration as the sole final timestamp.
    waypoints = sorted({0., duration, *(min(t, duration) for t in trajectory.waypoint_times_s[1:-1])})
    if len(waypoints) <= limit:
        extra = limit - len(waypoints)
        times = set(waypoints)
        times.update(duration * i / (extra + 1) for i in range(1, extra + 1))
    else:
        times = {duration * i / (limit - 1) for i in range(limit)}
    times.update((0.0, duration))
    return sorted(times)


def _catalog(engine):
    """Reuse each real model's local mesh with its FK transform per frame."""
    import mujoco
    import numpy as np
    from transport_challenge.joint_kinematics import WorldGeometry
    cube = np.asarray([(x, y, z) for x in (-1., 1.) for y in (-1., 1.) for z in (-1., 1.)])
    faces = ((0,1,3),(0,3,2),(4,6,7),(4,7,5),(0,4,5),(0,5,1),
             (2,3,7),(2,7,6),(0,2,6),(0,6,4),(1,5,7),(1,7,3))
    catalog, indices = [], []
    for index, name in enumerate(engine.geom_names):
        owner = engine.owners[index]
        if owner["kind"] not in ("arm", "tray"):
            continue
        shape = int(engine.model.geom_type[index])
        if shape == mujoco.mjtGeom.mjGEOM_MESH:
            mesh = int(engine.model.geom_dataid[index])
            first, count = int(engine.model.mesh_vertadr[mesh]), int(engine.model.mesh_vertnum[mesh])
            vertices = engine.model.mesh_vert[first:first+count].astype(float)
            first, count = int(engine.model.mesh_faceadr[mesh]), int(engine.model.mesh_facenum[mesh])
            triangles = engine.model.mesh_face[first:first+count].astype(int).tolist()
            kind = "mesh"
        elif shape == mujoco.mjtGeom.mjGEOM_BOX:
            vertices = cube * engine.model.geom_size[index]
            triangles, kind = list(faces), "box"
        else:
            raise ValueError("Unsupported robot/cup preview geometry: " + name)
        checked = WorldGeometry(name, owner["kind"], owner["entity"], owner["link"],
                                kind, (0.,0.,0.), (1.,0.,0.,0.),
                                vertices_m=vertices.tolist(), triangles=triangles)
        require(checked.vertices_m and checked.triangles, "Preview mesh has no surface")
        catalog.append(dict(geometry_id=name, owner_kind=owner["kind"], entity_id=owner["entity"],
                            link_id=owner["link"], shape=kind,
                            vertices_local_m=checked.vertices_m, triangles=checked.triangles))
        indices.append(index)
    require({c["entity_id"] for c in catalog if c["owner_kind"] == "arm"} == {"arm_0", "arm_1", "arm_2"}
            and any(c["owner_kind"] == "tray" for c in catalog), "Preview must include all three arms and cup")
    return catalog, indices


def _frame(cpu, indices, q, time_s):
    from supplied_support.cpu_geometry import _quat
    from transport_challenge.contracts import point
    engine = cpu.geometry
    # This CPU-only operation calls mj_kinematics, never mj_step or IK.
    cpu._set(q)
    poses = []
    for index in indices:
        position = point(engine.data.geom_xpos[index].tolist(), 3)
        quaternion = point(_quat(engine.data.geom_xmat[index].reshape(3, 3)).tolist(), 4)
        require(abs(math.hypot(*quaternion) - 1.) < 1e-6, "Nonunit preview quaternion")
        poses.append(dict(geometry_id=engine.geom_names[index], position_m=position,
                          quaternion_wxyz=quaternion))
    return dict(time_s=time_s, q_rad=list(q), poses=poses, cup_position_m=engine.body_pose.position_m)


def build_preview(saved_path, *, frames_per_stage=80):
    """Read a full CPU export and reconstruct only its saved nominal geometry."""
    require(type(frames_per_stage) is int and 2 <= frames_per_stage <= 200,
            "--frames-per-stage must be an integer from 2 to 200")
    from physics.tier_mission_plan import TierMissionPlan
    from physics.automatic_runtime_binding import prepare_automatic_scenario
    from physics.one_carry_planning import material_inputs
    from physics.carried_body import WorldPose
    from transport_challenge.contracts import digest
    from transport_challenge.three_arm_design import ThreeArmDesign, bind_three_arm_design
    from supplied_support.cpu_geometry import CpuGeometry

    saved_path = Path(saved_path).resolve(strict=True)
    plan = TierMissionPlan.from_file(saved_path)
    value = plan.record()
    identity_path = saved_path.parent / "input_identity.json"
    identity_sha = sha(identity_path)
    identity = read(identity_path)
    require(set(identity) == {"world", "scenario", "design"}, "Malformed saved input identity")
    tier = plan.task_contract["difficulty"]
    map_id = MAPS[tier]
    task = ROOT / "cases/difficulty_20261106" / map_id
    source_files = {(Path("cases/difficulty_20261106") / map_id / name).as_posix(): sha(task/name)
                    for name in MAP_FILES}
    original_world, original_scenario = read(task/"world.json"), read(task/"initialization.json")
    target, manifest = read(task/"difficulty_target.json"), read(task/"manifest.json")
    require(target["difficulty"] == tier and target["case_id"] == original_world["case_record"]["case_id"],
            "Saved tier and fixed map differ")
    require(sha(task/"manifest.json") == value["task_manifest_sha256"], "Saved task manifest differs")
    for relative, expected in manifest["files"].items():
        require(sha(ROOT/relative) == expected, "Fixed map file differs: " + relative)
    # Importing this exporter in another checkout cannot silently use different
    # supplied geometry code. Student code is never imported or executed here.
    roots = [Path(name).parent.parent for name in value["source_pins"]
             if Path(name).as_posix().endswith("/physics/plan_student_transport.py")]
    require(len(roots) == 1, "Saved source root is ambiguous")
    original_root = roots[0]
    for name, expected in value["source_pins"].items():
        old = Path(name)
        if old.is_relative_to(original_root):
            relative = old.relative_to(original_root)
            if relative.parts[0] != "student":
                require(sha(ROOT/relative) == expected, "Current supplied source differs: " + str(relative))
    design = ThreeArmDesign.from_record(identity["design"], original_world)
    bound = bind_three_arm_design(design, original_world, original_scenario)
    scenario, _ = prepare_automatic_scenario(bound["scenario"])
    world, scenario = material_inputs(bound["world"], scenario, "beads")
    require(identity == dict(world=world, scenario=scenario, design=design.to_record()),
            "Saved layout identity differs from its bound fixed map")
    plan.verify_bound_inputs(world, scenario, design_sha256=design.sha256,
                             task_manifest_sha256=sha(task/"manifest.json"))
    require(digest(original_world) == plan.task_contract["source_world_sha256"], "Original world digest differs")
    engine = CpuGeometry(world, scenario)
    expected_limits = value["stages"][0]["trajectory"]["limits"]["position_rad"]
    # Saved precision is part of the existing CPU plan, never a display choice.
    if [list(row) for row in engine.bounds] != expected_limits:
        ids = [engine.model.joint("arm_0__"+name).id for name in scenario["case"]["arm_joint_names"]]
        engine.bounds = tuple(tuple(float(v) for v in row) for row in engine.model.jnt_range[ids])
    require([list(row) for row in engine.bounds] == expected_limits, "Saved native joint bounds differ")
    for name, expected in engine.source_pins.items():
        require(value["source_pins"].get(name) == expected, "Installed robot asset differs from saved source")
    geometry, indices = _catalog(engine)
    commands = copy.deepcopy(value["initial_commands_by_arm"])
    stages = []
    for saved in value["stages"]:
        arm, kind = saved["active_arm_id"], saved["kind"]
        trajectory = _trajectory(saved["trajectory"])
        require(saved["expected_peer_commands_by_arm"] == {n:c for n,c in commands.items() if n != arm}
                and list(trajectory.waypoints[0]) == commands[arm]["q_rad"], "Saved peer boundary differs")
        body = WorldPose(**{k:saved["nominal_body_world"][k] for k in ("position_m", "quaternion_wxyz")})
        cpu = engine.configure(arm, commands, body, holder_arm_id=arm if kind == "carry" else None,
                               hand_to_body=saved["nominal_hand_to_body"] if kind == "carry" else None)
        frames = [_frame(cpu, indices, trajectory.sample(t).q_rad, t)
                  for t in sample_times(trajectory, frames_per_stage)]
        require(frames[0]["q_rad"] == saved["trajectory"]["waypoints"][0]
                and frames[-1]["q_rad"] == saved["trajectory"]["waypoints"][-1], "Preview endpoints changed")
        row = dict(stage_id=saved["stage_id"], kind=kind, active_arm_id=arm,
                   source_support_id=saved["support_id"], destination_support_id=saved.get("destination_support_id"),
                   duration_s=trajectory.duration_s, physical_duration_known=False,
                   source_trajectory=copy.deepcopy(saved["trajectory"]), frames=frames)
        stages.append(row)
        commands[arm]["q_rad"] = list(trajectory.waypoints[-1])
        schedule = plan.kinematic_return_after(saved["stage_id"])
        if schedule is not None:
            # Cup stays at the saved supported target throughout this display-only
            # operation. Physical settling and actual release are not synthesized.
            body = WorldPose(**{k:saved["nominal_goal_body_world"][k] for k in ("position_m", "quaternion_wxyz")})
            cpu = engine.configure(arm, commands, body)
            legs = [_trajectory(leg["trajectory"]) for leg in schedule["legs"]]
            points = [q for leg_index, tr in enumerate(legs)
                      for q in list(reversed(tr.waypoints))[0 if leg_index == 0 else 1:]]
            durations = [t for tr in legs for t in reversed(tr.edge_durations_s)]
            from transport_challenge.joint_planning import JointTrajectory
            reverse = JointTrajectory(points, durations, legs[0].limits)
            # Sample the ORIGINAL interpolators backward, preserving their exact
            # q values. The concatenated reverse record describes the display.
            frames = []
            for t in sample_times(reverse, frames_per_stage):
                if t <= legs[0].duration_s:
                    q = legs[0].sample(max(0., legs[0].duration_s-t)).q_rad
                else:
                    q = legs[1].sample(max(0., min(legs[1].duration_s, legs[1].duration_s-(t-legs[0].duration_s)))).q_rad
                if t == 0.: q = legs[0].waypoints[-1]
                if t == reverse.duration_s: q = legs[-1].waypoints[0]
                frames.append(_frame(cpu, indices, q, t))
            stages.append(dict(stage_id=arm+"__kinematic_return", kind="kinematic_return", active_arm_id=arm,
                               source_support_id=saved["destination_support_id"], destination_support_id=None,
                               duration_s=reverse.duration_s, simulation_duration_s=0., physical_duration_known=True,
                               source_stage_ids=[leg["source_stage_id"] for leg in schedule["legs"]],
                               original_return_legs=copy.deepcopy(schedule["legs"]),
                               source_trajectory=reverse.to_record(), frames=frames))
            commands[arm] = copy.deepcopy(schedule["target_command"])

    design_record = design.to_record()
    bases = [dict(id=robot["id"], mount_region=choice["mount_region"],
                  position_m=[choice["base_placement"]["x_m"], choice["base_placement"]["y_m"], robot["base_z_m"]],
                  yaw_rad=choice["base_placement"]["yaw_rad"])
             for robot, choice in zip(scenario["robots"], design_record["arms"])]
    regions = {r["support_id"]:r for r in design.space_record()["relay_regions"]}
    handoffs = [dict(index=i, support_id=relay["support_id"],
                     position_m=[relay["x_m"], relay["y_m"], regions[relay["support_id"]]["support_top_z_m"]],
                     body_root_z_m=regions[relay["support_id"]]["body_root_z_m"])
                for i, relay in enumerate(design_record["relays"][:len(plan.required_arm_ids)-1])]
    result = dict(schema_version="ap-student-motion-preview-v1", difficulty=tier, map_id=map_id,
                  source_files_sha256=source_files, saved_plan_sha256=plan.file_sha256, bundle_sha256=plan.sha256,
                  design_sha256=design.sha256, design=design_record, bases=bases, handoffs=handoffs,
                  geometry=geometry, stages=stages, frames_per_stage=frames_per_stage,
                  scope=dict(geometry_only=True, genesis_imported=False, physical_steps=0, ik_calls=0, planner_calls=0,
                             source_commands_and_times_unchanged=True, beads_simulated=False,
                             collision_or_retention_certificate=False, scene_completion_time_available=False,
                             note="Nominal commanded collision meshes and cup only. Stage time excludes grasp, release, holds and physical settling. Donor return is display-only with zero simulation time. Use Genesis for physical verification."))
    plan.verify_sources()
    require(sha(identity_path) == identity_sha, "Saved input identity changed during export")
    for relative, expected in source_files.items():
        require(sha(ROOT/relative) == expected, "Map changed during preview export")
    json.dumps(result, allow_nan=False)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--saved-tier", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--frames-per-stage", type=int, default=80,
                        help="2 to 200 display samples per stage (default 80). Does not change saved timing or validation.")
    args = parser.parse_args(argv)
    output = args.output.resolve()
    if output.exists():
        parser.error("Use a fresh preview JSON output")
    if not output.is_relative_to((ROOT/"output").resolve()):
        parser.error("Write the preview under this repository's output directory")
    started = time.perf_counter()
    blocker = NoGenesis()
    sys.meta_path.insert(0, blocker)
    originals = {}
    old_ik = None
    try:
        require(not any(n == "genesis" or n.startswith("genesis.") for n in sys.modules), "Use a fresh CPU process")
        import mujoco
        def blocked(*args, **kwargs):
            raise RuntimeError("Physical integration is forbidden in the geometry preview")
        for name in ("mj_step", "mj_step1", "mj_step2", "mj_forward", "mj_inverse", "mj_Euler", "mj_RungeKutta"):
            if hasattr(mujoco, name):
                originals[name] = getattr(mujoco, name)
                setattr(mujoco, name, blocked)
        from supplied_support.cpu_geometry import CpuOracle
        old_ik = CpuOracle.ik
        CpuOracle.ik = blocked
        result = build_preview(args.saved_tier, frames_per_stage=args.frames_per_stage)
        result["export_wall_s"] = time.perf_counter()-started
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(result, stream, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
            stream.write("\n")
        print(json.dumps(dict(status="CPU_MOTION_PREVIEW_EXPORTED", output=str(output),
                              stages=len(result["stages"]), frames=sum(len(s["frames"]) for s in result["stages"]),
                              bytes=output.stat().st_size, export_wall_s=result["export_wall_s"], physical_steps=0)))
        return 0
    except (OSError, ValueError, RuntimeError, KeyError, TypeError) as error:
        parser.exit(2, "Preview rejected: " + str(error) + "\n")
    finally:
        if old_ik is not None: CpuOracle.ik = old_ik
        for name, original in originals.items(): setattr(mujoco, name, original)
        sys.meta_path.remove(blocker)


if __name__ == "__main__":
    raise SystemExit(main())
