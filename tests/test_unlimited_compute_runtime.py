"""CPU-only regressions for elapsed-time reporting, never native qualification."""
import ast
from contextlib import ExitStack
import io
import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from physics import review_process_memory as monitor
from physics import review_tier_transport as review


class OwnedWorkerWaitTests(unittest.TestCase):
    def test_unlimited_wait_never_reads_host_deadline_and_keeps_polling(self):
        process=Mock()
        process.poll.side_effect=[None,None,None,0,0]
        process.wait.side_effect=[subprocess.TimeoutExpired("fixture",.1)]*3
        memory=SimpleNamespace(limit_exceeded=Mock())
        memory.limit_exceeded.is_set.return_value=False
        with patch.object(monitor.time,"perf_counter",side_effect=AssertionError("No deadline clock needed")), \
                patch.object(monitor,"kill_owned_tree") as kill:
            receipt=monitor.wait_owned_worker(process,memory,{"pid":123})
        self.assertEqual(process.wait.call_count,3)
        self.assertEqual(memory.limit_exceeded.is_set.call_count,3)
        self.assertTrue(all(call.kwargs=={"timeout":.1} for call in process.wait.call_args_list))
        self.assertEqual(receipt,dict(exit_code=0,timed_out=False,memory_limit_stop=False,termination=None))
        kill.assert_not_called()

    def test_memory_breach_still_terminates_only_owned_tree(self):
        process=Mock();process.poll.side_effect=[None,-9]
        memory=SimpleNamespace(limit_exceeded=Mock())
        memory.limit_exceeded.is_set.return_value=True
        identity={"pid":123,"creation":"fixture"}
        with patch.object(monitor,"kill_owned_tree",return_value={"terminated":True}) as kill:
            receipt=monitor.wait_owned_worker(process,memory,identity,deadline=None)
        kill.assert_called_once_with(process,identity)
        process.wait.assert_not_called()
        self.assertTrue(receipt["memory_limit_stop"]);self.assertFalse(receipt["timed_out"])

    def test_explicit_infrastructure_deadline_retains_its_meaning(self):
        process=Mock();process.poll.side_effect=[None,-9]
        memory=SimpleNamespace(limit_exceeded=Mock())
        memory.limit_exceeded.is_set.return_value=False
        with patch.object(monitor.time,"perf_counter",return_value=11.), \
                patch.object(monitor,"kill_owned_tree") as kill:
            receipt=monitor.wait_owned_worker(process,memory,{"pid":123},deadline=10.)
        kill.assert_called_once();self.assertTrue(receipt["timed_out"])
        self.assertFalse(receipt["memory_limit_stop"])


