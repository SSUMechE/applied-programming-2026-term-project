"""Five-bead release and historical isolation regressions.

Data-only synthetic records use test_only rules. These tests do not establish
native transport qualification, contact correctness, or current hardware cost.
"""
from __future__ import annotations

import copy
from io import BytesIO
import json
from pathlib import Path
import tempfile
import unittest
from urllib.parse import urlencode
from wsgiref.util import setup_testing_defaults

from leaderboard.contracts import ProtocolError, validate_evaluation_record, validate_rule
from leaderboard.server import create_app
from leaderboard.store import Store
from leaderboard.v3 import archived_rule, ideal_bead_policy_contract
from leaderboard.v3_store import AttemptStore
from tests.test_unlimited_compute_scoring import (
    attempt_fixture, freeze, post, report_fixture, rule_fixture,
)


def get(app, path, query, token=None):
    environ = {}
    setup_testing_defaults(environ)
    environ.update(REQUEST_METHOD="GET", PATH_INFO=path,
                   QUERY_STRING=urlencode(query), **{"wsgi.input": BytesIO()})
    if token:
        environ["HTTP_AUTHORIZATION"] = "Bearer " + token
    captured = {}
    def start(status, headers, exc_info=None):
        captured["status"] = int(status.split()[0])
    captured["body"] = json.loads(b"".join(app(environ, start)))
    return captured


class FiveBeadScoringTests(unittest.TestCase):
    def test_generation_binds_fixed_count_without_rewriting_historical_policy(self):
        self.assertEqual(ideal_bead_policy_contract()["sample_count"], 5)
        for generation, count in ((3, 4), (4, 3), (5, 3), (6, 5)):
            for difficulty in ("easy", "medium", "hard"):
                rule = rule_fixture(difficulty, generation)
                validate_rule(rule)
                self.assertEqual(rule["sample_count"], count)
                if generation >= 4:
                    self.assertEqual(rule["ideal_bead_policy"],
                                     ideal_bead_policy_contract(sample_count=count))
                for wrong in (1, 2, 3, 4, 5, 6, True):
                    if type(wrong) is int and wrong == count:
                        continue
                    changed = copy.deepcopy(rule)
                    changed["sample_count"] = wrong
                    with self.assertRaises(ProtocolError):
                        validate_rule(freeze(changed))
        self.assertFalse(archived_rule(rule_fixture(generation=4)))
        self.assertFalse(archived_rule(rule_fixture(generation=5)))

    def test_five_ids_and_policy_are_required_even_if_record_is_relabelled(self):
        five = rule_fixture(generation=6)
        report = report_fixture(five)
        validate_evaluation_record(five, report)
        for count in (0, 3, 4, 6):
            changed = copy.deepcopy(report)
            changed["initial_sample_ids"] = [f"synthetic_bead_{i}" for i in range(count)]
            changed["final_sample_ids"] = changed["initial_sample_ids"][:]
            with self.assertRaises(ProtocolError):
                validate_evaluation_record(five, changed)
        changed = copy.deepcopy(report)
        changed["final_sample_ids"][-1] = changed["final_sample_ids"][0]
        with self.assertRaises(ProtocolError):
            validate_evaluation_record(five, changed)
        wrong = copy.deepcopy(five)
        wrong["ideal_bead_policy"] = ideal_bead_policy_contract(sample_count=3)
        with self.assertRaises(ProtocolError):
            validate_rule(freeze(wrong))
        old = rule_fixture(generation=5)
        with self.assertRaises(ProtocolError):
            validate_evaluation_record(five, report_fixture(old))

    def test_actual_server_keeps_historical_bytes_and_separate_populations(self):
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "synthetic.sqlite3"
            store = Store(database)
            attempts = AttemptStore(store)
            token = store.provision_team("synthetic-owned-team")
            outsider = store.provision_team("synthetic-other-team")
            app = create_app(database)
            rules = {g: rule_fixture(generation=g) for g in (4, 5, 6)}
            records = {}
            for generation in (4, 5):
                rule = rules[generation]
                store.add_rule(rule, allow_test_rules=True)
                attempts.activate(rule, str(generation)*64)
                report = report_fixture(rule, 10)
                records[generation] = attempt_fixture(rule, report)
                self.assertEqual(post(app, records[generation], token)["status"], 201)
            with store.connection() as db:
                before = [tuple(row) for row in db.execute(
                    "SELECT * FROM attempts_v3 ORDER BY sequence")]
                old_rules = [tuple(row) for row in db.execute(
                    "SELECT * FROM rules ORDER BY rule_id")]
            current = rules[6]
            store.add_rule(current, allow_test_rules=True)
            attempts.activate(current, "6"*64)
            records[6] = attempt_fixture(current, report_fixture(current, 1e100))
            self.assertEqual(post(app, records[6], token)["status"], 201)
            self.assertEqual(post(app, records[6], token)["status"], 200)
            for generation, rule in rules.items():
                history = get(app, "/api/v3/history", {"rule_id": rule["rule_id"]}, token)
                self.assertEqual(history["status"], 200)
                self.assertEqual([r["run_id"] for r in history["body"]["records"]],
                                 [records[generation]["run_id"]])
                board = get(app, "/api/v3/board", {"rule_id": rule["rule_id"]})
                self.assertEqual(board["status"], 200)
                self.assertEqual(board["body"]["population_n"], 1)
                self.assertEqual(board["body"]["rows"][0]["run_id"],
                                 records[generation]["run_id"])
                self.assertEqual(board["body"]["rule"]["sample_count"], rule["sample_count"])
                for secret in ("evaluation", "student_snapshot", "source_pins", "trajectory"):
                    self.assertNotIn(secret, board["body"]["rows"][0])
                self.assertEqual(get(app, "/api/v3/attempt",
                    {"run_id": records[generation]["run_id"]}, outsider)["status"], 404)
                self.assertEqual(get(app, "/api/v3/history",
                    {"rule_id": rule["rule_id"]})["status"], 401)
            with store.connection() as db:
                after = [tuple(row) for row in db.execute(
                    "SELECT * FROM attempts_v3 WHERE rule_digest IN (?,?) ORDER BY sequence",
                    (rules[4]["rule_digest"], rules[5]["rule_digest"]))]
                historical = [tuple(row) for row in db.execute(
                    "SELECT * FROM rules WHERE rule_id IN (?,?) ORDER BY rule_id",
                    (rules[4]["rule_id"], rules[5]["rule_id"]))]
            self.assertEqual(before, after)
            self.assertEqual(old_rules, historical)

    def test_server_rejects_cross_rule_record_without_mutating_history(self):
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "synthetic.sqlite3"
            store = Store(database)
            attempts = AttemptStore(store)
            old, new = rule_fixture(generation=5), rule_fixture(generation=6)
            for rule in (old, new):
                store.add_rule(rule, allow_test_rules=True)
            token = store.provision_team("synthetic-team")
            app = create_app(database)
            record = attempt_fixture(old, report_fixture(old))
            self.assertEqual(post(app, record, token)["status"], 201)
            relabelled = copy.deepcopy(record)
            relabelled.update(rule_id=new["rule_id"], rule_digest=new["rule_digest"])
            self.assertGreaterEqual(post(app, relabelled, token)["status"], 400)
            self.assertEqual(len(attempts.history("synthetic-team", old)["records"]), 1)
            self.assertEqual(attempts.history("synthetic-team", new)["records"], [])


if __name__ == "__main__":
    unittest.main()

