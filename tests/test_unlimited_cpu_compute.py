"""Supplied measurement-policy regressions. No physical completion claim."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from physics import arm_mission_plan
from physics.plan_student_transport import supervise_cpu_worker
from physics.tier_mission_plan import TierMissionPlan, seal_tier_bundle
from tests.test_arm_assembly_contract import synthetic_component
from tests.test_tier_route_contract import selected, contract_only_bundle
from transport_challenge.contracts import ContractError

def measurement_receipt(cost):
    return dict(schema_version="owned-cpu-worker-measurement-v2",
        status="COMPLETE_RECORDED", timed_out=False, worker_exit_code=0,
        cleanup_errors=[], source_bindings_verified=True,
        parent_genesis_imported_modules=[], parent_blocked_genesis_import_attempts=[],
        requested_wall_limit_s=None, timeout_s=None,
        parent_wall_s_before_publication=cost,
        worker_elapsed_including_cleanup_s=cost-1,
        worker_identity={"pid":1,"windows_creation_filetime_100ns":1})

def bind_measurement(value, cost):
    receipt=measurement_receipt(cost)
    value["cpu_process_supervisor"]=receipt
    value["cpu_planning_wall_s"]=cost
    value["cpu_planning_result"].update(process_wall_s=cost,
        cpu_process_supervisor=copy.deepcopy(receipt))
    return value

class UnlimitedCpuTests(unittest.TestCase):
    def test_real_child_outlives_legacy_timeout_and_completes(self):
        with tempfile.TemporaryDirectory() as temp:
            receipt=supervise_cpu_worker(
                [sys.executable,"-B","-c","import time; time.sleep(0.2); print('FINISHED')"],
                output=Path(temp),timeout_s=0.001)
            self.assertEqual(receipt["worker_exit_code"],0)
            self.assertFalse(receipt["timed_out"])
            self.assertIsNone(receipt["timeout_s"])
            self.assertEqual(receipt["schema_version"],"owned-cpu-worker-measurement-v2")
            self.assertGreater(receipt["worker_elapsed_including_cleanup_s"],0.2)
            self.assertIn("FINISHED",(Path(temp)/"worker_stdout.log").read_text())

    def test_manual_cancel_cleans_owned_child_without_timeout_claim(self):
        class Child:
            code=None
            def wait(self,*args,**kwargs): raise KeyboardInterrupt()
            def poll(self): return self.code
        child=Child()
        identity={"pid":1,"windows_creation_filetime_100ns":1}
        def stop(process, owned):
            self.assertIs(process,child);self.assertEqual(owned,identity)
            process.code=1
            return {"stopped":True}
        with tempfile.TemporaryDirectory() as temp, \
             patch("physics.plan_student_transport.subprocess.Popen",return_value=child), \
             patch("physics.review_process_memory.creation_identity",return_value=identity), \
             patch("physics.review_process_memory.kill_owned_tree",side_effect=stop) as kill:
            receipt=supervise_cpu_worker([],output=Path(temp),timeout_s=None)
        self.assertTrue(receipt["interrupted"])
        self.assertFalse(receipt["timed_out"])
        self.assertEqual(receipt["failure"]["type"],"CancelledByUser")
        self.assertNotEqual(receipt["worker_exit_code"],0)
        kill.assert_called_once()

    def test_independent_arm_accepts_long_measured_time(self):
        value=arm_mission_plan.seal(bind_measurement(synthetic_component(),10**8))
        accepted=arm_mission_plan.validate_arm_record(value)
        self.assertEqual(accepted["cpu_planning_wall_s"],10**8)

    def test_independent_measurement_rejects_limits_or_failure(self):
        for field,bad in (("requested_wall_limit_s",5400),("timeout_s",5400),
                          ("interrupted",True),("failure",{"type":"CancelledByUser"}),
                          ("worker_elapsed_including_cleanup_s",10**8+1)):
            value=bind_measurement(synthetic_component(),10**8)
            value["cpu_process_supervisor"][field]=bad
            value["cpu_planning_result"]["cpu_process_supervisor"][field]=bad
            with self.subTest(field=field),self.assertRaises(ContractError):
                arm_mission_plan.validate_arm_record(arm_mission_plan.seal(value))

    def test_full_tier_accepts_long_measured_time_without_weakening_source(self):
        with tempfile.TemporaryDirectory() as temp:
            pin=Path(temp)/"source.txt";pin.write_text("Structural fixture")
            contract,bound=selected("medium")
            value=seal_tier_bundle(bind_measurement(contract_only_bundle(contract,bound,pin),10**8))
            path=Path(temp)/"plan.json"
            path.write_text(json.dumps(value),encoding="utf-8")
            plan=TierMissionPlan(value,path=path)
            self.assertTrue(plan.verify_sources())
            pin.write_text("changed")
            with self.assertRaises(ContractError):plan.verify_sources()

if __name__=="__main__":unittest.main()

