"""Failure propagation with synthetic current-policy evidence and no Genesis."""
import ast,copy,hashlib,json,struct,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from physics import device_tier_mission as native
from physics.ideal_bead_policy import BeadLifecycle,policy_contract
from physics.automatic_manipulation import make_prepared_bead_receipt
from transport_challenge.contracts import ContractError,digest


class FailedNativeProofTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.output=Path(self.temp.name);self.directory=self.output/'device_tier_execution';self.directory.mkdir()
        self.source=self.output/'fixture_source.py';self.source.write_text('# Synthetic unit-test source only.\n')
        self.pins={str(self.source):hashlib.sha256(self.source.read_bytes()).hexdigest()}
        self.ids=[dict(id='sample_'+str(i),entity_idx=10+i,mass_kg=.01,radius_m=.01) for i in range(3)]
        self.positions=[[-.02,0.,0.],[0.,0.,0.],[.02,0.,0.]]
        world=dict(initial_contents=[dict(v,shape='sphere',frame='body',position_units='m',mass_units='kg',
            center_body_m=p) for v,p in zip(self.ids,self.positions)])
        self.world_path=self.output/'world.json';self.world_path.write_text(json.dumps(world))
        contract=dict(required_arm_ids=['arm_0','arm_1'],support_route=['source','relay','goal'],source_world_sha256=digest(world))
        planned=dict(source_pins={str(self.world_path):hashlib.sha256(self.world_path.read_bytes()).hexdigest()})
        self.plan=SimpleNamespace(sha256='bundle',file_sha256='planfile',task_contract=contract,
                                  verify_sources=lambda:True,record=lambda:copy.deepcopy(planned))
        ledger=BeadLifecycle(self.ids,self.positions,contract['required_arm_ids'],support_route=contract['support_route'])
        initial_pause=self.transition('pause',0);initial=self.transition('reset',0)
        ledger.record_reset('INITIAL',initial,arm_id='arm_0',support_id='source',initial_pause=initial_pause,
                            support_evidence=self.support('source',0,initial=True))
        ledger.record_reset('PRE_PICK',self.transition('reset',1),arm_id='arm_0',support_id='source',
                            stage_id='arm_0__approach',support_evidence=self.support('source',1))
        ledger.begin_carry('arm_0','arm_0__carry',self.transition('resume',1))
        phase=self.carry_phase();endpoint=self.observation(201)
        row=[0.,0.,0.,1.,0.,0.,0.]+[0.]*7
        for p,m in zip(self.positions,phase['payload']['expected_native_masses_kg']):row.extend(p+[0.,0.,0.,1.,m])
        raw_path=self.directory/'synthetic_carry.npz'
        native.np.savez(raw_path,payload=native.np.tile(row,(200,1)))
        phase['raw_history']=dict(file=str(raw_path),sha256=hashlib.sha256(raw_path.read_bytes()).hexdigest())
        reads=dict(schema_version='same-clock-carry-endpoint-readbacks-v1',native_scene_step=201,
            raw_history=copy.deepcopy(phase['raw_history']),raw_post_ordinal=199,raw_post_payload=row,
            native_snapshot=self.state('CARRY',201))
        ledger.finish_carry([phase],endpoint,reads);ledger.pause(self.transition('pause',201))
        reset=self.transition('reset',201)
        ledger.record_reset('PLACE',reset,arm_id='arm_0',support_id='relay',stage_id='arm_0__carry',
                            support_evidence=self.support('relay',201))
        policy=ledger.record();native_events=[]
        for event in policy['events']:
            for key in ('initial_pause','native_transition','native_resume','native_pause'):
                if event['inputs'].get(key) is not None:native_events.append(event['inputs'][key])
        metadata=dict(mode='OFF',bead_identities=self.ids,native_scene_identity=[1,3,4,5,6],source_hashes=self.pins,
            carry_original_solver_path=False,events=native_events,current_guard_flags=[3200,0,0,0,0],off_native_substeps=3200)
        prepared=make_prepared_bead_receipt(observation=self.observation(401),identities=self.ids,native_gate=metadata,
                                          latest_reset=reset,lifecycle=policy)
        gate=dict(schema_version='deferred-prepared-bead-released-rest-gate-v1',passed=True,strict_material=False,
            expected_intervals=200,expected_material_endpoints=0,bead_dynamics_evaluated=False,
            material_rest='NOT_APPLICABLE_DISABLED',all_arms_rest={'passed':True},body_rest={'passed':True},
            actual_unsupported_held_s=.5,support_id='relay',ideal_bead_prepared=prepared)
        ledger.note_failure('Synthetic later-leg failure. No native qualification claimed.')
        self.value=dict(schema_version=native.SCHEMA,passed=False,full_mission_physical_verified=False,
            failure=dict(type='PhysicalTransportRejected',message='actual spill'),persistent_scene=True,
            contents='beads',saved_bundle_sha256='bundle',saved_plan_file_sha256='planfile',
            task_contract=contract,task_contract_sha256=digest(contract),native_output_directory=str(self.directory),
            original_sample_ids=self.ids,ideal_bead_policy=policy_contract(),ideal_bead_lifecycle=ledger.record(),
            prepared_bead_body_local_positions_m=self.positions,phases=[phase],
            completed_stage_ids=['arm_0__approach','arm_0__carry'],
            stage_gates=[dict(stage_id='arm_0__carry',kind='carry',active_arm_id='arm_0',gate=gate,additional_material_wait_s=0.)],
            boundary_events=[dict(stage_id='arm_0__carry',event='release',ready=dict(passed=True))],
            donor_returns=[dict(stage_id='arm_0__carry',receipt=dict(passed=True))])
        self.value['partial_progress']=native.partial_progress(self.value);self.write()

    def state(self,mode,step):
        return dict(scene_identity=[1,3,4,5,6],native_clock=[step,step*.0025,step*16,0],
            non_bead_qpos_sha256='1'*64,non_bead_qvel_sha256='2'*64,non_bead_constraint_cache_sha256='3'*64,
            non_bead_collision_pair_sha256='4'*64,collision_pair_sha256=('5' if mode=='CARRY' else '6')*64,
            bead_identities=copy.deepcopy(self.ids),bead_qpos=[p+[1.,0.,0.,0.] for p in self.positions],
            bead_qvel=[[0.]*6 for _ in self.ids],mode=mode)

    def transition(self,event,step):
        before=self.state('CARRY' if event=='pause' else 'OFF',step)
        after=self.state('CARRY' if event=='resume' else 'OFF',step)
        result=dict(event=event,before=before,after=after,native_clock=after['native_clock'],physical_steps=0,source_hashes=self.pins)
        if event=='reset':result.update(requested_body_local_positions_m=self.positions,requested_world_positions_m=self.positions,zero_velocity=True)
        return result

    def support(self,name,step,initial=False):
        return dict(passed=True,support_id=name,native_scene_step=step,simulation_s=step*.0025,material_rest_assessed=False,
            evidence_kind='fresh_source_geometry' if initial else 'native_endpoint_support',support_contact_evaluated=not initial)

    def observation(self,step):
        return dict(native_scene_step=step,simulation_s=step*.0025,
            materials=[dict(v,position_world_m=p,velocity_world_m_s=[0.]*3,inside_carrier=True) for v,p in zip(self.ids,self.positions)])

    def carry_phase(self):
        payload=dict(schema_version='deferred-native-payload-post-audit-v1',passed=True,original_bead_ids=self.ids,bead_count=3,
            actual_POST_samples=200,compact_host_polls=200,actual_internal_POST_samples=3200,requested_POST_samples=200,
            observed_prefix_only=False,immediate_failure_halt_preserved=True,source_bindings_unchanged=True,
            first_escape=None,escape_witnesses=[],first_failed_step=None,first_failed_global_substep=None,
            flags=dict(nonfinite=False,native_mass_changed=False,bead_outside=False,body_tilt=False,quaternion_invalid=False))
        mass_bytes=struct.pack('<fff',*[v['mass_kg'] for v in self.ids])
        payload.update(native_mass_dtype='<f4',expected_native_masses_kg=list(struct.unpack('<fff',mass_bytes)),
            initial_native_mass_sha256=hashlib.sha256(mass_bytes).hexdigest())
        return dict(phase='carry',stage_id='arm_0__carry',active_arm_id='arm_0',actual_holder_arm_id='arm_0',
            native_scene_identity=[1,2,3,4],actual_intervals=200,start_native_step=1,end_native_step=201,payload=payload,
            control=dict(passed=True,arm_order=['arm_0','arm_1','arm_2'],controlled_dof_count=27),
            contacts=dict(passed=True,PRE_samples=3200,POST_samples=3200,category_counts={'UNALLOWED_CONTACT':0}),
            support=dict(passed=True),handling_checkpoint=dict(passed=True))

    def write(self):self.path=self.directory/'TIER_DEVICE_RESULT.json';self.path.write_text(json.dumps(self.value))
    def read(self,source=True):return native.load_failed_tier_proof(self.directory,self.plan,contents='beads',source_bindings_unchanged=source)
    def test_owned_failed_prefix_is_preserved_without_success(self):
        record,binding=self.read()
        self.assertEqual(record['partial_progress']['valid_carry_count'],1)
        self.assertFalse(binding['physical_transport_accepted']);self.assertFalse(binding['full_mission_passed'])
    def test_foreign_bundle_source_or_unaccepted_carry_is_unknown(self):
        original=copy.deepcopy(self.value)
        with self.assertRaises(ContractError):self.read(False)
        self.value['saved_bundle_sha256']='foreign';self.write()
        with self.assertRaises(ContractError):self.read()
        self.value=copy.deepcopy(original);self.value['donor_returns']=[];self.write()
        with self.assertRaises(ContractError):self.read()
        self.value=copy.deepcopy(original);self.value['ideal_bead_lifecycle']['carry_seals']=[];self.write()
        with self.assertRaises(ContractError):self.read()
        self.value=copy.deepcopy(original);self.value['stage_gates'][0]['gate']['strict_material']=True;self.write()
        with self.assertRaises(ContractError):self.read()
        self.value=copy.deepcopy(original);self.write();self.source.write_text('# Changed source.\n')
        with self.assertRaises(ContractError):self.read()
    def test_missing_sidecar_is_not_zero_progress(self):
        self.path.unlink()
        with self.assertRaises(FileNotFoundError):self.read()
    def test_actual_runtime_exception_branch_attaches_then_reraises(self):
        source=Path(native.__file__).with_name('episode_runtime.py')
        tree=ast.parse(source.read_text(encoding='utf-8'))
        block=next(n for n in ast.walk(tree) if isinstance(n,ast.Try) and n.body
            and isinstance(n.body[0],ast.Assign) and isinstance(n.body[0].value,ast.Call)
            and isinstance(n.body[0].value.func,ast.Name) and n.body[0].value.func.id=='execute_device_tier_mission')
        def fail(*args,**kwargs):raise RuntimeError('physical run failed')
        result={};scope=dict(execute_device_tier_mission=fail,context={},output=self.output,deadline=1,
            student_driver=SimpleNamespace(saved_plan=self.plan),empty_mode=False,source_check=lambda:True,
            result=result,digest=digest,scene=SimpleNamespace(t=200))
        with self.assertRaisesRegex(RuntimeError,'physical run failed'):
            exec(compile(ast.fix_missing_locations(ast.Module(body=[block],type_ignores=[])),str(source),'exec'),scope)
        self.assertEqual(result['device_tier_execution']['partial_progress']['valid_carry_count'],1)
        self.assertFalse(result['full_mission_passed']);self.assertFalse(result['physical_transport_accepted'])
        self.assertEqual(scope['steps'],200)


if __name__=='__main__':unittest.main(verbosity=2)
