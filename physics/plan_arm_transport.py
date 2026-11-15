"""CPU-only independent arm planning and shared owned-process publication.

Students implement the existing plan_path callback. This supplied command plans
only one canonical approach/carry pair. The merge command reuses the worker and
publication infrastructure but never calls a student planner or an IK solver.
"""
from __future__ import annotations

import argparse
import ast
import base64
from contextlib import contextmanager
import copy
import hashlib
import inspect
import json
import math
from pathlib import Path
import sys
import time
import traceback

from physics.arm_mission_plan import (
    ROOT, ARMS, SCHEMA, SCOPE, CPU_STATUS, ArmMissionPlan, read, write_new, seal,
    relative_path, python_snapshot, used_student_sources, source_manifest,
    current_pins, common_problem, canonical_boundary, validate_component_set,
    model_asset_root, model_asset_sources,
)
from physics.saved_motion_planning import require, file_sha, saved_wait_policy
from physics.plan_student_transport import (
    NoGenesis, block_integration, restore_integration, load_strategy, TimedStrategy,
    native_geometry_precision, cpu_geometry_evidence, supervise_cpu_worker,
    cpu_attribution, PLANNING_OPTIONS, optional_strategy_receipt,
)
from transport_challenge.contracts import digest


def parser(operation="plan"):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--difficulty", choices=("easy", "medium", "hard"), required=True)
    p.add_argument("--task", type=Path)
    p.add_argument("--design", type=Path)
    p.add_argument("--material", choices=("beads",), default="beads")
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--wall-limit-s", type=float, default=0., help="Deprecated and ignored. Computation time is recorded without a cap.")
    p.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    if operation == "plan":
        p.add_argument("--arm", choices=ARMS, required=True)
        p.add_argument("--strategy")
        p.add_argument("--seed", type=int, default=71)
        p.add_argument("--max-queries", type=int, default=50000)
        p.add_argument("--endpoint-attempts", type=int, default=32)
        p.add_argument("--edge-timing-hook", action="store_true")
    else:
        p.add_argument("--arm-plans", type=Path, nargs="+", required=True)
    return p


def prepare_problem(args):
    """Build current shared geometry without another arm's paths or any dynamics."""
    from student_design_input import COMPACT_SCHEMA, expand_design
    from transport_challenge.three_arm_design import ThreeArmDesign, bind_three_arm_design
    from physics.automatic_runtime_binding import prepare_automatic_scenario
    from physics.one_carry_planning import material_inputs
    from physics.tier_mission_plan import make_task_contract, verify_task_design
    from supplied_support.cpu_geometry import CpuGeometry
    case_names = {"easy": "easy_open_v1", "medium": "medium_staggered_v1", "hard": "hard_industrial_v1"}
    task = (args.task or ROOT / "cases/difficulty_20261106" / case_names[args.difficulty]).resolve(strict=True)
    design_path = (args.design or ROOT / "student" / (args.difficulty + "_design.json")).resolve(strict=True)
    task_relative, design_relative = relative_path(task), relative_path(design_path)
    require(design_relative.startswith("student/"), "Independent design input must remain under student/")
    manifest = read(task / "manifest.json")
    for name, expected in manifest["files"].items():
        require(file_sha(ROOT / name) == expected, "Provided task source changed: " + name)
    original_world, original_scenario = read(task / "world.json"), read(task / "initialization.json")
    target = read(task / "difficulty_target.json")
    require(target["difficulty"] == args.difficulty
            and target["case_id"] == original_world["case_record"]["case_id"], "Difficulty and map differ")
    raw_design = read(design_path)
    expanded = expand_design(raw_design, difficulty=args.difficulty, world=original_world,
                defaults=read(ROOT / "supplied_design_defaults.json")) if raw_design.get("schema_version") == COMPACT_SCHEMA else raw_design
    design = ThreeArmDesign.from_record(expanded, original_world)
    bound = bind_three_arm_design(design, original_world, original_scenario)
    world = bound["world"]
    scenario, overlay = prepare_automatic_scenario(bound["scenario"])
    world, scenario = material_inputs(world, scenario, "beads", precision=None, runtime_profile=None)
    contract = make_task_contract(original_world, original_scenario, difficulty=args.difficulty,
                    task_manifest_sha256=file_sha(task / "manifest.json"), selected_world=world)
    verify_task_design(contract, world, scenario)
    geometry = native_geometry_precision(CpuGeometry(world, scenario), scenario, "32")
    model_assets = {Path(p).resolve().relative_to(model_asset_root()).as_posix(): h
                    for p, h in geometry.source_pins.items()}
    require(model_assets == model_asset_sources(), "Actual CPU collision assets differ from source closure")
    commands = {name: {"q_rad": values[:7], "finger_q_m": values[7:]}
                for name, values in geometry.initial_q_by_arm.items()}
    constants = {}
    for node in ast.parse((ROOT / "physics/episode_runtime.py").read_text(encoding="utf-8")).body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name) and node.targets[0].id in ("PROTOCOL", "TRANSPORT_PROTOCOL"):
            constants[node.targets[0].id] = ast.literal_eval(node.value)
    waits = {**constants, "additional_material_wait_s": .5, "maximum_additional_material_wait_s": 5.,
             "policy": "Original supplied actual support/rest gates decide whether the source-pinned conditional material wait is needed. No student route/timing change."}
    require(all(waits.get(k) == v for k, v in saved_wait_policy().items()), "Provided wait policy differs")
    return {
        "world": world, "scenario": scenario, "design": design.to_record(),
        "task_contract": contract, "initial_commands_by_arm": commands,
        "automatic_initialization_overlay": overlay,
        "task_relative": task_relative, "design_relative": design_relative,
        "joint_speed_scale": bound["joint_speed_scale"], "provided_wait_policy": waits,
        "task_manifest_sha256": file_sha(task / "manifest.json"),
        "model_assets": model_assets,
    }, geometry


