"""Unlimited compute-time regressions. Mocked workers never claim native qualification."""
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from contextlib import ExitStack
from unittest.mock import Mock, patch

from physics import review_arm_practice as tier_practice, review_arm_plan as arm_practice
from physics import review_student_transport as one_review, review_process_memory as monitor


class UnlimitedPracticeTests(unittest.TestCase):
    def _run_supervisor(self, module, *, legacy=0., cancel=False, memory_stop=False, native_pass=True):
        with TemporaryDirectory() as temporary, ExitStack() as stack:
            directory=Path(temporary); output=directory/'practice'
            saved_flag='--saved-arm' if module is arm_practice else '--saved-tier'
            argv=[saved_flag,str(directory/'saved.json'),'--difficulty','hard','--task',str(directory),
                  '--design',str(directory/'design.json'),'--arm','arm_2','--contents','beads',
                  '--output',str(output),'--wall-limit-s',str(legacy)]
            plan=SimpleNamespace(sha256='bundle',file_sha256='file',verify_sources=Mock(return_value=True))
            worker={'practice_passed':native_pass,'native':{'failure':None if native_pass else {'type':'Spill','message':'preserved'}},
                    'scene_handoff_and_cleanup_ok':True}
            process=Mock();process.poll.return_value=None if cancel else 0
            memory=Mock();memory.finish.return_value={'observed_sampled_peak_within_limit':not memory_stop}
            stack.enter_context(patch.object(module,'load_plan',return_value=plan))
            stack.enter_context(patch.object(module,'practice_source_snapshot',return_value={'source':'hash'}))
            stack.enter_context(patch.object(module,'hardware_snapshot',return_value={}))
            stack.enter_context(patch.object(module,'read_record',return_value=worker))
            stack.enter_context(patch('student_design_input.prepare_arguments',side_effect=lambda command,raw:(raw,{})))
            popen=stack.enter_context(patch.object(module.subprocess,'Popen',return_value=process))
            stack.enter_context(patch.object(monitor,'ProcessMemorySampler',return_value=memory))
            stack.enter_context(patch.object(monitor,'creation_identity',return_value={'pid':123,'creation':'owned'}))
            kill=stack.enter_context(patch.object(monitor,'kill_owned_tree'))
            waiter=stack.enter_context(patch.object(monitor,'wait_owned_worker',side_effect=KeyboardInterrupt() if cancel else None,
                return_value={'exit_code':0,'timed_out':False,'memory_limit_stop':memory_stop,'termination':None}))
            if module is arm_practice:
                stack.enter_context(patch.object(module,'verify_bound_worker_plan',return_value=plan))
            clock=stack.enter_context(patch.object(module.time,'perf_counter',side_effect=[10.,300010.]))
            err=io.StringIO();stack.enter_context(patch('sys.stderr',err));stack.enter_context(patch('sys.stdout',io.StringIO()))
            if cancel:
                with self.assertRaises(KeyboardInterrupt):module.main(argv)
                kill.assert_called_once_with(process,{'pid':123,'creation':'owned'})
                memory.finish.assert_called_once()
                self.assertFalse((output/'PRACTICE_RESULT.json').exists())
                self.assertTrue(output.with_suffix('.worker.log').exists())
                return
            code=module.main(argv);receipt=json.loads((output/'PRACTICE_RESULT.json').read_text())
            self.assertEqual(waiter.call_args.kwargs['deadline'],None)
            cmd=popen.call_args.args[0];self.assertEqual(cmd[cmd.index('--wall-limit-s')+1],'0')
            self.assertEqual(receipt['review_wall_s'],300000.)
            self.assertIsNone(receipt['wall_limit_s']);self.assertEqual(receipt['wall_time_policy'],'measure_only')
            self.assertFalse(receipt['timed_out']);self.assertFalse(receipt['ranking_eligible']);self.assertFalse(receipt['full_mission_passed'])
            json.dumps(receipt,allow_nan=False)
            kill.assert_not_called();memory.finish.assert_called_once()
            if legacy>0:self.assertIn('deprecated and ignored',err.getvalue())
            if native_pass and not memory_stop:
                self.assertEqual(code,0);self.assertTrue(receipt['practice_passed']);self.assertEqual(receipt['failed_checks'],[])
            else:
                self.assertEqual(code,1);self.assertFalse(receipt['practice_passed'])
                if memory_stop:self.assertIn('memory_limit',receipt['failed_checks'])
                if not native_pass:self.assertEqual(receipt['failure']['type'],'Spill')

    def test_defaults_are_unlimited_for_all_practice_entrypoints(self):
        for module in (tier_practice,arm_practice,one_review):
            self.assertEqual(module.parser().get_default('wall_limit_s'),0.)

    def test_long_elapsed_is_measurement_not_rejection(self):
        for module in (tier_practice,arm_practice):
            with self.subTest(module=module.__name__):self._run_supervisor(module)

    def test_legacy_positive_limit_is_not_forwarded_or_enforced(self):
        for module in (tier_practice,arm_practice):
            for old_limit in (1.,5400.,1000000.):
                with self.subTest(module=module.__name__,limit=old_limit):self._run_supervisor(module,legacy=old_limit)

    def test_memory_stop_and_actual_spill_still_reject(self):
        for module in (tier_practice,arm_practice):
            with self.subTest(module=module.__name__,failure='memory'):self._run_supervisor(module,memory_stop=True)
            with self.subTest(module=module.__name__,failure='spill'):self._run_supervisor(module,native_pass=False)

    def test_manual_cancel_owns_cleanup_and_never_publishes_success(self):
        for module in (tier_practice,arm_practice):
            with self.subTest(module=module.__name__):self._run_supervisor(module,cancel=True)

    def test_invalid_nonfinite_or_negative_legacy_input_still_rejected(self):
        for module in (tier_practice,arm_practice):
            for value in ('nan','inf','-1'):
                flag='--saved-arm' if module is arm_practice else '--saved-tier'
                argv=[flag,'saved','--difficulty','hard','--task','task','--design','design','--arm','arm_2','--contents','empty','--output','output','--wall-limit-s',value]
                with self.subTest(module=module.__name__,value=value),patch('sys.stderr',io.StringIO()),self.assertRaises(SystemExit):module.main(argv)

    def test_legacy_one_carry_supervisor_keeps_long_elapsed_as_cost_only(self):
        from physics.one_carry_planning import SavedOneCarryPlan
        with TemporaryDirectory() as temporary, ExitStack() as stack:
            directory=Path(temporary);output=directory/'one_carry';process=Mock(pid=123);process.poll.return_value=0
            plan=SimpleNamespace(record=lambda:{'payload':'beads','cpu_planning_result':{}},verify_sources=lambda:True,sha256='bundle',file_sha256='file')
            memory=Mock();memory.finish.return_value={'status':'SAMPLED','observed_sampled_peak_within_limit':True}
            def start(command,**kwargs):
                self.assertEqual(command[command.index('--wall-limit-s')+1],'0')
                self.assertNotIn('--gpu-wall-allowance-s',command)
                output.mkdir();(output/'result.json').write_text(json.dumps({'status':'ONE_CARRY_TRANSPORT_SAMPLED_PASS','one_carry_transport_accepted':True,'source_bindings_unchanged':True,'saved_bundle_sha256':'bundle','saved_plan_file_sha256':'file','cleanup_errors':[],'failure':None}))
                (output/'ONLINE_CALL_GUARD.json').write_text(json.dumps({'blocked_online_calls':{'plan_student_motion':0,'plan_joint_motion':0,'ik':0}}));return process
            stack.enter_context(patch.object(SavedOneCarryPlan,'from_file',return_value=plan))
            stack.enter_context(patch('physics.review_tier_transport.cpu_cost_attribution',return_value={'review_attribution_s':200000.,'total_s':200001.,'search_s':1.,'policy':'fixture'}))
            stack.enter_context(patch.object(one_review.subprocess,'Popen',side_effect=start))
            stack.enter_context(patch.object(monitor,'creation_identity',return_value={'pid':123,'windows_creation_filetime_100ns':116444736000000001}))
            stack.enter_context(patch.object(monitor,'ProcessMemorySampler',return_value=memory))
            wait=stack.enter_context(patch.object(monitor,'wait_owned_worker',return_value={'exit_code':0,'timed_out':False,'memory_limit_stop':False,'termination':None}))
            kill=stack.enter_context(patch.object(monitor,'kill_owned_tree'))
            stack.enter_context(patch.object(one_review.time,'perf_counter',side_effect=[10.,300010.]))
            stack.enter_context(patch('sys.stdout',io.StringIO()));err=io.StringIO();stack.enter_context(patch('sys.stderr',err))
            code=one_review.main(['--saved-carry',str(directory/'saved.json'),'--task',str(directory),'--design',str(directory/'design.json'),'--output',str(output),'--wall-limit-s','1'])
            self.assertEqual(code,0);self.assertIsNone(wait.call_args.kwargs['deadline']);kill.assert_not_called()
            receipt=json.loads((output/'REVIEW_SUPERVISOR.json').read_text());self.assertTrue(receipt['review_accepted'])
            self.assertEqual(receipt['additive_cpu_plus_review_wall_s'],500000.);self.assertIsNone(receipt['requested_complete_budget_s'])
            self.assertIsNone(receipt['observed_additive_cost_within_budget']);self.assertFalse(receipt['full_mission_passed'])
            self.assertIn('deprecated and ignored',err.getvalue());json.dumps(receipt,allow_nan=False)


