"""Supplied data-contract regressions, not native simulation evidence.

All success-shaped records below are explicitly test_only synthetic envelopes.
No robot trajectory, solved placement, account, network or GPU is used.
"""
from __future__ import annotations

import copy
import hashlib
from io import BytesIO
import json
from pathlib import Path
import sys
import tempfile
import unittest
import uuid
from wsgiref.util import setup_testing_defaults

from leaderboard.contracts import (
    CHECKS, ProtocolError, canonical_bytes, digest, validate_evaluation_record,
    validate_rule,
)
from leaderboard.evaluate import NATIVE_BOUND_FIELDS, verify_review
from leaderboard.server import create_app
from leaderboard.store import Store
from leaderboard.v3 import ideal_bead_policy_contract, ranked_rows, validate_attempt

ROOT = Path(__file__).resolve().parents[1]
SOURCE = b"# Synthetic contract-only evaluator fixture. No physics.\n"

def freeze(rule):
    rule["rule_digest"] = digest({k:v for k,v in rule.items() if k != "rule_digest"})
    return rule

def rule_fixture(difficulty="easy", generation=6):
    # Use only public task/score constants. No saved answer or native receipt.
    candidates = sorted((ROOT/"leaderboard/rules").glob(
        f"term_v*/ap2026-{difficulty}-beads-term-v*.json"))
    if not candidates:
        raise AssertionError("The released public rule constants are missing.")
    value = json.loads(candidates[-1].read_bytes())
    value.update(rule_id=f"ap2026-{difficulty}-beads-term-v{generation}",
        test_only=True, qualified=True,
        qualification_reason="Synthetic data-contract fixture. Not native qualification.",
        evaluation_sources={"synthetic_evaluator.py":hashlib.sha256(SOURCE).hexdigest()},
        runtime_versions={"python_major_minor":f"{sys.version_info.major}.{sys.version_info.minor}"},
        max_wall_s=None if generation >= 5 else 25,
        sample_count=5 if generation >= 6 else 3 if generation >= 4 else 4)
    if generation >= 4:
        value["ideal_bead_policy"] = ideal_bead_policy_contract(sample_count=value["sample_count"])
    else:
        value.pop("ideal_bead_policy", None)
    return freeze(value)

def report_fixture(rule, wall_s=10**8):
    arms = rule["arm_ids"]
    count = len(arms)
    terminal = {"easy":"relay_1_support","medium":"relay_2_support","hard":"goal_support"}[rule["difficulty"]]
    route = ["source_support"]+[h["support_id"] for h in rule["handoffs"]]+[terminal]
    student_files = {"student/synthetic_fixture.py":"1"*64}
    snapshot = {"schema_version":"ap-student-snapshot-v1","files":student_files,
                "snapshot_sha256":digest(student_files)}
    report = {
        "schema_version":"ap-full-mission-evaluation-v3","run_id":str(uuid.uuid4()),
        "scope":"full_mission","status":"PASS","backend":"genesis_cuda",
        "initialization_mode":"original_task_initial","input_sha256":"1"*64,
        "artifact_sha256":"2"*64,"completed_stage_ids":[s["stage_id"] for s in rule["stages"]],
        "completed_arm_ids":copy.deepcopy(arms),
        "handoffs":[dict(h,released_supported_and_resting=True,same_sample_acquired=True)
                    for h in rule["handoffs"]],
        "donor_returns":[dict(arm_id=a,policy="kinematic_return",original_path_reversed=True,
                             physical_steps=0) for a in rule["donor_return_arm_ids"]],
        "checks":{key:True for key in CHECKS},
        "initial_sample_ids":[f"synthetic_bead_{i}" for i in range(rule["sample_count"])],
        "final_sample_ids":[f"synthetic_bead_{i}" for i in range(rule["sample_count"])],
        "final_rest_steps":rule["minimum_final_rest_steps"],"completion_ticks":1000,
        "completion_clock":{"schema_version":"ap-first-motion-final-rest-observation-v1",
            "native_initial_step":0,"first_motion_start_step":200,
            "final_rest_end_step":1200,"native_end_step":1200},
        "resources":{"wall_s":wall_s,"sampled_peak_process_vram_bytes":1000000,
            "valid_memory_samples":1,"unknown_memory_samples":0,
            "timed_out":False,"memory_limit_stop":False},
        "source_hashes_before":copy.deepcopy(rule["evaluation_sources"]),
        "source_hashes_after":copy.deepcopy(rule["evaluation_sources"]),
        "runtime_versions":copy.deepcopy(rule["runtime_versions"]),
        "student_snapshot":snapshot,"support_route":route,"carried_distance_mm":count*1000,
        "distance_metric":{"schema_version":"ap-carried-cup-center-distance-v1",
            "sample_dt_s":.0025,"path_length_m":float(count),"distance_mm":count*1000,
            "counted_intervals":count*10,
            "segments":[{"arm_id":a,"from_native_step":200+i*10,"to_native_step":210+i*10,
                         "path_length_m":1.} for i,a in enumerate(arms)],
            "rounding":"nearest_mm_half_up_once_after_total",
            "excludes":["unheld_approach","donor_return","released_settle"],
            "reference_point_body_m":[0.,0.,0.]},
    }
    for key in ("rule_digest","task_manifest_sha256","initialization_sha256",
                "physics_signature_sha256","terminal_target_sha256","material"):
        report[key] = rule[key]
    return report