def base_record(inputs):
    identity = {k: copy.deepcopy(inputs[k]) for k in ("world", "scenario", "design")}
    value = {
        "schema_version": SCHEMA, "scope": SCOPE, "payload": "beads",
        "rigid_runtime_profile": inputs["world"]["rigid_runtime_profile"],
        "post_place_policy": "kinematic_return",
        "task_relative": inputs["task_relative"], "design_relative": inputs["design_relative"],
        "task_contract": copy.deepcopy(inputs["task_contract"]),
        "task_contract_sha256": digest(inputs["task_contract"]),
        "world_sha256": digest(inputs["world"]), "scenario_sha256": digest(inputs["scenario"]),
        "design_sha256": digest(inputs["design"]),
        "task_manifest_sha256": inputs["task_manifest_sha256"],
        "initial_commands_by_arm": copy.deepcopy(inputs["initial_commands_by_arm"]),
        "automatic_initialization_overlay": copy.deepcopy(inputs["automatic_initialization_overlay"]),
        "initial_representation": "Each declared7q and provided .04m fingers converted once to the existing native float32 representation. No later path/time snapping.",
        "provided_wait_policy": copy.deepcopy(inputs["provided_wait_policy"]),
        "input_identity": identity,
        "portable_sources": source_manifest(inputs, {}),
        "source_pins": {}, "bound_source_root": str(ROOT),
        "stages": [], "post_place_resets": [], "post_place_returns": [],
        "nominal_only": True, "physical_grasp_verified": False, "physical_transport_verified": False,
        "full_mission_passed": False, "student_release_approved": False, "ranking_eligible": False,
        "cpu_process_supervisor": {"status": "WORKER_CANDIDATE_PENDING"},
    }
    value["common_problem"] = common_problem(value)
    value["common_problem_sha256"] = digest(value["common_problem"])
    return value


class GuardedOracle:
    def __init__(self, query, check):
        self.query, self.check = query, check

    def state_valid(self, q):
        self.check()
        result = self.query.state_valid(q)
        self.check()
        return result

    def edge_valid(self, a, b):
        self.check()
        result = self.query.edge_valid(a, b)
        self.check()
        return result

    def last_query_diagnostic(self):
        return self.query.last_query_diagnostic()


def expected_hand_target(kind, body_pose, destination_body, transform):
    """The same terminal construction used by the complete supplied CPU planner."""
    import numpy as np
    from physics.carried_body import WorldPose
    from physics.provided_manipulation import grasp_endpoint_candidates
    from supplied_support.cpu_geometry import _rotation, _quat
    if kind == "approach":
        raise ValueError("Approach target additionally requires the vessel model")
    nominal_goal = copy.deepcopy(destination_body)
    nominal_command = copy.deepcopy(nominal_goal)
    nominal_command["position_m"][2] -= .0003
    rotation = _rotation(nominal_command["quaternion_wxyz"]) @ _rotation(transform["quaternion_hand_body_wxyz"]).T
    position = np.asarray(nominal_command["position_m"]) - rotation @ transform["translation_hand_m"]
    return nominal_goal, nominal_command, WorldPose(position.tolist(), _quat(rotation).tolist()).to_record()


