"""Independent native practice of one saved approach and supported carry.

The frozen tier engine supplies control, collision, payload and handling checks.
Only the selected leg is run. Its source is a declared clock-zero practice
initialization, never evidence that a preceding arm physically delivered it.
"""
from __future__ import annotations

import copy
import math
from pathlib import Path
import time
import traceback

import numpy as np

from transport_challenge.contracts import digest
from physics.device_tier_mission import (
    DT, INITIAL_TICKS, SUBSTEPS, _Mission, _host, artifact,
    free_body_state, require, save, sha, trajectory_commands,
)

SCHEMA = 'arm-practice-native-result-v1'
BOUNDARY_SCOPE = (
    'Independent nominal-boundary practice initialized before the first native '
    'step. Cup and payload start on the selected saved source support. All '
    'three robots retain their chosen bases and saved peer commands. No '
    'preceding physical carry, handoff continuity, restored checkpoint, complete '
    'mission or ranking result is established.'
)


def selected_boundary(plan, arm_id):
    """Return the exact saved leg and all three commands without changing a plan."""
    require(arm_id in plan.required_arm_ids, 'Practice arm does not participate in this tier')
    indices = [i for i, signature in enumerate(plan.stage_signatures)
               if signature[2] == arm_id]
    require(len(indices) == 2 and indices[1] == indices[0] + 1,
            'Practice requires one adjacent saved approach/carry pair')
    approach, carry = [plan.stage(i) for i in indices]
    require(approach['kind'] == 'approach' and carry['kind'] == 'carry'
            and approach['support_id'] == carry['support_id'],
            'Saved practice stages do not share the declared source')
    initial = plan.record()['initial_commands_by_arm']
    commands = copy.deepcopy(approach['expected_peer_commands_by_arm'])
    commands[arm_id] = copy.deepcopy(initial[arm_id])
    commands[arm_id]['q_rad'] = list(approach['trajectory']['waypoints'][0])
    require(set(commands) == {'arm_0', 'arm_1', 'arm_2'}
            and carry['expected_peer_commands_by_arm'] == approach['expected_peer_commands_by_arm']
            and carry['trajectory']['waypoints'][0] == approach['trajectory']['waypoints'][-1],
            'Saved practice peer commands or consecutive endpoints differ')
    return dict(arm_id=arm_id, stage_indices=indices, stages=[approach, carry],
                commands=commands, source_support_id=approach['support_id'],
                destination_support_id=carry['destination_support_id'],
                body_world=copy.deepcopy(approach['nominal_body_world']))


def _clock(scene):
    return [int(scene.t), float(scene.cur_t), int(scene.sim.cur_substep_global),
            int(scene.sim.cur_substep_local)]


def _body_and_arms(context):
    return dict(free_bodies=free_body_state(context),
                arms={name: dict(qpos=_host(arm.get_qpos()).reshape(-1).tolist(),
                                qd=_host(arm.get_dofs_velocity()).reshape(-1).tolist())
                      for name, arm in sorted(context['arms'].items())},
                holder_arm_id=context['automatic_grasp'].holder,
                native_clock=_clock(context['scene']))


def _fixed_scene(context):
    scene = context['scene']
    bases = {}
    for name, arm in sorted(context['arms'].items()):
        root = arm.get_link('link0')
        bases[name] = dict(position_m=_host(root.get_pos(relative=False)).reshape(3).tolist(),
                           quaternion_wxyz=_host(root.get_quat(relative=False)).reshape(4).tolist())
    return dict(scene_identity=[id(scene), id(scene.sim), id(scene.rigid_solver),
                                id(scene.rigid_solver.dyn_state)],
                counts=dict(dofs=int(scene.rigid_solver.n_dofs),
                            q_coordinates=int(scene.rigid_solver.n_qs),
                            geometries=int(scene.rigid_solver.n_geoms),
                            links=int(scene.rigid_solver.n_links)),
                dt_s=float(scene.sim.dt), substeps=int(scene.sim.substeps),
                bases=bases, world_sha256=digest(context['world']),
                scenario_sha256=digest(context['scenario']),
                vessel_geometry_sha256=context['model'].geometry_sha256,
                bead_identities=copy.deepcopy(context['ids']),
                contact_coverage_sha256=digest(context['coverage']))


