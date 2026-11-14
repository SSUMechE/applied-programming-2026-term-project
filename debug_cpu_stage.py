"""Optional local CPU stage diagnosis. Never exports a replayable mission.

Use a previous tier CPU output from this layout. Earlier saved stages supply
the preceding state, while the selected student algorithm may have changed.
Run the normal full CPU command again before any Genesis review.
"""
from __future__ import annotations

import argparse
import copy
import json
import math
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parent


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--cpu-dir', type=Path, required=True, help='Existing CPU output or its worker directory')
    p.add_argument('--difficulty', choices=('easy', 'medium', 'hard'), required=True)
    p.add_argument('--stage', default='next', help='next, or a completed stage ID such as arm_0__carry')
    p.add_argument('--task', type=Path)
    p.add_argument('--design', type=Path)
    p.add_argument('--strategy', default='student.joint_strategy')
    p.add_argument('--seed', type=int, help='Original full CPU seed, inferred from a completed stage when available')
    p.add_argument('--max-queries', type=int, help='Original per-stage query budget, inferred when available')
    p.add_argument('--endpoint-attempts', type=int, help='Original endpoint IK budget, inferred when available')
    p.add_argument('--plan', action='store_true', help='Also call your current strategy on this one request')
    p.add_argument('--edge-timing-hook', action='store_true')
    p.add_argument('--wall-limit-s', type=float, default=120, help='Positive diagnostic process limit, default 120 seconds')
    p.add_argument('--output', type=Path, required=True, help='Fresh diagnostic directory')
    p.add_argument('--worker', action='store_true', help=argparse.SUPPRESS)
    return p


