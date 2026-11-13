"""Instructor checks for the supplied reporter. No student implementation task."""
from contextlib import redirect_stdout, redirect_stderr
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from diagnostics import client as c
from diagnostics.process_tree import OwnedJob


SOURCE = dict(package_version=c.PACKAGE_VERSION, git_commit=None,
              evaluation_sources_sha256=None, student_snapshot_sha256=None, dirty=None)


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def report(self, **changes):
        args = dict(root=self.root, kind="plan", argv=["--difficulty", "hard"],
                    exit_code=1, duration_s=1.5, tail="ValueError: invalid angle\n",
                    records={}, source=SOURCE)
        args.update(changes)
        return c.make_report(**args)

    def test_exact_command_forwarding_and_success(self):
        argv = ["--task", "cases/a path", "--output=output/run", "--seed", "71"]
        self.assertEqual(c.child_command("plan", argv),
                         [sys.executable, "-B", "-m", "physics.plan_student_transport", *argv])
        self.assertEqual(c.child_command("test", []),
                         [sys.executable, "-B", "-m", "unittest", "tests.test_student_strategy"])
        self.assertEqual(c.option(["--output", "old", "--output=fresh", "--", "--output", "ignored"], "output"), "fresh")
        (self.root / "check_environment.py").write_text(
            "import sys,json\nprint(json.dumps(sys.argv[1:]))\n", encoding="utf-8")
        output = io.StringIO()
        with redirect_stdout(output), patch.object(c, "metadata_source", return_value=SOURCE):
            code = c.run_course(self.root, "check", argv)
        self.assertEqual(code, 0)
        self.assertIn(json.dumps(argv), output.getvalue())
        self.assertFalse((self.root / c.QUEUE).exists())

    def test_failure_exit_and_offline_queue_without_raw_stdout(self):
        secret = "LOCAL_TEST_SECRET_DO_NOT_SHARE"
        (self.root / "check_environment.py").write_text(
            "import sys\nprint('arbitrary student source must never be uploaded')\n"
            "print('password=secret-value')\nraise ValueError('token=" + secret + "')\n",
            encoding="utf-8")
        with patch.dict(os.environ, {"AP_LEADERBOARD_TEAM_TOKEN": secret}), \
                patch.object(c, "upload", side_effect=OSError("offline")), \
                patch.object(c, "metadata_source", return_value=SOURCE), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            code = c.run_course(self.root, "check", [])
        self.assertEqual(code, 1)
        paths = list((self.root / c.QUEUE).glob("*.json"))
        self.assertEqual(len(paths), 1)
        raw = paths[0].read_text(encoding="utf-8")
        self.assertNotIn(secret, raw)
        self.assertNotIn("arbitrary student source", raw)
        self.assertNotIn("raise ValueError", raw)
        self.assertNotIn("secret-value", raw)
        report = json.loads(raw)
        self.assertEqual(report["failure"]["exception_type"], "ValueError")
        self.assertEqual(report["failure"]["category"], "environment_error")

    def test_stale_outputs_are_not_attributed_to_new_failure(self):
        out = self.root / "output/existing"
        out.mkdir(parents=True)
        path = out / "REVIEW_SUPERVISOR.json"
        c.atomic_json(path, {"outcome": "PHYSICAL_REJECTED"})
        files = c.observed_files(self.root, "review", ["--output", str(out)])
        before = {key: c.fingerprint(value) for key, value in files.items()}
        self.assertEqual(c.fresh_records(files, before), {})
        report = self.report(kind="review", tail="review_tier_transport.py: error: Use a fresh review directory\n",
                             records=c.fresh_records(files, before))
        self.assertEqual(report["failure"]["category"], "program_error")
        self.assertIn("fresh review", report["failure"]["message"])
        c.atomic_json(path, {"outcome": "PHYSICAL_REJECTED", "new": True})
        self.assertIn("REVIEW_SUPERVISOR.json", c.fresh_records(files, before))
        log = out / "worker_stdout.log"
        log.write_text('source-code-private\nTypeError: genuine worker failure\n')
        records = c.fresh_records(files, before)
        report = self.report(kind="plan", records=records, tail="")
        self.assertEqual(report["failure"]["exception_type"], "TypeError")
        self.assertNotIn("source-code-private", json.dumps(report))

    def test_console_failure_does_not_abort_or_change_child_exit(self):
        (self.root / "check_environment.py").write_text(
            "import sys\nprint('visible progress')\nsys.exit(int(sys.argv[1]))\n", encoding="utf-8")
        class FailedConsole:
            def __init__(self, failure):
                self.failure = failure
            def write(self, value):
                raise self.failure
            def flush(self):
                pass
        for error in (BrokenPipeError(), UnicodeEncodeError("ascii", "한", 0, 1, "unsupported")):
            for expected in (0, 7):
                with patch.object(sys, "stdout", FailedConsole(error)), \
                        patch.object(sys, "stderr", FailedConsole(error)), \
                        patch.object(c, "metadata_source", return_value=SOURCE), \
                        patch.object(c, "upload", return_value={"status": "pending"}):
                    self.assertEqual(c.run_course(self.root, "check", [str(expected)]), expected)

    def test_interrupt_racing_with_success_keeps_actual_zero(self):
        process = Mock()
        process.returncode = 0
        process.poll.return_value = 0
        process.stdout.readline.side_effect = KeyboardInterrupt()
        with patch.object(c.subprocess, "Popen", return_value=process), \
                patch.object(c, "OwnedJob"), patch.object(c, "metadata_source", return_value=SOURCE), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(c.run_course(self.root, "check", []), 0)
        self.assertFalse((self.root / c.QUEUE).exists())

    def test_categories_and_bounded_stack_metadata(self):
        native = {"REVIEW_SUPERVISOR.json": {"outcome": "PHYSICAL_REJECTED",
                   "worker_failure": {"type": "PhysicalTransportRejected", "message": "control:acceleration_limit"}}}
        self.assertEqual(self.report(kind="review", records=native)["failure"]["category"], "physics_rejection")
        cpu = {"CPU_PLANNING_RESULT.json": {"status": "CPU_PLAN_FAILED",
               "failure": {"type": "PlanningBudgetExceeded", "message": "budget exhausted"}}}
        self.assertEqual(self.report(records=cpu)["failure"]["category"], "planning_failure")
        cpu["CPU_PLANNING_RESULT.json"]["failure"] = {"type": "TypeError", "message": "student function raised"}
        self.assertEqual(self.report(records=cpu)["failure"]["category"], "program_error")
        self.assertEqual(self.report(kind="test")["failure"]["category"], "test_failure")
        self.assertEqual(self.report(tail="NotImplementedError: complete planner\n")["failure"]["code"], "STUDENT_NOT_IMPLEMENTED")
        self.assertEqual(self.report(exit_code=130)["failure"]["category"], "interrupted")
        trace = 'Traceback (most recent call last):\n  File "' + str(self.root / "student/plan.py") + '", line 9, in plan\n    secret_source_here()\nValueError: invalid\n'
        report = self.report(tail=trace)
        self.assertIn("<project>", report["failure"]["traceback_tail"])
        self.assertNotIn(str(self.root), json.dumps(report))
        self.assertNotIn("secret_source_here", json.dumps(report))
        for private_path in (r"C:\Users\First Last\private\plan.py", r"\\server\private-share\student-name\plan.py"):
            trace = '  File "' + private_path + '", line 9, in plan\nValueError: bad input\n'
            report = self.report(tail=trace)
            self.assertIn("<path>/plan.py", report["failure"]["traceback_tail"])
            self.assertNotIn("First Last", json.dumps(report))
            self.assertNotIn("private-share", json.dumps(report))
        env = {"schema_version": "applied-programming-environment-check-v1",
               "status": "ENVIRONMENT_METADATA_MISMATCH",
               "package_versions": [{"package": "torch", "expected": "2.14.0+cu126", "actual": "wrong", "passed": False}],
               "SDK_source_checks": [{"source": "genesis/example.py", "passed": False}],
               "platform_checks": {"Windows": False}}
        report = self.report(kind="check", records={"environment": env})
        self.assertEqual(report["failure"]["code"], "ENVIRONMENT_METADATA_MISMATCH")
        self.assertIn("torch: expected 2.14.0+cu126, found wrong", report["failure"]["message"])
        self.assertIn("SDK source mismatch", report["failure"]["message"])
        self.assertIn("Platform mismatch", report["failure"]["message"])

    def test_http_retry_is_idempotent_and_requires_ack_hash(self):
        seen = {}
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                raw = self.rfile.read(int(self.headers["Content-Length"]))
                value = json.loads(raw)
                key = value["report_id"]
                duplicate = key in seen
                seen[key] = raw
                body = c.canonical_bytes(dict(status="accepted", report_id=key, sequence=1,
                     duplicate=duplicate, body_sha256=hashlib.sha256(raw).hexdigest()))
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        report = self.report()
        path = self.root / c.QUEUE / (report["report_id"] + ".json")
        c.atomic_json(path, report)
        config = {"server": f"http://127.0.0.1:{server.server_port}", "token": "test-token", "token_file": None}
        before = path.read_bytes()
        first = c.upload(path, config)
        second = c.upload(path, config)
        self.assertEqual(first["status"], "uploaded")
        self.assertFalse(first["duplicate"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(len(seen), 1)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(c.retry(self.root, config), [])
        path.with_suffix(".status.json").unlink()
        config["token"] = ""
        self.assertEqual(c.retry(self.root, config)[0]["reason"], "TEAM_TOKEN_REQUIRED")
        largest = self.report()
        largest["failure"].update(message="", traceback_tail="")
        remaining = 32768 - len(c.canonical_bytes(largest))
        largest["failure"].update(traceback_tail="🧪" * (remaining // 4), message="x" * (remaining % 4))
        self.assertEqual(len(c.canonical_bytes(largest)), 32768)
        maximum_path = self.root / c.QUEUE / (largest["report_id"] + ".json")
        c.atomic_json(maximum_path, largest)
        self.assertEqual(maximum_path.stat().st_size, 32769)
        config["token"] = "test-token"
        self.assertEqual(c.upload(maximum_path, config)["status"], "uploaded")

    def test_configuration_path_only_and_unsafe_url_rejected(self):
        token = Path(self.tmp.name).parent / (self.root.name + ".token")
        token.write_text("private-team-token", encoding="utf-8")
        self.addCleanup(token.unlink)
        with patch.object(c, "ROOT", self.root), redirect_stdout(io.StringIO()):
            self.assertEqual(c.main(["configure", "--token-file", str(token)]), 0)
        raw = (self.root / c.CONFIG).read_text()
        self.assertNotIn("private-team-token", raw)
        self.assertEqual(c.configuration(self.root)["token"], "private-team-token")
        for value in ("http://example.org", "https://token@example.org", "https://example.org/x", "https://example.org?secret=x"):
            with self.assertRaises(ValueError):
                c.server_url(value)

    @unittest.skipUnless(os.name == "nt", "Windows command-tree ownership")
    def test_owned_job_terminates_only_its_child_and_grandchild(self):
        job = OwnedJob()
        script = self.root / "parent.py"
        marker = self.root / "grandchild.pid"
        script.write_text(
            "import subprocess,sys,time\nfrom pathlib import Path\n"
            "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(120)'])\n"
            "Path(sys.argv[1]).write_text(str(p.pid))\ntime.sleep(120)\n", encoding="utf-8")
        process = subprocess.Popen([sys.executable, str(script), str(marker)],
                                   creationflags=subprocess.CREATE_NO_WINDOW | 0x4)
        try:
            job.assign(process)
            job.resume(process)
            deadline = time.monotonic() + 10
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(.05)
            self.assertTrue(marker.exists())
            import ctypes
            from ctypes import wintypes
            api = ctypes.WinDLL("kernel32", use_last_error=True)
            api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            api.OpenProcess.restype = wintypes.HANDLE
            api.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            api.WaitForSingleObject.restype = wintypes.DWORD
            api.CloseHandle.argtypes = [wintypes.HANDLE]
            handle = api.OpenProcess(0x100000, False, int(marker.read_text()))
            self.assertTrue(handle)
            try:
                self.assertEqual(api.WaitForSingleObject(handle, 0), 258)
                job.close()
                process.wait(timeout=5)
                self.assertEqual(api.WaitForSingleObject(handle, 5000), 0)
            finally:
                api.CloseHandle(handle)
        finally:
            job.close()
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)

    @unittest.skipUnless(os.name == "nt", "Windows DWORD exit status")
    def test_outer_python_preserves_native_dword_exit_status(self):
        (self.root / "check_environment.py").write_text(
            "import ctypes,sys\nctypes.windll.kernel32.ExitProcess(int(sys.argv[1]))\n", encoding="utf-8")
        script = ("import sys; from pathlib import Path; from diagnostics.client import run_course,exit_status; "
                  "sys.exit(exit_status(run_course(Path(sys.argv[1]),'check',[sys.argv[2]])))")
        env = os.environ.copy()
        for key in ("AP_LEADERBOARD_TEAM_TOKEN", "AP_LEADERBOARD_TOKEN_FILE"):
            env.pop(key, None)
        for code in (7, 3221225786, 4294967295):
            result = subprocess.run([sys.executable, "-B", "-c", script, str(self.root), str(code)],
                                    cwd=c.ROOT, env=env, capture_output=True, timeout=20)
            self.assertEqual(result.returncode, code, result.stderr.decode(errors="replace"))


if __name__ == "__main__":
    unittest.main()