def _initial_fingerprint(context, original):
    """Capture fresh native arrays without inventing a dispatcher owner."""
    from physics.initial_state_fingerprint import RIGID_FIELDS, _get, array_record, native_array
    scene = context['scene']
    require(_clock(scene) == [0, 0., 0, 0], 'Practice fingerprint must precede physics')
    fields = {}
    for name in RIGID_FIELDS:
        key = 'rigid.' + name
        fields[key] = array_record(native_array(_get(scene.rigid_solver, name)))
        require(fields[key]['shape'] == original['fields'][key]['shape']
                and fields[key]['dtype'] == original['fields'][key]['dtype'],
                'Practice native field layout changed: ' + key)
        require(fields[key]['finite'] or name in ('dyn_info.dofs.limit', 'dyn_info.dofs.force_range'),
                'Practice native field is nonfinite: ' + key)
    grasp = context['automatic_grasp'].snapshot('arm_0')
    for name in ('eq_type', 'eq_obj1id', 'eq_obj2id', 'eq_data', 'sol_params'):
        raw = native_array(_get(scene.rigid_solver, 'dyn_info.equalities.' + name))
        fields['active_equalities.' + name] = array_record(raw[:grasp['inventory']['count']])
    require(_clock(scene) == [0, 0., 0, 0], 'Practice fingerprint advanced the clock')
    result = dict(schema_version='arm-practice-initial-observables-v1',
                  status='PRACTICE_INITIAL_OBSERVABLES_CAPTURED', native_clock=_clock(scene),
                  fields=fields, actual_state=_body_and_arms(context),
                  actual_observation_sha256=digest(context['initial_observation']),
                  handling_guard=context['handling_enforcer'].guard.status(),
                  grasp_snapshot=grasp, immutable_scene=_fixed_scene(context),
                  source_scene_fingerprint_sha256=original['record_sha256'],
                  fresh_scene_equivalence_claimed=False, restored_checkpoint_claimed=False,
                  complete_dispatcher_history_claimed=False,
                  scope='Fresh native arrays after explicit t0 practice initialization. '
                        'The original dispatcher remains unused. No solver-reset equivalence claim.')
    result['record_sha256'] = digest(result)
    return result