def prepare_stage(args, check=lambda: None):
    """Reconstruct a nominal boundary with the same supplied CPU contracts."""
    from dataclasses import replace
    import numpy as np
    from physics.automatic_runtime_binding import prepare_automatic_scenario
    from physics.carried_body import VesselModel, WorldPose, measure_hand_to_body
    from physics.one_carry_planning import material_inputs
    from physics.plan_student_transport import read, sha, PLANNING_OPTIONS, native_geometry_precision
    from physics.provided_manipulation import grasp_endpoint_candidates, solve_endpoint
    from physics.tier_mission_plan import make_task_contract, verify_task_design, task_stages
    from physics.saved_motion_planning import require
    from supplied_support.cpu_geometry import CpuGeometry, _rotation, _quat
    from transport_challenge.joint_context import JointBasePlacement, JointTaskContext
    from transport_challenge.joint_kinematics import BoundedKinematics
    from transport_challenge.joint_planning import JointLimits, JointTrajectory, make_joint_request
    from transport_challenge.three_arm_design import ThreeArmDesign, bind_three_arm_design

    run = args.cpu_dir.resolve(strict=True)
    if not (run / 'input_identity.json').is_file():
        run = run / 'worker'
    identity = read(run / 'input_identity.json')
    summary = read(run / 'CPU_PLANNING_RESULT.json')
    require(summary.get('planning_scope') == 'tier-mission' and summary.get('material') == 'beads'
            and summary.get('post_place_policy') == 'kinematic_return'
            and summary.get('source_bindings_unchanged') is True
            and summary.get('genesis_initialized') is False
            and summary.get('native_physical_steps') == summary.get('cpu_physical_steps') == 0,
            'Need a completed CPU-only tier receipt with unchanged source bindings')
    original_pins = read(run / 'source_pins.json')
    pipeline_paths = [Path(p) for p in original_pins
                      if Path(p).as_posix().endswith('/physics/plan_student_transport.py')]
    require(len(pipeline_paths) == 1, 'Cannot identify the original CPU repository')
    original_root = pipeline_paths[0].parent.parent
    current_pins = {}
    for name, expected in original_pins.items():
        old = Path(name)
        if old.is_relative_to(original_root):
            relative = old.relative_to(original_root)
            if relative.parts[0] == 'student' and relative.suffix == '.py':
                continue  # Algorithm edits are the purpose of this local diagnosis.
            current = ROOT / relative
        else:
            current = old  # Exact installed mesh assets remain source-bound.
        require(current.is_file() and sha(current) == expected,
                'Stale design, map or supplied source. Run full CPU again: ' + str(current))
        current_pins[str(current.resolve())] = expected
    rule_path = ROOT / 'leaderboard/rules/term_v3' / ('ap2026-' + args.difficulty + '-beads-term-v3.json')
    rule = read(rule_path)
    for relative, expected in rule['evaluation_sources'].items():
        current = ROOT / relative
        require(current.is_file() and sha(current) == expected, 'Protected evaluation source differs: ' + relative)
        current_pins[str(current.resolve())] = expected
    current_pins[str(rule_path)] = sha(rule_path)
    map_names = {'easy': 'easy_open_v1', 'medium': 'medium_staggered_v1', 'hard': 'hard_industrial_v1'}
    task = (args.task or ROOT / 'cases/difficulty_20261106' / map_names[args.difficulty]).resolve()
    design_path = (args.design or ROOT / 'student' / (args.difficulty + '_design.json')).resolve()
    from student_design_input import prepare_arguments, option
    forwarded, compact_binding = prepare_arguments('plan',
        ['--scope', 'tier-mission', '--difficulty', args.difficulty,
         '--task', str(task), '--design', str(design_path)], root=ROOT)
    design_path = Path(option(forwarded, 'design')).resolve()
    require(str(design_path) in current_pins, 'Use the original pinned design file')
    if compact_binding is not None:
        current_pins[compact_binding['student_file']] = compact_binding['student_file_sha256']
        for supplied_name in ('student_design_input.py', 'supplied_design_defaults.json', 'project_run.py'):
            current_pins[str(ROOT / supplied_name)] = sha(ROOT / supplied_name)
    manifest = read(task / 'manifest.json')
    require(current_pins.get(str(task / 'manifest.json')) == sha(task / 'manifest.json'), 'Use the original pinned map')
    for relative, expected in manifest['files'].items():
        require(sha(ROOT / relative) == expected, 'Map manifest mismatch: ' + relative)
    original_world, original_scenario = read(task / 'world.json'), read(task / 'initialization.json')
    target = read(task / 'difficulty_target.json')
    require(target['difficulty'] == args.difficulty and target['case_id'] == original_world['case_record']['case_id'],
            'Selected difficulty and map disagree')
    design = ThreeArmDesign.from_record(read(design_path), original_world)
    bound = bind_three_arm_design(design, original_world, original_scenario)
    scenario, _ = prepare_automatic_scenario(bound['scenario'])
    world, scenario = material_inputs(bound['world'], scenario, 'beads')
    require(identity == {'world': world, 'scenario': scenario, 'design': design.to_record()},
            'CPU input identity differs from the current map and design')
    contract = make_task_contract(original_world, original_scenario, difficulty=args.difficulty,
                                 task_manifest_sha256=sha(task / 'manifest.json'), selected_world=world)
    verify_task_design(contract, world, scenario)
    queue = task_stages(contract)
    completed = summary.get('completed_stages')
    require(type(completed) is int and 0 <= completed <= len(queue), 'Invalid completed stage count')
    if completed:
        first = read(run / (queue[0][0] + '.json'))['offline_planning']
        original_options = dict(seed=first['planning_request']['seed'],
                                max_queries=first['planning_request']['max_queries'],
                                endpoint_attempts=first['terminal']['cpu_solver_result']['attempts'])
        for name, value in original_options.items():
            require(getattr(args, name) in (None, value), 'Use the original seed/query budget and endpoint-attempts')
            setattr(args, name, value)
    require(all(getattr(args, name) is not None for name in ('seed', 'max_queries', 'endpoint_attempts')),
            'No completed stage retained the original budgets. Supply the original --seed, --max-queries and --endpoint-attempts')
    stage_ids = [row[0] for row in queue]
    selected = completed if args.stage == 'next' else stage_ids.index(args.stage)
    require(selected < len(queue), 'All stages completed. Select an explicit completed stage ID')
    require(selected <= completed, 'The requested stage has missing preceding CPU stages')
    geometry = native_geometry_precision(CpuGeometry(world, scenario), scenario, '32')
    current_pins.update(geometry.source_pins)
    commands = {name: {'q_rad': values[:7], 'finger_q_m': values[7:]}
                for name, values in geometry.initial_q_by_arm.items()}
    initial = copy.deepcopy(commands)
    body = WorldPose(**{key: scenario['body_pose'][key] for key in ('position_m', 'quaternion_wxyz')})
    transform = None
    route = contract['support_route']
    input_pins = {str(run / name): sha(run / name)
                  for name in ('input_identity.json', 'CPU_PLANNING_RESULT.json', 'source_pins.json')}
    for index, (stage_id, kind, arm) in enumerate(queue[:selected]):
        check()
        path = run / (stage_id + '.json')
        stage = read(path)
        input_pins[str(path)] = sha(path)
        limits = JointLimits(geometry.bounds, scenario['case']['motion_limits']['velocity_rad_s'],
                             scenario['case']['motion_limits']['acceleration_rad_s2'])
        trajectory = JointTrajectory(stage['trajectory']['waypoints'], stage['trajectory']['edge_durations_s'], limits)
        request = stage['offline_planning']['planning_request']
        expected_request = make_joint_request(commands[arm]['q_rad'], trajectory.waypoints[-1], limits,
                                             max_queries=args.max_queries, seed=args.seed + index,
                                             options=PLANNING_OPTIONS).to_record()
        slot = contract['required_arm_ids'].index(arm)
        require((stage['stage_id'], stage['kind'], stage['active_arm_id']) == (stage_id, kind, arm)
                and stage['trajectory'] == trajectory.to_record()
                and list(trajectory.waypoints[0]) == commands[arm]['q_rad']
                and request == expected_request
                and stage['expected_peer_commands_by_arm'] == {n: c for n, c in commands.items() if n != arm}
                and stage['nominal_body_world'] == body.to_record()
                and stage['support_id'] == route[slot]
                and stage['destination_support_id'] == (route[slot + 1] if kind == 'carry' else None),
                'Saved stage boundary or original seed/query budget differs: ' + stage_id)
        terminal = stage['offline_planning']['terminal']['cpu_solver_result']
        require(terminal['attempts'] == args.endpoint_attempts and terminal['seed'] == args.seed + index,
                'Use the original endpoint-attempts and seed')
        cpu = geometry.configure(arm, commands, body, holder_arm_id=arm if kind == 'carry' else None,
                                 hand_to_body=transform if kind == 'carry' else None)
        commands[arm]['q_rad'] = list(trajectory.waypoints[-1])
        if kind == 'approach':
            hand = cpu.fk(trajectory.waypoints[-1])
            transform = measure_hand_to_body(WorldPose(hand.position_m, hand.quaternion_wxyz), body).to_record()
        else:
            goal_body = scenario['supported_body_poses'][route[slot + 1]]
            require(stage['nominal_goal_body_world'] == goal_body and stage['nominal_hand_to_body'] == transform,
                    'Saved carry body target or grasp transform differs')
            body = WorldPose(goal_body['position_m'], goal_body['quaternion_wxyz'])
            transform = None
            if arm in contract['required_arm_ids'][:-1]:
                commands[arm] = copy.deepcopy(initial[arm])
    stage_id, kind, arm = queue[selected]
    slot = contract['required_arm_ids'].index(arm)
    destination = route[slot + 1] if kind == 'carry' else None
    cpu = geometry.configure(arm, commands, body, holder_arm_id=arm if kind == 'carry' else None,
                             hand_to_body=transform if kind == 'carry' else None)
    limits = JointLimits(cpu.bounds, scenario['case']['motion_limits']['velocity_rad_s'],
                         scenario['case']['motion_limits']['acceleration_rad_s2'])
    start = tuple(commands[arm]['q_rad'])
    require(cpu.state_valid(start), 'CPU stage start invalid: ' + json.dumps(cpu.last_query_diagnostic()))
    if kind == 'approach':
        hand_target = grasp_endpoint_candidates(VesselModel.from_container(scenario['container']), body.to_record())[0].to_record()
    else:
        command_body = copy.deepcopy(scenario['supported_body_poses'][destination])
        command_body['position_m'][2] -= 0.0003
        rotation = _rotation(command_body['quaternion_wxyz']) @ _rotation(transform['quaternion_hand_body_wxyz']).T
        position = np.asarray(command_body['position_m']) - rotation @ transform['translation_hand_m']
        hand_target = WorldPose(position.tolist(), _quat(rotation).tolist()).to_record()
    endpoint_kin = BoundedKinematics(cpu, cpu.bounds, scenario['case']['tool_workspace_m'],
                                    max_fk_calls=args.endpoint_attempts, max_ik_calls=args.endpoint_attempts, wall_check=check)
    terminal = solve_endpoint(endpoint_kin, cpu, start, (hand_target,), attempts=args.endpoint_attempts,
                              seed=args.seed + selected, wall_check=check)
    require(terminal['status'] == 'ENDPOINT_FOUND', 'CPU nominal endpoint not found: ' + json.dumps(terminal))
    goal = tuple(terminal['q_rad'])
    case = copy.deepcopy(scenario['case'])
    case['planning']['tracking_clearance_m'] = geometry.clearance
    case['planning']['edge_max_joint_delta_rad'] = geometry.resolution
    recipe = next(r for r in scenario['robots'] if r['id'] == arm)
    context = JointTaskContext.from_case(case, base_placement=JointBasePlacement(**recipe['base_placement']), limits=limits,
                                        start_tool_pose=cpu.fk(start), goal_tool_pose=cpu.fk(goal), active_arm_id=arm,
                                        motion_kind=kind, source_support_id=route[slot], destination_support_id=destination)
    context = replace(context, fixed_base_z_m=recipe.get('base_z_m', case['base_z_m']))
    request = make_joint_request(start, goal, limits, max_queries=args.max_queries, seed=args.seed + selected, options=PLANNING_OPTIONS)
    if selected < completed:
        path = run / (stage_id + '.json')
        saved = read(path)
        input_pins[str(path)] = sha(path)
        require(request.to_record() == saved['offline_planning']['planning_request']
                and context.to_record() == saved['offline_planning']['plan_evidence']['joint_task_context']
                and terminal == saved['offline_planning']['terminal']['cpu_solver_result'],
                'Reconstructed completed stage differs from its original request, context or endpoint')
    return dict(cpu=cpu, geometry=geometry, request=request, context=context, limits=limits,
                speed_scale=bound['joint_speed_scale'], pins={**current_pins, **input_pins},
                metadata=dict(stage_id=stage_id, source_support_id=route[slot], destination_support_id=destination,
                              support_route=route, expected_peer_commands_by_arm={n: c for n, c in commands.items() if n != arm},
                              nominal_body_world=body.to_record(), nominal_hand_to_body=transform,
                              nominal_goal_hand_world=hand_target, terminal=terminal,
                              request=request.to_record(), context=context.to_record(),
                              preceding_stages_reused=selected, original_completed_stages=completed))


