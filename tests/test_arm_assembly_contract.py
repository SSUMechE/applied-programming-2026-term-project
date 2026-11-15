"""Supplied CPU-only independent-arm contract checks.

These tiny structural fixtures are not collision-free solutions or physical
completion evidence. Real geometry and owned-worker integration are checked
separately. No fixture is exported as a student mission.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

from tests.test_relay_binding import inputs
from tests.test_tier_route_contract import contract_only_bundle
from transport_challenge.contracts import ContractError, digest
from transport_challenge.joint_planning import JointLimits, JointTrajectory
from transport_challenge.three_arm_design import ThreeArmDesign, bind_three_arm_design
from physics.tier_mission_plan import make_task_contract
from physics import arm_mission_plan as arm


def geometric_unit_fixture():
    """Real validator inputs with a transparent toy oracle, never a map solution."""
    from physics import plan_arm_transport as planning
    from physics.carried_body import VesselModel, WorldPose, measure_hand_to_body
    from physics.provided_manipulation import grasp_endpoint_candidates
    value = synthetic_component("easy", "arm_0")
    identity = value["input_identity"]
    inputs_record = {**copy.deepcopy(identity),
        **{k: copy.deepcopy(value[k]) for k in ("task_contract", "initial_commands_by_arm",
            "task_relative", "design_relative", "provided_wait_policy", "task_manifest_sha256")},
        "automatic_initialization_overlay": {}, "joint_speed_scale": .5}
    inputs_record["world"]["rigid_runtime_profile"] = "rigid16_bead_tc2h_v1"
    value["input_identity"] = {k: copy.deepcopy(inputs_record[k]) for k in ("world", "scenario", "design")}
    value["world_sha256"] = digest(inputs_record["world"])
    body = WorldPose(**{k: value["arm_boundary"]["body_world"][k]
                       for k in ("position_m", "quaternion_wxyz")})
    model = VesselModel.from_container(identity["scenario"]["container"])
    target = grasp_endpoint_candidates(model, body.to_record())[0].to_record()
    transform = measure_hand_to_body(WorldPose(target["position_m"], target["quaternion_wxyz"]), body).to_record()
    motion = identity["scenario"]["case"]["motion_limits"]
    bounds = value["stages"][0]["trajectory"]["limits"]["position_rad"]
    limits = JointLimits(bounds, motion["velocity_rad_s"], motion["acceleration_rad_s2"])
    for stage in value["stages"]:
        old = stage["trajectory"]
        stage["trajectory"] = JointTrajectory(old["waypoints"], old["edge_durations_s"], limits).to_record()
        stage["nominal_hand_to_body"] = None
        stage["nominal_goal_body_world"] = stage["nominal_command_body_world"] = None
        stage["nominal_goal_hand_world"] = copy.deepcopy(target)
        if stage["kind"] == "carry":
            goal, command, hand = planning.expected_hand_target("carry", body,
                value["arm_boundary"]["destination_body_world"], transform)
            stage.update(nominal_hand_to_body=transform, nominal_goal_body_world=goal,
                         nominal_command_body_world=command, nominal_goal_hand_world=hand)
    value["common_problem"] = arm.common_problem(value)
    value["common_problem_sha256"] = digest(value["common_problem"])
    value = arm.seal(value)

    class ToyOracle:
        def __init__(self, owner, stage):
            self.owner, self.stage = owner, stage
            self.bounds = bounds
            self.states = self.edges = 0
        def fk(self, q):
            pose = copy.deepcopy(self.stage["nominal_goal_hand_world"])
            pose["position_m"][0] += self.owner.endpoint_offset
            return WorldPose(pose["position_m"], pose["quaternion_wxyz"])
        def state_valid(self, q):
            self.states += 1
            return not self.owner.reject_state
        def edge_valid(self, first, second):
            self.edges += 1
            return not self.owner.reject_edge
        def evidence(self):
            return {"fixture_only": True, "states_checked": self.states, "edges_checked": self.edges}
        def last_query_diagnostic(self):
            return {"fixture_only": True}

    class ToyGeometry:
        def __init__(self):
            self.queries = []
            self.reject_state = self.reject_edge = False
            self.endpoint_offset = 0.
        def configure(self, arm_id, commands, body_pose, *, holder_arm_id=None, hand_to_body=None):
            stage = value["stages"][0 if holder_arm_id is None else 1]
            oracle = ToyOracle(self, stage); self.queries.append(oracle)
            return oracle

    return inputs_record, value, ToyGeometry()


def synthetic_component(tier="hard", selected="arm_0"):
    """Exercise the schema with deliberately unsolved, tiny joint motions."""
    world, scenario, design_record = inputs(tier)
    design = ThreeArmDesign.from_record(design_record, world)
    bound = bind_three_arm_design(design, world, scenario)
    contract = make_task_contract(world, scenario, difficulty=tier,
                                 task_manifest_sha256="a" * 64,
                                 selected_world=bound["world"])
    with tempfile.TemporaryDirectory() as directory:
        pin = Path(directory) / "structural_source.txt"
        pin.write_text("Structural fixture only", encoding="utf-8")
        full = contract_only_bundle(contract, bound, pin)
    result = copy.deepcopy(full)
    result.update(schema_version=arm.SCHEMA, scope=arm.SCOPE,
                  selected_arm_id=selected, ranking_eligible=False,
                  task_relative="cases/structural_fixture",
                  design_relative="student/shared_design.json",
                  bound_source_root=str(Path.cwd().resolve()),
                  input_identity=dict(world=bound["world"], scenario=bound["scenario"],
                                      design=design.to_record()),
                  post_place_returns=[])
    result["stages"] = [s for s in result["stages"] if s["active_arm_id"] == selected]
    result["arm_boundary"] = arm.canonical_boundary(
        contract, result["initial_commands_by_arm"], bound["scenario"], selected)
    for stage in result["stages"]:
        stage["nominal_body_world"] = copy.deepcopy(result["arm_boundary"]["body_world"])
    module = "student." + selected + "_structural_strategy"
    name = module.replace(".", "/") + ".py"
    code = ("def plan_path(request, oracle, *, kinematics=None, context=None):\n"
            "    return None\n")
    snapshot = {"sha256": hashlib.sha256(code.encode()).hexdigest(),
                "bytes_base64": base64.b64encode(code.encode()).decode("ascii")}
    result["portable_sources"] = {
        "schema_version": arm.PORTABLE_SCHEMA,
        "provided": {"physics/structural_fixture.py": "b" * 64},
        "student": {name: snapshot}, "panda_xml_sha256": "c" * 64,
        "model_assets": {"assets/xml/franka_emika_panda/panda.xml": "c" * 64},
    }
    result["source_pins"] = {
        str(Path(result["bound_source_root"]) / "physics/structural_fixture.py"): "b" * 64,
        str(Path(result["bound_source_root"]) / name): snapshot["sha256"],
        str(Path(result["bound_source_root"]).parent / "model/assets/xml/franka_emika_panda/panda.xml"): "c" * 64,
    }
    result["offline_planner"] = {
        "module": module, "source_sha256": snapshot["sha256"],
        "plan_path_source_file": name, "plan_path_source_text": code,
        "plan_path_source_sha256": snapshot["sha256"],
    }
    result["stored_motion_duration_s"] = sum(
        s["trajectory"]["duration_s"] for s in result["stages"])
    receipt = {
        "schema_version": "owned-cpu-worker-budget-v1",
        "status": "COMPLETE_WITHIN_BUDGET", "timed_out": False,
        "worker_exit_code": 0, "cleanup_errors": [], "source_bindings_verified": True,
        "parent_genesis_imported_modules": [], "parent_blocked_genesis_import_attempts": [],
        "requested_wall_limit_s": 0., "parent_wall_s_before_publication": 2.,
        "worker_identity": {"pid": 1, "windows_creation_filetime_100ns": 1},
        "worker_elapsed_including_cleanup_s": 1., "timeout_s": None,
    }
    result["cpu_process_supervisor"] = receipt
    result["cpu_planning_wall_s"] = 2.
    result["cpu_planning_result"].update(
        status=arm.CPU_STATUS, planning_scope=arm.SCOPE, completed_stages=2,
        process_wall_s=2., cpu_process_supervisor=copy.deepcopy(receipt))
    result["common_problem"] = arm.common_problem(result)
    result["common_problem_sha256"] = digest(result["common_problem"])
    return arm.seal(result)


class IndependentArmRecordTests(unittest.TestCase):
    def test_each_participating_arm_has_only_its_two_stages(self):
        for tier, count in (("easy", 1), ("medium", 2), ("hard", 3)):
            for index in range(count):
                selected = "arm_" + str(index)
                with self.subTest(tier=tier, selected=selected):
                    value = synthetic_component(tier, selected)
                    accepted = arm.validate_arm_record(value)
                    self.assertEqual([s["active_arm_id"] for s in accepted["stages"]],
                                     [selected, selected])
                    self.assertEqual(accepted["arm_boundary"]["global_stage_indices"],
                                     [2 * index, 2 * index + 1])
                    self.assertFalse(accepted["full_mission_passed"])
                    self.assertFalse(accepted["ranking_eligible"])

    def test_assembly_requires_exact_arm_set_and_orders_components(self):
        for tier, count in (("easy", 1), ("medium", 2), ("hard", 3)):
            plans = [synthetic_component(tier, "arm_" + str(i)) for i in range(count)]
            ordered = arm.validate_component_set(list(reversed(plans)), difficulty=tier)
            self.assertEqual([r["selected_arm_id"] for r in ordered],
                             ["arm_" + str(i) for i in range(count)])
            for invalid in (plans + plans[:1], plans[:-1], []):
                with self.subTest(tier=tier, count=len(invalid)), self.assertRaises(ContractError):
                    arm.validate_component_set(invalid, difficulty=tier)
            with self.assertRaises(ContractError):
                arm.validate_component_set(plans, difficulty="medium" if tier != "medium" else "hard")

    def test_status_physical_claims_and_worker_failure_are_rejected(self):
        base = synthetic_component()
        variants = []
        for key in ("full_mission_passed", "physical_transport_verified",
                    "physical_grasp_verified", "student_release_approved", "ranking_eligible"):
            value = copy.deepcopy(base); value[key] = True; variants.append((key, value))
        for key, replacement in (("completed_stages", 1), ("status", "CPU_PLAN_PREFIX_ONLY"),
                                 ("native_physical_steps", 1), ("cpu_physical_steps", 1),
                                 ("genesis_initialized", True), ("failure", {"type": "Failed"})):
            value = copy.deepcopy(base); value["cpu_planning_result"][key] = replacement
            variants.append((key, value))
        for key, replacement in (("timed_out", True), ("worker_exit_code", 1),
                                 ("source_bindings_verified", False), ("cleanup_errors", ["failed"]),
                                 ("worker_identity", {}), ("parent_wall_s_before_publication", 3.),
                                 ("worker_elapsed_including_cleanup_s", -1.),
                                 ("worker_elapsed_including_cleanup_s", 3.),
                                 ("timeout_s", 1.)):
            value = copy.deepcopy(base)
            value["cpu_process_supervisor"][key] = replacement
            value["cpu_planning_result"]["cpu_process_supervisor"][key] = replacement
            variants.append((key, value))
        for name, value in variants:
            with self.subTest(field=name), self.assertRaises(ContractError):
                arm.validate_arm_record(arm.seal(value))

    def test_altered_boundary_route_stage_and_time_are_rejected(self):
        base = synthetic_component()
        variants = []
        value = copy.deepcopy(base); value["stages"].reverse(); variants.append(("order", value))
        value = copy.deepcopy(base); value["selected_arm_id"] = "arm_1"; variants.append(("wrong_arm", value))
        value = copy.deepcopy(base)
        value["stages"][1]["trajectory"]["waypoints"][0][0] += .01
        variants.append(("discontinuity", value))
        value = copy.deepcopy(base)
        value["stages"][0]["expected_peer_commands_by_arm"]["arm_1"]["q_rad"][0] += .01
        variants.append(("peer", value))
        value = copy.deepcopy(base); value["stages"][1]["destination_support_id"] = "source_support"
        variants.append(("route", value))
        value = copy.deepcopy(base); value["stages"][0]["nominal_body_world"]["position_m"][0] += .01
        variants.append(("cup_origin", value))
        value = copy.deepcopy(base); value["stages"][1]["trajectory"]["edge_durations_s"][0] *= 2
        variants.append(("saved_time_representation", value))
        value = copy.deepcopy(base); value["stored_motion_duration_s"] += .01
        variants.append(("time_total", value))
        value = copy.deepcopy(base); value["arm_boundary"]["physical_continuity_claimed"] = True
        variants.append(("fictional_handoff", value))
        for name, value in variants:
            with self.subTest(field=name), self.assertRaises(ContractError):
                arm.validate_arm_record(arm.seal(value))

    def test_changed_common_layout_and_conflicting_used_helper_rejected(self):
        plans = [synthetic_component("medium", "arm_" + str(i)) for i in range(2)]
        changed = copy.deepcopy(plans)
        changed[1]["common_problem"]["design_sha256"] = "d" * 64
        changed[1]["common_problem_sha256"] = digest(changed[1]["common_problem"])
        changed[1] = arm.seal(changed[1])
        with self.assertRaises(ContractError):
            arm.validate_component_set(changed)
        for i, value in enumerate(plans):
            code = ("# different used helper " + str(i)).encode()
            value["portable_sources"]["student"]["student/shared_helper.py"] = {
                "sha256": hashlib.sha256(code).hexdigest(),
                "bytes_base64": base64.b64encode(code).decode("ascii")}
            value["source_pins"][str(Path(value["bound_source_root"]) / "student/shared_helper.py")] = hashlib.sha256(code).hexdigest()
            plans[i] = arm.seal(value)
        with self.assertRaisesRegex(ContractError, "helper"):
            arm.validate_component_set(plans)

    def test_producer_source_snapshot_and_function_identity_rejected_if_changed(self):
        base = synthetic_component()
        for field in ("bytes", "module", "function", "function_file"):
            value = copy.deepcopy(base)
            name = next(iter(value["portable_sources"]["student"]))
            if field == "bytes":
                value["portable_sources"]["student"][name]["bytes_base64"] = base64.b64encode(b"changed").decode()
            elif field == "module":
                value["offline_planner"]["module"] = "student.unrelated"
            elif field == "function":
                value["offline_planner"]["plan_path_source_text"] = "def other(): pass\n"
            else:
                value["offline_planner"]["plan_path_source_file"] = "student.unrelated.py"
            with self.subTest(field=field), self.assertRaises(ContractError):
                arm.validate_arm_record(arm.seal(value))

    def test_materialization_preserves_original_payload_and_rejects_motion_edit(self):
        original = synthetic_component()
        raw = json.dumps(original).encode()
        bound = copy.deepcopy(original)
        bound["component_origin"] = {
            "schema_version": "explicit-arm-plan-materialization-v1",
            "producer_file_sha256": hashlib.sha256(raw).hexdigest(),
            "producer_bytes_base64": base64.b64encode(raw).decode(),
            "producer_bundle_sha256": original["bundle_sha256"],
        }
        producer_root = Path(original["bound_source_root"])
        receiving_root = producer_root.parent / "structural_receiving_checkout"
        bound["bound_source_root"] = str(receiving_root)
        bound["source_pins"] = {
            str(receiving_root / Path(name).relative_to(producer_root))
            if Path(name).is_relative_to(producer_root) else name: value
            for name, value in original["source_pins"].items()
        }
        accepted = arm.validate_arm_record(arm.seal(bound))
        self.assertEqual(arm.origin_payload(accepted), (original, raw))
        self.assertEqual(accepted["stages"], original["stages"])
        self.assertNotEqual(accepted["source_pins"], original["source_pins"])
        bound["stages"][1]["trajectory"]["waypoints"][-1][0] += .001
        with self.assertRaises(ContractError):
            arm.validate_arm_record(arm.seal(bound))
        bound = copy.deepcopy(accepted)
        bound["component_origin"]["producer_file_sha256"] = "0" * 64
        with self.assertRaises(ContractError):
            arm.validate_arm_record(arm.seal(bound))

    def test_declared_old_source_paths_must_match_portable_hashes(self):
        base = synthetic_component()
        for alteration in ("missing", "hash", "foreign", "relative_root"):
            value = copy.deepcopy(base)
            name = str(Path(value["bound_source_root"]) / "physics/structural_fixture.py")
            if alteration == "missing":
                value["source_pins"].pop(name)
            elif alteration == "hash":
                value["source_pins"][name] = "d" * 64
            elif alteration == "foreign":
                value["source_pins"][str(Path(value["bound_source_root"]) / "unknown.py")] = "d" * 64
            else:
                value["bound_source_root"] = "relative/root"
            with self.subTest(alteration=alteration), self.assertRaises(ContractError):
                arm.validate_arm_record(arm.seal(value))


class FixedGeometryRevalidationTests(unittest.TestCase):
    def run_validation(self, inputs_record, component, geometry, check=lambda: None):
        from physics import plan_arm_transport as planning
        # Isolate checkout lookup only. Geometry, endpoints, paths, timing and
        # state/edge validation use the real supplied revalidation function.
        with patch.object(planning, "source_manifest", return_value=copy.deepcopy(component["portable_sources"])):
            return planning.revalidate_stages(inputs_record, geometry, [component], check)

    def test_every_saved_state_and_edge_is_checked_without_changing_motion(self):
        inputs_record, component, geometry = geometric_unit_fixture()
        before = copy.deepcopy(component)
        evidence = self.run_validation(inputs_record, component, geometry)
        self.assertTrue(evidence["passed"])
        self.assertEqual(component, before)
        self.assertEqual(len(geometry.queries), 2)
        self.assertTrue(all(q.states >= 2 and q.edges == 1 for q in geometry.queries))
        self.assertTrue(all(v == 0 for v in evidence["blocked_algorithm_calls"].values()))
        self.assertEqual([r["trajectory_sha256"] for r in evidence["stages"]],
                         [digest(s["trajectory"]) for s in component["stages"]])

    def test_invalid_state_interior_edge_and_fk_endpoint_are_rejected(self):
        for field, value in (("reject_state", True), ("reject_edge", True), ("endpoint_offset", .01)):
            inputs_record, component, geometry = geometric_unit_fixture()
            setattr(geometry, field, value)
            with self.subTest(field=field), self.assertRaises(ContractError):
                self.run_validation(inputs_record, component, geometry)
            if field == "reject_edge":
                self.assertTrue(any(q.edges for q in geometry.queries))

    def test_current_layout_attachment_and_terminal_changes_are_rejected(self):
        for mutation in ("current_home", "current_identity", "attachment", "hand_target"):
            inputs_record, component, geometry = geometric_unit_fixture()
            if mutation == "current_home":
                inputs_record["initial_commands_by_arm"]["arm_1"]["q_rad"][0] += .01
            elif mutation == "current_identity":
                inputs_record["scenario"]["robots"][1]["q_rad"][0] += .01
            elif mutation == "attachment":
                component["stages"][1]["nominal_hand_to_body"]["translation_hand_m"][0] += .01
                component = arm.seal(component)
            else:
                component["stages"][0]["nominal_goal_hand_world"]["position_m"][0] += .01
                component = arm.seal(component)
            with self.subTest(mutation=mutation), self.assertRaises(ContractError):
                self.run_validation(inputs_record, component, geometry)

    def test_search_ik_and_parameterization_are_blocked_and_restored(self):
        from physics import plan_arm_transport as planning
        import transport_challenge.joint_planning as joint
        import physics.provided_manipulation as manipulation
        from transport_challenge.joint_kinematics import BoundedKinematics
        from supplied_support.cpu_geometry import CpuOracle
        targets = ((joint, "plan_joint_motion"), (joint, "parameterize_joint_path"),
                   (manipulation, "solve_endpoint"), (BoundedKinematics, "ik"), (CpuOracle, "ik"))
        originals = [(obj, name, getattr(obj, name)) for obj, name in targets]
        with planning.forbid_merge_search() as calls:
            for obj, name in targets:
                with self.subTest(name=name), self.assertRaisesRegex(RuntimeError, "forbids"):
                    getattr(obj, name)()
            self.assertTrue(all(v == 1 for v in calls.values()))
        for obj, name, original in originals:
            self.assertIs(getattr(obj, name), original)
        inputs_record, component, geometry = geometric_unit_fixture()
        with self.assertRaisesRegex(RuntimeError, "forbids"):
            self.run_validation(inputs_record, component, geometry,
                                check=lambda: joint.parameterize_joint_path())
        self.assertIs(joint.parameterize_joint_path, dict((n, v) for _, n, v in originals)["parameterize_joint_path"])


class PortableSourceTests(unittest.TestCase):
    def test_lazy_imported_helper_keeps_its_precallback_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); student = root / "student"; student.mkdir()
            code = "def plan_path(*args, **kwargs):\n    return None\n"
            strategy_file = student / "chosen.py"; strategy_file.write_text(code, encoding="utf-8")
            helper_file = student / "helper.py"; helper_file.write_text("VALUE = 1\n", encoding="utf-8")
            strategy = types.ModuleType("student.chosen"); strategy.__file__ = str(strategy_file)
            exec(compile(code, str(strategy_file), "exec"), strategy.__dict__)
            before = arm.python_snapshot(root)
            helper = types.ModuleType("student.helper"); helper.__file__ = str(helper_file)
            with patch.dict(sys.modules, {"student.helper": helper}):
                used = arm.used_student_sources(strategy, before, root)
                self.assertIn("student/helper.py", used)
                helper_file.write_text("VALUE = 2\n", encoding="utf-8")
                with self.assertRaisesRegex(ContractError, "changed while planning"):
                    arm.used_student_sources(strategy, before, root)
            late_file = student / "late.py"; late_file.write_text("VALUE = 3\n", encoding="utf-8")
            late = types.ModuleType("student.late"); late.__file__ = str(late_file)
            with patch.dict(sys.modules, {"student.late": late}):
                with self.assertRaisesRegex(ContractError, "added or changed"):
                    arm.used_student_sources(strategy, before, root)

    def test_noncanonical_and_escaping_names_rejected(self):
        for name in ("../escape.py", "student/../escape.py", "/absolute.py",
                     "D:escape.py", "student\\x.py", "student//x.py", "student/./x.py", ""):
            with self.subTest(name=name), self.assertRaises(ContractError):
                arm.checked_relative(name)

    def test_used_helper_changes_fail_but_unrelated_teammate_changes_are_allowed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            names = ("physics/fixed.py", "project_run.py", "student_design_input.py",
                     "supplied_design_defaults.json", "cases/toy/world.json",
                     "student/shared_design.json", "student/arm_0.py",
                     "student/used_helper.py", "student/other_arm.py")
            for name in names:
                path = root / name; path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("# structural fixture\n", encoding="utf-8")
            xml = root / "test_robot.xml"; xml.write_text("<test/>", encoding="utf-8")
            sources = {}
            for name in ("student/arm_0.py", "student/used_helper.py"):
                data = (root / name).read_bytes()
                sources[name] = {"sha256": hashlib.sha256(data).hexdigest(),
                                 "bytes_base64": base64.b64encode(data).decode()}
            identity = {"task_relative": "cases/toy", "design_relative": "student/shared_design.json"}
            assets = {"assets/xml/franka_emika_panda/panda.xml": hashlib.sha256(xml.read_bytes()).hexdigest()}
            with patch.object(arm, "panda_xml", return_value=xml), patch.object(arm, "model_asset_sources", return_value=assets):
                manifest = arm.source_manifest(identity, sources, root)
                kwargs = dict(verify_current=True, root=root, **identity)
                self.assertTrue(arm.validate_source_manifest(manifest, **kwargs))
                (root / "student/other_arm.py").write_text("# new teammate work\n", encoding="utf-8")
                self.assertTrue(arm.validate_source_manifest(manifest, **kwargs))
                for name in ("student/used_helper.py", "student/shared_design.json", "physics/fixed.py"):
                    path = root / name; old = path.read_bytes()
                    path.write_bytes(old + b"# changed\n")
                    with self.subTest(name=name), self.assertRaises(ContractError):
                        arm.validate_source_manifest(manifest, **kwargs)
                    path.write_bytes(old)
                extra = root / "physics/new_supplied.py"
                extra.write_text("# changed supplied closure\n", encoding="utf-8")
                with self.assertRaisesRegex(ContractError, "source set changed"):
                    arm.validate_source_manifest(manifest, **kwargs)
                extra.unlink()
                assets["assets/xml/franka_emika_panda/assets/new_collision.obj"] = "e" * 64
                with self.assertRaisesRegex(ContractError, "collision mesh closure changed"):
                    arm.validate_source_manifest(manifest, **kwargs)


if __name__ == "__main__":
    unittest.main()

