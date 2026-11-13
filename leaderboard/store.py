"""SQLite persistence for source-matched student-run leaderboard receipts.

This module does not evaluate physics or promote a receipt to native proof.
The HTTP boundary must validate submissions with the supplied contracts first.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import hmac
import json
from pathlib import Path
import re
import secrets
import sqlite3

from .contracts import canonical_bytes


TRUST_LABEL = "student_run_source_matched"
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
DIGEST = re.compile(r"[0-9a-f]{64}\Z")


class StoreError(ValueError):
    pass


class Conflict(StoreError):
    pass


class NotFound(StoreError):
    pass


class Unauthorized(StoreError):
    pass


def canonical(value):
    return canonical_bytes(value).decode("utf-8")


def token_digest(token):
    return hashlib.sha256(b"applied-programming-team-token-v1\0" + token.encode("utf-8")).hexdigest()


def identifier(value, label):
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise StoreError(f"Invalid {label}")
    return value


def rule_digest(value):
    if not isinstance(value, str) or not DIGEST.fullmatch(value):
        raise StoreError("Invalid rule digest")
    return value


def utc_now():
    return datetime.now(timezone.utc).isoformat()


class Store:
    """One SQLite file, short independent connections and atomic best updates."""

    def __init__(self, path):
        if str(path) == ":memory:":
            raise StoreError("Use a database file so independent request connections share state")
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS teams (
                    team_id TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL,
                    token_digest TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    token_updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS rules (
                    rule_digest TEXT PRIMARY KEY,
                    rule_id TEXT NOT NULL UNIQUE,
                    rule_json TEXT NOT NULL,
                    installed_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS submissions (
                    submission_id TEXT NOT NULL,
                    team_id TEXT NOT NULL REFERENCES teams(team_id),
                    rule_digest TEXT NOT NULL REFERENCES rules(rule_digest),
                    request_digest TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    summary_json TEXT NOT NULL,
                    completion_ticks INTEGER NOT NULL CHECK(completion_ticks > 0),
                    trust_label TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(team_id, submission_id)
                );
                CREATE TABLE IF NOT EXISTS best_results (
                    team_id TEXT NOT NULL REFERENCES teams(team_id),
                    rule_digest TEXT NOT NULL REFERENCES rules(rule_digest),
                    submission_id TEXT NOT NULL,
                    completion_ticks INTEGER NOT NULL CHECK(completion_ticks > 0),
                    PRIMARY KEY(team_id, rule_digest),
                    FOREIGN KEY(team_id,submission_id) REFERENCES submissions(team_id,submission_id)
                );
                CREATE INDEX IF NOT EXISTS best_board
                    ON best_results(rule_digest, completion_ticks, team_id);
            """)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(str(self.path), timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=10000")
        try:
            yield db
        finally:
            if db.in_transaction:
                db.rollback()
            db.close()

    def provision_team(self, team_id, display_name=None, *, rotate=False):
        """Return a new random token once. Only its domain-separated hash is stored."""
        identifier(team_id, "team ID")
        name = team_id if display_name is None else display_name
        if not isinstance(name, str) or not 1 <= len(name) <= 100 or any(ord(c) < 32 for c in name):
            raise StoreError("Display name must contain 1..100 printable characters")
        if type(rotate) is not bool:
            raise StoreError("Token rotation must be explicit")
        token, now = secrets.token_urlsafe(32), utc_now()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT team_id,display_name FROM teams WHERE team_id=?", (team_id,)).fetchone()
            if old and not rotate:
                raise Conflict("Team already exists. Rotate its token explicitly.")
            if not old and rotate:
                raise NotFound("Cannot rotate a team that does not exist")
            if old:
                if display_name is None:
                    name = old["display_name"]
                db.execute("UPDATE teams SET display_name=?,token_digest=?,token_updated_at=? WHERE team_id=?",
                           (name, token_digest(token), now, team_id))
            else:
                db.execute("INSERT INTO teams VALUES(?,?,?,?,?)", (team_id, name, token_digest(token), now, now))
            db.commit()
        return token

    def authenticate(self, team_id, token):
        if not isinstance(team_id, str) or not isinstance(token, str) or not 1 <= len(token) <= 512:
            return False
        with self.connection() as db:
            row = db.execute("SELECT token_digest FROM teams WHERE team_id=?", (team_id,)).fetchone()
        expected = row[0] if row else "0" * 64
        return hmac.compare_digest(expected, token_digest(token)) and row is not None

    def authenticate_token(self, token):
        """Resolve ownership from the credential, never a submitted team field."""
        if not isinstance(token, str) or not 1 <= len(token) <= 512:
            raise Unauthorized("A valid team bearer token is required")
        with self.connection() as db:
            row = db.execute("SELECT team_id,display_name FROM teams WHERE token_digest=?",
                             (token_digest(token),)).fetchone()
        if not row:
            raise Unauthorized("A valid team bearer token is required")
        return {"team_id": row["team_id"], "display_name": row["display_name"]}

    def install_rule(self, digest, record, *, allow_test_rules=False):
        """Install immutable already-validated rule bytes under their contract digest."""
        rule_digest(digest)
        if not isinstance(record, dict):
            raise StoreError("Rule must be a JSON object")
        from .contracts import validate_rule
        validate_rule(record)
        if digest != record["rule_digest"]:
            raise StoreError("Rule digest differs from validated rule bytes")
        if type(allow_test_rules) is not bool:
            raise StoreError("Synthetic rule permission must be explicit")
        if record["test_only"] and not allow_test_rules:
            raise StoreError("Synthetic rules require explicit allow_test_rules=True")
        name = identifier(record.get("rule_id"), "rule ID")
        encoded = canonical(record)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT rule_json FROM rules WHERE rule_digest=?", (digest,)).fetchone()
            if old:
                if old[0] != encoded:
                    raise Conflict("A rule digest cannot be rebound to different settings")
                db.commit()
                return False
            if db.execute("SELECT 1 FROM rules WHERE rule_id=?", (name,)).fetchone():
                raise Conflict("A rule ID is immutable. Give a revised rule a new versioned ID.")
            db.execute("INSERT INTO rules VALUES(?,?,?,?)", (digest, name, encoded, utc_now()))
            db.commit()
        return True

    def add_rule(self, record, *, allow_test_rules=False):
        return self.install_rule(record.get("rule_digest"), record, allow_test_rules=allow_test_rules)

    def get_rule(self, digest):
        rule_digest(digest)
        with self.connection() as db:
            row = db.execute("SELECT rule_json FROM rules WHERE rule_digest=?", (digest,)).fetchone()
        if not row:
            raise NotFound("Unknown rule")
        return json.loads(row[0])

    def get_rule_by_id(self, name):
        identifier(name, "rule ID")
        with self.connection() as db:
            row = db.execute("SELECT rule_json FROM rules WHERE rule_id=?", (name,)).fetchone()
        if not row:
            raise NotFound("Unknown rule")
        return json.loads(row[0])

    def list_rules(self):
        with self.connection() as db:
            rows = db.execute("SELECT rule_digest,rule_json FROM rules ORDER BY installed_at,rule_digest").fetchall()
        return [{"rule_digest": r[0], "rule": json.loads(r[1])} for r in rows]

    def record_submission(self, team_id, digest, submission_id, payload, summary):
        """Record a validated receipt and lower the team's best inside one transaction."""
        identifier(team_id, "team ID"); identifier(submission_id, "submission ID"); rule_digest(digest)
        ticks = summary.get("completion_ticks") if isinstance(summary, dict) else None
        if type(ticks) is not int or not 0 < ticks <= 9_000_000_000_000_000:
            raise StoreError("Completion ticks must be a positive bounded integer")
        if summary.get("evaluation_mode") != TRUST_LABEL:
            raise StoreError("Only the declared source-matched student-run trust label is supported")
        if summary.get("submission_id") != submission_id or summary.get("rule_digest") != digest:
            raise StoreError("Validated summary identity differs from its submission or rule")
        encoded, normalized = canonical(payload), canonical(summary)
        digest_request = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if not db.execute("SELECT 1 FROM teams WHERE team_id=?", (team_id,)).fetchone():
                raise Unauthorized("Unknown team")
            if not db.execute("SELECT 1 FROM rules WHERE rule_digest=?", (digest,)).fetchone():
                raise NotFound("Unknown rule")
            old = db.execute("SELECT * FROM submissions WHERE team_id=? AND submission_id=?",
                             (team_id, submission_id)).fetchone()
            duplicate = False
            if old:
                if old["rule_digest"] != digest or old["request_digest"] != digest_request:
                    raise Conflict("Submission ID is already bound to a different team, rule or receipt")
                duplicate = True
            improved = False
            if not duplicate:
                db.execute("INSERT INTO submissions VALUES(?,?,?,?,?,?,?,?,?)",
                           (submission_id, team_id, digest, digest_request, encoded, normalized, ticks, TRUST_LABEL, utc_now()))
                changed = db.execute("""INSERT INTO best_results VALUES(?,?,?,?)
                    ON CONFLICT(team_id,rule_digest) DO UPDATE SET
                    submission_id=excluded.submission_id,completion_ticks=excluded.completion_ticks
                    WHERE excluded.completion_ticks < best_results.completion_ticks""",
                    (team_id, digest, submission_id, ticks))
                improved = changed.rowcount == 1
            else:
                submission_id, ticks = old["submission_id"], old["completion_ticks"]
            best = db.execute("SELECT completion_ticks FROM best_results WHERE team_id=? AND rule_digest=?",
                              (team_id, digest)).fetchone()[0]
            db.commit()
        return {"submission_id": submission_id, "team_id": team_id, "rule_digest": digest,
                "completion_ticks": ticks, "best_completion_ticks": best, "trust_label": TRUST_LABEL,
                "evaluation_mode": TRUST_LABEL, "duplicate": duplicate, "improved": improved}

    def get_submission(self, team_id, submission_id):
        identifier(team_id, "team ID"); identifier(submission_id, "submission ID")
        with self.connection() as db:
            row = db.execute("SELECT * FROM submissions WHERE submission_id=? AND team_id=?",
                             (submission_id, team_id)).fetchone()
        if not row:
            raise NotFound("Submission not found for this team")
        return {"submission_id": row["submission_id"], "team_id": row["team_id"],
                "rule_digest": row["rule_digest"], "completion_ticks": row["completion_ticks"],
                "trust_label": row["trust_label"], "created_at": row["created_at"],
                "evaluation_mode": row["trust_label"],
                "summary": json.loads(row["summary_json"])}

    def leaderboard(self, digest):
        self.get_rule(digest)
        with self.connection() as db:
            rows = db.execute("""SELECT b.team_id,t.display_name,b.submission_id,b.completion_ticks,
                    RANK() OVER (ORDER BY b.completion_ticks) AS shared_rank
                FROM best_results b JOIN teams t ON t.team_id=b.team_id
                WHERE b.rule_digest=? ORDER BY b.completion_ticks,b.team_id""", (digest,)).fetchall()
        return [{"rank": row["shared_rank"], "team_id": row["team_id"],
                 "display_name": row["display_name"], "submission_id": row["submission_id"],
                 "completion_ticks": row["completion_ticks"], "trust_label": TRUST_LABEL,
                 "evaluation_mode": TRUST_LABEL} for row in rows]
