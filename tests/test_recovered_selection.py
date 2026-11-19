"""Exercise the published CLI against a disposable test-only leaderboard, with no simulation."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from wsgiref.simple_server import WSGIRequestHandler, make_server

from leaderboard.contracts import canonical_bytes
from leaderboard.server import create_app
from leaderboard.store import Store
from leaderboard.v3 import capture_student_snapshot
from leaderboard.v3_store import AttemptStore
from tests.test_unlimited_compute_scoring import SOURCE, package_fixture, report_fixture, rule_fixture


ROOT = Path(__file__).resolve().parents[1]
QUEUE = Path("output/leaderboard_queue")


class QuietServer(WSGIRequestHandler):
    def log_message(self, *args):
        pass


class RecoveredSelection(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.checkout = Path(temp.name) / "synthetic-team"
        self.checkout.mkdir()
        for name in ("project_run.py", "update_project.py"):
            shutil.copy2(ROOT / name, self.checkout)
        for name in ("diagnostics", "leaderboard"):
            shutil.copytree(ROOT / name, self.checkout / name, ignore=shutil.ignore_patterns("__pycache__"))
        (self.checkout / "student").mkdir()
        self.student = Path("student/synthetic.py")
        (self.checkout / self.student).write_bytes(SOURCE)
        (self.checkout / "synthetic_evaluator.py").write_bytes(SOURCE)
        self.git("init", "-q")
        self.git("add", "student")
        self.git("commit", "-qm", "Synthetic evaluated student snapshot")
        self.commit = self.git("rev-parse", "HEAD")
        self.rule = rule_fixture()
        (self.checkout / "synthetic_rule.json").write_bytes(canonical_bytes(self.rule))

        database = Path(temp.name) / "test-only.sqlite"
        store = Store(database)
        store.add_rule(self.rule, allow_test_rules=True)
        self.attempts = AttemptStore(store)
        self.attempts.activate(self.rule, "6" * 64)
        token = store.provision_team("synthetic-team")
        app = create_app(database)
        self.offline = False
        self.requests = []

        def gateway(env, start_response):
            self.requests.append(env["PATH_INFO"])
            if self.offline:
                start_response("503 Service Unavailable", [("Content-Type", "application/json")])
                return [b"{}"]
            return app(env, start_response)

        server = make_server("127.0.0.1", 0, gateway, handler_class=QuietServer)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(server.server_close)
        self.addCleanup(worker.join)
        self.addCleanup(server.shutdown)
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("AP_LEADERBOARD_")}
        self.env.update(AP_LEADERBOARD_TEAM_TOKEN=token, NO_PROXY="127.0.0.1", no_proxy="127.0.0.1",
                        AP_LEADERBOARD_URL=f"http://127.0.0.1:{server.server_port}")

    def git(self, *args):
        return subprocess.check_output(["git", "-c", "user.name=Synthetic Test",
            "-c", "user.email=fixture@example.invalid", "-c", "commit.gpgSign=false",
            "-c", "core.autocrlf=false", "-c", "core.hooksPath=.git/unused-hooks", *args],
            cwd=self.checkout, text=True).strip()

    def cli(self, *args, code=0):
        result = subprocess.run([sys.executable, "-B", "project_run.py", *map(str, args)],
            cwd=self.checkout, env=self.env, stdin=subprocess.DEVNULL, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, code, (result.stdout, result.stderr))
        # retry also prints a diagnostics summary, and redirected parent output
        # may flush after the child. Identify the leaderboard result by content.
        return next(row for line in result.stdout.splitlines() if line.startswith(b"{")
                    and "diagnostics" not in (row := json.loads(line)))

    def review(self, name):
        relative = Path("output") / name / "full"
        directory = self.checkout / relative
        directory.mkdir(parents=True)
        report = report_fixture(self.rule)
        report["student_snapshot"] = capture_student_snapshot(self.checkout)
        package_fixture(directory / "score_evidence", self.rule, report)
        (directory / "result.json").write_bytes(b'{"test_only_native_fixture":true}')
        immutable = directory / "score_evidence/FULL_MISSION_EVALUATION.json"
        supervisor = {"scope": "full_mission", "contents": "beads", "difficulty": "easy",
            "rule_digest": self.rule["rule_digest"], "student_snapshot": report["student_snapshot"],
            "review_accepted": True, "physical_transport_accepted": True, "full_mission_passed": True,
            "score_export": {"run_id": report["run_id"], "evaluation_sha256": hashlib.sha256(immutable.read_bytes()).hexdigest()},
            "finished_at_utc": "2026-11-19T00:00:00+00:00"}
        (directory / "REVIEW_SUPERVISOR.json").write_bytes(canonical_bytes(supervisor))
        return relative, report["run_id"]

    def record(self, review):
        return self.cli("record", "--rule", "synthetic_rule.json", "--review-dir", review)

    def select(self, review, *options, code=0):
        return self.cli("select", "--review-dir", review, "--commit", self.commit, *options, code=code)

    def test_recover_retry_move_and_select_the_intended_success(self):
        first, first_id = self.review("first")
        original_error = self.checkout / first / "LEADERBOARD_STATUS.json"
        original_error.write_bytes(b'{"status":"recording_error"}\n')
        original = {p.relative_to(self.checkout): p.read_bytes()
                    for p in (self.checkout / first).rglob("*") if p.is_file()}
        self.assertEqual(self.select(first, code=2)["code"], "LOCAL_RECORD_MISSING")
        self.offline = True
        self.assertEqual(self.record(first)["status"], "pending")
        queued = QUEUE / (first_id + ".json")
        queue_bytes = (self.checkout / queued).read_bytes()
        self.offline = False
        retry = self.cli("retry")
        self.assertEqual([(x["run_id"], x["status"]) for x in retry["attempts"]], [(first_id, "uploaded")])

        second, second_id = self.review("other")
        uploaded = self.record(second)
        self.assertEqual((uploaded["status"], uploaded["run_id"]), ("uploaded", second_id))
        # Original successful automatic receipts used absolute paths. Moving a checkout
        # also leaves the historical supervisor's export paths stale in real reviews.
        old_status = self.checkout / second / "LEADERBOARD_STATUS.json"
        old_status.write_bytes(canonical_bytes(uploaded))
        moved = self.checkout.with_name("moved-team")
        self.checkout.rename(moved)
        self.checkout = moved
        for review, run in ((second, second_id), (first, first_id)):
            selected = self.select(review)
            self.assertEqual((selected["status"], selected["run_id"], selected["commit_id"]),
                             ("selected", run, self.commit))
        history = self.cli("history", "--rule-id", self.rule["rule_id"])["records"]
        self.assertEqual([(r["run_id"], r["selected"]) for r in history], [(second_id, False), (first_id, True)])
        self.assertEqual(self.attempts.board(self.rule, "selected")["rows"][0]["run_id"], first_id)
        self.assertEqual((self.checkout / queued).read_bytes(), queue_bytes)
        self.assertEqual({p: (self.checkout / p).read_bytes() for p in original}, original)
        self.assertEqual((self.checkout / second / "LEADERBOARD_STATUS.json").read_bytes(), canonical_bytes(uploaded))
        self.assertEqual((self.checkout / self.student).read_bytes(), SOURCE)

    def test_recovery_without_status_rejects_unrelated_record_and_changed_commit(self):
        first, first_id = self.review("first")
        self.assertEqual(self.record(first)["status"], "uploaded")
        self.assertFalse((self.checkout / first / "LEADERBOARD_STATUS.json").exists())
        self.assertEqual(self.select(first)["run_id"], first_id)
        second, second_id = self.review("second")
        self.record(second)
        wrong_queue = self.checkout / "wrong-queue"
        wrong_queue.mkdir()
        shutil.copy2(self.checkout / QUEUE / (second_id + ".json"), wrong_queue / (first_id + ".json"))
        before = len(self.requests)
        self.assertEqual(self.select(first, "--queue-dir", wrong_queue, code=2)["code"], "SELECTION_REVIEW_MISMATCH")
        (self.checkout / self.student).write_bytes(SOURCE + b"# changed\n")
        self.git("add", "student")
        self.git("commit", "-qm", "Synthetic changed snapshot")
        self.commit = self.git("rev-parse", "HEAD")
        self.assertEqual(self.select(first, code=2)["code"], "SELECTION_COMMIT_MISMATCH")
        self.assertEqual(len(self.requests), before)


if __name__ == "__main__":
    unittest.main()
