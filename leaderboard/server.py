"""Local WSGI service for explicitly source-matched student-run results.

The reference HTTP server is a local development entry point. A future public
deployment can host create_app() with its own production WSGI/TLS setup.
Student code and physics are never executed by this service.
"""
from __future__ import annotations

import argparse
from http import HTTPStatus
import json
import os
from pathlib import Path
import socketserver
import sqlite3
import sys
from urllib.parse import parse_qs, urlsplit
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

from .contracts import ProtocolError, canonical_bytes, decode_json, validate_rule, validate_submission
from .store import Conflict, NotFound, Store, StoreError, TRUST_LABEL, Unauthorized
from .v3 import RULE_SCHEMA
from .v3_store import AttemptStore


MAX_REQUEST_BYTES = 1024 * 1024
DASHBOARD = Path(__file__).resolve().parent / "assets/dashboard.html"
TRUST_DESCRIPTION = (
    "Student-run evaluation with source hashes matched to the frozen rule. "
    "This is not cryptographic proof of native execution or a professor rerun."
)


def strict_json(raw):
    return decode_json(raw)


def public_rule(rule):
    """Expose frozen board metadata, never tokens or internal operator settings."""
    fields = ("schema_version", "rule_id", "rule_digest", "difficulty", "material", "qualified",
              "qualification_reason", "dt_s", "test_only", "completion_clock", "max_completion_ticks", "competition")
    metadata = {**{name: rule[name] for name in fields if name in rule}, "evaluation_mode": TRUST_LABEL}
    if "max_completion_ticks" in rule:
        metadata["max_simulation_seconds"] = rule["max_completion_ticks"] * rule["dt_s"]
    return metadata