def attempt_fixture(rule, report):
    return {"schema_version":"ap-attempt-record-v3","run_id":report["run_id"],
        "rule_id":rule["rule_id"],"rule_digest":rule["rule_digest"],"outcome":"SUCCESS",
        "finished_at_utc":"2026-11-15T00:00:00+00:00",
        "student_snapshot":copy.deepcopy(report["student_snapshot"]),"evaluation":report,
        "failure":None,"valid_carry_count":len(rule["arm_ids"]),
        "required_carry_count":len(rule["arm_ids"]),"evidence_sha256":"3"*64}

def package_fixture(directory, rule, report):
    directory.mkdir()
    input_bytes = canonical_bytes({"synthetic_contract_fixture":True})
    report["input_sha256"] = hashlib.sha256(input_bytes).hexdigest()
    fields = NATIVE_BOUND_FIELDS | {"completion_clock","carried_distance_mm","distance_metric",
                                    "support_route","student_snapshot"}
    native = {key:copy.deepcopy(report[key]) for key in fields}
    native.update(schema_version="ap-full-mission-native-result-v3",
        status="FULL_MISSION_PASS",full_mission_passed=True,source_bindings_unchanged=True,
        failure=None,cleanup_errors=[])
    raw = canonical_bytes(native)
    report["artifact_sha256"] = hashlib.sha256(raw).hexdigest()
    (directory/"EVALUATED_INPUT.json").write_bytes(input_bytes)
    (directory/"result.json").write_bytes(raw)
    (directory/"FULL_MISSION_EVALUATION.json").write_bytes(canonical_bytes(report))

def post(app, record, token):
    raw = canonical_bytes(record)
    environ = {}
    setup_testing_defaults(environ)
    environ.update(REQUEST_METHOD="POST",PATH_INFO="/api/v3/records",
        CONTENT_TYPE="application/json",CONTENT_LENGTH=str(len(raw)),
        HTTP_AUTHORIZATION="Bearer "+token,**{"wsgi.input":BytesIO(raw)})
    captured = {}
    def start(status, headers, exc_info=None):
        captured["status"] = int(status.split()[0])
    captured["body"] = json.loads(b"".join(app(environ,start)))
    return captured