def plan_selected_arm(args, inputs, geometry, check, output, before_student):
    from dataclasses import replace
    from physics.carried_body import VesselModel, WorldPose, measure_hand_to_body
    from physics.provided_manipulation import grasp_endpoint_candidates, solve_endpoint
    from transport_challenge.joint_context import JointBasePlacement, JointTaskContext
    from transport_challenge.joint_kinematics import BoundedKinematics
    from transport_challenge.joint_planning import JointLimits, make_joint_request, plan_joint_motion
    require(args.arm in inputs["task_contract"]["required_arm_ids"], "Choose an arm participating in this difficulty")
    strategy = load_strategy(args.strategy or ("student." + args.arm + "_strategy"))
    check()
    measured = TimedStrategy(strategy)
    hook = getattr(strategy, "choose_edge_durations_s", None) if args.edge_timing_hook else None
    require(not args.edge_timing_hook or callable(hook), "Selected timing hook is not callable")
    record = base_record(inputs)
    record["selected_arm_id"] = args.arm
    boundary = canonical_boundary(inputs["task_contract"], inputs["initial_commands_by_arm"], inputs["scenario"], args.arm)
    record["arm_boundary"] = boundary
    body_pose = WorldPose(**{k: boundary["body_world"][k] for k in ("position_m", "quaternion_wxyz")})
    model = VesselModel.from_container(inputs["scenario"]["container"])
    commands = copy.deepcopy(inputs["initial_commands_by_arm"])
    transform = None
    for local_index, kind in enumerate(("approach", "carry")):
        check()
        started = time.perf_counter()
        global_index = 2 * int(args.arm[-1]) + local_index
        stage_id = args.arm + "__" + kind
        query = geometry.configure(args.arm, commands, body_pose,
                    holder_arm_id=args.arm if kind == "carry" else None,
                    hand_to_body=transform if kind == "carry" else None)
        oracle = GuardedOracle(query, check)
        start = tuple(commands[args.arm]["q_rad"])
        limits = JointLimits(query.bounds, inputs["scenario"]["case"]["motion_limits"]["velocity_rad_s"],
                             inputs["scenario"]["case"]["motion_limits"]["acceleration_rad_s2"])
        require(oracle.state_valid(start), "Independent arm stage start is invalid: " + json.dumps(query.last_failure))
        nominal_goal = nominal_command = None
        if kind == "approach":
            hand_target = grasp_endpoint_candidates(model, body_pose.to_record())[0].to_record()
        else:
            nominal_goal, nominal_command, hand_target = expected_hand_target(
                kind, body_pose, boundary["destination_body_world"], transform)
        endpoint_started = time.perf_counter()
        kin = BoundedKinematics(query, query.bounds, inputs["scenario"]["case"]["tool_workspace_m"],
                max_fk_calls=args.endpoint_attempts, max_ik_calls=args.endpoint_attempts, wall_check=check)
        endpoint = solve_endpoint(kin, oracle, start, (hand_target,), attempts=args.endpoint_attempts,
                                  seed=args.seed + global_index, wall_check=check)
        require(endpoint["status"] == "ENDPOINT_FOUND", "CPU nominal endpoint not found for " + stage_id)
        endpoint_wall = time.perf_counter() - endpoint_started
        goal = tuple(endpoint["q_rad"])
        recipe = next(r for r in inputs["scenario"]["robots"] if r["id"] == args.arm)
        context_case = copy.deepcopy(inputs["scenario"]["case"])
        context_case["planning"]["tracking_clearance_m"] = geometry.clearance
        context_case["planning"]["edge_max_joint_delta_rad"] = geometry.resolution
        context = JointTaskContext.from_case(context_case,
            base_placement=JointBasePlacement(**recipe["base_placement"]), limits=limits,
            start_tool_pose=query.fk(start), goal_tool_pose=query.fk(goal), active_arm_id=args.arm,
            motion_kind=kind, source_support_id=boundary["source_support_id"],
            destination_support_id=boundary["destination_support_id"] if kind == "carry" else None)
        context = replace(context, fixed_base_z_m=recipe.get("base_z_m", context_case["base_z_m"]))
        search_kin = BoundedKinematics(query, query.bounds, context_case["tool_workspace_m"],
                            max_fk_calls=50000, max_ik_calls=10000, wall_check=check)
        request = make_joint_request(start, goal, limits, max_queries=args.max_queries,
                                     seed=args.seed + global_index, options=PLANNING_OPTIONS)
        planning_started = time.perf_counter()
        plan = plan_joint_motion(request, oracle, measured, limits, speed_scale=inputs["joint_speed_scale"],
                 kinematics=search_kin, context=context, edge_timing_hook=hook,
                 oracle_description="Independent canonical-home arm pair in the complete shared three-robot/map/cup geometry. CPU geometry only. Native practice and full mission remain separate.")
        planning_wall = time.perf_counter() - planning_started
        trajectory = plan.trajectory.to_record()
        stage = {
            "stage_id": stage_id, "kind": kind, "active_arm_id": args.arm,
            "support_id": boundary["source_support_id"],
            "destination_support_id": boundary["destination_support_id"] if kind == "carry" else None,
            "trajectory": trajectory, "nominal_body_world": body_pose.to_record(),
            "nominal_goal_body_world": nominal_goal, "nominal_command_body_world": nominal_command,
            "nominal_goal_hand_world": hand_target,
            "nominal_hand_to_body": copy.deepcopy(transform) if kind == "carry" else None,
            "expected_peer_commands_by_arm": {n: copy.deepcopy(c) for n, c in commands.items() if n != args.arm},
            "offline_planning": {
                "terminal": {"schema_version": "cpu-nominal-terminal-endpoint-v1",
                    "cpu_solver_result": endpoint, "physical_grasp_verified": False, "native_ik_performed": False},
                "terminal_wall_s": endpoint_wall, "planning_wall_s": planning_wall,
                "planning_request": request.to_record(), "stage_wall_s": time.perf_counter() - started,
                "plan_evidence": plan.evidence, "cpu_geometry": query.evidence(),
                "strategy_receipt": optional_strategy_receipt(strategy),
                "global_stage_index": global_index,
            },
        }
        record["stages"].append(stage)
        write_new(output / (stage_id + ".json"), stage)
        commands[args.arm]["q_rad"] = list(trajectory["waypoints"][-1])
        if kind == "approach":
            actual = query.fk(trajectory["waypoints"][-1])
            transform = measure_hand_to_body(WorldPose(actual.position_m, actual.quaternion_wxyz), body_pose).to_record()
        print(json.dumps({"event": "cpu_arm_stage_completed", "stage_id": stage_id,
                          "trajectory_simulation_s": trajectory["duration_s"],
                          "stage_wall_s": stage["offline_planning"]["stage_wall_s"]}), flush=True)
    used = used_student_sources(strategy, before_student)
    record["portable_sources"] = source_manifest(inputs, used)
    record["source_pins"] = current_pins(record["portable_sources"])
    function_text = inspect.getsource(strategy.plan_path)
    record["offline_planner"] = {
        "module": strategy.__name__, "source_sha256": file_sha(inspect.getsourcefile(strategy)),
        "plan_path_source_file": relative_path(inspect.getsourcefile(strategy.plan_path)),
        "plan_path_source_text": function_text,
        "plan_path_source_sha256": hashlib.sha256(function_text.encode()).hexdigest(),
        "cpu_pipeline_module": __name__, "strategy_role": "student_independent_arm",
        "seed": args.seed, "max_queries": args.max_queries, "endpoint_attempts": args.endpoint_attempts,
        "joint_speed_scale": inputs["joint_speed_scale"], "use_edge_timing_hook": args.edge_timing_hook,
        "planning_options": copy.deepcopy(PLANNING_OPTIONS), "post_place_policy": "kinematic_return",
    }
    record["common_problem"] = common_problem(record)
    record["common_problem_sha256"] = digest(record["common_problem"])
    return record, measured.search_wall_s, {
        "selected_arm_id": args.arm, "student_plan_path_invocations": measured.calls,
        "other_arm_planner_invocations": 0, "global_stage_indices": boundary["global_stage_indices"],
    }


