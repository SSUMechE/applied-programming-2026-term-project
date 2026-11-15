"""Internal server checks for public ID masking. No network or simulation."""
from copy import deepcopy
from io import BytesIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from wsgiref.util import setup_testing_defaults

from leaderboard.server import create_app
from diagnostics.public_identity import PublicIdentityApplication, public_board_rows
from leaderboard.v3 import RULE_SCHEMA


def request(app, path, query="", token=None):
    env = {}
    setup_testing_defaults(env)
    env.update(REQUEST_METHOD="GET", PATH_INFO=path, QUERY_STRING=query,
               CONTENT_LENGTH="0", **{"wsgi.input": BytesIO()})
    if token:
        env["HTTP_AUTHORIZATION"] = "Bearer " + token
    captured = {}

    def start(status, headers):
        captured["status"] = int(status.split()[0])
        captured["headers"] = dict(headers)

    captured["raw"] = b"".join(app(env, start))
    captured["value"] = json.loads(captured["raw"])
    return captured


class PublicIdentityPrivacyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.core = create_app(Path(self.temp.name) / "board.sqlite")
        self.app = PublicIdentityApplication(self.core)
        # Synthetic IDs only. Full IDs remain the authenticated account identity.
        self.team = "20260001-20260002"
        self.token = self.app.store.provision_team(self.team, self.team)
        self.rule = dict(schema_version=RULE_SCHEMA, rule_id="ap2026-easy-beads-term-v6",
                         rule_digest="a" * 64, dt_s=.0025, difficulty="easy")
        self.row = dict(team_id=self.team, display_name=self.team, rank=1,
                        carried_distance_mm=1250, completion_ticks=400,
                        simulation_seconds=1., C=0., C_exact=[0, 1],
                        competition_adjustment=5., competition_adjustment_exact=[5, 1],
                        population_n=1, valid_carry_count=1, required_carry_count=1,
                        outcome="SUCCESS", run_id=self.team + "-run",
                        submission_id=self.team + "-submission",
                        commit_id="1" * 40, student_snapshot_sha256="2" * 64,
                        unexpected_private_field={"repository": self.team})
        self.rule_patch = patch.object(self.core, "_rule", return_value=self.rule)
        self.rule_patch.start()
        self.addCleanup(self.rule_patch.stop)

    def assert_public_row(self, row):
        self.assertEqual(row["display_name"], "****0001-****0002")
        for field in ("team_id", "run_id", "submission_id", "commit_id",
                      "student_snapshot_sha256", "unexpected_private_field"):
            self.assertNotIn(field, row)
        for field in ("rank", "carried_distance_mm", "completion_ticks", "simulation_seconds",
                      "C", "C_exact", "competition_adjustment", "competition_adjustment_exact",
                      "population_n", "valid_carry_count", "required_carry_count", "outcome"):
            self.assertEqual(row[field], self.row[field])

    def test_name_mask_preserves_suffix_zeros_and_historic_nicknames(self):
        cases = {
            "20260001": "****0001",
            "20260001-20260002-20260003": "****0001-****0002-****0003",
            "Team 20260001 / Student20260002": "Team ****0001 / Student****0002",
            "12345": "****2345",
            "0001": "0001",
            "Team Panda": "Team Panda",
        }
        for name, expected in cases.items():
            with self.subTest(name=name):
                self.assertEqual(public_board_rows([{"display_name": name}])[0]["display_name"], expected)

    def test_public_v3_live_selected_staging_and_frozen(self):
        for view in ("live", "selected"):
            for enabled in (True, False):
                for frozen in (True, False):
                    with self.subTest(view=view, enabled=enabled, frozen=frozen):
                        board = dict(rows=[deepcopy(self.row)] if enabled else [],
                                     staging_rows=[] if enabled else [deepcopy(self.row)],
                                     enabled=enabled, view=view, frozen=frozen,
                                     population_sha256="f" * 64)
                        before = deepcopy(board)
                        with patch.object(self.app.attempts, "board", return_value=board):
                            response = request(self.app, "/api/v3/board",
                                               "rule_id=" + self.rule["rule_id"] + "&view=" + view)
                        self.assertEqual(response["status"], 200)
                        self.assertEqual(response["headers"]["Cache-Control"], "no-store")
                        self.assertNotIn(b"20260001", response["raw"])
                        self.assertNotIn(b"20260002", response["raw"])
                        self.assert_public_row(response["value"]["rows" if enabled else "staging_rows"][0])
                        self.assertEqual(response["value"]["population_sha256"], "f" * 64)
                        self.assertEqual(board, before, "Publication must not mutate stored/private rows.")

    def test_v3_legacy_url_alias_masks_both_row_sets(self):
        board = dict(rows=[deepcopy(self.row)], staging_rows=[deepcopy(self.row)],
                     enabled=True, frozen=False)
        with patch.object(self.app.attempts, "board", return_value=board):
            response = request(self.app, "/api/leaderboard", "rule_id=" + self.rule["rule_id"])
        self.assertEqual(response["status"], 200)
        for field in ("rows", "staging_rows"):
            self.assert_public_row(response["value"][field][0])
        self.assertNotIn(self.team.encode(), response["raw"])

    def test_legacy_time_only_board_masks_identity_and_unneeded_metadata(self):
        legacy_rule = dict(self.rule, schema_version="legacy-test-rule", qualified=True)
        rows = [deepcopy(self.row)]
        with patch.object(self.core, "_rule", return_value=legacy_rule), \
                patch.object(self.app.store, "leaderboard", return_value=rows):
            response = request(self.app, "/api/leaderboard", "rule_id=legacy-test")
        self.assertEqual(response["status"], 200)
        self.assert_public_row(response["value"]["rows"][0])
        self.assertNotIn(self.team.encode(), response["raw"])

    def test_matching_suffixes_do_not_merge_teams_or_change_scores(self):
        other = dict(self.row, team_id="20270001-20270002",
                     display_name="20270001-20270002", rank=2,
                     carried_distance_mm=2000, C=1., C_exact=[1, 1])
        board = dict(rows=[deepcopy(self.row), other], staging_rows=[], enabled=True)
        with patch.object(self.app.attempts, "board", return_value=board):
            response = request(self.app, "/api/v3/board", "rule_id=" + self.rule["rule_id"])
        rows = response["value"]["rows"]
        self.assertEqual(len(rows), 2)
        self.assertEqual([row["display_name"] for row in rows], ["****0001-****0002"] * 2)
        self.assertEqual([row["rank"] for row in rows], [1, 2])
        self.assertEqual([row["carried_distance_mm"] for row in rows], [1250, 2000])

    def test_private_history_keeps_full_identity_and_authentication(self):
        history = dict(records=[deepcopy(self.row)], selected=None, next_before_sequence=None)
        with patch.object(self.app.attempts, "history", return_value=history) as get_history:
            anonymous = request(self.app, "/api/v3/history", "rule_id=" + self.rule["rule_id"])
            get_history.assert_not_called()
            response = request(self.app, "/api/v3/history",
                               "rule_id=" + self.rule["rule_id"], token=self.token)
            get_history.assert_called_once_with(self.team, self.rule, before_sequence=None, limit=100)
        self.assertEqual(anonymous["status"], 401)
        self.assertEqual(response["status"], 200)
        self.assertEqual(response["value"], history)
        self.assertEqual(self.app.store.authenticate_token(self.token)["display_name"], self.team)

    def test_private_attempt_is_unmodified(self):
        attempt = dict(rule_id=self.rule["rule_id"], private_metadata={"team": self.team})
        with patch.object(self.app.attempts, "get_attempt", return_value=attempt) as get_attempt:
            response = request(self.app, "/api/v3/attempt", "run_id=synthetic", token=self.token)
            get_attempt.assert_called_once_with(self.team, "synthetic")
        self.assertEqual(response["status"], 200)
        self.assertEqual(response["value"], attempt)

    def test_malformed_public_responses_fail_closed(self):
        cases = (
            ("200 OK", "application/json", b'{"rows":'),
            ("200 OK", "application/json", b'{"rows":"20260001"}'),
            ("200 OK", "application/json", b'{"rows":[{"display_name":null}]}'),
            ("200 OK", "text/html", b"<p>20260001</p>"),
            ("500 Internal Server Error", "text/plain", b"20260001 internal error"),
            ("404 Not Found", "application/json", b'{"error":"20260001"}'),
        )
        for status, media, body in cases:
            with self.subTest(status=status, media=media, body=body):
                def raw_app(environ, start_response):
                    start_response(status, [("Content-Type", media), ("Content-Length", str(len(body)))])
                    return [body]
                response = request(PublicIdentityApplication(raw_app), "/api/v3/board")
                self.assertEqual(response["status"], 503 if status.startswith("200") else int(status[:3]))
                self.assertNotIn(b"20260001", response["raw"])
                self.assertEqual(response["value"]["error"]["code"], "PUBLIC_BOARD_UNAVAILABLE")
                self.assertEqual(int(response["headers"]["Content-Length"]), len(response["raw"]))

    def test_actual_server_factory_installs_projection_and_preserves_private_interfaces(self):
        from diagnostics.server import create_app as create_classroom_app
        root = Path(self.temp.name)
        secret = root / "instructor.secret"
        secret.write_text("synthetic-instructor-secret-" * 3, encoding="ascii")
        app = create_classroom_app(root / "classroom.sqlite", root / "diagnostics.sqlite",
                                   secret, public_origin="https://class.example.test")
        self.assertIsInstance(app, PublicIdentityApplication)
        self.assertIs(app.leaderboard, app.application.leaderboard)
        self.assertIs(app.enrollment, app.application.enrollment)
        raw = dict(rows=[deepcopy(self.row)], staging_rows=[], enabled=True)
        with patch.object(app.leaderboard, "_rule", return_value=self.rule), \
                patch.object(app.leaderboard.attempts, "board", return_value=raw):
            response = request(app, "/api/v3/board", "rule_id=" + self.rule["rule_id"])
        self.assertEqual(response["status"], 200)
        self.assert_public_row(response["value"]["rows"][0])


if __name__ == "__main__":
    unittest.main()
