"""CPU contract tests. Structural fixtures do not assert physical completion."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

from tests.test_relay_binding import inputs
from transport_challenge.contracts import ContractError, digest
from transport_challenge.joint_planning import JointLimits, JointTrajectory
from transport_challenge.three_arm_design import ThreeArmDesign, bind_three_arm_design, design_space
from physics.tier_mission_plan import (make_task_contract, verify_task_design,
    validate_task_contract, TierMissionPlan, task_stages, donor_return_schedule,
    seal_tier_bundle, CPU_STATUS)
from physics.saved_motion_planning import (saved_wait_policy, file_sha,
    make_saved_empty_overlay, verify_saved_input_overlay, EMPTY_OVERLAY_KEY)


def selected(tier, relay_ids=()):
    world, scenario, design = inputs(tier)
    by_id = {r['support_id']: r for r in design_space(world)['relay_regions']}
    for i, name in enumerate(relay_ids):
        region = by_id[name]
        design['relays'][i] = dict(support_id=name, x_m=sum(region['x_m'])/2, y_m=sum(region['y_m'])/2)
    bound = bind_three_arm_design(ThreeArmDesign.from_record(design, world), world, scenario)
    contract = make_task_contract(world, scenario, difficulty=tier, task_manifest_sha256='a'*64,
                                 selected_world=bound['world'])
    return contract, bound


def contract_only_bundle(contract, bound, source_path):
    """A tiny synthetic structural bundle, never used as physics evidence."""
    initial = {r['id']: {'q_rad': r['q_rad'], 'finger_q_m': r['finger_q_m']}
               for r in bound['scenario']['robots']}
    limits = JointLimits([[-4., 4.]]*7, [10.]*7, [100.]*7)
    stages, commands = [], copy.deepcopy(initial)
    for stage_id, kind, arm in task_stages(contract):
        i = int(arm[-1]); q0 = commands[arm]['q_rad']; q1 = list(q0); q1[-1] += .01
        stage = dict(stage_id=stage_id, kind=kind, active_arm_id=arm,
            support_id=contract['support_route'][i],
            destination_support_id=contract['support_route'][i+1] if kind=='carry' else None,
            trajectory=JointTrajectory([q0,q1],[.5],limits).to_record(),
            expected_peer_commands_by_arm={k:copy.deepcopy(v) for k,v in commands.items() if k!=arm})
        if kind=='carry':
            stage['nominal_goal_body_world'] = bound['scenario']['supported_body_poses'][stage['destination_support_id']]
        stages.append(stage); commands[arm]['q_rad'] = q1
        if kind=='carry' and arm in contract['required_arm_ids'][:-1]: commands[arm] = copy.deepcopy(initial[arm])
    summary = dict(status=CPU_STATUS, planning_scope='tier-mission', completed_stages=len(stages), failure=None,
        source_bindings_unchanged=True, genesis_initialized=False, genesis_imported_modules=[],
        blocked_genesis_import_attempts=[], blocked_integration_attempts=[], native_physical_steps=0, cpu_physical_steps=0)
    return seal_tier_bundle(dict(schema_version='saved-tier-mission-v1', scope='tier_mission',
        task_contract=contract, task_contract_sha256=digest(contract), post_place_policy='kinematic_return',
        payload='beads', rigid_runtime_profile='rigid16_bead_tc2h_v1', nominal_only=True,
        physical_grasp_verified=False, physical_transport_verified=False, full_mission_passed=False,
        student_release_approved=False, source_pins={str(source_path.resolve()):file_sha(source_path)},
        provided_wait_policy=saved_wait_policy(), offline_planner=dict(module='tests.contract_fixture',
            source_sha256='test-only',plan_path_source_sha256='test-only'),
        stages=stages, initial_commands_by_arm=initial, post_place_resets=[],
        post_place_returns=donor_return_schedule(stages, initial, contract), cpu_planning_result=summary,
        cpu_process_supervisor={'status':'WORKER_CANDIDATE_PENDING'}, world_sha256=digest(bound['world']),
        scenario_sha256=digest(bound['scenario']), design_sha256=bound['receipt']['design_sha256'],
        task_manifest_sha256='a'*64))


class TierRouteContractTests(unittest.TestCase):
    def test_selected_surfaces_reach_complete_tier_bundle(self):
        for tier, relays, route in (
            ('easy', (), ['source_support','relay_1_support']),
            ('medium', ('quality_1_support',), ['source_support','quality_1_support','relay_2_support']),
            ('hard', ('quality_2_support','quality_1_support'), ['source_support','quality_2_support','quality_1_support','goal_support'])):
            with self.subTest(tier=tier), tempfile.TemporaryDirectory() as temp:
                contract, bound = selected(tier, relays)
                self.assertEqual(contract['support_route'], route)
                self.assertTrue(verify_task_design(contract, bound['world'], bound['scenario']))
                pin = Path(temp)/'source'; pin.write_text('structural test only')
                record = contract_only_bundle(contract, bound, pin)
                path = Path(temp)/'plan.json'; path.write_text(json.dumps(record))
                plan = TierMissionPlan(record, path=path, require_supervisor=False)
                self.assertEqual(plan.support_route, tuple(route))
                self.assertEqual(len(record['stages']), 2*len(contract['required_arm_ids']))
                self.assertEqual(len(record['post_place_returns']), contract['required_handoff_count'])
                self.assertTrue(plan.verify_bound_inputs(bound['world'], bound['scenario']))
                empty_world, empty_scenario, _ = make_saved_empty_overlay(bound['world'], bound['scenario'])
                self.assertTrue(plan.verify_bound_inputs(empty_world, empty_scenario))
                limits = JointLimits(**record['stages'][0]['trajectory']['limits'])
                self.assertTrue(plan.verify_initial(empty_world, empty_scenario, record['initial_commands_by_arm'],
                    {arm:limits for arm in record['initial_commands_by_arm']}))
                self.assertFalse(plan.record()['full_mission_passed'])

    def test_immutable_source_and_terminal_and_selected_route(self):
        contract, bound = selected('hard', ('quality_1_support','quality_2_support'))
        for kind in ('source', 'terminal', 'route'):
            w, s = copy.deepcopy((bound['world'],bound['scenario']))
            if kind=='route': w['relay_supports'].reverse()
            else:
                support = contract[kind+'_support_id']
                w['support_targets_by_id'][support]['body_world']['position_m'][0] += .01
            with self.subTest(kind=kind), self.assertRaises(ContractError): verify_task_design(contract,w,s)
        for value in (['source_support','quality_1_support','quality_1_support','goal_support'],
                      ['source_support',{},'quality_2_support','goal_support']):
            changed=copy.deepcopy(contract); changed['support_route']=value
            with self.assertRaises(ContractError): validate_task_contract(changed)

    def test_terminal_cannot_be_selected_as_intermediate(self):
        with self.assertRaises(ContractError): selected('medium', ('relay_2_support','quality_1_support'))
        with self.assertRaises(ContractError): selected('easy', ('quality_1_support',))

    def test_unset_student_choices_fail_before_fk(self):
        world,_,design=inputs('hard')
        for key in ('mount_region','base_placement'):
            data=copy.deepcopy(design);data['arms'][0][key]=None
            with self.assertRaisesRegex(ContractError,'Edit the student design'):
                ThreeArmDesign.from_record(data,world)
        design['relays'][0]['x_m']=None
        with self.assertRaisesRegex(ContractError,'Edit the student design'): ThreeArmDesign.from_record(design,world)

    def test_empty_overlay_only_removes_contents(self):
        _,bound=selected('medium',('quality_1_support',))
        world,scenario=bound['world'],bound['scenario']
        w,s,receipt=make_saved_empty_overlay(world,scenario)
        kwargs=dict(world_sha256=digest(world),scenario_sha256=digest(scenario))
        self.assertEqual(verify_saved_input_overlay(w,s,**kwargs),(world,scenario))
        self.assertFalse(receipt['scored_result'])
        self.assertEqual(w['rigid_runtime_profile'],world['rigid_runtime_profile'])
        for mutation in ('physics','robot','marker','provenance','contents','profile'):
            a,b=copy.deepcopy((w,s))
            if mutation=='physics':a['physics_settings']['dt_s']=.01
            elif mutation=='robot':b['robots'][0]['q_rad'][0]+=.1
            elif mutation=='marker':a[EMPTY_OVERLAY_KEY]['initial_contents'][0]['id']='different'
            elif mutation=='provenance':b['provenance']['arbitrary_override']=True
            elif mutation=='contents':a['initial_contents']=[world['initial_contents'][0]]
            else:a['rigid_runtime_profile']='empty_default1_v1'
            with self.subTest(mutation=mutation),self.assertRaises(ContractError):verify_saved_input_overlay(a,b,**kwargs)


if __name__=='__main__':unittest.main()