class LeaderboardApplication:
    def __init__(self, db_path, *, max_request_bytes=MAX_REQUEST_BYTES):
        if type(max_request_bytes) is not int or not 1 <= max_request_bytes <= 16 * MAX_REQUEST_BYTES:
            raise ValueError("Request byte limit must be an integer in1..16777216")
        self.store = Store(db_path)
        self.attempts = AttemptStore(self.store)
        self.max_request_bytes = max_request_bytes

    def _team(self, environ):
        auth = environ.get("HTTP_AUTHORIZATION", "")
        parts = auth.split()
        if len(parts) != 2 or parts[0].lower() != "bearer":
            raise Unauthorized("A valid team bearer token is required")
        return self.store.authenticate_token(parts[1])

    def _body(self, environ):
        if environ.get("HTTP_TRANSFER_ENCODING"):
            raise ProtocolError("CONTENT_LENGTH_REQUIRED", "Use a fixed Content-Length JSON request", 411)
        length = environ.get("CONTENT_LENGTH", "")
        if not length:
            raise ProtocolError("CONTENT_LENGTH_REQUIRED", "Content-Length is required", 411)
        if not length.isascii() or not length.isdecimal():
            raise ProtocolError("INVALID_CONTENT_LENGTH", "Content-Length must be a nonnegative integer")
        count = int(length)
        if count > self.max_request_bytes:
            raise ProtocolError("REQUEST_TOO_LARGE", "The JSON request exceeds the configured byte limit", 413)
        media = environ.get("CONTENT_TYPE", "").split(";", 1)[0].strip().lower()
        if media != "application/json":
            raise ProtocolError("JSON_REQUIRED", "Content-Type must be application/json", 415)
        raw = environ["wsgi.input"].read(count)
        if len(raw) != count:
            raise ProtocolError("INCOMPLETE_BODY", "The JSON request body is incomplete")
        value = decode_json(raw)
        if not isinstance(value, dict):
            raise ProtocolError("JSON_OBJECT_REQUIRED", "Submission must be one JSON object")
        return value

    def dispatch(self, environ):
        method, path = environ.get("REQUEST_METHOD", "GET"), environ.get("PATH_INFO", "/")
        if len(path) > 512 or len(environ.get("QUERY_STRING", "")) > 1024:
            raise ProtocolError("INVALID_REQUEST_TARGET", "Request target is too long", 414)
        if method == "GET" and path == "/":
            return 200, DASHBOARD.read_bytes(), "text/html; charset=utf-8"
        if method == "GET" and path in ("/health", "/api/health"):
            return 200, {"status": "ok", "application": "Applied Programming", "evaluation_mode": TRUST_LABEL}, None
        if method == "GET" and path == "/api/rules":
            rules = [public_rule(validate_rule(r["rule"])) for r in self.store.list_rules()]
            for metadata in rules:
                if metadata['schema_version'] == RULE_SCHEMA:
                    metadata['scoring_enabled'] = self.attempts.activation(metadata) is not None
            return 200, {"rules": rules, "evaluation_mode": TRUST_LABEL,
                         "trust_description": TRUST_DESCRIPTION}, None
        if path.startswith('/api/v3/'):
            return self.dispatch_v3(environ)
        if method == "GET" and path == "/api/leaderboard":
            query = parse_qs(environ.get("QUERY_STRING", ""), keep_blank_values=True,
                             strict_parsing=True, max_num_fields=4)
            if set(query) != {"rule_id"} or len(query["rule_id"]) != 1 or not query["rule_id"][0]:
                raise ProtocolError("RULE_ID_REQUIRED", "Use exactly one rule_id query parameter")
            rule = validate_rule(self.store.get_rule_by_id(query["rule_id"][0]))
            if rule['schema_version'] == RULE_SCHEMA:
                return 200, {**self.attempts.board(rule), 'rule': public_rule(rule),
                             'trust_description': TRUST_DESCRIPTION}, None
            enabled = rule["qualified"] is True
            rows = self.store.leaderboard(rule["rule_digest"]) if enabled else []
            for row in rows:
                row["simulation_seconds"] = row["completion_ticks"] * rule["dt_s"]
            return 200, {"rule": public_rule(rule), "rows": rows, "enabled": enabled,
                         "ranking_metric": "completion_ticks", "evaluation_mode": TRUST_LABEL,
                         "trust_description": TRUST_DESCRIPTION}, None
        if method == "POST" and path == "/api/submissions":
            team = self._team(environ)
            payload = self._body(environ)
            name = payload.get("rule_id")
            if not isinstance(name, str):
                raise ProtocolError("RULE_ID_REQUIRED", "Submission needs its frozen rule_id")
            rule = validate_rule(self.store.get_rule_by_id(name))
            if rule['schema_version'] == RULE_SCHEMA:
                raise ProtocolError('V3_ATTEMPT_REQUIRED', 'Use automatic v3 attempt recording and explicit final selection.', 409)
            summary = validate_submission(rule, payload)
            stored = self.store.record_submission(team["team_id"], rule["rule_digest"],
                summary["submission_id"], payload, summary)
            return 200 if stored["duplicate"] else 201, {"status": "accepted", **stored,
                "simulation_seconds": summary["simulation_seconds"],
                "trust_description": TRUST_DESCRIPTION}, None
        if method == "GET" and path.startswith("/api/submissions/"):
            team = self._team(environ)
            name = path[len("/api/submissions/"):]
            if not name or "/" in name or environ.get("QUERY_STRING"):
                raise ProtocolError("INVALID_SUBMISSION_ID", "Use a single submission ID without query parameters")
            return 200, self.store.get_submission(team["team_id"], name), None
        if path in ("/", "/api/rules", "/api/leaderboard", "/api/submissions") or path.startswith("/api/submissions/"):
            raise ProtocolError("METHOD_NOT_ALLOWED", "This endpoint does not support the requested method", 405)
        raise ProtocolError("NOT_FOUND", "Unknown endpoint", 404)

    def dispatch_v3(self, environ):
        method, path = environ.get('REQUEST_METHOD', 'GET'), environ.get('PATH_INFO', '')
        if method == 'POST' and path in ('/api/v3/records', '/api/v3/selection'):
            team = self._team(environ)
            value = self._body(environ)
            name = value.get('rule_id')
            if not isinstance(name, str):
                raise ProtocolError('RULE_ID_REQUIRED', 'Supply the exact rule ID.')
            rule = validate_rule(self.store.get_rule_by_id(name))
            if path.endswith('/records'):
                result = self.attempts.record(team['team_id'], rule, value)
                return 200 if result['duplicate'] else 201, result, None
            return 200, self.attempts.select(team['team_id'], rule, value), None
        if method == 'GET' and path in ('/api/v3/board', '/api/v3/history', '/api/v3/attempt'):
            query = parse_qs(environ.get('QUERY_STRING', ''), keep_blank_values=True, strict_parsing=True, max_num_fields=4)
            if any(len(values) != 1 or not values[0] for values in query.values()):
                raise ProtocolError('INVALID_QUERY', 'Use each query field exactly once.')
            if path == '/api/v3/attempt':
                if set(query) != {'run_id'}:
                    raise ProtocolError('RUN_ID_REQUIRED', 'Use one run_id query parameter.')
                team = self._team(environ)
                return 200, self.attempts.get_attempt(team['team_id'], query['run_id'][0]), None
            allowed = {'rule_id', 'view'} if path == '/api/v3/board' else {'rule_id', 'before_sequence', 'limit'}
            if 'rule_id' not in query or set(query) - allowed:
                raise ProtocolError('RULE_ID_REQUIRED', 'Use one rule_id and supported optional filters.')
            rule = validate_rule(self.store.get_rule_by_id(query['rule_id'][0]))
            if rule['schema_version'] != RULE_SCHEMA:
                raise ProtocolError('V3_RULE_REQUIRED', 'Legacy time-only boards remain separate.', 409)
            if path == '/api/v3/board':
                result = self.attempts.board(rule, query.get('view', ['live'])[0])
                return 200, {**result, 'rule': public_rule(rule), 'trust_description': TRUST_DESCRIPTION}, None
            team = self._team(environ)
            cursor = int(query['before_sequence'][0]) if 'before_sequence' in query else None
            limit = int(query.get('limit', ['100'])[0])
            return 200, self.attempts.history(team['team_id'], rule, before_sequence=cursor, limit=limit), None
        if path in ('/api/v3/records', '/api/v3/selection', '/api/v3/board', '/api/v3/history', '/api/v3/attempt'):
            raise ProtocolError('METHOD_NOT_ALLOWED', 'Unsupported method for this endpoint.', 405)
        raise ProtocolError('NOT_FOUND', 'Unknown v3 endpoint.', 404)

    def __call__(self, environ, start_response):
        try:
            status, value, media = self.dispatch(environ)
        except ProtocolError as exc:
            status, value, media = exc.status, {"error": {"code": exc.code, "message": str(exc)}}, None
        except Unauthorized as exc:
            status, value, media = 401, {"error": {"code": "UNAUTHORIZED", "message": str(exc)}}, None
        except NotFound as exc:
            status, value, media = 404, {"error": {"code": "NOT_FOUND", "message": str(exc)}}, None
        except Conflict as exc:
            status, value, media = 409, {"error": {"code": "IDEMPOTENCY_CONFLICT", "message": str(exc)}}, None
        except (StoreError, ValueError) as exc:
            status, value, media = 400, {"error": {"code": "INVALID_REQUEST", "message": str(exc)}}, None
        except (OSError, sqlite3.Error):
            status, value, media = 503, {"error": {"code": "SERVICE_UNAVAILABLE", "message": "Persistent storage is temporarily unavailable"}}, None
        except Exception:
            status, value, media = 500, {"error": {"code": "INTERNAL_ERROR", "message": "The request could not be processed"}}, None
        body = value if isinstance(value, bytes) else canonical_bytes(value)
        headers = [("Content-Type", media or "application/json; charset=utf-8"),
                   ("Content-Length", str(len(body))), ("Cache-Control", "no-store"),
                   ("X-Content-Type-Options", "nosniff"),
                   ("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; base-uri 'none'; frame-ancestors 'none'")]
        if status == 401:
            headers.append(("WWW-Authenticate", 'Bearer realm="Applied Programming"'))
        start_response(f"{status} {HTTPStatus(status).phrase}", headers)
        return [body]


