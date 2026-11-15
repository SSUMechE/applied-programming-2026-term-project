"""Portable, source-bound independent-arm CPU plans.

An arm plan is local practice input, never a partial full-mission certificate.
Transfer preserves the producer payload and authenticates its used student files
in the receiving checkout. Materialization changes only declared source paths.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import importlib.metadata
import inspect
import json
import math
from pathlib import Path, PurePosixPath
import sys
import time
import xml.etree.ElementTree as ET

from transport_challenge.contracts import ContractError, digest, point
from transport_challenge.joint_planning import JointLimits, JointTrajectory
from physics.saved_motion_planning import SavedMissionPlan, file_sha, require, verify_saved_input_overlay
from physics.tier_mission_plan import validate_task_contract, verify_task_design

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "saved-arm-mission-v1"
SCOPE = "independent_arm"
CPU_STATUS = "CPU_GEOMETRIC_ARM_MISSION_PLANNED"
PORTABLE_SCHEMA = "portable-arm-source-manifest-v1"
ARMS = ("arm_0", "arm_1", "arm_2")


def read(path):
    return json.loads(Path(path).read_bytes())


def write_new(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(value, indent=2, allow_nan=False) + "\n")


def seal(value):
    value = copy.deepcopy(value)
    value.pop("bundle_sha256", None)
    value["bundle_sha256"] = digest(value)
    return value


def relative_path(path, root=ROOT):
    path = Path(path).resolve(strict=True)
    require(path.is_relative_to(root.resolve()), "Input must belong to this project checkout")
    return path.relative_to(root.resolve()).as_posix()


def checked_relative(name):
    require(type(name) is str and "\\" not in name and ":" not in name,
            "Portable source needs a POSIX relative path")
    p = PurePosixPath(name)
    require(not p.is_absolute() and p.parts and ".." not in p.parts and "." not in p.parts
            and p.as_posix() == name,
            "Portable source escapes the project")
    return name


def panda_xml():
    return Path(importlib.metadata.distribution("genesis-world").locate_file(
        "genesis/assets/xml/franka_emika_panda/panda.xml")).resolve(strict=True)


def model_asset_root():
    return Path(importlib.metadata.distribution("genesis-world").locate_file("genesis")).resolve(strict=True)


def model_asset_sources():
    """Exact collision XML/mesh closure used by the supplied CPU geometry."""
    root, panda = model_asset_root(), panda_xml()
    xml = ET.parse(panda).getroot()
    referenced = {g.get("mesh") for g in xml.find("worldbody/body").iter("geom")
                  if g.get("class") != "visual" and g.get("mesh") is not None}
    paths = [panda]
    for mesh in xml.findall("asset/mesh"):
        if mesh.get("name", Path(mesh.get("file")).stem) in referenced:
            paths.append((panda.parent / "assets" / mesh.get("file")).resolve(strict=True))
    require(all(p.is_relative_to(root) for p in paths), "Robot asset escaped installed Genesis package")
    return {p.relative_to(root).as_posix(): file_sha(p) for p in paths}


def provided_paths(task_relative, design_relative, root=ROOT):
    task = root / checked_relative(task_relative)
    names = set()
    for directory in ("physics", "transport_challenge", "supplied_support"):
        names.update(p.relative_to(root).as_posix() for p in (root / directory).glob("*.py"))
    for name in ("project_run.py", "student_design_input.py", "supplied_design_defaults.json"):
        names.add(name)
    names.update(p.relative_to(root).as_posix() for p in task.glob("*.json"))
    names.add(checked_relative(design_relative))
    return sorted(names)


def python_snapshot(root=ROOT):
    return {p.relative_to(root).as_posix(): p.read_bytes()
            for p in sorted((root / "student").rglob("*.py"))}


def used_student_sources(strategy, before, root=ROOT):
    """Capture lazy imports too, retaining bytes from before callback execution."""
    paths = {Path(inspect.getsourcefile(strategy)).resolve(),
             Path(inspect.getsourcefile(strategy.plan_path)).resolve()}
    for module in tuple(sys.modules.values()):
        filename = getattr(module, "__file__", None)
        if filename:
            p = Path(filename).resolve()
            if p.suffix == ".py" and p.is_relative_to(root / "student"):
                paths.add(p)
    result = {}
    for path in sorted(paths):
        require(path.is_relative_to(root / "student"), "Student callback source escaped student/")
        name = path.relative_to(root).as_posix()
        require(name in before and path.read_bytes() == before[name],
                "Used student source was added or changed while planning: " + name)
        data = before[name]
        result[name] = {"sha256": hashlib.sha256(data).hexdigest(),
                        "bytes_base64": base64.b64encode(data).decode("ascii")}
    return result


def source_manifest(inputs, used_sources, root=ROOT):
    return {
        "schema_version": PORTABLE_SCHEMA,
        "provided": {name: file_sha(root / name) for name in
                     provided_paths(inputs["task_relative"], inputs["design_relative"], root)},
        "student": copy.deepcopy(used_sources),
        "panda_xml_sha256": file_sha(panda_xml()),
        "model_assets": copy.deepcopy(inputs["model_assets"] if "model_assets" in inputs else model_asset_sources()),
    }


def validate_source_manifest(manifest, *, verify_current=False, task_relative=None,
                             design_relative=None, root=ROOT):
    require(type(manifest) is dict and set(manifest) ==
            {"schema_version", "provided", "student", "panda_xml_sha256", "model_assets"}
            and manifest["schema_version"] == PORTABLE_SCHEMA, "Malformed portable source manifest")
    require(type(manifest["provided"]) is dict and manifest["provided"]
            and type(manifest["student"]) is dict and manifest["student"],
            "Portable supplied/student source closure is missing")
    for name, expected in manifest["provided"].items():
        checked_relative(name)
        require(type(expected) is str and len(expected) == 64, "Malformed supplied source hash")
        if verify_current:
            require(file_sha(root / name) == expected, "Shared supplied input changed: " + name)
    for name, record in manifest["student"].items():
        checked_relative(name)
        require(name.startswith("student/") and name.endswith(".py")
                and type(record) is dict and set(record) == {"sha256", "bytes_base64"},
                "Malformed used student module snapshot")
        try:
            data = base64.b64decode(record["bytes_base64"], validate=True)
        except Exception as exc:
            raise ContractError("Invalid student source bytes: " + name) from exc
        require(hashlib.sha256(data).hexdigest() == record["sha256"],
                "Archived student source bytes differ: " + name)
        if verify_current:
            require((root / name).read_bytes() == data,
                    "Used producer module/helper changed: " + name)
    require(type(manifest["panda_xml_sha256"]) is str and len(manifest["panda_xml_sha256"]) == 64,
            "Malformed robot model identity")
    assets = manifest["model_assets"]
    require(type(assets) is dict and assets and
            assets.get("assets/xml/franka_emika_panda/panda.xml") == manifest["panda_xml_sha256"],
            "Robot collision asset closure is missing")
    for name, expected in assets.items():
        checked_relative(name)
        require(name.startswith("assets/") and type(expected) is str and len(expected) == 64,
                "Malformed package-relative robot asset")
    if verify_current:
        require(set(manifest["provided"]) == set(provided_paths(task_relative, design_relative, root)),
                "Supplied source set changed since this arm plan")
        require(file_sha(panda_xml()) == manifest["panda_xml_sha256"], "Installed robot model changed")
        require(model_asset_sources() == assets, "Installed robot collision mesh closure changed")
    return True


def current_pins(manifest, root=ROOT):
    pins = {str((root / name).resolve()): expected for name, expected in manifest["provided"].items()}
    pins.update({str((root / name).resolve()): r["sha256"] for name, r in manifest["student"].items()})
    pins.update({str((model_asset_root() / name).resolve()): expected
                 for name, expected in manifest["model_assets"].items()})
    return pins


def common_problem(value):
    return {
        "schema_version": "shared-independent-arm-problem-v1",
        "task_relative": value["task_relative"], "design_relative": value["design_relative"],
        "task_contract": value["task_contract"],
        "world_sha256": value["world_sha256"], "scenario_sha256": value["scenario_sha256"],
        "design_sha256": value["design_sha256"],
        "task_manifest_sha256": value["task_manifest_sha256"],
        "initial_commands_by_arm": value["initial_commands_by_arm"],
        "provided_sources": value["portable_sources"]["provided"],
        "panda_xml_sha256": value["portable_sources"]["panda_xml_sha256"],
        "model_assets": value["portable_sources"]["model_assets"],
        "provided_wait_policy": value["provided_wait_policy"],
    }


def canonical_boundary(contract, initial_commands, scenario, arm_id):
    require(arm_id in contract["required_arm_ids"], "Selected arm does not participate in this tier")
    i = int(arm_id[-1])
    source, destination = contract["support_route"][i:i + 2]
    require(set(initial_commands) == set(ARMS), "All three home commands are required")
    return {
        "schema_version": "shared-home-arm-boundary-v1",
        "arm_id": arm_id, "source_support_id": source, "destination_support_id": destination,
        "commands_by_arm": copy.deepcopy(initial_commands),
        "body_world": copy.deepcopy(scenario["supported_body_poses"][source]),
        "destination_body_world": copy.deepcopy(scenario["supported_body_poses"][destination]),
        "global_stage_indices": [2 * i, 2 * i + 1],
        "physical_continuity_claimed": False,
    }


def validate_cpu_receipt(value, *, require_supervisor, expected_status=CPU_STATUS,
                         expected_scope=SCOPE, expected_stages=2):
    summary = value.get("cpu_planning_result", {})
    require(summary.get("status") == expected_status and summary.get("planning_scope") == expected_scope
            and summary.get("completed_stages") == expected_stages and summary.get("failure") is None
            and summary.get("source_bindings_unchanged") is True
            and summary.get("genesis_initialized") is False
            and summary.get("genesis_imported_modules") == []
            and summary.get("blocked_genesis_import_attempts") == []
            and summary.get("blocked_integration_attempts") == []
            and summary.get("native_physical_steps") == summary.get("cpu_physical_steps") == 0,
            "Independent CPU planning did not complete honestly")
    receipt = value.get("cpu_process_supervisor", {})
    if not require_supervisor:
        require(receipt.get("status") == "WORKER_CANDIDATE_PENDING", "Unowned CPU candidate")
        return
    measured = receipt.get("schema_version") == "owned-cpu-worker-measurement-v2"
    require(receipt == summary.get("cpu_process_supervisor")
            and ((measured and receipt.get("status") == "COMPLETE_RECORDED")
                 or (receipt.get("schema_version") == "owned-cpu-worker-budget-v1"
                     and receipt.get("status") == "COMPLETE_WITHIN_BUDGET"))
            and receipt.get("interrupted", False) is False and receipt.get("failure") is None
            and receipt.get("timed_out") is False and receipt.get("worker_exit_code") == 0
            and receipt.get("cleanup_errors") == [] and receipt.get("source_bindings_verified") is True
            and receipt.get("parent_genesis_imported_modules") == []
            and receipt.get("parent_blocked_genesis_import_attempts") == [],
            "A completed owned CPU worker receipt is required")
    cost, limit = summary.get("process_wall_s"), receipt.get("requested_wall_limit_s")
    require(type(cost) in (int, float) and math.isfinite(cost) and cost > 0
            and ((measured and limit is None) or (not measured and type(limit) in (int, float)
                 and math.isfinite(limit) and limit >= 0 and (limit == 0 or cost < limit)))
            and cost == value.get("cpu_planning_wall_s") == receipt.get("parent_wall_s_before_publication"),
            "CPU parent time attribution differs")
    identity = receipt.get("worker_identity", {})
    require(type(identity.get("pid")) is int and identity["pid"] > 0
            and type(identity.get("windows_creation_filetime_100ns")) is int
            and identity["windows_creation_filetime_100ns"] > 0,
            "CPU worker creation identity is missing")
    elapsed, allowance = receipt.get("worker_elapsed_including_cleanup_s"), receipt.get("timeout_s")
    require(type(elapsed) in (int, float) and math.isfinite(elapsed) and 0 <= elapsed <= cost
            and ((measured and allowance is None) or (not measured and
                 ((limit == 0 and allowance is None) or
                  (limit > 0 and type(allowance) in (int, float) and math.isfinite(allowance)
                   and 0 < allowance <= limit)))), "CPU worker elapsed measurement differs")


def origin_payload(value):
    """Return intact original producer bytes, never fabricate an old source receipt."""
    origin = value.get("component_origin")
    if origin is None:
        return None
    require(type(origin) is dict and set(origin) ==
            {"schema_version", "producer_file_sha256", "producer_bytes_base64", "producer_bundle_sha256"}
            and origin["schema_version"] == "explicit-arm-plan-materialization-v1",
            "Malformed arm materialization provenance")
    try:
        data = base64.b64decode(origin["producer_bytes_base64"], validate=True)
        original = json.loads(data)
    except Exception as exc:
        raise ContractError("Original producer payload is not intact JSON bytes") from exc
    require(hashlib.sha256(data).hexdigest() == origin["producer_file_sha256"]
            and original.get("bundle_sha256") == origin["producer_bundle_sha256"]
            and "component_origin" not in original, "Original producer identity changed")
    require(original["bundle_sha256"] == digest({k: v for k, v in original.items() if k != "bundle_sha256"}),
            "Original producer canonical digest changed")
    allowed = {"source_pins", "bound_source_root", "bundle_sha256", "component_origin"}
    require({k: v for k, v in value.items() if k not in allowed} ==
            {k: v for k, v in original.items() if k not in allowed},
            "Materialization changed more than explicit source locations")
    return original, data


def validate_arm_record(value, *, require_supervisor=True):
    require(type(value) is dict and value.get("schema_version") == SCHEMA
            and value.get("scope") == SCOPE, "Require an explicit independent-arm plan")
    require(value.get("bundle_sha256") == digest({k: v for k, v in value.items() if k != "bundle_sha256"}),
            "Arm bundle digest differs")
    contract = validate_task_contract(value.get("task_contract"))
    require(value.get("task_contract_sha256") == digest(contract), "Shared tier task changed")
    arm = value.get("selected_arm_id")
    require(arm in contract["required_arm_ids"], "Arm does not belong to the shared tier")
    require(value.get("payload") == "beads" and value.get("rigid_runtime_profile") == "rigid16_bead_tc2h_v1"
            and value.get("post_place_policy") == "kinematic_return"
            and value.get("nominal_only") is True
            and all(value.get(k) is False for k in ("physical_grasp_verified", "physical_transport_verified",
                    "full_mission_passed", "student_release_approved", "ranking_eligible")),
            "Independent planning cannot claim physical/full mission qualification")
    validate_source_manifest(value.get("portable_sources"))
    checked_relative(value["task_relative"]); checked_relative(value["design_relative"])
    identity = value.get("input_identity")
    require(type(identity) is dict and set(identity) == {"world", "scenario", "design"},
            "Arm input identity is missing")
    require(digest(identity["world"]) == value["world_sha256"]
            and digest(identity["scenario"]) == value["scenario_sha256"]
            and digest(identity["design"]) == value["design_sha256"],
            "Arm input identity differs")
    verify_task_design(contract, identity["world"], identity["scenario"])
    initial = value.get("initial_commands_by_arm", {})
    require(set(initial) == set(ARMS), "Arm plan omitted a real scene robot")
    for command in initial.values():
        require(set(command) == {"q_rad", "finger_q_m"}, "Malformed home command")
        point(command["q_rad"], 7); point(command["finger_q_m"], 2)
    boundary = canonical_boundary(contract, initial, identity["scenario"], arm)
    require(value.get("arm_boundary") == boundary, "Canonical arm boundary changed")
    require(value.get("common_problem") == common_problem(value)
            and value.get("common_problem_sha256") == digest(value["common_problem"]),
            "Shared problem identity changed")
    stages = value.get("stages")
    require(type(stages) is list and [(s.get("stage_id"), s.get("kind"), s.get("active_arm_id")) for s in stages] ==
            [(arm + "__approach", "approach", arm), (arm + "__carry", "carry", arm)],
            "Arm plan needs exactly its approach and carry")
    commands = copy.deepcopy(initial)
    previous_limits = None
    for stage in stages:
        tr = stage["trajectory"]
        trajectory = JointTrajectory(tr["waypoints"], tr["edge_durations_s"], JointLimits(**tr["limits"]))
        require(trajectory.to_record() == tr, "Saved arm trajectory/time representation changed")
        require(tr["waypoints"][0] == commands[arm]["q_rad"], "Arm path endpoint continuity changed")
        require(stage["expected_peer_commands_by_arm"] == {n: c for n, c in commands.items() if n != arm},
                "Arm plan peers differ from shared home commands")
        require(previous_limits is None or previous_limits == tr["limits"], "Arm limits changed between stages")
        previous_limits = tr["limits"]
        require(stage["support_id"] == boundary["source_support_id"]
                and stage["destination_support_id"] == (boundary["destination_support_id"] if stage["kind"] == "carry" else None),
                "Arm support route changed")
        require(stage["nominal_body_world"] == boundary["body_world"], "Arm source body pose changed")
        if stage["kind"] == "carry":
            require(stage["nominal_goal_body_world"] == boundary["destination_body_world"],
                    "Arm destination body pose changed")
        commands[arm]["q_rad"] = copy.deepcopy(tr["waypoints"][-1])
    require(value.get("stored_motion_duration_s") == sum(s["trajectory"]["duration_s"] for s in stages),
            "Arm stored timing total changed")
    require(value.get("post_place_resets") == [] and value.get("post_place_returns") == [],
            "Independent arm plan cannot claim an executed donor return")
    from physics.saved_motion_planning import saved_wait_policy
    require(all(value.get("provided_wait_policy", {}).get(k) == v for k, v in saved_wait_policy().items()),
            "Provided handling timing changed")
    producer = value.get("offline_planner", {})
    module = producer.get("module", "")
    require(type(module) is str and module.startswith("student.")
            and all(p.isidentifier() for p in module.split(".")), "Producer module is not a student strategy")
    module_path = module.replace(".", "/") + ".py"
    snapshots = value["portable_sources"]["student"]
    require(module_path in snapshots and producer.get("source_sha256") == snapshots[module_path]["sha256"],
            "Producer module does not match its source snapshot")
    function_path = producer.get("plan_path_source_file")
    function_text = producer.get("plan_path_source_text")
    require(function_path in snapshots and type(function_text) is str and function_text
            and hashlib.sha256(function_text.encode()).hexdigest() == producer.get("plan_path_source_sha256")
            and function_text in base64.b64decode(snapshots[function_path]["bytes_base64"]).decode("utf-8").replace("\r\n", "\n"),
            "Actual plan_path source does not match archived code")
    require(type(value.get("source_pins")) is dict and value["source_pins"]
            and type(value.get("bound_source_root")) is str and Path(value["bound_source_root"]).is_absolute(),
            "Explicit source-path binding missing")
    bound_root = Path(value["bound_source_root"])
    portable = value["portable_sources"]
    declared = {str(bound_root / n): h for n, h in portable["provided"].items()}
    declared.update({str(bound_root / n): r["sha256"] for n, r in portable["student"].items()})
    pins = value["source_pins"]
    require(all(pins.get(n) == h for n, h in declared.items()), "Declared source paths differ from portable sources")
    extra = set(pins) - set(declared)
    assets = portable["model_assets"]
    require(len(extra) == len(assets) and all(Path(n).is_absolute() for n in extra),
            "Declared robot-model source path count differs")
    for name, expected in assets.items():
        matching = [n for n in extra if Path(n).as_posix().endswith("/" + name)]
        require(len(matching) == 1 and pins[matching[0]] == expected,
                "Declared robot-model source path differs: " + name)
    prefixes = {Path(n).as_posix()[:-len(name)] for name in assets for n in extra
                if Path(n).as_posix().endswith("/" + name)}
    require(len(prefixes) == 1, "Robot collision assets must share one recorded package root")
    validate_cpu_receipt(value, require_supervisor=require_supervisor)
    origin = origin_payload(value)
    if origin is not None:
        validate_arm_record(origin[0], require_supervisor=require_supervisor)
    return copy.deepcopy(value)


class ArmMissionPlan(SavedMissionPlan):
    """Two-stage local plan with complete shared-scene identity."""
    def __init__(self, record, *, path, require_supervisor=True):
        self.path = Path(path).resolve(strict=True)
        self.file_sha256 = file_sha(self.path)
        value = validate_arm_record(record, require_supervisor=require_supervisor)
        self.sha256 = value["bundle_sha256"]
        self.post_place_policy = "kinematic_return"
        arm = value["selected_arm_id"]
        self.stage_signatures = ((arm + "__approach", "approach", arm), (arm + "__carry", "carry", arm))
        self._json = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
        self.verify_sources()

    @property
    def selected_arm_id(self):
        return self.record()["selected_arm_id"]

    @property
    def required_arm_ids(self):
        return (self.selected_arm_id,)

    @property
    def task_contract(self):
        return self.record()["task_contract"]

    @property
    def support_route(self):
        return tuple(self.task_contract["support_route"])

    def input_identity(self):
        return self.record()["input_identity"]

    def current_source_pins(self):
        return current_pins(self.record()["portable_sources"])

    def verify_sources(self):
        require(file_sha(self.path) == self.file_sha256, "Saved arm file changed")
        value = self.record()
        validate_source_manifest(value["portable_sources"], verify_current=True,
                                 task_relative=value["task_relative"], design_relative=value["design_relative"])
        return True

    def verify_bound_inputs(self, world, scenario, *, design_sha256=None, task_manifest_sha256=None):
        value = self.record()
        world, scenario = verify_saved_input_overlay(world, scenario,
                    world_sha256=value["world_sha256"], scenario_sha256=value["scenario_sha256"])
        verify_task_design(self.task_contract, world, scenario)
        require(value["arm_boundary"] == canonical_boundary(self.task_contract,
                    value["initial_commands_by_arm"], scenario, self.selected_arm_id), "Runtime arm boundary differs")
        if design_sha256 is not None:
            require(design_sha256 == value["design_sha256"], "Shared student design changed")
        if task_manifest_sha256 is not None:
            require(task_manifest_sha256 == value["task_manifest_sha256"], "Shared task manifest changed")
        return True

    def materialize(self, destination):
        """Write an explicit local path binding while retaining producer bytes."""
        self.verify_sources()
        source = self.record()
        existing = origin_payload(source)
        original, data = existing if existing is not None else (source, self.path.read_bytes())
        value = copy.deepcopy(original)
        value["component_origin"] = {
            "schema_version": "explicit-arm-plan-materialization-v1",
            "producer_file_sha256": hashlib.sha256(data).hexdigest(),
            "producer_bytes_base64": base64.b64encode(data).decode("ascii"),
            "producer_bundle_sha256": original["bundle_sha256"],
        }
        value["source_pins"] = self.current_source_pins()
        value["bound_source_root"] = str(ROOT)
        value = seal(value)
        write_new(destination, value)
        return ArmMissionPlan(value, path=destination)


def materialize_arm_plan(path, destination):
    return ArmMissionPlan.from_file(path).materialize(destination)


def validate_component_set(plans, *, difficulty=None):
    require(type(plans) in (list, tuple) and plans, "Assembly needs arm plans")
    records = [p.record() if isinstance(p, ArmMissionPlan) else validate_arm_record(p) for p in plans]
    contract = records[0]["task_contract"]
    if difficulty is not None:
        require(contract["difficulty"] == difficulty, "Assembly difficulty differs")
    ids = [r["selected_arm_id"] for r in records]
    require(len(ids) == len(set(ids)) and set(ids) == set(contract["required_arm_ids"]),
            "Assembly requires each participating arm exactly once")
    common = records[0]["common_problem"]
    require(all(r["common_problem"] == common and r["common_problem_sha256"] == digest(common) for r in records),
            "Arm plans use different shared designs, sources, peers or task boundaries")
    used = {}
    for record in records:
        for name, snapshot in record["portable_sources"]["student"].items():
            require(name not in used or used[name] == snapshot, "Arm producers disagree about a used helper: " + name)
            used[name] = snapshot
    return [next(r for r in records if r["selected_arm_id"] == arm) for arm in contract["required_arm_ids"]]