def _initialize(mission, boundary):
    """Place native bodies once at t0 and seed the same checks from that state."""
    from physics.carried_body import WorldPose, measure_hand_to_body, compose_body_world
    from physics.episode_handling_enforcement import SampledHandlingEnforcer
    c, driver, scene = mission.c, mission.driver, mission.scene
    require(_clock(scene) == [0, 0., 0, 0] and mission.helper.holder is None
            and driver.held is None and not mission.phases,
            'Practice initialization requires an untouched, unheld native scene')
    before = _body_and_arms(c)
    fixed = _fixed_scene(c)
    inventory = mission.helper.snapshot('arm_0')['inventory']
    source = WorldPose(c['initial_observation']['body_world']['position_m'],
                       c['initial_observation']['body_world']['quaternion_wxyz'])
    target = WorldPose(boundary['body_world']['position_m'], boundary['body_world']['quaternion_wxyz'])
    require(boundary['body_world'] == driver.scenario['supported_body_poses'][boundary['source_support_id']],
            'Practice source differs from the selected design support')
    native_dtype = _host(c['tray'].get_qpos()).dtype
    body_commands = [('cup', c['tray'], target)]
    local_beads = []
    for identity, bead in zip(c['ids'], c['beads']):
        q = _host(bead.get_qpos()).reshape(7)
        relative = measure_hand_to_body(source, WorldPose(q[:3].tolist(), q[3:].tolist()))
        local_beads.append(dict(id=identity['id'], body_local_pose=relative.to_record()))
        body_commands.append((identity['id'], bead, compose_body_world(target, relative)))
    requested = {}
    for name, arm in c['arms'].items():
        command = boundary['commands'][name]
        q = np.asarray(command['q_rad'] + command['finger_q_m'], dtype=native_dtype)
        arm.set_qpos(q, zero_velocity=True)
        arm.set_dofs_velocity(np.zeros(9, dtype=native_dtype))
        actual = _host(arm.get_qpos()).reshape(9)
        require(np.array_equal(actual, q), 'Native practice robot command did not read back exactly')
        tau = _host(c['gravity'][name].torques()).reshape(9)
        arm.set_dofs_act_bias(tau, -c['kp'], -c['kv'])
        arm.control_dofs_position_velocity(q, np.zeros(9, dtype=native_dtype))
        motor = _host(arm.get_dofs_control_force()).reshape(9)
        require(np.all(np.abs(tau) <= c['effort'])
                and np.all(np.abs(motor) <= c['effort'] + 1e-5)
                and float(np.max(np.abs(motor - np.clip(tau, -c['effort'], c['effort'])))) <= .005,
                'Practice INITIAL gravity-PD identity failed')
        requested[name] = q.tolist()
    for name, entity, pose in body_commands:
        q = np.asarray([*pose.position_m, *pose.quaternion_wxyz], dtype=native_dtype)
        entity.set_qpos(q, zero_velocity=True)
        entity.set_dofs_velocity(np.zeros(6, dtype=native_dtype))
        require(np.allclose(_host(entity.get_qpos()).reshape(7), q, atol=2e-7, rtol=0.),
                'Native practice body pose did not read back')
        requested[name] = q.tolist()
    require(_fixed_scene(c) == fixed and _clock(scene) == [0, 0., 0, 0]
            and mission.helper.snapshot('arm_0')['inventory'] == inventory,
            'Practice initialization changed geometry, physics, bases, inventory or time')
    after = _body_and_arms(c)
    require(all(v == 0. for arm in after['arms'].values() for v in arm['qd'])
            and all(v == 0. for body in after['free_bodies']['bodies'] for v in body['qd']),
            'Practice INITIAL contains nonzero native velocities')
    observation = copy.deepcopy(c['observe']())
    require(all(row['inside_carrier'] for row in observation['materials']),
            'Practice payload does not begin inside the cup')
    previous_guard = c['handling_enforcer']
    require(type(previous_guard) is SampledHandlingEnforcer
            and previous_guard.guard.status()['observations_accepted'] == 0,
            'Practice cannot discard an observed handling history')
    c['handling_enforcer'] = SampledHandlingEnforcer(
        body_identity=previous_guard.identity, model_geometry=c['model'].geometry_record(),
        holder_ids=previous_guard.holders, support_ids=previous_guard.supports,
        initial_observation=observation, dt_s=DT, reach_envelopes=previous_guard.reach_envelopes,
        automatic_grasp=mission.helper, rigid_profile=previous_guard.rigid_profile)
    c['initial_observation'] = copy.deepcopy(observation)
    c['observation'] = copy.deepcopy(observation)
    mission.initial = copy.deepcopy(observation)
    mission.observation = copy.deepcopy(observation)
    mission.commands = copy.deepcopy(boundary['commands'])
    mission.q = np.asarray([v for n in mission.names
                            for v in mission.commands[n]['q_rad'] + mission.commands[n]['finger_q_m']], np.float64)
    driver.initial_supply = copy.deepcopy(observation['materials'])
    driver.support_id = boundary['source_support_id']
    receipt = dict(schema_version='arm-practice-clock-zero-initialization-v1',
                   source_support_id=boundary['source_support_id'], before=before, after=after,
                   requested_native_qpos=requested, bead_local_poses=local_beads,
                   all_three_dynamic_robots_preserved=True, fixed_scene=fixed,
                   physical_steps=0, native_clock_unchanged=True,
                   previous_physical_continuity_claimed=False, scope=BOUNDARY_SCOPE)
    return receipt