class UnlimitedComputeScoringTests(unittest.TestCase):
    def test_current_rule_identity_cannot_downgrade_its_wire_contract(self):
        for generation in (5,6,99):
            for version in ("ap-leaderboard-rule-v1","ap-leaderboard-rule-v2"):
                rule=rule_fixture(generation=generation)
                rule["schema_version"]=version
                with self.assertRaises(ProtocolError) as error:
                    validate_rule(freeze(rule))
                self.assertEqual(error.exception.code,"RULE_VERSION")

    def test_new_generations_require_explicit_uncapped_rule(self):
        for generation in (5,6):
            for difficulty in ("easy","medium","hard"):
                with self.subTest(generation=generation,difficulty=difficulty):
                    rule=rule_fixture(difficulty,generation)
                    validate_rule(rule)
                    for cap in (0,3600,86400,True):
                        changed=copy.deepcopy(rule)
                        changed["max_wall_s"]=cap
                        with self.assertRaises(ProtocolError):
                            validate_rule(freeze(changed))

    def test_each_generation_keeps_its_fixed_bead_count_and_handling_policy(self):
        for generation in (5,6):
            rule=rule_fixture(generation=generation)
            for field,value in (("sample_count",4),("ideal_bead_policy",None)):
                with self.subTest(generation=generation,field=field):
                    changed=copy.deepcopy(rule)
                    changed[field]=value
                    with self.assertRaises(ProtocolError):
                        validate_rule(freeze(changed))
            changed=copy.deepcopy(rule)
            changed["ideal_bead_policy"]["spill_failures_sticky"]=False
            with self.assertRaises(ProtocolError):
                validate_rule(freeze(changed))

    def test_future_official_generation_requires_an_explicit_contract(self):
        for generation in (7,99):
            with self.assertRaises(ProtocolError) as error:
                validate_rule(rule_fixture(generation=generation))
            self.assertEqual(error.exception.code,"RULE_GENERATION")

    def test_positive_finite_wall_time_has_no_magnitude_cap(self):
        for difficulty in ("easy","medium","hard"):
            rule=rule_fixture(difficulty)
            for elapsed in (1e-300,1.,10**8,1e308,10**400):
                with self.subTest(difficulty=difficulty,elapsed=str(elapsed)[:25]):
                    report=report_fixture(rule,elapsed)
                    validate_evaluation_record(rule,report)
                    validate_attempt(rule,attempt_fixture(rule,report))

    def test_invalid_elapsed_metadata_is_rejected(self):
        rule=rule_fixture()
        for elapsed in (0,-1,float("nan"),float("inf"),float("-inf"),None,True,"100"):
            with self.subTest(elapsed=repr(elapsed)):
                with self.assertRaises(ProtocolError):
                    validate_evaluation_record(rule,report_fixture(rule,elapsed))

    def test_historical_numeric_rules_keep_their_exact_cap(self):
        for generation in (3,4):
            rule=rule_fixture(generation=generation)
            validate_evaluation_record(rule,report_fixture(rule,25))
            with self.assertRaises(ProtocolError):
                validate_evaluation_record(rule,report_fixture(rule,25.0001))
            rule["max_wall_s"]=None
            with self.assertRaises(ProtocolError):
                validate_rule(freeze(rule))

    def test_simulated_deadlines_remain_exact_for_all_tiers(self):
        for difficulty,seconds in (("easy",40),("medium",85),("hard",105)):
            rule=rule_fixture(difficulty)
            self.assertEqual(rule["max_completion_ticks"]*.0025,seconds)
            report=report_fixture(rule,1e308)
            report["completion_ticks"]=rule["max_completion_ticks"]
            for key in ("final_rest_end_step","native_end_step"):
                report["completion_clock"][key]=200+report["completion_ticks"]
            validate_evaluation_record(rule,report)
            report["completion_ticks"]+=1
            for key in ("final_rest_end_step","native_end_step"):
                report["completion_clock"][key]+=1
            with self.assertRaises(ProtocolError) as error:
                validate_evaluation_record(rule,report)
            self.assertEqual(error.exception.code,"TRANSPORT_DEADLINE_EXCEEDED")

    def test_physical_identity_memory_and_interruption_checks_remain(self):
        rule=rule_fixture("hard")
        mutations=[
            lambda r:r["checks"].update(sample_retained=False),
            lambda r:r["checks"].update(collision_free=False),
            lambda r:r["checks"].update(tracking_passed=False),
            lambda r:r.update(status="FAIL"),
            lambda r:r.update(scope="independent_arm"),
            lambda r:r["completed_arm_ids"].pop(),
            lambda r:r["handoffs"].pop(),
            lambda r:r["final_sample_ids"].pop(),
            lambda r:r["resources"].update(memory_limit_stop=True),
            lambda r:r["resources"].update(timed_out=True),
            lambda r:r["resources"].update(sampled_peak_process_vram_bytes=rule["max_vram_bytes"]+1),
            lambda r:r["resources"].update(valid_memory_samples=0),
        ]
        for index,mutate in enumerate(mutations):
            with self.subTest(index=index):
                report=report_fixture(rule,1e308)
                mutate(report)
                with self.assertRaises(ProtocolError):
                    validate_evaluation_record(rule,report)

    def test_compute_time_does_not_change_score_rank_or_bonus(self):
        rows=[dict(team_id=f"synthetic-{i}",carried_distance_mm=1000+i*11,
                   completion_ticks=1000-i*7,wall_s=1.) for i in range(6)]
        baseline=ranked_rows(rows,"hard")
        changed=copy.deepcopy(rows)
        for i,row in enumerate(changed):
            row["wall_s"]=1e300/(i+1)
        scored=ranked_rows(changed,"hard")
        keys=("team_id","rank","C_exact","competition_adjustment_exact","population_n")
        self.assertEqual([{k:r[k] for k in keys} for r in baseline],
                         [{k:r[k] for k in keys} for r in scored])

    def test_existing_report_packager_accepts_long_time_and_rejects_tampering(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            (root/"synthetic_evaluator.py").write_bytes(SOURCE)
            rule=rule_fixture("medium")
            report=report_fixture(rule,1e308)
            directory=root/"synthetic_envelope"
            package_fixture(directory,rule,report)
            actual,audit=verify_review(rule,directory,project_root=root)
            self.assertEqual(actual,report)
            self.assertFalse(audit["native_physics_launched"])
            (root/"synthetic_evaluator.py").write_bytes(SOURCE+b"# modified\n")
            with self.assertRaises(ProtocolError):
                verify_review(rule,directory,project_root=root)

    def test_actual_local_server_records_long_run_and_rejects_bad_evidence(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/"test_only.sqlite"
            store=Store(path)
            rule=rule_fixture("hard")
            store.add_rule(rule,allow_test_rules=True)
            token=store.provision_team("synthetic-team")
            app=create_app(path)
            report=report_fixture(rule,1e308)
            record=attempt_fixture(rule,report)
            accepted=post(app,record,token)
            self.assertEqual(accepted["status"],201,accepted)
            self.assertEqual(post(app,record,token)["status"],200)
            for kind in ("memory","spill","deadline","interrupted"):
                report=report_fixture(rule,1e308)
                if kind=="memory": report["resources"]["memory_limit_stop"]=True
                if kind=="spill": report["checks"]["sample_retained"]=False
                if kind=="interrupted": report["resources"]["timed_out"]=True
                if kind=="deadline":
                    report["completion_ticks"]=rule["max_completion_ticks"]+1
                    for key in ("final_rest_end_step","native_end_step"):
                        report["completion_clock"][key]=200+report["completion_ticks"]
                self.assertGreaterEqual(post(app,attempt_fixture(rule,report),token)["status"],400)

if __name__=="__main__":
    unittest.main()

