"""Local Genesis practice from one independently planned, source-bound arm.

Only this dedicated worker temporarily adapts the frozen scene loader. The
actual saved object keeps its independent-arm schema, all three physical robots
remain present, and only the existing local-practice callback can execute.
"""
from __future__ import annotations

import argparse
import copy
from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import time

from physics.review_tier_transport import hardware_snapshot, read_record, save
from physics.review_arm_practice import ArmPracticeComplete

ROOT = Path(__file__).resolve().parents[1]
PRACTICE_FILES = ('project_run.py', 'physics/review_arm_plan.py',
                 'physics/arm_mission_plan.py', 'physics/review_arm_practice.py',
                 'physics/device_arm_practice.py', 'physics/arm_practice_validation.py')


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--saved-arm', type=Path, required=True)
    p.add_argument('--difficulty', choices=('easy', 'medium', 'hard'), required=True)
    p.add_argument('--task', type=Path, required=True)
    p.add_argument('--design', type=Path, required=True)
    p.add_argument('--arm', choices=('arm_0', 'arm_1', 'arm_2'), required=True)
    p.add_argument('--contents', choices=('empty', 'beads'), required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--wall-limit-s', type=float, default=0., help='Deprecated compatibility option. Computation wall time is measured without a limit.')
    p.add_argument('--vram-limit-gb', type=float, default=5.)
    p.add_argument('--memory-interval-s', type=float, default=.5)
    p.add_argument('--worker', action='store_true', help=argparse.SUPPRESS)
    return p


def practice_source_snapshot():
    manifest = json.loads((ROOT / 'course_release.json').read_bytes())['managed_files']
    result = {}
    for name in PRACTICE_FILES:
        actual = hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
        if actual != manifest.get(name):
            raise ValueError('Provided independent practice source changed: ' + name)
        result[name] = actual
    return result


def _identity(plan, arm_id):
    from physics.arm_mission_plan import ArmMissionPlan
    record = plan.record()
    if (type(plan) is not ArmMissionPlan
            or record.get('schema_version') != 'saved-arm-mission-v1'
            or record.get('scope') != 'independent_arm'
            or record.get('selected_arm_id') != arm_id
            or tuple(plan.required_arm_ids) != (arm_id,)
            or arm_id not in plan.task_contract['required_arm_ids']
            or any(record.get(k) is not False for k in
                   ('physical_grasp_verified', 'physical_transport_verified',
                    'full_mission_passed', 'student_release_approved'))):
        raise ValueError('Require the selected independent arm plan, never a full-tier result')
    from physics.device_arm_practice import selected_boundary
    selected_boundary(plan, arm_id)
    if plan.verify_sources() is not True:
        raise ValueError('Independent arm source inputs changed. Plan this arm again.')
    return record


def load_plan(args):
    from physics.arm_mission_plan import ArmMissionPlan
    from physics.automatic_runtime_binding import prepare_automatic_scenario
    from physics.tier_mission_plan import make_task_contract
    from transport_challenge.three_arm_design import ThreeArmDesign, bind_three_arm_design
    plan = ArmMissionPlan.from_file(args.saved_arm)
    _identity(plan, args.arm)
    if plan.task_contract['difficulty'] != args.difficulty:
        raise ValueError('Difficulty differs from the independently saved arm')
    world = json.loads((args.task / 'world.json').read_bytes())
    scenario = json.loads((args.task / 'initialization.json').read_bytes())
    design = ThreeArmDesign.from_record(json.loads(args.design.read_bytes()), world)
    bound = bind_three_arm_design(design, world, scenario)
    effective_scenario, _ = prepare_automatic_scenario(bound['scenario'])
    task_hash = hashlib.sha256((args.task / 'manifest.json').read_bytes()).hexdigest()
    contract = make_task_contract(world, scenario, difficulty=args.difficulty,
                                  task_manifest_sha256=task_hash, selected_world=bound['world'])
    if contract != plan.task_contract:
        raise ValueError('Shared task, handoffs or layout differ from this arm plan')
    plan.verify_bound_inputs(bound['world'], effective_scenario,
                             design_sha256=design.sha256, task_manifest_sha256=task_hash)
    return plan


@contextmanager
def local_scene_binding(plan, arm_id, selected_leg):
    """Bind exactly one local file and callback, then restore every temporary name.

    The frozen driver checks an exact class identity at construction. The alias
    is scoped to this worker and preserves the real ArmMissionPlan object/schema.
    No full-tier record or fabricated upstream stages are created.
    """
    from physics import tier_mission_plan as tier, device_tier_mission as frozen
    from physics.arm_mission_plan import ArmMissionPlan
    _identity(plan, arm_id)
    if not callable(selected_leg):
        raise ValueError('An owned local-practice callback is required')
    original_class = tier.TierMissionPlan
    original_callback = frozen.execute_device_tier_mission
    own_loader = 'from_file' in ArmMissionPlan.__dict__
    original_loader = ArmMissionPlan.__dict__.get('from_file')
    if original_class is ArmMissionPlan:
        raise RuntimeError('Independent practice adapters cannot nest')
    evidence = dict(schema_version='owned-independent-arm-scene-binding-v1',
                    scope='arm_practice', plan_schema='saved-arm-mission-v1',
                    selected_arm_id=arm_id, local_plan_file=str(plan.path),
                    local_plan_file_sha256=plan.file_sha256, local_bundle_sha256=plan.sha256,
                    loader_calls=0, callback_calls=0, restored=False,
                    full_tier_callback_allowed=False)

    def load_exact(cls, path, *args, **kwargs):
        if (cls is not ArmMissionPlan or args or kwargs
                or Path(path).resolve(strict=True) != plan.path
                or tier.TierMissionPlan is not ArmMissionPlan
                or frozen.execute_device_tier_mission is not run_selected
                or evidence['loader_calls'] != 0):
            raise ValueError('Only the exact owned local arm file may enter this practice scene')
        _identity(plan, arm_id)
        evidence['loader_calls'] += 1
        return plan

    def run_selected(context, *, output, wall_deadline):
        if (tier.TierMissionPlan is not ArmMissionPlan
                or frozen.execute_device_tier_mission is not run_selected
                or evidence['loader_calls'] != 1 or evidence['callback_calls'] != 0
                or context['student_driver'].saved_plan is not plan):
            raise RuntimeError('Independent practice lost its exact plan/callback ownership')
        _identity(plan, arm_id)
        evidence['callback_calls'] += 1
        return selected_leg(context, output=output, wall_deadline=wall_deadline)

    try:
        frozen.execute_device_tier_mission = run_selected
        ArmMissionPlan.from_file = classmethod(load_exact)
        tier.TierMissionPlan = ArmMissionPlan
        yield evidence
    finally:
        tier.TierMissionPlan = original_class
        if own_loader:
            ArmMissionPlan.from_file = original_loader
        else:
            delattr(ArmMissionPlan, 'from_file')
        frozen.execute_device_tier_mission = original_callback
        evidence['restored'] = True


def producer_identity(plan):
    """Keep the original producer identity across explicit local materialization."""
    origin = plan.record().get('component_origin')
    if origin is None:
        return dict(file_sha256=plan.file_sha256, bundle_sha256=plan.sha256)
    if origin.get('schema_version') != 'explicit-arm-plan-materialization-v1':
        raise ValueError('Unknown independent arm materialization provenance')
    return dict(file_sha256=origin['producer_file_sha256'],
                bundle_sha256=origin['producer_bundle_sha256'])


def run_worker(args):
    from physics import run_student_three_arm as entry
    from physics.device_arm_practice import execute
    from physics.review_student_transport import block_online_planning
    original = load_plan(args)
    original_record = _identity(original, args.arm)
    args.output.mkdir(parents=True, exist_ok=False)
    plan = original.materialize(args.output / 'BOUND_ARM_PLAN.json')
    _identity(plan, args.arm)
    if (plan.record()['stages'] != original_record['stages']
            or plan.task_contract != original.task_contract
            or plan.record()['initial_commands_by_arm'] != original_record['initial_commands_by_arm']
            or producer_identity(plan) != producer_identity(original)):
        raise ValueError('Materializing an arm plan changed its stages, task or peer commands')
    bound_plan = dict(file=str(plan.path), sha256=plan.file_sha256, bundle_sha256=plan.sha256,
                      schema_version='saved-arm-mission-v1', scope='independent_arm')
    input_plan = dict(file=str(original.path), sha256=original.file_sha256,
                     bundle_sha256=original.sha256, schema_version=original_record['schema_version'],
                     scope=original_record['scope'], selected_arm_id=args.arm)
    captured = {}
    original_argv = sys.argv

    def selected_leg(context, *, output, wall_deadline):
        if captured:
            raise RuntimeError('Practice scene callback invoked more than once')
        captured['native'] = execute(context, output=args.output / 'arm_execution',
                                     deadline=None, arm_id=args.arm)
        if captured['native'].get('practice_passed') is True:
            from physics.arm_practice_validation import validate_arm_practice_native
            validate_arm_practice_native(captured['native'], plan_record=plan.record())
        raise ArmPracticeComplete('Independent arm practice ended. No full-tier execution was requested.')

    scene_output = args.output / 'scene_setup'
    sys.argv = [str(ROOT / 'physics/run_student_three_arm.py'),
                '--mode', 'tier_mission', '--saved-tier', str(plan.path),
                '--difficulty', args.difficulty, '--task', str(args.task.resolve()),
                '--design', str(args.design.resolve()), '--output', str(scene_output),
                '--contents', args.contents, '--grasp-policy', 'automatic',
                '--query-fixture-readback', 'batched', '--step-limit', '0',
                '--wall-limit-s', '0',
                '--rigid16-readback', 'native', '--trace-mode', 'compact',
                '--execution-backend', 'device']
    calls = {}
    binding = None
    try:
        with block_online_planning() as calls:
            with local_scene_binding(plan, args.arm, selected_leg) as binding:
                entry.main()
    finally:
        sys.argv = original_argv
        save(args.output / 'ONLINE_CALL_GUARD.json', {'blocked_online_calls': calls, 'scope': 'arm_practice'})
        if binding is not None:
            save(args.output / 'LOCAL_SCENE_BINDING.json', binding)
    scene = read_record(scene_output / 'result.json')
    native = captured.get('native', {})
    cleanup_ok = ((scene.get('failure') or {}).get('type') == 'ArmPracticeComplete'
                  and scene.get('cleanup_errors') == []
                  and scene.get('source_bindings_unchanged') is True
                  and scene.get('full_mission_passed') is False)
    guarded = set(calls) == {'plan_student_motion', 'plan_joint_motion', 'ik'} and all(v == 0 for v in calls.values())
    owned = (binding is not None and binding['loader_calls'] == binding['callback_calls'] == 1
             and binding['restored'] is True and binding['full_tier_callback_allowed'] is False)
    accepted = native.get('practice_passed') is True and cleanup_ok and guarded and owned
    result = dict(schema_version='arm-practice-worker-v1', scope='arm_practice',
                  practice_passed=accepted, full_mission_passed=False, ranking_eligible=False,
                  standalone_arm_plan=True, input_arm_plan=input_plan, bound_arm_plan=bound_plan,
                  local_scene_binding=binding, native=native,
                  source_bindings_unchanged=scene.get('source_bindings_unchanged'),
                  scene_handoff_and_cleanup_ok=cleanup_ok, cleanup_errors=scene.get('cleanup_errors'),
                  scene_exit=scene.get('failure'), blocked_online_calls=calls,
                  entry_timing=scene.get('entry_timing'), process_wall_s=scene.get('process_wall_s'))
    save(args.output / 'PRACTICE_WORKER.json', result)
    return 0 if accepted else 1


def verify_bound_worker_plan(worker, output, original):
    """Authenticate the local file instead of trusting a worker-supplied path."""
    from physics.arm_mission_plan import ArmMissionPlan
    path = Path(output).resolve() / 'BOUND_ARM_PLAN.json'
    record = worker.get('bound_arm_plan', {})
    if record.get('file') != str(path):
        raise ValueError('Practice local arm artifact escaped its owned output')
    plan = ArmMissionPlan.from_file(path)
    _identity(plan, original.required_arm_ids[0])
    expected_bound = dict(file=str(path), sha256=plan.file_sha256, bundle_sha256=plan.sha256,
                          schema_version='saved-arm-mission-v1', scope='independent_arm')
    if (record != expected_bound
            or producer_identity(plan) != producer_identity(original)
            or worker.get('native', {}).get('saved_bundle_sha256') != plan.sha256
            or worker.get('native', {}).get('saved_plan_file_sha256') != plan.file_sha256
            or plan.record()['stages'] != original.record()['stages']
            or plan.task_contract != original.task_contract):
        raise ValueError('Practice native receipt differs from its exact local arm plan')
    expected_input = dict(file=str(original.path), sha256=original.file_sha256,
                          bundle_sha256=original.sha256, schema_version='saved-arm-mission-v1',
                          scope='independent_arm', selected_arm_id=original.required_arm_ids[0])
    if worker.get('input_arm_plan') != expected_input:
        raise ValueError('Practice original arm component provenance changed')
    return plan


def main(argv=None):
    started = time.perf_counter()
    p = parser()
    raw = list(sys.argv[1:] if argv is None else argv)
    args = p.parse_args(raw)
    if not math.isfinite(args.wall_limit_s) or args.wall_limit_s < 0:
        p.error('The deprecated wall-limit value must be finite and nonnegative')
    if args.wall_limit_s > 0:
        print('Note: --wall-limit-s is deprecated and ignored. Computation wall time is measured without a limit.', file=sys.stderr)
    if not 0 < args.vram_limit_gb <= 5 or not .1 <= args.memory_interval_s <= 3:
        p.error('Use process VRAM at most 5 GB and memory sampling 0.1..3 seconds')
    try:
        from student_design_input import prepare_arguments
        forwarded, binding = prepare_arguments('review', raw)
        args = p.parse_args(forwarded)
        args.wall_limit_s = 0.
        plan = load_plan(args)
        sources = practice_source_snapshot()
    except (ValueError, OSError, KeyError, TypeError) as exc:
        p.error(str(exc))
    args.output = args.output.resolve()
    if args.worker:
        return run_worker(args)
    if os.name != 'nt':
        p.error('This supplied resource monitor requires Windows')
    log = args.output.with_suffix('.worker.log')
    memory_path = args.output.with_suffix('.memory_samples.jsonl')
    if any(path.exists() for path in (args.output, log, memory_path)):
        p.error('Use a fresh practice output directory and log names')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    hardware = hardware_snapshot()
    command = [sys.executable, '-B', '-m', 'physics.review_arm_plan', '--worker',
               '--saved-arm', str(args.saved_arm.resolve()), '--difficulty', args.difficulty,
               '--task', str(args.task.resolve()), '--design', str(args.design.resolve()),
               '--arm', args.arm, '--contents', args.contents, '--output', str(args.output),
               '--wall-limit-s', '0', '--vram-limit-gb', str(args.vram_limit_gb),
               '--memory-interval-s', str(args.memory_interval_s)]
    from physics.review_process_memory import ProcessMemorySampler, creation_identity, kill_owned_tree, wait_owned_worker
    with log.open('xb') as stream:
        process = subprocess.Popen(command, cwd=str(ROOT), stdout=stream, stderr=subprocess.STDOUT,
                                   creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        identity = None
        try:
            identity = creation_identity(process)
            memory = ProcessMemorySampler(process, memory_path, started=started,
                                          interval_s=args.memory_interval_s, limit_gb=args.vram_limit_gb)
            memory.start()
        except BaseException:
            if identity is not None:
                kill_owned_tree(process, identity)
            else:
                process.kill()
                process.wait(timeout=3.)
            raise
        print(json.dumps({'event': 'Local arm practice started', 'arm': args.arm,
                          'contents': args.contents, 'output': str(args.output)}), flush=True)
        try:
            waited = wait_owned_worker(process, memory, identity, deadline=None)
        finally:
            if process.poll() is None:
                kill_owned_tree(process, identity)
            memory_receipt = memory.finish()
    args.output.mkdir(parents=True, exist_ok=True)
    worker = read_record(args.output / 'PRACTICE_WORKER.json')
    bound_plan = None
    try:
        bound_plan = verify_bound_worker_plan(worker, args.output, plan)
        unchanged = plan.verify_sources() is True and practice_source_snapshot() == sources
    except Exception:
        unchanged = False
    physical = worker.get('practice_passed') is True and unchanged
    accepted = (physical and waited['exit_code'] == 0 and not waited['timed_out']
                and not waited['memory_limit_stop']
                and memory_receipt.get('observed_sampled_peak_within_limit') is True)
    failed_checks = []
    for name, ok in (
        ('native_practice', worker.get('practice_passed') is True),
        ('source_bindings', unchanged), ('worker_exit', waited['exit_code'] == 0),
        ('worker_interrupted', not waited['timed_out']),
        ('memory_limit', not waited['memory_limit_stop']),
        ('memory_measurement', memory_receipt.get('observed_sampled_peak_within_limit') is True),
    ):
        if not ok:
            failed_checks.append(name)
    native_failure = (worker.get('native') or {}).get('failure')
    failure = native_failure or (None if accepted else {
        'type': 'PracticeReviewRejected', 'message': ', '.join(failed_checks),
        'scene_detail': worker.get('scene_exit') if worker.get('scene_handoff_and_cleanup_ok') is not True else None})
    record = dict(schema_version='student-arm-practice-v1', scope='arm_practice',
                  status='ARM_PRACTICE_PASS' if accepted else 'ARM_PRACTICE_FAILED',
                  practice_passed=accepted, physical_transport_accepted=physical,
                  full_mission_passed=False, ranking_eligible=False,
                  leaderboard_status='local_practice_only', difficulty=args.difficulty,
                  failure=failure, failed_checks=failed_checks,
                  arm_id=args.arm, contents=args.contents, saved_bundle_sha256=bound_plan.sha256 if bound_plan is not None else None,
                  saved_file_sha256=bound_plan.file_sha256 if bound_plan is not None else None,
                  standalone_arm_plan=True, input_arm_plan=worker.get('input_arm_plan'),
                  bound_arm_plan=worker.get('bound_arm_plan'), plan_scope='independent_arm',
                  plan_schema_version='saved-arm-mission-v1', source_bindings_unchanged=unchanged,
                  practice_sources=sources, compact_design_binding=binding,
                  review_wall_s=time.perf_counter()-started, worker=worker,
                  wall_limit_s=None, wall_time_policy='measure_only',
                  exit_code=waited['exit_code'], timed_out=waited['timed_out'],
                  memory_limit_stop=waited['memory_limit_stop'], process_memory=memory_receipt,
                  hardware_before=hardware, worker_command=command,
                  note='Independent CPU arm component, materialized locally and practiced at its nominal supported boundary. No preceding physical handoff, full completion, or competition claim.')
    save(args.output / 'PRACTICE_RESULT.json', record)
    print(json.dumps({k: record[k] for k in ('status', 'arm_id', 'contents', 'review_wall_s', 'ranking_eligible')}))
    return 0 if accepted else 1


if __name__ == '__main__':
    raise SystemExit(main())