def _run_leg(mission, boundary):
    from physics.provided_manipulation import grasp_endpoint_candidates, placement_endpoint, assess_place_ready
    from physics.automatic_manipulation import pick
    from physics.deferred_gpu_tail import assess_support_history, released_numeric_gate
    m = mission
    actor = boundary['arm_id']
    source, destination = boundary['source_support_id'], boundary['destination_support_id']
    m.prepare_beads(required_arm_ids=[actor],support_route=[source,destination],scope='arm_practice')
    m.hold('practice_initial_settle', 'settle', actor, source, .5)
    require(int(m.scene.t) == INITIAL_TICKS, 'Practice initial stabilization must use exactly 200 ticks')
    for index, stage in zip(boundary['stage_indices'], boundary['stages']):
        m.check()
        kind = stage['kind']
        op = dict(stage_id=stage['stage_id'], kind=kind, active_arm_id=actor,
                  support_id=source, destination=destination)
        if kind == 'approach':
            op['supplied_pick_endpoint'] = copy.deepcopy(stage['nominal_goal_hand_world'])
        m.driver.current = op
        if kind == 'approach':
            require(m.helper.holder is None and m.driver.held is None, 'Practice approach begins held')
            endpoint = copy.deepcopy(stage['nominal_goal_hand_world'])
            m.driver.endpoint_hand_pose = endpoint
        else:
            require(m.helper.holder == actor and m.driver.held is not None, 'Practice carry lacks native acquisition')
            m.driver.goal = placement_endpoint(m.driver.world, m.driver.scenario, m.driver.held, destination)
            endpoint = m.driver.goal['hand_world']
        goal = dict(kind='world_hand_pose', position_m=endpoint['position_m'],
                    quaternion_wxyz=endpoint['quaternion_wxyz'])
        trajectory = m.validate_stage(index, stage, goal)
        table = trajectory_commands(trajectory, m.commands, actor, m.c['limits_by_arm'][actor])
        begin = int(m.scene.t)
        if kind == 'carry':
            m.begin_bead_carry(actor,stage['stage_id'])
        carry_phases, raw_support = m.phase(stage['stage_id'], kind, actor,
                                 source if kind == 'approach' else destination, table)
        m.commands[actor]['q_rad'] = list(trajectory['waypoints'][-1])
        if kind == 'approach':
            m.hold(stage['stage_id'] + '__endpoint_settle', 'settle', actor, source, .5)
            m.reset_beads('PRE_PICK',actor,source,stage_id=stage['stage_id'])
            before = m.snapshot()
            gate = pick(m.driver, m.state(), op)
            m.observation = copy.deepcopy(m.c['observe']())
            after = m.snapshot()
            require(before['free_bodies'] == after['free_bodies'], 'Practice acquisition changed free-body state')
            m.events.append(dict(event='acquire', arm_id=actor, stage_id=stage['stage_id'],
                                 support_id=source, before=before, after=after,
                                 receipt=copy.deepcopy(m.driver.automatic_attachment_receipt), gate=gate))
            m.gates.append(dict(stage_id=stage['stage_id'], kind=kind, active_arm_id=actor, gate=gate))
        else:
            support = assess_support_history(raw_support, start_ordinal=begin * SUBSTEPS,
                                             phase='carry', holder_arm_id=actor)
            load = support['maximum_consecutive_unsupported_held_s']
            require(load >= .5, 'Practice lacks the original unsupported-load window')
            m.finish_bead_carry(actor,stage['stage_id'],carry_phases,destination)
            m.hold(stage['stage_id'] + '__supported_hold', 'place', actor, destination, .5)
            ready = assess_place_ready(m.observation, m.rows, m.c['settings'], m.c['rest_policy'], m.driver.goal)
            require(ready['passed'], 'Practice placement lacks actual support/body rest')
            before = m.snapshot()
            snapshot = before['grasp']

            def supported(actual):
                m.check()
                require(actual == snapshot, 'Stale practice supported-release snapshot')
                return dict(passed=True, native_clock=actual['native_clock'],
                            body_entity_idx=actual['body_entity_idx'], support_id=destination,
                            evidence_sha256=digest(ready))

            release = m.helper.release(actor, support_ready=supported)
            m.driver.automatic_attachment_receipt = None
            m.driver.held = None
            m.observation = copy.deepcopy(m.c['observe']())
            after = m.snapshot()
            require(before['free_bodies'] == after['free_bodies'], 'Practice release changed free-body state')
            event = dict(event='release', arm_id=actor, stage_id=stage['stage_id'], support_id=destination,
                         before=before, after=after, receipt=release, ready=ready)
            m.events.append(event)
            m.hold(stage['stage_id'] + '__release', 'release', actor, destination, 1., permission=actor)
            m.hold(stage['stage_id'] + '__released_settle', 'released_settle', actor, destination, 1.)
            targets = {n: m.q[9*j+7:9*j+9].copy() for j, n in enumerate(m.names)}
            extra = 0.
            gate = released_numeric_gate(m.rows, settings=m.c['settings'], rest_policy=m.c['rest_policy'],
                goal=m.driver.goal, arm_order=m.names, finger_targets=targets, load_bearing_s=load,
                model=m.c['model'], identities=m.c['ids'], strict_material=m.contents=='empty', contents=m.contents,
                ideal_bead_prepared=m.prepared_bead_receipt())
            require(gate['passed'], 'Practice destination release/rest gate failed')
            m.gates.append(dict(stage_id=stage['stage_id'], kind=kind, active_arm_id=actor, gate=gate,
                                carried_support=support, additional_material_wait_s=extra))
        m.completed.append(stage['stage_id'])
        m.driver.current = None
        save(m.output / (stage['stage_id'] + '__practice_gate.json'), m.gates[-1])
    m.check()
    final_handling = m.handling.finish()
    require(final_handling['passed'] and final_handling['supported_pickup_count'] == 1
            and final_handling['release_count'] == 1 and m.helper.holder is None,
            'Practice requires exactly one native supported acquisition and release')
    return final_handling


