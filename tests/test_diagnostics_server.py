"""Internal ungraded support checks. In-process WSGI/temporary SQLite only."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import copy
from io import BytesIO
import json
from pathlib import Path
import secrets
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch
import uuid
from urllib.parse import urlencode
from wsgiref.util import setup_testing_defaults

from diagnostics.protocol import (CATEGORIES, SCHEMA_VERSION, DiagnosticError, canonical_bytes,
    decode_json, report_signature, sanitize_text, validate_report)
from diagnostics.server import COOKIE, create_app
from leaderboard.store import Store


def example():
    return dict(schema_version=SCHEMA_VERSION, report_id=str(uuid.uuid4()), occurred_at_utc="2026-11-14T00:00:00+00:00",
        command=dict(kind="plan", exit_code=1, duration_s=3.5, arguments={"seed": 71, "output": "output/example", "cuda": False}),
        source=dict(package_version="diagnostics-v1", git_commit="1"*40, evaluation_sources_sha256="2"*64,
            student_snapshot_sha256=None, dirty=False),
        context=dict(difficulty="hard", phase="arm_2__carry", rule_id="ap2026-hard-beads-term-v3",
            run_directory="output/example", contents="beads", case_id="hard_industrial_v1", saved_plan_sha256=None, native_run_id=None),
        failure=dict(category="planning_failure", code="CPU_PLAN_FAILED", exception_type="ContractError",
            message="Planner returned no bounded path.", traceback_tail=""),
        environment=dict(python="3.12", platform="Windows", torch=None, genesis=None, gpu_name=None))


def request(app, path, *, method="GET", value=None, raw=None, token=None, cookie=None, origin=None, query="", headers=None):
    env = {}
    setup_testing_defaults(env)
    env.update(REQUEST_METHOD=method, PATH_INFO=path, QUERY_STRING=query, **{"wsgi.url_scheme": "https"})
    if raw is None:
        raw = b"" if value is None else canonical_bytes(value)
    env.update(CONTENT_LENGTH=str(len(raw)), CONTENT_TYPE="application/json", **{"wsgi.input": BytesIO(raw)})
    if token is not None:
        env["HTTP_AUTHORIZATION"] = "Bearer "+token
    if cookie is not None:
        env["HTTP_COOKIE"] = cookie
    if origin is not None:
        env["HTTP_ORIGIN"] = origin
    env.update(headers or {})
    captured = {}
    def start(status, response_headers, exc_info=None):
        captured.update(status=int(status.split()[0]), headers=dict(response_headers))
    body = b"".join(app(env, start))
    captured["raw"] = body
    captured["value"] = json.loads(body) if captured["headers"]["Content-Type"].startswith("application/json") else body.decode("utf-8")
    return captured


class DiagnosticsServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.board_path, self.diag_path = self.root/"board.sqlite", self.root/"diagnostics.sqlite"
        board = Store(self.board_path)
        self.team_a = board.provision_team("team-a")
        self.team_b = board.provision_team("team-b")
        self.admin = secrets.token_urlsafe(48)
        self.secret_file = self.root/"instructor.secret"
        self.secret_file.write_text(self.admin, encoding="ascii")
        self.origin = "https://class.example.test"
        self.app = create_app(self.board_path, self.diag_path, self.secret_file, public_origin=self.origin)

    def post(self, report, token=None):
        return request(self.app, "/api/diagnostics/reports", method="POST", value=report, token=token or self.team_a)

    def login(self):
        result = request(self.app, "/instructor/login", method="POST", value={"token": self.admin}, origin=self.origin)
        self.assertEqual(result["status"], 200)
        return result["headers"]["Set-Cookie"].split(";", 1)[0]

    def detail(self, report, team="team-a", **kwargs):
        return request(self.app, "/api/instructor/issues/detail", query=urlencode(dict(team_id=team, report_id=report["report_id"])), **kwargs)

    def test_team_isolation_and_server_derived_identity(self):
        report = example()
        self.assertEqual(request(self.app, "/api/diagnostics/reports", method="POST", value=report)["status"], 401)
        self.assertEqual(self.post(report)["status"], 201)
        own = request(self.app, "/api/diagnostics/own", token=self.team_a)["value"]["reports"]
        self.assertEqual(len(own), 1)
        self.assertEqual(own[0]["team_id"], "team-a")
        self.assertEqual(request(self.app, "/api/diagnostics/own", token=self.team_b)["value"]["reports"], [])
        self.assertEqual(request(self.app, "/api/diagnostics/own", token=self.team_b, query="team_id=team-a")["status"], 400)
        forged = {**example(), "team_id": "team-b"}
        self.assertEqual(self.post(forged)["status"], 400)
        self.assertEqual(self.detail(report, token=self.team_a)["status"], 401)
        self.assertEqual(self.detail(report, token=self.admin)["status"], 200)
        self.assertEqual(self.post(example(), token=self.admin)["status"], 401)

    def test_idempotency_persistence_and_per_team_namespace(self):
        report = example()
        first = self.post(report)
        duplicate = self.post(copy.deepcopy(report))
        self.assertEqual((first["status"], duplicate["status"]), (201, 200))
        self.assertTrue(duplicate["value"]["duplicate"])
        self.assertEqual(first["value"]["sequence"], duplicate["value"]["sequence"])
        changed = copy.deepcopy(report); changed["failure"]["message"] = "Different immutable report"
        self.assertEqual(self.post(changed)["status"], 409)
        self.assertEqual(self.post(report, self.team_b)["status"], 201)
        restarted = create_app(self.board_path, self.diag_path, self.secret_file, public_origin=self.origin)
        self.assertEqual(len(request(restarted, "/api/diagnostics/own", token=self.team_a)["value"]["reports"]), 1)
        with closing(sqlite3.connect(self.diag_path)) as db:
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("UPDATE reports SET body_json='{}'")

    def test_concurrent_identical_and_distinct_reports(self):
        report = example()
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: self.post(copy.deepcopy(report)), range(16)))
        self.assertEqual([r["status"] for r in results].count(201), 1)
        self.assertEqual([r["status"] for r in results].count(200), 15)
        self.assertEqual(len({r["value"]["sequence"] for r in results}), 1)
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(self.post, [example() for _ in range(12)]))
        self.assertTrue(all(r["status"] == 201 for r in results))
        self.assertEqual(len(request(self.app, "/api/diagnostics/own", token=self.team_a)["value"]["reports"]), 13)

    def test_cookie_login_origin_expiry_tamper_and_logout(self):
        for origin in (None, "https://evil.example", "null", "http://class.example.test"):
            with self.subTest(origin=origin):
                self.assertEqual(request(self.app, "/instructor/login", method="POST", value={"token": self.admin}, origin=origin)["status"], 403)
        for bad in (self.team_a, "wrong", "비밀"):
            self.assertEqual(request(self.app, "/instructor/login", method="POST", value={"token": bad}, origin=self.origin)["status"], 401)
        login = request(self.app, "/instructor/login", method="POST", raw=urlencode({"token": self.admin}).encode(), origin=self.origin,
            headers={"CONTENT_TYPE": "application/x-www-form-urlencoded"})
        self.assertEqual(login["status"], 200)
        header = login["headers"]["Set-Cookie"]
        for value in ("HttpOnly", "Secure", "SameSite=Strict", "Path=/"):
            self.assertIn(value, header)
        self.assertNotIn(self.admin, header+login["raw"].decode())
        cookie = header.split(";", 1)[0]
        self.assertEqual(request(self.app, "/api/instructor/issues/list", cookie=cookie)["status"], 200)
        self.assertEqual(request(self.app, "/api/instructor/issues/list", cookie=cookie+"x")["status"], 401)
        self.assertEqual(request(self.app, "/api/instructor/issues/list", cookie=cookie, origin="https://evil.example")["status"], 403)
        with patch("diagnostics.server.time.time", return_value=time.time()+4000):
            self.assertEqual(request(self.app, "/api/instructor/issues/list", cookie=cookie)["status"], 401)
        cookie = self.login()
        self.assertEqual(request(self.app, "/instructor/logout", method="POST", cookie=cookie)["status"], 403)
        self.assertEqual(request(self.app, "/instructor/logout", method="POST", cookie=cookie, origin=self.origin)["status"], 200)
        self.assertEqual(request(self.app, "/api/instructor/issues/list", cookie=cookie)["status"], 401)

    def test_status_audit_is_private_and_attempt_body_immutable(self):
        report = example(); original = self.post(report)["value"]["body_sha256"]
        update = dict(team_id="team-a", report_id=report["report_id"], status="investigating", fixed_commit=None, note="Checking the supplied API.")
        cookie = self.login()
        for kwargs, expected in [({"token": self.team_a}, 401), ({"cookie": cookie}, 403),
            ({"cookie": cookie, "origin": "https://evil.example"}, 403)]:
            self.assertEqual(request(self.app, "/api/instructor/issues/status", method="POST", value=update, **kwargs)["status"], expected)
        ok = request(self.app, "/api/instructor/issues/status", method="POST", value=update, cookie=cookie, origin=self.origin)
        self.assertEqual(ok["status"], 200)
        self.assertEqual(len(ok["value"]["status_history"]), 1)
        update.update(status="resolved", fixed_commit="c"*40, note="Supplied fix available.")
        ok = request(self.app, "/api/instructor/issues/status", method="POST", value=update, token=self.admin)
        self.assertEqual(ok["status"], 200)
        self.assertEqual(len(ok["value"]["status_history"]), 2)
        duplicate = request(self.app, "/api/instructor/issues/status", method="POST", value=update, token=self.admin)
        self.assertFalse(duplicate["value"]["changed"])
        self.assertEqual(ok["value"]["attempt"]["body_sha256"], original)
        self.assertEqual(ok["value"]["attempt"]["report"], validate_report(report))
        self.assertEqual(request(self.app, "/api/diagnostics/own", token=self.team_a)["value"]["reports"][0]["status"], "resolved")
        for key, value in [("status", "deleted"), ("fixed_commit", "x"), ("note", "x"*2001)]:
            invalid = {**update, key: value}
            self.assertEqual(request(self.app, "/api/instructor/issues/status", method="POST", value=invalid, token=self.admin)["status"], 400)

    def test_groups_filters_and_own_pagination(self):
        reports = [example() for _ in range(3)]
        reports[1]["failure"]["message"] = "Later failure at step 9999"
        reports[1]["source"]["git_commit"] = "f"*40
        reports[1]["command"]["arguments"]["output"] = "output/another"
        self.assertEqual(report_signature(reports[0]), report_signature(reports[1]))
        self.post(reports[0]); self.post(reports[1], self.team_b)
        reports[2]["source"]["package_version"] = "diagnostics-v2"; self.post(reports[2])
        all_groups = request(self.app, "/api/instructor/issues/list", token=self.admin)["value"]["issues"]
        self.assertEqual(len(all_groups), 2)
        grouped = next(item for item in all_groups if item["version"] == "diagnostics-v1")
        self.assertEqual((grouped["occurrences"], grouped["teams"]), (2, 2))
        for query in ("team_id=team-b", "version=diagnostics-v2", "category=planning_failure&version=diagnostics-v2", "status=new&team_id=team-b"):
            self.assertEqual(len(request(self.app, "/api/instructor/issues/list", token=self.admin, query=query)["value"]["issues"]), 1)
        first = request(self.app, "/api/diagnostics/own", token=self.team_a, query="limit=1")["value"]
        second = request(self.app, "/api/diagnostics/own", token=self.team_a, query="limit=1&before="+str(first["next_before"]))["value"]
        self.assertNotEqual(first["reports"][0]["report_id"], second["reports"][0]["report_id"])
        self.assertIsNone(second["next_before"])

    def test_bounded_strict_schema_and_http_bodies(self):
        mutations = [lambda r:r.update(extra="field"), lambda r:r["command"].update(exit_code=0),
            lambda r:r["command"].update(exit_code=True), lambda r:r["command"].update(duration_s=10**1000),
            lambda r:r["command"]["arguments"].update(token="secret"), lambda r:r["command"]["arguments"].update(seed=[]),
            lambda r:r["context"].update(run_directory="../private"), lambda r:r["context"].update(run_directory="C:/private"),
            lambda r:r["source"].update(git_commit="short"), lambda r:r["failure"].update(category="success"),
            lambda r:r["failure"].update(message="x"*2049), lambda r:r["environment"].update(extra="field")]
        for mutate in mutations:
            value=example(); mutate(value)
            with self.subTest(mutation=mutate): self.assertEqual(self.post(value)["status"], 400)
        for raw in (b'{"schema_version":1,"schema_version":2}', b'{"x":NaN}', b'\xff', b'['*1000+b']'*1000):
            self.assertEqual(request(self.app,"/api/diagnostics/reports",method="POST",raw=raw,token=self.team_a)["status"],400)
        for headers, expected in [({"CONTENT_LENGTH":""},411),({"HTTP_TRANSFER_ENCODING":"chunked"},411),
            ({"CONTENT_LENGTH":"32769"},413),({"CONTENT_TYPE":"text/plain"},415),({"CONTENT_LENGTH":"5"},400)]:
            self.assertEqual(request(self.app,"/api/diagnostics/reports",method="POST",raw=b'{}',token=self.team_a,headers=headers)["status"],expected)
        for query in ("limit=0", "limit=101", "limit=1&limit=2", "before=-1", "unknown=x"):
            self.assertEqual(request(self.app,"/api/diagnostics/own",token=self.team_a,query=query)["status"],400)

    def test_all_unique_groups_and_repeated_attempts_are_pageable(self):
        repeated = [example() for _ in range(103)]
        for report in repeated:
            self.assertEqual(self.post(report)["status"], 201)
        for index in range(104):
            report = example(); report["failure"]["code"] = "DISTINCT_"+str(index)
            self.assertEqual(self.post(report)["status"], 201)
        groups, cursor = [], None
        while True:
            query = {"limit": 100}
            if cursor is not None:
                query["before"] = cursor
            result = request(self.app, "/api/instructor/issues/list", token=self.admin, query=urlencode(query))["value"]
            groups.extend(result["issues"])
            cursor = result["next_before"]
            if cursor is None:
                break
        self.assertEqual(len(groups), 105)
        self.assertEqual(len({item["signature"] for item in groups}), 105)
        grouped = next(item for item in groups if item["code"] == "CPU_PLAN_FAILED")
        self.assertEqual(grouped["occurrences"], 103)
        attempts, cursor = [], None
        while True:
            query = dict(team_id="team-a", report_id=repeated[0]["report_id"])
            if cursor is not None:
                query["before"] = cursor
            result = request(self.app, "/api/instructor/issues/detail", token=self.admin, query=urlencode(query))["value"]
            self.assertEqual(result["attempt"]["report_id"], repeated[0]["report_id"])
            attempts.extend(result["related_attempts"])
            cursor = result["related_next_before"]
            if cursor is None:
                break
        self.assertEqual({item["report_id"] for item in attempts}, {item["report_id"] for item in repeated})
        self.assertEqual(len(attempts), 103)
        for path in ("/api/instructor/issues/list", "/api/instructor/issues/detail"):
            self.assertEqual(request(self.app, path, token=self.admin, query="before=-1")["status"], 400)

    def test_categories_and_windows_interruption_codes(self):
        for category in CATEGORIES:
            report=example(); report["failure"]["category"]=category
            report["command"]["kind"]="test" if category=="test_failure" else "review"
            report["command"]["exit_code"]=-1073741510 if category=="interrupted" else 1
            self.assertEqual(self.post(report)["status"],201)
        report=example();report["command"]["exit_code"]=4294967295
        self.assertEqual(self.post(report)["status"],201)

    def test_xss_credentials_and_no_public_details(self):
        report=example(); attack='<img src=x onerror="alert(1)"><script>alert(2)</script>'
        report["failure"]["message"]=attack+" Bearer "+self.team_a+" token="+self.admin
        report["failure"]["traceback_tail"]="\x1b[31mOops\x1b[0m\x00 "+self.team_a
        self.assertEqual(self.post(report)["status"],201)
        detail=self.detail(report,token=self.admin)
        self.assertIn(attack,detail["value"]["attempt"]["report"]["failure"]["message"])
        self.assertNotIn(self.team_a,detail["raw"].decode());self.assertNotIn(self.admin,detail["raw"].decode())
        self.assertNotIn("\\u001b",detail["raw"].decode())
        page=request(self.app,"/instructor")
        self.assertEqual(page["status"],200)
        self.assertNotIn(attack,page["value"])
        self.assertNotIn("innerHTML",page["value"]);self.assertNotIn("localStorage",page["value"])
        self.assertIn("textContent",page["value"])
        self.assertIn("script-src 'sha256-",page["headers"]["Content-Security-Policy"])
        self.assertNotIn("script-src 'unsafe-inline'",page["headers"]["Content-Security-Policy"])
        self.assertEqual(page["headers"]["Cache-Control"],"no-store")
        self.assertEqual(request(self.app,"/instructor",query="token="+self.admin)["status"],400)
        for path in ("/api/instructor/issues/list","/api/instructor/issues/detail","/api/diagnostics/own"):
            self.assertEqual(request(self.app,path)["status"],401)
        self.assertEqual(request(self.app,"/api/instructor/issues/list",token="비밀")["status"],401)

    def test_old_routes_are_byte_exact_delegation_and_no_activation(self):
        for path, query, method, value in [("/","","GET",None),("/health","","GET",None),
            ("/api/rules","","GET",None),("/api/v3/history","rule_id=absent","GET",None),
            ("/api/v3/records","","POST",{}),("/unknown","","GET",None)]:
            direct=request(self.app.leaderboard,path,query=query,method=method,value=value,token=self.team_a)
            wrapper=request(self.app,path,query=query,method=method,value=value,token=self.team_a)
            self.assertEqual(wrapper,direct)
        self.post(example())
        with closing(sqlite3.connect(self.board_path)) as db:
            tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertNotIn("reports",tables);self.assertNotIn("status_history",tables)
            self.assertEqual(db.execute("SELECT count(*) FROM rules").fetchone()[0],0)
            self.assertEqual(db.execute("SELECT count(*) FROM submissions").fetchone()[0],0)
        with closing(sqlite3.connect(self.diag_path)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM reports").fetchone()[0],1)

    def test_separate_credentials_and_storage_required(self):
        for origin in ("http://class.example.test","https://class.example.test/path","https://user:pass@class.example.test"):
            with self.assertRaises(ValueError): create_app(self.board_path,self.diag_path,self.secret_file,public_origin=origin)
        with self.assertRaises(ValueError): create_app(self.board_path,self.board_path,self.secret_file,public_origin=self.origin)
        self.secret_file.write_text(self.team_a,encoding="ascii")
        with self.assertRaises(ValueError): create_app(self.board_path,self.diag_path,self.secret_file,public_origin=self.origin)
        self.assertEqual(sanitize_text("token=abc Bearer xyz",200),"token=[redacted] Bearer [redacted]")
        with self.assertRaises(DiagnosticError): decode_json(b'{"a":1,"a":2}')


if __name__ == "__main__":
    unittest.main()