class UnlimitedFullReviewTests(unittest.TestCase):
    def run_review(self, *, legacy=0., native_pass=True, memory_stop=False, cancel=False, export_error=False):
        from physics.tier_mission_plan import TierMissionPlan
        from leaderboard import contracts
        from physics import full_tier_result
        with TemporaryDirectory() as temporary, ExitStack() as stack:
            root=Path(temporary);output=root/"review"
            argv=["--saved-tier",str(root/"saved.json"),"--difficulty","medium","--task",str(root/"task"),
                  "--design",str(root/"design.json"),"--output",str(output),"--wall-limit-s",str(legacy),
                  "--rule",str(root/"rule.json")]
            record={"cpu_planning_result":{"process_wall_s":200000.}}
            plan=SimpleNamespace(task_contract={"difficulty":"medium"},record=lambda:record,
                                 sha256="fixture-bundle",file_sha256="fixture-file",verify_sources=lambda:True)
            process=Mock();process.poll.return_value=None if cancel else 0
            memory=Mock();memory.finish.return_value={"observed_sampled_peak_within_limit":not memory_stop}
            stack.enter_context(patch.object(TierMissionPlan,"from_file",return_value=plan))
            stack.enter_context(patch.object(contracts,"load_rule",return_value={
                "difficulty":"medium","schema_version":"fixture-only","rule_digest":"fixture-rule","qualified":False}))
            stack.enter_context(patch.object(contracts,"source_snapshot",return_value={"fixture":"hash"}))
            stack.enter_context(patch.object(contracts,"runtime_snapshot",return_value={"fixture":"version"}))
            stack.enter_context(patch.object(review,"hardware_snapshot",return_value={}))
            stack.enter_context(patch.object(review,"read_record",return_value={"blocked_online_calls":{}}))
            assess=stack.enter_context(patch.object(review,"assess_native",return_value=(native_pass,{"fixture":True})))
            popen=stack.enter_context(patch.object(review.subprocess,"Popen",return_value=process))
            stack.enter_context(patch.object(monitor,"ProcessMemorySampler",return_value=memory))
            identity={"pid":123,"creation":"fixture"}
            stack.enter_context(patch.object(monitor,"creation_identity",return_value=identity))
            kill=stack.enter_context(patch.object(monitor,"kill_owned_tree"))
            waited=stack.enter_context(patch.object(monitor,"wait_owned_worker",
                side_effect=KeyboardInterrupt() if cancel else None,
                return_value={"exit_code":0,"timed_out":False,"memory_limit_stop":memory_stop,"termination":None}))
            # Includes a large additional interval spent exporting after native completion.
            stack.enter_context(patch.object(review.time,"perf_counter",side_effect=[10.,300010.,500010.]))
            def export(**kwargs):
                kwargs["output"].mkdir()
                if export_error:raise ValueError("fixture export failure")
                return {"fixture_only":True}
            exporter=stack.enter_context(patch.object(full_tier_result,"export_full_review",side_effect=export))
            online=stack.enter_context(patch.object(review,"finalize_online_record",return_value={"status":"fixture_no_network"}))
            err=io.StringIO()
            stack.enter_context(patch("sys.stderr",err))
            stack.enter_context(patch("sys.stdout",io.StringIO()))
            if cancel:
                with self.assertRaises(KeyboardInterrupt):review.main(argv)
                kill.assert_called_once_with(process,identity);memory.finish.assert_called_once()
                self.assertFalse((output/"REVIEW_SUPERVISOR.json").exists())
                exporter.assert_not_called();online.assert_not_called()
                return
            code=review.main(argv)
            receipt=json.loads((output/"REVIEW_SUPERVISOR.json").read_bytes())
            self.assertIsNone(waited.call_args.kwargs["deadline"])
            cmd=popen.call_args.args[0]
            self.assertEqual(float(cmd[cmd.index("--wall-limit-s")+1]),0.)
            self.assertNotIn("--gpu-wall-allowance-s",cmd)
            self.assertEqual(receipt["gpu_review_process_wall_s"],500000.)
            self.assertEqual(receipt["additive_cpu_plus_review_wall_s"],700000.)
            self.assertEqual(receipt["total_cpu_and_gpu_wall_s_including_search"],700000.)
            self.assertIsNone(receipt["wall_limit_s"]);self.assertEqual(receipt["wall_time_policy"],"measure_only")
            self.assertIsNone(receipt["requested_complete_budget_s"])
            self.assertIsNone(receipt["observed_additive_cost_within_budget"])
            self.assertFalse(receipt["timed_out"]);json.dumps(receipt,allow_nan=False)
            if legacy>0:self.assertIn("deprecated and ignored",err.getvalue())
            kill.assert_not_called();memory.finish.assert_called_once();assess.assert_called_once()
            if native_pass and not memory_stop and not export_error:
                self.assertEqual(code,0);self.assertTrue(receipt["full_mission_passed"])
                exporter.assert_called_once()
                self.assertFalse((output/"score_evidence/EXPORT_REVOKED.json").exists())
            else:
                self.assertNotEqual(code,0);self.assertFalse(receipt["review_accepted"])
                self.assertFalse(receipt["full_mission_passed"])
                if export_error:
                    self.assertEqual(receipt["outcome"],"RESULT_EXPORT_FAILED")
                    self.assertTrue((output/"score_evidence/EXPORT_REVOKED.json").exists())
                else:exporter.assert_not_called()

    def test_default_is_unlimited_and_large_measured_cpu_native_export_costs_pass(self):
        self.assertEqual(review.parser().get_default("wall_limit_s"),0.)
        self.run_review()

    def test_positive_legacy_values_are_ignored_not_rejected_or_enforced(self):
        for value in (1.,5400.,1000000.):
            with self.subTest(value=value):self.run_review(legacy=value)

    def test_memory_native_and_export_failure_still_reject(self):
        self.run_review(memory_stop=True)
        self.run_review(native_pass=False)
        self.run_review(export_error=True)

    def test_manual_cancellation_still_cleans_owned_process_without_success(self):
        self.run_review(cancel=True)

    def test_invalid_legacy_numbers_reject_before_source_or_native_admission(self):
        from physics.tier_mission_plan import TierMissionPlan
        argv=["--saved-tier","saved","--difficulty","medium","--task","task","--design","design","--output","output"]
        for value in ("nan","inf","-1"):
            with self.subTest(value=value), patch.object(TierMissionPlan,"from_file") as loader, \
                    patch("sys.stderr",io.StringIO()),self.assertRaises(SystemExit):
                review.main(argv+["--wall-limit-s",value])
            loader.assert_not_called()


