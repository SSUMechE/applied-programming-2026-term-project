"""Local per-arm Genesis practice using an unchanged saved tier trajectory.

This additive adapter reuses the frozen scene and audited physical phase engine.
Its separate result schema is never a full-mission or leaderboard receipt.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import time

from physics.review_tier_transport import hardware_snapshot, read_record, save

ROOT = Path(__file__).resolve().parents[1]
PRACTICE_FILES = ('project_run.py', 'physics/review_arm_practice.py', 'physics/device_arm_practice.py')


class ArmPracticeComplete(Exception):
    """Leave the frozen full-tier wrapper without producing full-tier evidence."""


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--saved-tier', type=Path, required=True)
    p.add_argument('--difficulty', choices=('easy', 'medium', 'hard'), required=True)
    p.add_argument('--task', type=Path, required=True)
    p.add_argument('--design', type=Path, required=True)
    p.add_argument('--arm', choices=('arm_0', 'arm_1', 'arm_2'), required=True)
    p.add_argument('--contents', choices=('empty', 'beads'), required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--wall-limit-s', type=float, default=1800., help='Local wall-time guard, not a completion score.')
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
            raise ValueError('Provided practice source changed: ' + name)
        result[name] = actual
    return result


def load_plan(args):
    from physics.tier_mission_plan import TierMissionPlan
    plan = TierMissionPlan.from_file(args.saved_tier)
    if plan.task_contract['difficulty'] != args.difficulty:
        raise ValueError('Difficulty differs from the saved CPU mission')
    if args.arm not in plan.required_arm_ids:
        raise ValueError('Choose a participating arm for this difficulty: ' + ', '.join(plan.required_arm_ids))
    if plan.verify_sources() is not True:
        raise ValueError('Saved CPU inputs changed. Plan again before practice.')
    return plan


def run_worker(args):
    from physics import run_student_three_arm as entry, device_tier_mission as frozen
    from physics.device_arm_practice import execute
    from physics.review_student_transport import block_online_planning
    captured = {}
    original_callback, original_argv = frozen.execute_device_tier_mission, sys.argv

    def selected_leg(context, *, output, wall_deadline):
        if captured:
            raise RuntimeError('Practice scene callback invoked more than once')
        captured['native'] = execute(context, output=args.output / 'arm_execution',
                                     deadline=wall_deadline, arm_id=args.arm)
        if captured['native'].get('practice_passed') is True:
            from physics.arm_practice_validation import validate_arm_practice_native
            validate_arm_practice_native(captured['native'],plan_record=context['student_driver'].saved_plan.record())
        raise ArmPracticeComplete('Local arm practice ended. No full-tier execution was requested.')

    args.output.mkdir(parents=True, exist_ok=False)
    scene_output = args.output / 'scene_setup'
    sys.argv = [str(ROOT / 'physics/run_student_three_arm.py'),
                '--mode', 'tier_mission', '--saved-tier', str(args.saved_tier.resolve()),
                '--difficulty', args.difficulty, '--task', str(args.task.resolve()),
                '--design', str(args.design.resolve()), '--output', str(scene_output),
                '--contents', args.contents, '--grasp-policy', 'automatic',
                '--query-fixture-readback', 'batched', '--step-limit', '0',
                '--wall-limit-s', str(args.wall_limit_s - 20.),
                '--rigid16-readback', 'native', '--trace-mode', 'compact',
                '--execution-backend', 'device']
    frozen.execute_device_tier_mission = selected_leg
    calls = {}
    try:
        with block_online_planning() as calls:
            entry.main()
    finally:
        frozen.execute_device_tier_mission, sys.argv = original_callback, original_argv
        save(args.output / 'ONLINE_CALL_GUARD.json', {'blocked_online_calls': calls, 'scope': 'arm_practice'})
    scene = read_record(scene_output / 'result.json')
    native = captured.get('native', {})
    # The frozen scene wrapper intentionally records the sentinel as a failure.
    # Keep that original record. Only this independent practice schema can pass.
    cleanup_ok = ((scene.get('failure') or {}).get('type') == 'ArmPracticeComplete'
                  and scene.get('cleanup_errors') == []
                  and scene.get('source_bindings_unchanged') is True
                  and scene.get('full_mission_passed') is False)
    guarded = set(calls) == {'plan_student_motion', 'plan_joint_motion', 'ik'} and all(v == 0 for v in calls.values())
    accepted = native.get('practice_passed') is True and cleanup_ok and guarded
    result = dict(schema_version='arm-practice-worker-v1', scope='arm_practice',
                  practice_passed=accepted, full_mission_passed=False, ranking_eligible=False,
                  native=native, source_bindings_unchanged=scene.get('source_bindings_unchanged'),
                  scene_handoff_and_cleanup_ok=cleanup_ok, cleanup_errors=scene.get('cleanup_errors'),
                  scene_exit=scene.get('failure'), blocked_online_calls=calls,
                  entry_timing=scene.get('entry_timing'), process_wall_s=scene.get('process_wall_s'))
    save(args.output / 'PRACTICE_WORKER.json', result)
    return 0 if accepted else 1


def main(argv=None):
    started = time.perf_counter()
    p = parser()
    raw = list(sys.argv[1:] if argv is None else argv)
    args = p.parse_args(raw)
    if not math.isfinite(args.wall_limit_s) or not 60 <= args.wall_limit_s <= 5400:
        p.error('Use a local wall-time guard between 60 and 5400 seconds')
    if not 0 < args.vram_limit_gb <= 5 or not .1 <= args.memory_interval_s <= 3:
        p.error('Use process VRAM at most 5 GB and memory sampling 0.1..3 seconds')
    try:
        from student_design_input import prepare_arguments
        forwarded, binding = prepare_arguments('review', raw)
        args = p.parse_args(forwarded)
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
    command = [sys.executable, '-B', '-m', 'physics.review_arm_practice', '--worker',
               '--saved-tier', str(args.saved_tier.resolve()), '--difficulty', args.difficulty,
               '--task', str(args.task.resolve()), '--design', str(args.design.resolve()),
               '--arm', args.arm, '--contents', args.contents, '--output', str(args.output),
               '--wall-limit-s', str(args.wall_limit_s), '--vram-limit-gb', str(args.vram_limit_gb),
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
            waited = wait_owned_worker(process, memory, identity, deadline=started + args.wall_limit_s)
        finally:
            if process.poll() is None:
                kill_owned_tree(process, identity)
            memory_receipt = memory.finish()
    args.output.mkdir(parents=True, exist_ok=True)
    worker = read_record(args.output / 'PRACTICE_WORKER.json')
    try:
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
        ('wall_limit', not waited['timed_out']),
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
                  arm_id=args.arm, contents=args.contents, saved_bundle_sha256=plan.sha256,
                  saved_file_sha256=plan.file_sha256, source_bindings_unchanged=unchanged,
                  practice_sources=sources, compact_design_binding=binding,
                  review_wall_s=time.perf_counter()-started, worker=worker,
                  exit_code=waited['exit_code'], timed_out=waited['timed_out'],
                  memory_limit_stop=waited['memory_limit_stop'], process_memory=memory_receipt,
                  hardware_before=hardware, worker_command=command,
                  note='Independent nominal-boundary practice. No preceding physical handoff, full completion, or competition claim.')
    save(args.output / 'PRACTICE_RESULT.json', record)
    print(json.dumps({k: record[k] for k in ('status', 'arm_id', 'contents', 'review_wall_s', 'ranking_eligible')}))
    return 0 if accepted else 1


if __name__ == '__main__':
    raise SystemExit(main())