@contextmanager
def forbid_merge_search():
    """Make accidental planner, IK or time-parameterization calls fail loudly."""
    import transport_challenge.joint_planning as planning
    import physics.provided_manipulation as manipulation
    from transport_challenge.joint_kinematics import BoundedKinematics
    from supplied_support.cpu_geometry import CpuOracle
    targets = [(planning, "plan_joint_motion"), (planning, "parameterize_joint_path"),
               (manipulation, "solve_endpoint"), (BoundedKinematics, "ik"), (CpuOracle, "ik")]
    originals, calls = [], {}
    for obj, name in targets:
        if not hasattr(obj, name):
            continue
        label = getattr(obj, "__name__", type(obj).__name__) + "." + name
        calls[label] = 0
        original = getattr(obj, name)
        def deny(*args, _label=label, **kwargs):
            calls[_label] += 1
            raise RuntimeError("Assembly forbids search, IK and retiming: " + _label)
        originals.append((obj, name, original))
        setattr(obj, name, deny)
    try:
        yield calls
    finally:
        for obj, name, original in reversed(originals):
            setattr(obj, name, original)


def revalidate_stages(inputs, geometry, ordered_records, check):
    """Validate exact component paths in reconstructed serial geometry; no search."""
    from physics.carried_body import VesselModel, WorldPose, measure_hand_to_body
    from physics.provided_manipulation import grasp_endpoint_candidates
    from transport_challenge.joint_planning import JointLimits, JointTrajectory, make_joint_request, validate_joint_path
    records = validate_component_set(ordered_records, difficulty=inputs["task_contract"]["difficulty"])
    expected = base_record(inputs)
    require(all(r["common_problem"] == expected["common_problem"] for r in records),
            "Current actual shared inputs/home commands differ from producer problems")
    identity = {k: inputs[k] for k in ("world", "scenario", "design")}
    require(all(r["input_identity"] == identity for r in records), "Current actual expanded input identity differs")
    commands = copy.deepcopy(inputs["initial_commands_by_arm"])
    model = VesselModel.from_container(inputs["scenario"]["container"])
    body_pose = WorldPose(**{k: inputs["scenario"]["body_pose"][k] for k in ("position_m", "quaternion_wxyz")})
    rows = []
    with forbid_merge_search() as forbidden:
        for record in records:
            arm = record["selected_arm_id"]
            boundary = canonical_boundary(inputs["task_contract"], inputs["initial_commands_by_arm"], inputs["scenario"], arm)
            require(record["arm_boundary"] == boundary and body_pose.to_record() == boundary["body_world"],
                    "Serial cup boundary does not match the selected arm")
            transform = None
            for stage in record["stages"]:
                check()
                kind = stage["kind"]
                require(stage["expected_peer_commands_by_arm"] == {n: c for n, c in commands.items() if n != arm},
                        "Serial peer commands differ from a stored arm stage")
                query = geometry.configure(arm, commands, body_pose,
                            holder_arm_id=arm if kind == "carry" else None,
                            hand_to_body=transform if kind == "carry" else None)
                limits = JointLimits(query.bounds, inputs["scenario"]["case"]["motion_limits"]["velocity_rad_s"],
                                     inputs["scenario"]["case"]["motion_limits"]["acceleration_rad_s2"])
                tr = stage["trajectory"]
                require(tr["limits"] == limits.to_record() and tr["waypoints"][0] == commands[arm]["q_rad"],
                        "Stored arm limits or preceding command changed")
                trajectory = JointTrajectory(tr["waypoints"], tr["edge_durations_s"], limits)
                require(trajectory.to_record() == tr, "Assembly cannot repair or retime a path")
                if kind == "approach":
                    nominal_goal = nominal_command = None
                    hand_target = grasp_endpoint_candidates(model, body_pose.to_record())[0].to_record()
                    require(stage["nominal_hand_to_body"] is None, "Approach unexpectedly contains a held transform")
                else:
                    nominal_goal, nominal_command, hand_target = expected_hand_target(
                        kind, body_pose, boundary["destination_body_world"], transform)
                    require(stage["nominal_hand_to_body"] == transform, "Stored attachment differs from actual CPU approach FK")
                require(stage["nominal_body_world"] == body_pose.to_record()
                        and stage["nominal_goal_body_world"] == nominal_goal
                        and stage["nominal_command_body_world"] == nominal_command
                        and stage["nominal_goal_hand_world"] == hand_target,
                        "Stored terminal geometry differs from current shared design")
                actual = query.fk(tr["waypoints"][-1])
                position_error = math.dist(actual.position_m, hand_target["position_m"])
                dot = math.fsum(a * b for a, b in zip(actual.quaternion_wxyz, hand_target["quaternion_wxyz"]))
                cosine = abs(dot) / (math.hypot(*actual.quaternion_wxyz) * math.hypot(*hand_target["quaternion_wxyz"]))
                orientation_error = 2 * math.acos(min(1., cosine))
                require(position_error <= .003 and orientation_error <= .02,
                        "Stored endpoint fails the original supplied FK residual limits")
                request = make_joint_request(tuple(commands[arm]["q_rad"]), tuple(tr["waypoints"][-1]), limits,
                                             max_queries=50000, seed=0, options=PLANNING_OPTIONS)
                oracle = GuardedOracle(query, check)
                states = validate_joint_path(tr["waypoints"], request, limits, oracle)
                require([list(q) for q in states] == tr["waypoints"], "Validation changed stored waypoints")
                rows.append({
                    "stage_id": stage["stage_id"], "trajectory_sha256": digest(tr),
                    "whole_path_states_and_edges_valid": True, "endpoint_fk_valid": True,
                    "endpoint_position_error_m": position_error, "endpoint_orientation_error_rad": orientation_error,
                    "peer_commands_sha256": digest(stage["expected_peer_commands_by_arm"]),
                    "nominal_hand_to_body_sha256": digest(stage["nominal_hand_to_body"]),
                    "cpu_geometry": query.evidence(), "search_calls": 0, "ik_calls": 0, "retiming_calls": 0,
                })
                commands[arm]["q_rad"] = copy.deepcopy(tr["waypoints"][-1])
                if kind == "approach":
                    transform = measure_hand_to_body(WorldPose(actual.position_m, actual.quaternion_wxyz), body_pose).to_record()
                else:
                    body_pose = WorldPose(nominal_goal["position_m"], nominal_goal["quaternion_wxyz"])
                    if arm in inputs["task_contract"]["required_arm_ids"][:-1]:
                        commands[arm] = copy.deepcopy(inputs["initial_commands_by_arm"][arm])
        require(all(count == 0 for count in forbidden.values()), "Assembly attempted forbidden replanning")
    return {"schema_version": "fixed-arm-assembly-geometry-validation-v1", "passed": True,
            "stages": rows, "blocked_algorithm_calls": forbidden,
            "exact_stored_paths_and_times_preserved": True, "physical_steps": 0}


