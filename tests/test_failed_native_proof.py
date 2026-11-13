"""Failure evidence propagation without importing or stepping Genesis."""
import ast,copy,json,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from physics import device_tier_mission as native
from transport_challenge.contracts import ContractError,digest

class FailedNativeProofTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.output=Path(self.temp.name);self.directory=self.output/'device_tier_execution';self.directory.mkdir()
        contract=dict(required_arm_ids=['arm_0','arm_1'])
        self.plan=SimpleNamespace(sha256='bundle',file_sha256='planfile',task_contract=contract,verify_sources=lambda:True)
        self.value=dict(schema_version=native.SCHEMA,passed=False,full_mission_physical_verified=False,
            failure=dict(type='PhysicalTransportRejected',message='actual spill'),persistent_scene=True,
            contents='beads',saved_bundle_sha256='bundle',saved_plan_file_sha256='planfile',
            task_contract=contract,task_contract_sha256=digest(contract),native_output_directory=str(self.directory),
            completed_stage_ids=['arm_0__approach','arm_0__carry'],
            stage_gates=[dict(stage_id='arm_0__carry',kind='carry',gate=dict(passed=True,strict_material=True))],
            boundary_events=[dict(stage_id='arm_0__carry',event='release',ready=dict(passed=True))],
            donor_returns=[dict(stage_id='arm_0__carry',receipt=dict(passed=True))])
        self.value['partial_progress']=native.partial_progress(self.value);self.write()
    def write(self):self.path=self.directory/'TIER_DEVICE_RESULT.json';self.path.write_text(json.dumps(self.value))
    def read(self,source=True):return native.load_failed_tier_proof(self.directory,self.plan,contents='beads',source_bindings_unchanged=source)
    def test_owned_failed_prefix_is_preserved_without_success(self):
        record,binding=self.read()
        self.assertEqual(record['partial_progress']['valid_carry_count'],1)
        self.assertFalse(binding['physical_transport_accepted']);self.assertFalse(binding['full_mission_passed'])
    def test_foreign_bundle_source_or_unaccepted_carry_is_unknown(self):
        with self.assertRaises(ContractError):self.read(False)
        self.value['saved_bundle_sha256']='foreign';self.write()
        with self.assertRaises(ContractError):self.read()
        self.value['saved_bundle_sha256']='bundle';self.value['donor_returns']=[];self.write()
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

if __name__=='__main__':unittest.main()