def create_app(db_path, *, max_request_bytes=MAX_REQUEST_BYTES):
    return LeaderboardApplication(db_path, max_request_bytes=max_request_bytes)


class ThreadedWSGIServer(socketserver.ThreadingMixIn, WSGIServer):
    daemon_threads = True


class SafeRequestHandler(WSGIRequestHandler):
    def log_request(self, code="-", size="-"):
        # Do not log credentials, bodies or accidental query-string secrets.
        self.log_message('"%s %s" %s %s', self.command, urlsplit(self.path).path, code, size)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("init", "add-team", "add-rule", "serve", "freeze", "activate-v3"):
        sub = commands.add_parser(name)
        sub.add_argument("--db", type=Path, required=True)
        if name == "add-team":
            sub.add_argument("--team-id", required=True)
            sub.add_argument("--display-name")
            sub.add_argument("--token-file", type=Path, required=True)
            sub.add_argument("--rotate", action="store_true")
        if name == "add-rule":
            sub.add_argument("--rule", type=Path, required=True)
            sub.add_argument("--allow-test-rules", action="store_true",
                             help="Explicitly install a visibly synthetic local-test rule")
        if name == 'freeze':
            sub.add_argument('--rule-id', required=True,
                             help='Freeze successful final selections now. No calendar deadline is inferred.')
        if name == 'activate-v3':
            sub.add_argument('--rule-id', required=True)
            sub.add_argument('--evidence', type=Path, required=True,
                             help='Instructor qualification JSON for this exact rule digest.')
        if name == "serve":
            sub.add_argument("--host", default="127.0.0.1")
            sub.add_argument("--port", type=int, default=8000)
            sub.add_argument("--max-request-bytes", type=int, default=MAX_REQUEST_BYTES)
    args = parser.parse_args(argv)
    try:
        if args.command == "serve":
            if not 1 <= args.port <= 65535:
                raise ValueError("Port must be1..65535")
            app = create_app(args.db, max_request_bytes=args.max_request_bytes)
            with make_server(args.host, args.port, app, server_class=ThreadedWSGIServer,
                             handler_class=SafeRequestHandler) as httpd:
                print(json.dumps({"application": "Applied Programming", "local_development_server": True,
                    "listen": f"http://{args.host}:{args.port}", "evaluation_mode": TRUST_LABEL}), flush=True)
                httpd.serve_forever()
        else:
            db = Store(args.db)
            if args.command == "init":
                print(json.dumps({"database_initialized": True, "database": str(db.path)}))
            elif args.command == 'freeze':
                rule = validate_rule(db.get_rule_by_id(args.rule_id))
                print(json.dumps(AttemptStore(db).freeze(rule)))
            elif args.command == 'activate-v3':
                import hashlib
                rule = validate_rule(db.get_rule_by_id(args.rule_id))
                raw = args.evidence.read_bytes()
                evidence = strict_json(raw)
                if (type(evidence) is not dict or evidence.get('qualification_passed') is not True
                        or evidence.get('qualification_scope') != 'complete_v3_native_release'
                        or evidence.get('rule_id') != rule['rule_id'] or evidence.get('rule_digest') != rule['rule_digest']):
                    raise ProtocolError('QUALIFICATION_EVIDENCE', 'Use a completed instructor native-release qualification record for this exact rule.')
                print(json.dumps(AttemptStore(db).activate(rule, hashlib.sha256(raw).hexdigest())))
            elif args.command == "add-rule":
                rule = validate_rule(strict_json(args.rule.read_bytes()))
                added = db.install_rule(rule["rule_digest"], rule, allow_test_rules=args.allow_test_rules)
                print(json.dumps({"rule_id": rule["rule_id"], "rule_digest": rule["rule_digest"], "installed": added}))
            elif args.command == "add-team":
                target = args.token_file.resolve()
                target.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as stream:
                        token = db.provision_team(args.team_id, args.display_name, rotate=args.rotate)
                        stream.write(token + "\n")
                except Exception:
                    target.unlink(missing_ok=True)  # Only the file created exclusively above.
                    raise
                print(json.dumps({"team_id": args.team_id, "token_written": str(target),
                                  "secret_printed": False, "token_rotated": args.rotate}))
    except (ProtocolError, StoreError, OSError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
