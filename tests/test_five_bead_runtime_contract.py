"""Five/empty admission regressions. Pure fixtures do not qualify native physics."""
import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace as NS
import unittest

import numpy as np
from transport_challenge.contracts import ContractError,digest
from transport_challenge.material_access import BEAD_COUNT,material_status
from transport_challenge.joint_planning import JointLimits
from transport_challenge.vessel import sphere_fits_interior
from physics.carried_body import VesselModel
from physics.episode_runtime import validate_inputs
from physics.saved_motion_planning import make_saved_empty_overlay,verify_saved_input_overlay
from physics.ideal_bead_policy import BeadLifecycle,validate_lifecycle,policy_contract
from physics.ideal_bead_handling import IdealBeadHandling
from physics.contact_accuracy import (TARGET_ENTITIES,contact_accuracy_request,validate_request,
    rigid16_contact_request,prepare_rigid16_geometry,validate_rigid16_evidence,
    _rigid16_expected_native,RIGID16_SOURCE_PINS)
from tests.test_tier_route_contract import selected
from tests import test_failed_native_proof

ROOT=Path(__file__).resolve().parents[1]
CASES={'easy':'easy_open_v1','medium':'medium_staggered_v1','hard':'hard_industrial_v1'}
POSITIONS=[[-.03,0.,-.082],[0.,0.,-.082],[.03,0.,-.082],[0.,.03,-.082],[0.,-.03,-.082]]
LIMITS=JointLimits([[-4.,4.]]*7,[10.]*7,[100.]*7)

def bound(tier='hard'):
    _,b=selected(tier)
    reference=json.loads((ROOT/'cases/difficulty_20261106'/CASES[tier]/'physics_settings_reference.json').read_bytes())
    return b['world'],b['scenario'],reference

def contact_fixture(count,*,empty=False):
    entities=[];next_geom=0
    def entity(name,number,kind,link):
        nonlocal next_geom
        obj=NS(name=name,idx=len(entities),is_built=False,geoms=[])
        for _ in range(number):
            g=NS(idx=next_geom,type=kind,link=NS(name=link,idx=obj.idx),
                 sol_params=np.asarray([.01,1.,.9,.95,.001,.5,2.],np.float64))
            g.set_sol_params=lambda values,g=g:setattr(g,'sol_params',values.copy())
            obj.geoms.append(g);next_geom+=1
        entities.append(obj);return obj
    floor=entity('floor',1,0,'floor');tray=entity('tray_0',9,5,'payload')
    beads=[entity('tray_0_ball_'+str(i),1,1,'sphere') for i in range(count)]
    entity('unrelated_fixed_object',1,5,'fixed')
    request=rigid16_contact_request()
    before=prepare_rigid16_geometry(entities,request,floor=floor,tray=tray,beads=beads,
        box_type=5,sphere_type=1,plane_type=0,payload_mode='empty' if empty else None)
    rows=[dict(row,native_sol_params=_rigid16_expected_native(row['after_prebuild'],'float32',.0003125).tolist(),dtype='float32')
          for row in before['geometries']]
    evidence=dict(payload_mode='empty' if empty else None,schema_version='rigid16-bead-contact-native-v1',
        requested=request,dt_s=.0025,substeps=16,substeps_local=1,native_geometry_count=len(rows),
        resolved_contact_pruning_tolerance=None,native_pruning_tolerance=0.,native_default_timeconst_s=.01,
        selection=copy.deepcopy(before['selection']),geometries=rows,source_sha256=RIGID16_SOURCE_PINS)
    return evidence,request