class NativeExecutionGuardTests(unittest.TestCase):
    def owner(self):
        from physics.device_tier_mission import _Mission, INITIAL_TICKS
        owner=object.__new__(_Mission)
        owner.scene=SimpleNamespace(t=INITIAL_TICKS,sim=object(),
                                    rigid_solver=SimpleNamespace(dyn_state=object()))
        owner.c={"source_check":lambda:True}
        owner.deadline=-1.
        owner.contract={"completion_limit_ticks":100}
        owner.record={"native_scene_identity":owner.scene_identity()}
        return owner

    def test_host_elapsed_cannot_fail_native_check_but_simulated_limit_is_exact(self):
        from physics.device_tier_mission import INITIAL_TICKS
        from transport_challenge.contracts import ContractError
        owner=self.owner()
        with patch("physics.device_tier_mission.time.perf_counter",side_effect=AssertionError("No elapsed cutoff")):
            owner.check(100)
            with self.assertRaisesRegex(ContractError,"simulated transport deadline"):owner.check(101)
            owner.scene.t=INITIAL_TICKS+101
            with self.assertRaisesRegex(ContractError,"simulated transport deadline"):owner.check()

    def test_source_and_native_allocation_invariants_still_reject(self):
        from transport_challenge.contracts import ContractError
        owner=self.owner();owner.c["source_check"]=lambda:False
        with self.assertRaisesRegex(ContractError,"source bindings changed"):owner.check()
        owner.c["source_check"]=lambda:True;owner.scene.rigid_solver.dyn_state=object()
        with self.assertRaisesRegex(ContractError,"allocation was replaced"):owner.check()

    def test_compatibility_callback_preserves_source_admission_without_elapsed_gate(self):
        from transport_challenge.contracts import ContractError
        path=Path(review.__file__).with_name("episode_runtime.py")
        tree=ast.parse(path.read_text(encoding="utf-8"))
        node=next(n for n in ast.walk(tree) if isinstance(n,ast.FunctionDef) and n.name=="student_wall_check")
        namespace={"source_check":lambda:True,"ContractError":ContractError}
        exec(compile(ast.fix_missing_locations(ast.Module(body=[node],type_ignores=[])),str(path),"exec"),namespace)
        namespace["student_wall_check"]()
        namespace["source_check"]=lambda:False
        with self.assertRaisesRegex(ContractError,"source bindings changed"):namespace["student_wall_check"]()

    def test_student_dispatches_never_construct_remaining_host_allowance(self):
        root=Path(review.__file__).parent
        expected={"run_episode":"wall_limit_s","execute_device_tier_mission":"wall_deadline",
                  "execute_device_one_carry":"wall_deadline"}
        found=set()
        for filename in ("run_student_three_arm.py","episode_runtime.py"):
            tree=ast.parse((root/filename).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node,ast.Call) or not isinstance(node.func,ast.Name):continue
                name=node.func.id
                if name in expected:
                    value=next(kw.value for kw in node.keywords if kw.arg==expected[name])
                    self.assertIsInstance(value,ast.Constant);self.assertIsNone(value.value);found.add(name)
                if name=="continuation_planner":
                    self.assertIsInstance(node.args[1],ast.Constant);self.assertIsNone(node.args[1].value)
                    found.add(name)
        self.assertEqual(found,set(expected)|{"continuation_planner"})


if __name__=="__main__":unittest.main()