def _worker(args, operation):
    started = time.perf_counter()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    blocker = NoGenesis()
    require(not any(n == "genesis" or n.startswith("genesis.") for n in sys.modules),
            "CPU worker must start without Genesis")
    sys.meta_path.insert(0, blocker)
    import mujoco
    blocked = block_integration(mujoco)
    record = None
    search_wall = 0.
    extra = {}
    failure = None
    guard = None
    try:
        inputs, geometry = prepare_problem(args)
        before_student = python_snapshot()
        provided = source_manifest(inputs, {})
        pins = current_pins(provided)
        pins.update({str((ROOT / name).resolve()): hashlib.sha256(data).hexdigest()
                     for name, data in before_student.items()})
        from physics.source_guard import PinnedSourceGuard
        guard = PinnedSourceGuard(pins, full_interval_s=1.)
        require(guard.before_request(), "CPU input source boundary failed")
        def check():
            require(guard() is True, "CPU source changed during the operation")
        if operation == "plan":
            record, search_wall, extra = plan_selected_arm(args, inputs, geometry, check, output, before_student)
        else:
            from physics.merge_arm_transport import assemble_arm_plans
            record, search_wall, extra = assemble_arm_plans(args, inputs, geometry, check, output)
        check()
        require(not blocker.blocked_attempts and not blocked["attempts"], "Forbidden CPU/native operation attempted")
        write_new(output / "input_identity.json", {k: inputs[k] for k in ("world", "scenario", "design")})
    except BaseException as exc:
        failure = {"type": type(exc).__name__, "message": str(exc), "traceback": traceback.format_exc()}
    finally:
        source_ok = guard.after_request() if guard is not None else False
        restore_integration(mujoco, blocked)
    imported = [n for n in sys.modules if n == "genesis" or n.startswith("genesis.")]
    if failure is None and (not source_ok or imported or blocker.blocked_attempts or blocked["attempts"]):
        failure = {"type": "CpuSourceOrOperationViolation", "message": "CPU source/native admission failed"}
    from physics.tier_mission_plan import CPU_STATUS as TIER_STATUS
    status = CPU_STATUS if operation == "plan" else TIER_STATUS
    summary = {
        "status": status if failure is None else "CPU_ARM_OPERATION_FAILED",
        "planning_scope": SCOPE if operation == "plan" else "tier-mission",
        "operation": "independent_arm_plan" if operation == "plan" else "fixed_arm_path_assembly",
        "material": "beads", "completed_stages": len(record["stages"]) if record else 0,
        "failure": failure, "source_bindings_unchanged": source_ok,
        "source_guard": guard.receipt() if guard is not None else None,
        "genesis_imported_modules": imported, "blocked_genesis_import_attempts": blocker.blocked_attempts,
        "blocked_integration_attempts": list(blocked["attempts"]), "genesis_initialized": False,
        "native_physical_steps": 0, "cpu_physical_steps": 0,
        "process_wall_s": time.perf_counter() - started,
        "full_mission_passed": False, "physical_transport_verified": False, "student_release_approved": False,
        **extra,
    }
    summary.update(cpu_attribution(summary["process_wall_s"], search_wall))
    if operation == "merge":
        summary["cpu_resource_attribution_policy"].update(
            search_scope="No student callback, search, IK or timing hook is invoked by assembly.",
            review_scope="Actual supplied fixed-path CPU validation and owned-process publication.",
            measurement_wrapper="physics.plan_arm_transport owned fixed-path assembly worker")
    write_new(output / "CPU_PLANNING_RESULT.json", summary)
    if failure is None:
        record.update(cpu_planning_wall_s=summary["process_wall_s"], cpu_planning_result=copy.deepcopy(summary),
                      stored_motion_duration_s=sum(s["trajectory"]["duration_s"] for s in record["stages"]),
                      cpu_process_supervisor={"status": "WORKER_CANDIDATE_PENDING"})
        record.update({k: summary[k] for k in ("cpu_search_wall_s", "cpu_review_attribution_wall_s", "cpu_resource_attribution_policy")})
        filename = "saved_arm_mission.json" if operation == "plan" else "saved_tier_mission.json"
        write_new(output / filename, seal(record))
        write_new(output / "source_pins.json", record["source_pins"])
    print(json.dumps({"status": summary["status"], "completed_stages": summary["completed_stages"],
                      "process_wall_s": summary["process_wall_s"], "failure": failure}), flush=True)
    return 0 if failure is None else 1