def execute(context, *, output, deadline, arm_id):
    """Run the selected leg and save independent success or failure evidence.

    ``deadline`` is an absolute perf_counter deadline. ``output`` must be new.
    The caller owns scene destruction, source closure and resource admission.
    """
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    started = time.perf_counter()
    result = dict(schema_version=SCHEMA, scope='arm_practice', status='ARM_PRACTICE_NATIVE_FAILED',
                  practice_passed=False, full_mission_passed=False, full_mission_physical_verified=False,
                  ranking_eligible=False, student_release_approved=False, physical_handoff_verified=False,
                  arm_id=arm_id, contents=context.get('contents'), boundary_scope=BOUNDARY_SCOPE,
                  failure=None, cleanup_owned_by_caller=True,
                  source_hashes={str(Path(__file__).resolve()): sha(__file__)})
    mission = None
    try:
        require(type(deadline) in (int, float) and math.isfinite(deadline) and deadline > time.perf_counter(),
                'Finite future practice wall deadline required')
        plan = context['student_driver'].saved_plan
        boundary = selected_boundary(plan, arm_id)
        result.update(saved_bundle_sha256=plan.sha256, saved_plan_file_sha256=plan.file_sha256,
                      selected_boundary=boundary, required_arm_ids=list(plan.required_arm_ids),
                      source_scene_initial_fingerprint=copy.deepcopy(context['initial_fingerprint']))
        # Construct the frozen phase owner against its genuine original INITIAL.
        # Its full-tier run/finalizer is never called and its tier record is never published.
        mission = _Mission(context, output / 'native_phases', deadline)
        result['initialization'] = _initialize(mission, boundary)
        result['practice_initial_observation'] = copy.deepcopy(mission.observation)
        fingerprint = _initial_fingerprint(context, result['source_scene_initial_fingerprint'])
        save(output / 'practice_initial_fingerprint.json', fingerprint)
        result['practice_initial_fingerprint'] = artifact(output / 'practice_initial_fingerprint.json')
        save(output / 'practice_initialization.json', result['initialization'])
        result['final_handling'] = _run_leg(mission, boundary)
        require(context['source_check']() is True, 'Practice source closure failed')
        result.update(status='ARM_PRACTICE_NATIVE_PASS', practice_passed=True,
                      source_bindings_unchanged=True, actual_final_observation=context['observe'](),
                      actual_final_state=_body_and_arms(context), actual_destination_goal=copy.deepcopy(mission.driver.goal),
                      original_rest_thresholds_unchanged=True, no_online_replanning=True)
    except BaseException as exc:
        if mission is not None and mission.bead_lifecycle is not None and mission.bead_lifecycle.state != 'FAILED':
            mission.bead_lifecycle.note_failure(type(exc).__name__+': '+str(exc))
        result.update(failure=dict(type=type(exc).__name__, message=str(exc)), traceback=traceback.format_exc())
        try:
            result['actual_failed_state'] = _body_and_arms(context)
            result['actual_failed_observation'] = context['observe']()
        except Exception as read_error:
            result['failure_readback_error'] = dict(type=type(read_error).__name__, message=str(read_error))
    finally:
        if mission is not None:
            mission.record_bead_policy()
            for key in ('ideal_bead_policy','ideal_bead_lifecycle','prepared_bead_body_local_positions_m'):
                if key in mission.record:
                    result[key] = mission.record[key]
            result.update(phases=mission.phases, boundary_events=mission.events, stage_gates=mission.gates,
                          completed_stage_ids=mission.completed, native_output_directory=str(mission.output),
                          distance_metric=mission.distance.record(), final_native_step=int(mission.scene.t),
                          simulation_s=float(mission.scene.cur_t))
            for key in ('failed_phase_prefix', 'last_actual_history', 'distance_measurement_failure'):
                if key in mission.record:
                    result[key] = mission.record[key]
        result['native_wall_s'] = time.perf_counter() - started
        save(output / 'ARM_PRACTICE_NATIVE_RESULT.json', result)
    return result