def worker(args):
    from physics.plan_student_transport import (NoGenesis, block_integration, restore_integration,
                                                load_strategy, sha, save)
    from physics.source_guard import PinnedSourceGuard
    from transport_challenge.joint_kinematics import BoundedKinematics
    from transport_challenge.joint_planning import plan_joint_motion
    started = time.perf_counter()
    if any(n == 'genesis' or n.startswith('genesis.') for n in sys.modules):
        raise RuntimeError('Use a fresh CPU process without Genesis')
    blocker = NoGenesis()
    sys.meta_path.insert(0, blocker)
    import mujoco
    integration = block_integration(mujoco)
    result = dict(schema_version='optional-cpu-stage-diagnostic-v1', status='DIAGNOSTIC_FAILED',
                  replayable_path_exported=False, full_mission_passed=False, physical_transport_verified=False,
                  student_release_approved=False, native_physical_steps=0, cpu_physical_steps=0,
                  next_required_action='Run a fresh normal full CPU export before Genesis review.')
    guard = None

    def check():
        if time.perf_counter() - started >= args.wall_limit_s:
            raise TimeoutError('Optional diagnostic wall limit reached')
        if guard is not None and guard() is not True:
            raise RuntimeError('A diagnostic source or prior output changed during this run')

    try:
        prepared = prepare_stage(args, check)
        result.update(prepared['metadata'])
        pins = prepared['pins']
        pins.update({str(p.resolve()): sha(p) for p in (ROOT / 'student').rglob('*.py')})
        pins[str(Path(__file__).resolve())] = sha(__file__)
        guard = PinnedSourceGuard(pins, full_interval_s=1.0)
        if not guard.before_request():
            raise RuntimeError('Diagnostic source boundary failed')
        cpu = prepared['cpu']

        class Oracle:
            def state_valid(self, q):
                check()
                answer = cpu.state_valid(q)
                check()
                return answer

            def edge_valid(self, a, b):
                check()
                answer = cpu.edge_valid(a, b)
                check()
                return answer

            def last_query_diagnostic(self):
                return cpu.last_query_diagnostic()

        oracle = Oracle()
        request = prepared['request']
        result['direct_edge_valid'] = oracle.edge_valid(request.start, request.goal)
        result['direct_edge_diagnostic'] = oracle.last_query_diagnostic()
        result['status'] = 'DIAGNOSTIC_READY'
        if args.plan:
            strategy = load_strategy(args.strategy)
            kin = BoundedKinematics(cpu, cpu.bounds, prepared['context'].workspace_bounds_m,
                                    max_fk_calls=50000, max_ik_calls=10000, wall_check=check)
            hook = getattr(strategy, 'choose_edge_durations_s', None) if args.edge_timing_hook else None
            if args.edge_timing_hook and not callable(hook):
                raise RuntimeError('Timing opt-in requires choose_edge_durations_s')
            plan = plan_joint_motion(request, oracle, strategy, prepared['limits'],
                                     speed_scale=prepared['speed_scale'], kinematics=kin, context=prepared['context'],
                                     edge_timing_hook=hook, oracle_description='Optional isolated CPU diagnostic. No physical execution or mission export.')
            result['diagnostic_plan'] = plan.to_record()
            result['status'] = 'DIAGNOSTIC_STAGE_PLANNED'
    except BaseException as error:
        result.update(status='DIAGNOSTIC_FAILED', failure={'type': type(error).__name__, 'message': str(error)})
    finally:
        if guard is not None:
            result['source_bindings_unchanged'] = guard.after_request()
            if not result['source_bindings_unchanged']:
                result['status'] = 'DIAGNOSTIC_FAILED'
        result.update(blocked_genesis_import_attempts=blocker.blocked_attempts,
                      blocked_integration_attempts=integration['attempts'],
                      genesis_imported_modules=[n for n in sys.modules if n == 'genesis' or n.startswith('genesis.')])
        if blocker.blocked_attempts or integration['attempts'] or result['genesis_imported_modules']:
            result['status'] = 'DIAGNOSTIC_FAILED'
        restore_integration(mujoco, integration)
        result['wall_s'] = time.perf_counter() - started
        save(args.output / 'DIAGNOSTIC.json', result)
    print(json.dumps({k: result[k] for k in ('status', 'next_required_action')}), flush=True)
    return 0 if result['status'] != 'DIAGNOSTIC_FAILED' else 1


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    p = parser()
    args = p.parse_args(argv)
    if (not math.isfinite(args.wall_limit_s) or args.wall_limit_s <= 0
            or (args.max_queries is not None and args.max_queries < 1)
            or (args.endpoint_attempts is not None and not 1 <= args.endpoint_attempts <= 128)
            or (args.seed is not None and not 0 <= args.seed < 2**31 - 6)):
        p.error('Use positive finite wall/query budgets, 1..128 endpoint attempts and a nonnegative 31-bit seed')
    if args.worker:
        return worker(args)
    if args.output.exists():
        p.error('Use a fresh optional diagnostic output directory')
    args.output.mkdir(parents=True)
    from physics.plan_student_transport import supervise_cpu_worker, read, save
    command = [sys.executable, '-B', str(Path(__file__).resolve()), *argv, '--worker']
    receipt = supervise_cpu_worker(command, output=args.output, timeout_s=args.wall_limit_s)
    save(args.output / 'DIAGNOSTIC_PROCESS.json', receipt)
    result_path = args.output / 'DIAGNOSTIC.json'
    if not result_path.exists():
        save(result_path, dict(schema_version='optional-cpu-stage-diagnostic-v1',
             status='DIAGNOSTIC_TIMEOUT' if receipt['timed_out'] else 'DIAGNOSTIC_FAILED',
             failure={'type': 'CpuWorkerDidNotComplete', 'message': 'Consult DIAGNOSTIC_PROCESS.json and worker_stdout.log.'},
             replayable_path_exported=False, full_mission_passed=False, physical_transport_verified=False,
             student_release_approved=False,
             next_required_action='Run a fresh normal full CPU export before Genesis review.'))
    try:
        result = read(result_path)
        expected_status = 'DIAGNOSTIC_STAGE_PLANNED' if args.plan else 'DIAGNOSTIC_READY'
        admitted = (result.get('status') == expected_status and result.get('source_bindings_unchanged') is True
                    and all(result.get(k) == [] for k in ('blocked_genesis_import_attempts',
                            'blocked_integration_attempts', 'genesis_imported_modules'))
                    and all(result.get(k) is False for k in ('replayable_path_exported',
                            'full_mission_passed', 'physical_transport_verified', 'student_release_approved')))
    except (ValueError, AttributeError):
        admitted = False
    print(json.dumps({'diagnostic_report': str(result_path), 'timed_out': receipt['timed_out'],
                      'worker_exit_code': receipt['worker_exit_code'], 'replayable_path_exported': False}), flush=True)
    return 0 if (admitted and receipt['worker_exit_code'] == 0 and not receipt['timed_out']
                 and not receipt['cleanup_errors'] and not receipt.get('failure')) else 1


if __name__ == '__main__':
    raise SystemExit(main())
