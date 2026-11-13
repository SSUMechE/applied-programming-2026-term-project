"""Separate, ungraded SQLite failure inbox. Never mutates leaderboard data."""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3

from .protocol import DiagnosticError, canonical_bytes, report_signature


def utc_now():
    return datetime.now(timezone.utc).isoformat()


class DiagnosticStore:
    def __init__(self, path):
        if str(path) == ":memory:":
            raise ValueError("Use a persistent diagnostics file.")
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS reports (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    team_id TEXT NOT NULL, report_id TEXT NOT NULL,
                    body_sha256 TEXT NOT NULL, signature TEXT NOT NULL, body_json TEXT NOT NULL,
                    received_at_utc TEXT NOT NULL, category TEXT NOT NULL, version TEXT NOT NULL,
                    phase TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'new'
                        CHECK(status IN ('new','investigating','resolved')),
                    fixed_commit TEXT, note TEXT NOT NULL DEFAULT '', updated_at_utc TEXT NOT NULL,
                    UNIQUE(team_id, report_id)
                );
                CREATE INDEX IF NOT EXISTS diagnostic_signature ON reports(signature, sequence DESC);
                CREATE INDEX IF NOT EXISTS diagnostic_team ON reports(team_id, sequence DESC);
                CREATE TABLE IF NOT EXISTS status_history (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    report_sequence INTEGER NOT NULL REFERENCES reports(sequence),
                    previous_status TEXT NOT NULL, status TEXT NOT NULL,
                    fixed_commit TEXT, note TEXT NOT NULL, changed_at_utc TEXT NOT NULL
                );
                CREATE TRIGGER IF NOT EXISTS immutable_report_details
                BEFORE UPDATE OF team_id, report_id, body_sha256, signature, body_json,
                    received_at_utc, category, version, phase ON reports
                BEGIN SELECT RAISE(ABORT, 'immutable diagnostic report'); END;
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
            db.close()

    @staticmethod
    def summary(row):
        return {key: row[key] for key in ("sequence", "team_id", "report_id", "body_sha256", "signature",
            "received_at_utc", "category", "version", "phase", "status", "fixed_commit", "note", "updated_at_utc")}

    @classmethod
    def detail_row(cls, row):
        return {**cls.summary(row), "report": json.loads(row["body_json"])}

    def insert(self, team_id, report):
        body = canonical_bytes(report)
        digest = hashlib.sha256(body).hexdigest()
        signature = report_signature(report)
        now = utc_now()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT * FROM reports WHERE team_id=? AND report_id=?", (team_id, report["report_id"])).fetchone()
            if existing:
                if existing["body_sha256"] != digest:
                    raise DiagnosticError("IDEMPOTENCY_CONFLICT", "This report ID already belongs to different immutable report details.", 409)
                db.commit()
                return dict(status="accepted", report_id=existing["report_id"], sequence=existing["sequence"],
                    signature=existing["signature"], body_sha256=digest, duplicate=True)
            cursor = db.execute("""INSERT INTO reports
                (team_id,report_id,body_sha256,signature,body_json,received_at_utc,category,version,phase,updated_at_utc)
                VALUES (?,?,?,?,?,?,?,?,?,?)""", (team_id, report["report_id"], digest, signature,
                body.decode("utf-8"), now, report["failure"]["category"], report["source"]["package_version"], report["context"]["phase"], now))
            db.commit()
            return dict(status="accepted", report_id=report["report_id"], sequence=cursor.lastrowid,
                signature=signature, body_sha256=digest, duplicate=False)

    def own(self, team_id, *, before=None, limit=50):
        clauses, args = ["team_id=?"], [team_id]
        if before is not None:
            clauses.append("sequence<?")
            args.append(before)
        with self.connection() as db:
            rows = db.execute("SELECT * FROM reports WHERE " + " AND ".join(clauses) + " ORDER BY sequence DESC LIMIT ?", (*args, limit+1)).fetchall()
        return dict(reports=[self.detail_row(r) for r in rows[:limit]],
            next_before=rows[limit-1]["sequence"] if len(rows) > limit else None, diagnostics_only=True)

    def issues(self, filters, limit=50, *, before=None):
        columns = {"team_id": "team_id", "category": "category", "version": "version", "status": "status"}
        clauses, args = [], []
        for key, value in filters.items():
            clauses.append(columns[key]+"=?")
            args.append(value)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        having = " HAVING max(sequence) < ?" if before is not None else ""
        if before is not None:
            args.append(before)
        query = """SELECT r.*, g.occurrences, g.teams, g.new_count, g.investigating_count, g.resolved_count,
            g.first_received_at_utc FROM reports r JOIN
            (SELECT signature, max(sequence) AS latest, count(*) AS occurrences, count(DISTINCT team_id) AS teams,
             sum(status='new') AS new_count, sum(status='investigating') AS investigating_count,
             sum(status='resolved') AS resolved_count, min(received_at_utc) AS first_received_at_utc
             FROM reports""" + where + " GROUP BY signature" + having + ") g ON r.sequence=g.latest ORDER BY r.sequence DESC LIMIT ?"
        with self.connection() as db:
            rows = db.execute(query, (*args, limit+1)).fetchall()
        result = []
        for row in rows[:limit]:
            item = self.summary(row)
            item.update({key: row[key] for key in ("occurrences", "teams", "new_count", "investigating_count", "resolved_count", "first_received_at_utc")})
            report = json.loads(row["body_json"])
            item.update(command=report["command"]["kind"], code=report["failure"]["code"], exception_type=report["failure"]["exception_type"])
            result.append(item)
        return dict(issues=result, truncated=len(rows)>limit, grouped_by="stable_signature",
            next_before=rows[limit-1]["sequence"] if len(rows)>limit else None,
            grouping_scope="Counts use the current filters. Latest matching attempt identifies each row.", poll_interval_s=15, diagnostics_only=True)

    def detail(self, team_id, report_id, *, before=None):
        with self.connection() as db:
            row = db.execute("SELECT * FROM reports WHERE team_id=? AND report_id=?", (team_id, report_id)).fetchone()
            if not row:
                raise DiagnosticError("NOT_FOUND", "Diagnostic report not found.", 404)
            history = db.execute("SELECT previous_status,status,fixed_commit,note,changed_at_utc FROM status_history WHERE report_sequence=? ORDER BY sequence", (row["sequence"],)).fetchall()
            clause = " AND sequence<?" if before is not None else ""
            args = (row["signature"], before) if before is not None else (row["signature"],)
            related = db.execute("SELECT * FROM reports WHERE signature=?"+clause+" ORDER BY sequence DESC LIMIT 101", args).fetchall()
        return dict(attempt=self.detail_row(row), status_history=[dict(item) for item in history],
            related_attempts=[self.summary(item) for item in related[:100]], related_truncated=len(related)>100,
            related_next_before=related[99]["sequence"] if len(related)>100 else None, diagnostics_only=True)

    def set_status(self, team_id, report_id, status, fixed_commit, note):
        now = utc_now()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM reports WHERE team_id=? AND report_id=?", (team_id, report_id)).fetchone()
            if not row:
                raise DiagnosticError("NOT_FOUND", "Diagnostic report not found.", 404)
            # Retrying an identical update does not create duplicate history.
            changed = (row["status"], row["fixed_commit"], row["note"]) != (status, fixed_commit, note)
            if changed:
                db.execute("INSERT INTO status_history(report_sequence,previous_status,status,fixed_commit,note,changed_at_utc) VALUES(?,?,?,?,?,?)",
                    (row["sequence"], row["status"], status, fixed_commit, note, now))
                db.execute("UPDATE reports SET status=?,fixed_commit=?,note=?,updated_at_utc=? WHERE sequence=?",
                    (status, fixed_commit, note, now, row["sequence"]))
            db.commit()
        return dict(status="updated", changed=changed, **self.detail(team_id, report_id))
