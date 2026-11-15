"""Validate local practice evidence without promoting it to mission evidence."""
from transport_challenge.material_access import BEAD_COUNT
import copy
from pathlib import Path

import numpy as np

from transport_challenge.joint_planning import JointLimits
from physics.device_tier_mission import (require, sha, trajectory_commands, validate_phase_schedule,
    _validate_ideal_bead_record, _validate_phase_bead_mode, _prepared_release_gate_passes)
from physics.native_transport_metrics import CarriedDistance


def validate_arm_practice_native(record, *, plan_record):
    require(type(record) is dict and record.get('schema_version')=='arm-practice-native-result-v1'
            and record.get('scope')=='arm_practice' and record.get('practice_passed') is True
            and record.get('failure') is None and record.get('source_bindings_unchanged') is True,
            'Missing successful local native practice proof')
    require(all(record.get(k) is False for k in ('full_mission_passed','full_mission_physical_verified',
            'ranking_eligible','student_release_approved','physical_handoff_verified')),
            'Practice cannot claim a complete mission, ranking or physical handoff')
    arm=record['arm_id'];contents=record['contents'];contract=plan_record['task_contract']
    require(contents in ('beads','empty') and arm in contract['required_arm_ids']
            and record.get('saved_bundle_sha256')==plan_record['bundle_sha256'], 'Practice plan/material/arm identity differs')
    indices=[i for i,s in enumerate(plan_record['stages']) if s['active_arm_id']==arm]
    require(len(indices)==2 and indices[1]==indices[0]+1, 'Practice requires one saved adjacent leg')
    selected=[plan_record['stages'][i] for i in indices]
    approach,carry=selected
    require([s['kind'] for s in selected]==['approach','carry'], 'Practice stage order changed')
    commands=copy.deepcopy(approach['expected_peer_commands_by_arm'])
    commands[arm]=copy.deepcopy(plan_record['initial_commands_by_arm'][arm])
    commands[arm]['q_rad']=copy.deepcopy(approach['trajectory']['waypoints'][0])
    boundary=dict(arm_id=arm,stage_indices=indices,stages=selected,commands=commands,
        source_support_id=approach['support_id'],destination_support_id=carry['destination_support_id'],
        body_world=approach['nominal_body_world'])
    require(record.get('selected_boundary')==boundary and record.get('completed_stage_ids')==[s['stage_id'] for s in selected],
            'Practice altered the saved stages, source/destination or peer commands')
    initial=record['initialization'];fixed=initial['fixed_scene'];ids=fixed['bead_identities']
    require(initial.get('physical_steps')==0 and initial.get('native_clock_unchanged') is True
            and initial.get('previous_physical_continuity_claimed') is False
            and initial.get('all_three_dynamic_robots_preserved') is True
            and initial['before']['native_clock']==initial['after']['native_clock']==[0,0.,0,0]
            and set(fixed['bases'])=={'arm_0','arm_1','arm_2'}
            and fixed['counts']['dofs']==(33+6*BEAD_COUNT if contents=='beads' else 33)
            and fixed['counts']['q_coordinates']==(34+7*BEAD_COUNT if contents=='beads' else 34)
            and fixed['dt_s']==.0025 and fixed['substeps']==16
            and len(ids)==(BEAD_COUNT if contents=='beads' else 0), 'Practice changed the fixed native scene or initial scope')
    for name,command in commands.items():
        actual=initial['after']['arms'][name]
        require(np.allclose(actual['qpos'],command['q_rad']+command['finger_q_m'],atol=2e-7,rtol=0.)
                and actual['qd']==[0.]*9, 'Practice initial robot state differs from saved commands')
    require(np.allclose(record['practice_initial_observation']['body_world']['position_m'],
                        boundary['body_world']['position_m'],atol=2e-7,rtol=0.), 'Practice cup did not begin on the selected source')
    phases=record['phases'];schedule=copy.deepcopy(phases)
    require(schedule and schedule[0]['purpose']=='practice_initial_settle','Missing practice initial settle')
    schedule[0]['purpose']='initial_settle'
    end=validate_phase_schedule(schedule,dict(required_arm_ids=[arm]),dict(stages=selected))
    require(end==record['final_native_step']==record['actual_final_observation']['native_scene_step'], 'Practice final clock differs')
    shim=dict(record,task_contract=contract,original_sample_ids=ids)
    lifecycle=(_validate_ideal_bead_record(shim,plan_record,complete=True,scope='arm_practice',required_arm_ids=[arm],
        support_route=[boundary['source_support_id'],boundary['destination_support_id']]) if contents=='beads' else None)
    if contents=='empty':require(record.get('ideal_bead_lifecycle') is None, 'Empty practice cannot claim bead physics')
    identity=phases[0]['native_scene_identity'];raw_by_stage={s['stage_id']:[] for s in selected}
    directory=Path(record['native_output_directory']).resolve()
    distance=CarriedDistance({'interior_center_local_m':record['distance_metric']['reference_point_body_m']})
    for phase in phases:
        count=phase['actual_intervals'];payload=phase['payload']
        require(phase['native_scene_identity']==identity and all(phase[k].get('passed') is True
            for k in ('control','payload','contacts','support','handling_checkpoint')), 'Practice phase or native continuity failed')
        require(phase['control']['arm_order']==['arm_0','arm_1','arm_2'] and phase['control']['controlled_dof_count']==27
            and phase['contacts']['PRE_samples']==phase['contacts']['POST_samples']==count*16
            and payload['actual_POST_samples']==payload['compact_host_polls']==count
            and payload['actual_internal_POST_samples']==count*16 and payload['original_bead_ids']==ids
            and payload['immediate_failure_halt_preserved'] is True, 'Practice actual all-arm/internal coverage differs')
        if contents=='beads':_validate_phase_bead_mode(phase,ids,identity)
        for key in ('control','payload','contacts','support','handling_checkpoint'):
            require(phase[key]['source_hashes'] and all(sha(p)==h for p,h in phase[key]['source_hashes'].items()),
                    'Practice phase source changed')
        for key in ('raw_history','handling_file'):
            path=Path(phase[key]['file']).resolve()
            require(path.is_relative_to(directory) and sha(path)==phase[key]['sha256'], 'Practice artifact binding changed')
        with np.load(phase['raw_history']['file'],allow_pickle=False) as raw:
            require(raw['control'].shape==(count,27,13) and raw['payload'].shape==(count,14+8*BEAD_COUNT if contents=='beads' else 14)
                and raw['support'].shape==(count*16,6)
                and raw['command_position'].shape==raw['command_velocity'].shape==(count,27)
                and all(np.isfinite(raw[k]).all() for k in raw.files), 'Practice raw native history ABI differs')
            if phase['phase'] in ('approach','carry'):
                raw_by_stage[phase['stage_id']].append((raw['command_position'].copy(),raw['command_velocity'].copy()))
            if phase['actual_holder_arm_id'] is not None:
                distance.append(phase['actual_holder_arm_id'],phase['start_native_step'],phase['actual_initial_body_world'],raw['payload'])
            if lifecycle and phase['phase']=='carry' and phase['end_native_step']==lifecycle['carry_seals'][0]['end_native_step']:
                readbacks=lifecycle['carry_seals'][0]['endpoint_readbacks']
                require(readbacks['raw_history']==phase['raw_history']
                    and readbacks['native_scene_step']==phase['end_native_step']
                    and readbacks['raw_post_ordinal']==count-1
                    and readbacks['raw_post_payload']==raw['payload'][-1].tolist(),
                    'Practice sealed raw carry endpoint differs from its authenticated final POST')
                from physics.deferred_gpu_beads import bind_native_masses
                import hashlib
                native_dtype=np.dtype(payload['native_mass_dtype'])
                native_mass_bytes=np.asarray(payload['expected_native_masses_kg'],dtype=native_dtype)
                native_masses=bind_native_masses([m['mass_kg'] for m in ids],native_mass_bytes,native_dtype)
                require(native_masses.tolist()==payload['expected_native_masses_kg']
                    and hashlib.sha256(native_mass_bytes.tobytes()).hexdigest()==payload['initial_native_mass_sha256'],
                    'Practice final carry native mass representation or initial binding changed')
                for i,m in enumerate(lifecycle['carry_seals'][0]['endpoint']['materials']):
                    v=raw['payload'][-1,14+8*i:22+8*i]
                    require(v[:3].tolist()==m['position_world_m']
                        and v[6]==1 and v[7]==native_masses[i], 'Practice final carry original-bead proof differs from raw POST')
        checkpoint=phase['handling_checkpoint']
        require(checkpoint['PRE_samples']==checkpoint['POST_samples']==phase['end_native_step']*16
                and checkpoint['prefix_guard']==record['final_handling']['prefix_guard'], 'Practice handling prefix changed')
    for stage in selected:
        tr=stage['trajectory'];expected=trajectory_commands(tr,commands,arm,JointLimits(**tr['limits']))
        chunks=raw_by_stage[stage['stage_id']]
        require(chunks and np.array_equal(np.concatenate([v[0] for v in chunks]),expected['position'])
                and np.array_equal(np.concatenate([v[1] for v in chunks]),expected['velocity']),
                'Practice changed a saved trajectory, timing or peer command')
        commands[arm]['q_rad']=list(tr['waypoints'][-1])
    require(distance.record()==record['distance_metric'], 'Practice distance differs from actual held cup history')
    for event in record['boundary_events']:
        require(event['before']['free_bodies']==event['after']['free_bodies']
                and event['before']['native_scene_identity']==event['after']['native_scene_identity']==identity,
                'Practice attach/release changed physical state')
    require([e['event'] for e in record['boundary_events']]==['acquire','release']
            and record['final_handling']['passed'] is True
            and record['final_handling']['supported_pickup_count']==record['final_handling']['release_count']==1,
            'Practice requires exactly one actual acquisition and supported release')
    gate=record['stage_gates'][-1]['gate']
    require(gate['support_id']==boundary['destination_support_id'] and
            (_prepared_release_gate_passes(gate) if contents=='beads' else gate['passed'] and gate['strict_material'] is True),
            'Practice final actual destination release/rest gate failed')
    return True
