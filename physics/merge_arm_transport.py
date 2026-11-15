"""Assemble separately planned arms by checking their exact saved geometry.

This supplied CPU operation does not call a student planner, IK or timing hook.
Original components remain archived, with their actual producer source bytes.
Only a later unchanged full Genesis evaluation can establish mission success.
"""
from __future__ import annotations

import copy
import hashlib
import inspect
from pathlib import Path

from physics.arm_mission_plan import (
    ArmMissionPlan, common_problem, current_pins, source_manifest,
    validate_component_set,
)
from physics.saved_motion_planning import file_sha, require
from physics.tier_mission_plan import donor_return_schedule
from transport_challenge.contracts import digest

ROOT = Path(__file__).resolve().parents[1]


def assemble_arm_plans(args, inputs, geometry, check, output):
    """Return a newly checked complete CPU bundle and actual assembly evidence."""
    from physics.plan_arm_transport import base_record, revalidate_stages

    paths = [Path(p).resolve(strict=True) for p in args.arm_plans]
    require(len(paths) == len(set(paths)), 'Do not supply the same arm component twice')
    require(all(p.stat().st_size <= 64 * 1024 * 1024 for p in paths),
            'An arm component exceeds the 64 MB input bound')
    plans = [ArmMissionPlan.from_file(path) for path in paths]
    records = validate_component_set(plans, difficulty=args.difficulty)
    by_arm = {p.selected_arm_id: p for p in plans}
    used = {}
    for record in records:
        for name, snapshot in record['portable_sources']['student'].items():
            require(name not in used or used[name] == snapshot,
                    'Arm producers disagree about an imported helper: ' + name)
            used[name] = copy.deepcopy(snapshot)

    bundle = base_record(inputs)
    portable = source_manifest(inputs, used)
    expected = copy.deepcopy(bundle)
    expected['portable_sources'] = portable
    require(common_problem(expected) == records[0]['common_problem'],
            'Components do not match the current shared design, peers, fixed map or supplied release')
    for plan in plans:
        plan.verify_sources()
    check()

    # The endpoint and every fixed-resolution connection are checked against
    # today's shared whole-scene geometry. No constructor-only shortcut.
    validation = revalidate_stages(inputs, geometry, records, check)
    stages = [copy.deepcopy(stage) for record in records for stage in record['stages']]
    require([stage['active_arm_id'] for stage in stages] ==
            [arm for arm in inputs['task_contract']['required_arm_ids'] for _ in range(2)],
            'Assembled motion order differs from the actual task')
    check()

    archive = Path(output) / 'components'
    archive.mkdir(parents=True, exist_ok=False)
    sources = current_pins(portable)
    # The ordinary student review command expands compact designs into a
    # checked local cache. Bind those exact bytes as well as the original
    # student JSON so the unchanged full-score exporter sees its real input.
    from student_design_input import prepare_arguments, option
    from transport_challenge.three_arm_design import ThreeArmDesign
    import json
    forwarded, design_binding = prepare_arguments('review', [
        '--difficulty', args.difficulty,
        '--task', str(ROOT / inputs['task_relative']),
        '--design', str(ROOT / inputs['design_relative']),
    ])
    evaluated_design = Path(option(forwarded, 'design')).resolve(strict=True)
    actual_design = ThreeArmDesign.from_record(json.loads(evaluated_design.read_bytes()),
        json.loads((ROOT / inputs['task_relative'] / 'world.json').read_bytes()))
    require(actual_design.to_record() == inputs['design'],
            'Review design expansion differs from the assembled shared design')
    sources[str(evaluated_design)] = file_sha(evaluated_design)
    components = []
    for record in records:
        plan = by_arm[record['selected_arm_id']]
        raw = plan.path.read_bytes()
        require(hashlib.sha256(raw).hexdigest() == plan.file_sha256,
                'An arm component changed while assembling')
        saved = archive / (plan.selected_arm_id + '.json')
        with saved.open('xb') as stream:
            stream.write(raw)
        sources[str(saved.resolve())] = plan.file_sha256
        components.append({
            'arm_id': plan.selected_arm_id,
            'archived_file': str(saved.resolve()),
            'file_sha256': plan.file_sha256,
            'bundle_sha256': plan.sha256,
            'source_schema': record['schema_version'],
            'producer': copy.deepcopy(record['offline_planner']),
            'used_student_sources': copy.deepcopy(record['portable_sources']['student']),
            'stage_sha256': [digest(s) for s in record['stages']],
            'trajectory_sha256': [digest(s['trajectory']) for s in record['stages']],
            'original_cpu_wall_s': record['cpu_planning_wall_s'],
            'original_cpu_supervisor': copy.deepcopy(record['cpu_process_supervisor']),
        })
        plan.verify_sources()
    check()

    bundle.update(
        schema_version='saved-tier-mission-v1', scope='tier_mission',
        stages=stages, post_place_resets=[],
        post_place_returns=donor_return_schedule(stages, inputs['initial_commands_by_arm'], inputs['task_contract']),
        stored_motion_duration_s=sum(s['trajectory']['duration_s'] for s in stages),
        source_pins=sources, portable_sources=portable,
        offline_planner={
            'module': 'physics.merge_arm_transport',
            'source_sha256': file_sha(__file__),
            # Legacy field name consumed by unchanged saved-path replay. This
            # hash describes the actual assembler, not a claimed student solve.
            'plan_path_source_sha256': hashlib.sha256(inspect.getsource(assemble_arm_plans).encode()).hexdigest(),
            'plan_path_source_meaning': 'Source of provided assemble_arm_plans. No plan_path call occurs.',
            'assembly_entry_point': 'physics.merge_arm_transport.assemble_arm_plans',
            'cpu_pipeline_module': 'physics.plan_arm_transport',
            'strategy_role': 'provided_fixed_path_assembler',
            'post_place_policy': 'kinematic_return',
            'joint_speed_scale': inputs['joint_speed_scale'],
            'student_plan_path_invocations': 0, 'ik_calls': 0, 'retiming_calls': 0,
        },
        arm_assembly={
            'schema_version': 'fixed-path-arm-assembly-v1',
            'common_problem_sha256': records[0]['common_problem_sha256'],
            'component_count': len(components), 'components': components,
            'geometry_validation': validation,
            'paths_and_edge_times_unchanged': True,
            'student_plan_path_invocations': 0, 'ik_calls': 0, 'retiming_calls': 0,
            'practice_results_consumed': False,
            'physical_completion_claimed': False,
            'review_design_file': str(evaluated_design),
            'review_design_file_sha256': file_sha(evaluated_design),
            'compact_design_binding': design_binding,
            'cost_policy': 'This export reports its actual assembly validation cost. Original component CPU costs remain separate.',
        },
    )
    require([s['trajectory'] for s in bundle['stages']] ==
            [s['trajectory'] for record in records for s in record['stages']],
            'Assembly changed a saved path or time')
    return bundle, 0.0, {
        'operation': 'merge-arms', 'assembly_component_count': len(components),
        'student_plan_path_invocations': 0, 'ik_calls': 0, 'retiming_calls': 0,
        'assembly_geometry_validation': validation,
        'practice_results_consumed': False,
    }


def main(argv=None):
    from physics.plan_arm_transport import main as supplied_main
    return supplied_main(argv, operation='merge')


if __name__ == '__main__':
    raise SystemExit(main())