class FiveBeadRuntimeTests(unittest.TestCase):
    def test_all_tier_data_and_exact_five_recipe(self):
        self.assertEqual(BEAD_COUNT,5);self.assertIn('Five',material_status()['beads']['description'])
        self.assertEqual(policy_contract()['sample_count'],5)
        for tier in CASES:
            with self.subTest(tier=tier):
                w,s,ref=bound(tier);model,_,settings=validate_inputs(w,s,None,ref,student_limits=LIMITS)
                self.assertEqual([v['id'] for v in w['initial_contents']],['tray_0:'+str(i) for i in range(5)])
                self.assertEqual([v['center_body_m'] for v in w['initial_contents']],POSITIONS)
                self.assertTrue(all(sphere_fits_interior(model.geometry_record(),v['center_body_m'],v['radius_m']) for v in w['initial_contents']))
                self.assertEqual(len(s['contents']),5)
                self.assertEqual(settings['configured_grasp_physics']['simulation']['substeps'],16)
                self.assertEqual(settings['configured_grasp_physics']['simulation']['dt_s'],.0025)
                self.assertFalse(ref['native_scene_verified'])
                manifest=json.loads((ROOT/'cases/difficulty_20261106'/CASES[tier]/'manifest.json').read_bytes())
                self.assertEqual(manifest['sample_count'],5)
                for name,expected in {**manifest['files'],**manifest['supplemental_files']}.items():
                    self.assertEqual(hashlib.sha256((ROOT/name).read_bytes()).hexdigest(),expected)

    def test_old_or_extra_inventory_rejected_after_rebinding_world_hash(self):
        for n in (0,3,4,6):
            w,s,ref=copy.deepcopy(bound())
            if n==6:
                w['initial_contents'].append(copy.deepcopy(w['initial_contents'][-1]))
                s['contents'].append(copy.deepcopy(s['contents'][-1]))
                w['initial_content_world_centers_m'].append(w['initial_content_world_centers_m'][-1][:])
            else:
                w['initial_contents']=w['initial_contents'][:n];s['contents']=s['contents'][:n]
                w['initial_content_world_centers_m']=w['initial_content_world_centers_m'][:n]
            s['provenance']['world_sha256']=digest(w)
            with self.subTest(n=n),self.assertRaises(ContractError):validate_inputs(w,s,None,ref,student_limits=LIMITS)

    def test_empty_overlay_is_exactly_reversible_and_requires_current_five_source(self):
        w,s,ref=bound();empty_world,empty_scenario,_=make_saved_empty_overlay(w,s)
        self.assertEqual(empty_world['initial_contents'],[])
        restored=verify_saved_input_overlay(empty_world,empty_scenario,world_sha256=digest(w),scenario_sha256=digest(s))
        self.assertEqual(restored,(w,s))
        validate_inputs(empty_world,empty_scenario,None,ref,student_limits=LIMITS)
        wrong_w,wrong_s=copy.deepcopy((w,s))
        wrong_w['initial_contents']=wrong_w['initial_contents'][:3];wrong_s['contents']=wrong_s['contents'][:3]
        wrong_w['initial_content_world_centers_m']=wrong_w['initial_content_world_centers_m'][:3]
        with self.assertRaises(ContractError):make_saved_empty_overlay(wrong_w,wrong_s)

    def test_contact_inventory_and_resolution_for_five_and_empty(self):
        self.assertEqual(len(TARGET_ENTITIES),6)
        self.assertEqual(validate_request(contact_accuracy_request())['target_entities'],list(TARGET_ENTITIES))
        for count,empty in ((5,False),(0,True)):
            evidence,request=contact_fixture(count,empty=empty)
            validate_rigid16_evidence(evidence,request)
            self.assertEqual([len(evidence['selection'][key]) for key in ('floor','tray','beads')],[1,9,count])
            for row in evidence['geometries']:
                expected=.005 if row['contact_role']=='tray' else .0003125 if row['contact_role'] in ('floor','bead') else .01
                self.assertEqual(row['after_prebuild'][0],expected)
        for n in (3,4,6):
            with self.subTest(n=n),self.assertRaises(ContractError):contact_fixture(n)

    def test_contact_timing_identity_and_retention_tampering_fail(self):
        original,request=contact_fixture(5)
        for kind in ('dt','substeps','pruning','bead_timeconst','bead_identity','missing_bead'):
            wrong=copy.deepcopy(original)
            if kind=='dt':wrong['dt_s']=.005
            if kind=='substeps':wrong['substeps']=8
            if kind=='pruning':wrong['native_pruning_tolerance']=.001
            if kind=='bead_timeconst':wrong['geometries'][10]['native_sol_params'][0]=.005
            if kind=='bead_identity':wrong['geometries'][10]['entity']='wrong'
            if kind=='missing_bead':wrong['selection']['beads'].pop()
            with self.subTest(kind=kind),self.assertRaises(ContractError):validate_rigid16_evidence(wrong,request)

    def test_five_lifecycle_raw54_and_sticky_failure(self):
        fixture=test_failed_native_proof.FailedNativeProofTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
        record=fixture.value['ideal_bead_lifecycle']
        self.assertEqual(len(record['identities']),5)
        seal=record['carry_seals'][0];self.assertTrue(seal['original_beads_retained_before_reset'])
        self.assertEqual(len(seal['endpoint_readbacks']['raw_post_payload']),54)
        self.assertEqual(len(seal['endpoint_readbacks']['native_snapshot']['bead_qpos']),5)
        self.assertEqual(len(seal['endpoint_readbacks']['native_snapshot']['bead_qvel']),5)
        self.assertTrue(record['failures'])
        for n in (3,4,6):
            ids=copy.deepcopy(fixture.ids[:n]);positions=copy.deepcopy(fixture.positions[:n])
            if n==6:ids.append(copy.deepcopy(ids[-1]));positions.append(positions[-1][:])
            with self.subTest(n=n),self.assertRaises(ContractError):BeadLifecycle(ids,positions,['arm_0'],support_route=['source','goal'])

    def test_native_snapshot_uses_five_q7_and_qvel6_without_solver_step(self):
        gate=object.__new__(IdealBeadHandling);gate.native_identity=(1,2,3,4,5);gate.scene=NS(t=0,cur_t=0.,sim=NS(cur_substep_global=0,cur_substep_local=0))
        q=np.arange(69,dtype=np.float32).reshape(-1,1);v=np.arange(63,dtype=np.float32).reshape(-1,1);pairs=np.zeros((2,2),np.int32)
        gate.solver=NS(rigid_info=NS(qpos=NS(to_numpy=lambda:q)),
            dyn_state=NS(dofs=NS(vel=NS(to_numpy=lambda:v))),
            collider=NS(_collider_info=NS(collision_pair_idx=NS(to_numpy=lambda:pairs))))
        gate._constraint_cache_hash=lambda:'1'*64;gate.qids=list(range(34,69));gate.dids=list(range(33,63))
        gate.qkeep=list(range(34));gate.dkeep=list(range(33));gate.nonbead_pair_mask=np.ones((2,2),bool)
        gate.ids=[dict(id='sample'+str(i)) for i in range(5)];gate.mode='OFF'
        state=gate._snapshot()
        self.assertEqual(np.asarray(state['bead_qpos']).shape,(5,7))
        self.assertEqual(np.asarray(state['bead_qvel']).shape,(5,6))
        np.testing.assert_array_equal(state['bead_qpos'],q[34:,0].reshape(5,7))
        np.testing.assert_array_equal(state['bead_qvel'],v[33:,0].reshape(5,6))

if __name__=='__main__':unittest.main()