class NativeBoundaryCompatibilityTests(unittest.TestCase):
    def test_native_practice_discards_expired_host_deadline_but_preserves_failure(self):
        from physics import device_arm_practice as native
        with TemporaryDirectory() as temporary:
            context={'contents':'beads','student_driver':SimpleNamespace(saved_plan=SimpleNamespace(sha256='bundle',file_sha256='file',required_arm_ids=['arm_2'])),
                     'initial_fingerprint':{},'observe':lambda:{}}
            with patch.object(native,'selected_boundary',return_value={}),patch.object(native,'_Mission',side_effect=ValueError('physical admission failed')) as mission,patch.object(native,'_body_and_arms',return_value={}):
                result=native.execute(context,output=Path(temporary)/'native',deadline=-100.,arm_id='arm_2')
            self.assertIsNone(mission.call_args.args[2]);self.assertFalse(result['practice_passed'])
            self.assertEqual(result['failure']['message'],'physical admission failed');self.assertIsNone(result['wall_limit_s'])

    def test_one_carry_ignores_old_deadline_at_actual_boundary(self):
        from physics import device_one_carry as native
        from physics.one_carry_planning import SavedOneCarryPlan
        saved=object.__new__(SavedOneCarryPlan);command=SimpleNamespace(action_role='carry',active_arm_id='arm_0',step=11,action_id='carry')
        driver=SimpleNamespace(mode='one_carry',particle_mode=False,grasp_policy='automatic',saved_plan=saved)
        context={'student_driver':driver,'scene':SimpleNamespace(t=10,sim=SimpleNamespace(dt=.0025,substeps=16)),
                 'beads':[1,2,3,4,5],'ids':[1,2,3,4,5],'source_check':lambda:True,'automatic_grasp':SimpleNamespace(holder='arm_0'),
                 'dispatcher':SimpleNamespace(current_segment=SimpleNamespace(plan=SimpleNamespace(actions=[SimpleNamespace(action_id='carry',kind='trajectory',steps=100)])))}
        with TemporaryDirectory() as temporary,patch.object(SavedOneCarryPlan,'record',return_value={'payload':'beads'}),patch.object(SavedOneCarryPlan,'verify_sources',return_value=True),patch.object(native,'_run_source_bound_device',return_value={'fixture':True}) as run,patch.object(native,'validate_device_completion',return_value=True):
            result=native.execute_device_one_carry(context,command,output=Path(temporary)/'carry',wall_deadline=-1.)
            self.assertEqual(result,{'fixture':True});self.assertIsNone(run.call_args.kwargs['wall_deadline'])
            context['scene'].sim.substeps=8
            with self.assertRaisesRegex(ValueError,'native boundary'):native.execute_device_one_carry(context,command,output=Path(temporary)/'wrong',wall_deadline=None)

    def test_tail_preserves_source_and_simulation_deadline_checks(self):
        from physics.deferred_gpu_tail import run_supported_tail
        helper=SimpleNamespace(holder='arm_0');driver=SimpleNamespace(particle_mode=False,automatic_grasp=helper,simulation_deadline_s=10.)
        context={'scene':SimpleNamespace(t=20,cur_t=.05,sim=SimpleNamespace(dt=.0025,substeps=16)),
                 'student_driver':driver,'automatic_grasp':helper,'beads':[1,2,3,4,5],'ids':[1,2,3,4,5],'arms':{'arm_0':1},'source_check':lambda:False}
        with TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(ValueError,'Source bindings changed'):run_supported_tail(context,{},output=Path(temporary)/'source',wall_deadline=-1.)
            context['source_check']=lambda:True;context['scene'].cur_t=11.
            with self.assertRaisesRegex(ValueError,'simulation deadline exceeded'):run_supported_tail(context,{},output=Path(temporary)/'simulation',wall_deadline=None)


if __name__=='__main__':unittest.main()