def main(argv=None, *, operation="plan"):
    argv = list(sys.argv[1:] if argv is None else argv)
    p = parser(operation)
    args = p.parse_args(argv)
    if not math.isfinite(args.wall_limit_s) or args.wall_limit_s < 0:
        p.error("The deprecated wall-time argument must be finite and nonnegative")
    if args.wall_limit_s:
        print("Ignored --wall-limit-s: computation time is recorded, never capped.", file=sys.stderr)
    args.wall_limit_s = 0.
    if operation == "plan" and (args.max_queries < 1 or args.endpoint_attempts < 1):
        p.error("Planning and endpoint query budgets must be positive")
    if args.output.exists() or args.output.resolve() == ROOT:
        p.error("Use a fresh output directory")
    if args.worker:
        return _worker(args, operation)
    started = time.perf_counter()
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    blocker = NoGenesis()
    require(not any(n == "genesis" or n.startswith("genesis.") for n in sys.modules), "Fresh CPU parent required")
    sys.meta_path.insert(0, blocker)
    worker_args = []
    i = 0
    while i < len(argv):
        if argv[i] == "--output":
            i += 2
            continue
        if argv[i].startswith("--output="):
            i += 1
            continue
        worker_args.append(argv[i])
        i += 1
    worker = out / "worker"
    module = "physics.plan_arm_transport" if operation == "plan" else "physics.merge_arm_transport"
    command = [sys.executable, "-B", "-m", module, *worker_args, "--worker", "--output", str(worker)]
    receipt = supervise_cpu_worker(command, output=out, timeout_s=None)
    receipt.update(requested_wall_limit_s=None,
                   scope="Owned independent-arm CPU child startup through exit and cleanup. No Genesis.")
    summary_path = worker / "CPU_PLANNING_RESULT.json"
    summary = read(summary_path) if summary_path.is_file() else {
        "status": "CPU_ARM_OPERATION_FAILED", "completed_stages": 0, "cpu_search_wall_s": 0.,
        "failure": {"type": "CpuWorkerDidNotComplete", "message": "Read worker_stdout.log and CPU_PROCESS_RESULT.json"}}
    success = (receipt.get("worker_exit_code") == 0 and receipt.get("timed_out") is False
               and receipt.get("failure") is None
               and receipt.get("cleanup_errors") == [] and summary.get("failure") is None)
    filename = "saved_arm_mission.json" if operation == "plan" else "saved_tier_mission.json"
    try:
        if not success:
            raise RuntimeError("CPU worker did not complete successfully")
        from physics.tier_mission_plan import TierMissionPlan
        cls = ArmMissionPlan if operation == "plan" else TierMissionPlan
        value = read(worker / filename)
        cls(value, path=worker / filename, require_supervisor=False).verify_sources()
        for name, expected in value["source_pins"].items():
            require(file_sha(name) == expected, "CPU publication source changed: " + name)
        require(summary.get("source_bindings_unchanged") is True, "CPU worker source receipt failed")
        for path in worker.glob("*.json"):
            if path.name not in ("CPU_PLANNING_RESULT.json", filename):
                with (out / path.name).open("xb") as stream:
                    stream.write(path.read_bytes())
        elapsed = time.perf_counter() - started
        imported = [n for n in sys.modules if n == "genesis" or n.startswith("genesis.")]
        require(not imported and not blocker.blocked_attempts, "CPU parent imported Genesis")
        receipt.update(status="COMPLETE_RECORDED", parent_wall_s_before_publication=elapsed,
                       source_bindings_verified=True, worker_result_path=str(summary_path),
                       worker_result_sha256=file_sha(summary_path), parent_genesis_imported_modules=imported,
                       parent_blocked_genesis_import_attempts=list(blocker.blocked_attempts))
        summary["worker_process_wall_s"] = summary["process_wall_s"]
        summary["process_wall_s"] = elapsed
        attribution = cpu_attribution(elapsed, summary["cpu_search_wall_s"])
        if operation == "merge":
            attribution["cpu_resource_attribution_policy"].update(
                search_scope="No student callback, search, IK or timing hook is invoked by assembly.",
                review_scope="Actual supplied fixed-path CPU validation and owned-process publication.",
                measurement_wrapper="physics.plan_arm_transport owned fixed-path assembly worker")
        summary.update(attribution)
        summary["cpu_process_supervisor"] = copy.deepcopy(receipt)
        summary["process_wall_scope"] = "Parent entry through owned child cleanup and source admission before final publication writes."
        value.update(attribution)
        value.update(cpu_planning_result=copy.deepcopy(summary), cpu_process_supervisor=copy.deepcopy(receipt),
                     cpu_planning_wall_s=elapsed)
        value = seal(value)
        pending = out / (".pending_" + filename)
        write_new(pending, value)
        cls(value, path=pending).verify_sources()
        pending.rename(out / filename)
    except BaseException as exc:
        success = False
        elapsed = time.perf_counter() - started
        receipt.update(status="CPU_PUBLICATION_REJECTED",
                       parent_wall_s_before_publication=elapsed,
                       failure={"type": type(exc).__name__, "message": str(exc)})
        if "worker_process_wall_s" not in summary:
            summary["worker_process_wall_s"] = summary.get("process_wall_s")
        summary["process_wall_s"] = elapsed
        summary["process_wall_scope"] = "Parent entry through owned child cleanup and failed publication admission."
        summary.update(status="CPU_ARM_OPERATION_FAILED",
                       failure=summary.get("failure") or receipt["failure"])
    write_new(out / "CPU_PROCESS_RESULT.json", receipt)
    write_new(out / "CPU_PLANNING_RESULT.json", summary)
    print(json.dumps({"status": summary["status"], "completed_stages": summary.get("completed_stages", 0),
                      "process_wall_s": summary.get("process_wall_s"), "failure": summary.get("failure")}), flush=True)
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())

