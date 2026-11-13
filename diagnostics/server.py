"""Ungraded diagnostics WSGI composition. Old leaderboard routes are delegated.

The CLI is a local entry point. A separately authorized deployment must supply
HTTPS at public_origin. No grading rule, activation or student code is changed.
"""
import argparse
import base64
import hashlib
import hmac
from http import HTTPStatus
from http.cookies import SimpleCookie, CookieError
import json
from pathlib import Path
import re
import secrets
import sqlite3
import threading
import time
from urllib.parse import parse_qs, urlsplit
from wsgiref.simple_server import make_server

from leaderboard.server import create_app as create_leaderboard_app, ThreadedWSGIServer, SafeRequestHandler
from leaderboard.store import Unauthorized
from .protocol import (CATEGORIES, STATUSES, HEX40, MAX_REPORT_BYTES, DiagnosticError, canonical_bytes,
    decode_json, digest_or_none, exact_object, report_id, require, sanitize_text, text, validate_report)
from .store import DiagnosticStore

ASSET = Path(__file__).resolve().parent / "assets/instructor.html"
COOKIE = "__Host-ap_diagnostics"
TEAM_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")


def origin(value):
    try:
        parsed = urlsplit(value)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
            raise ValueError
        return ("https", parsed.hostname.lower(), parsed.port or 443)
    except (ValueError, AttributeError, TypeError):
        raise ValueError("Configure one HTTPS public origin without path or credentials.") from None


class DiagnosticsApplication:
    def __init__(self, leaderboard_db, diagnostics_db, admin_secret_file, *, public_origin, session_ttl_s=3600):
        paths = [Path(p).resolve() for p in (leaderboard_db, diagnostics_db, admin_secret_file)]
        if len(set(paths)) != 3:
            raise ValueError("Leaderboard, diagnostics and admin credential files must be separate.")
        self.public_origin = origin(public_origin)
        if type(session_ttl_s) is not int or not 300 <= session_ttl_s <= 28800:
            raise ValueError("Session lifetime must be 300..28800 seconds.")
        with paths[2].open("rb") as stream:
            raw = stream.read(1025)
        if len(raw) > 1024:
            raise ValueError("Invalid instructor credential file.")
        try:
            self._secret = raw.decode("ascii").strip()
        except UnicodeError:
            raise ValueError("Invalid instructor credential file.") from None
        if not re.fullmatch(r"[A-Za-z0-9._~+/=-]{32,512}", self._secret):
            raise ValueError("Instructor credential must be a separate random token of at least 32 characters.")
        self.leaderboard = create_leaderboard_app(paths[0])
        try:
            self.leaderboard.store.authenticate_token(self._secret)
        except Unauthorized:
            pass
        else:
            raise ValueError("Instructor credential must not also be a team token.")
        self.store = DiagnosticStore(paths[1])
        self.session_ttl_s = session_ttl_s
        self._sessions = {}
        self._lock = threading.Lock()

    @staticmethod
    def _bearer(environ):
        value = environ.get("HTTP_AUTHORIZATION", "")
        require(len(value) <= 4096, "UNAUTHORIZED", "Valid credentials are required.", 401)
        parts = value.split()
        require(len(parts) == 2 and parts[0].lower() == "bearer", "UNAUTHORIZED", "Valid credentials are required.", 401)
        return parts[1]

    def _team(self, environ):
        token = self._bearer(environ)
        try:
            team = self.leaderboard.store.authenticate_token(token)
        except Unauthorized:
            raise DiagnosticError("UNAUTHORIZED", "A valid team token is required.", 401) from None
        return team, environ.get("ap.enrollment.device_token", token)

    def _same_origin(self, environ):
        try:
            valid = origin(environ.get("HTTP_ORIGIN", "")) == self.public_origin
        except ValueError:
            valid = False
        require(valid, "ORIGIN_REQUIRED", "Use the configured instructor origin.", 403)

    def _signature(self, payload):
        return hmac.new(self._secret.encode("ascii"), b"ap-diagnostics-session-v1\0"+payload.encode("ascii"), hashlib.sha256).hexdigest()

    def _prune(self):
        now = int(time.time())
        self._sessions = {nonce: expiry for nonce, expiry in self._sessions.items() if expiry > now}

    def _session(self, environ):
        header = environ.get("HTTP_COOKIE", "")
        if len(header) > 4096:
            return None
        try:
            cookies = SimpleCookie()
            cookies.load(header)
            if COOKIE not in cookies:
                return None
            token = cookies[COOKIE].value
            payload, signature = token.split(".")
            if not hmac.compare_digest(self._signature(payload), signature):
                return None
            body = json.loads(base64.urlsafe_b64decode(payload + "="*(-len(payload)%4)))
            if type(body) is not dict or set(body) != {"nonce", "expires"}:
                return None
            if type(body["nonce"]) is not str or type(body["expires"]) is not int:
                return None
            with self._lock:
                self._prune()
                if self._sessions.get(body["nonce"]) == body["expires"]:
                    return body["nonce"]
        except (ValueError, TypeError, CookieError, UnicodeError):
            pass
        return None

    def _admin(self, environ, *, mutate=False):
        if environ.get("HTTP_AUTHORIZATION"):
            token = self._bearer(environ)
            require(token.isascii() and hmac.compare_digest(token, self._secret), "UNAUTHORIZED", "Instructor authentication is required.", 401)
            if environ.get("HTTP_ORIGIN"):
                self._same_origin(environ)
            return None
        nonce = self._session(environ)
        require(nonce is not None, "UNAUTHORIZED", "Instructor authentication is required.", 401)
        if mutate or environ.get("HTTP_ORIGIN"):
            self._same_origin(environ)
        return nonce

    @staticmethod
    def _body(environ, limit=MAX_REPORT_BYTES, *, form=False):
        require(not environ.get("HTTP_TRANSFER_ENCODING"), "CONTENT_LENGTH_REQUIRED", "Use fixed Content-Length.", 411)
        length = environ.get("CONTENT_LENGTH", "")
        require(type(length) is str and length.isascii() and length.isdecimal() and len(length) <= 10,
            "CONTENT_LENGTH_REQUIRED", "Use fixed Content-Length.", 411)
        count = int(length)
        require(count <= limit, "REQUEST_TOO_LARGE", "Request exceeds the diagnostics size limit.", 413)
        media = environ.get("CONTENT_TYPE", "").split(";", 1)[0].strip().lower()
        require(media == "application/json" or form and media == "application/x-www-form-urlencoded",
            "CONTENT_TYPE_REQUIRED", "Use the supported request content type.", 415)
        raw = environ["wsgi.input"].read(count)
        require(len(raw) == count, "INCOMPLETE_BODY", "Request body is incomplete.")
        if media == "application/x-www-form-urlencoded":
            try:
                fields = parse_qs(raw.decode("utf-8"), keep_blank_values=True, strict_parsing=True, max_num_fields=2)
                require(all(len(v) == 1 for v in fields.values()))
                return {k: v[0] for k, v in fields.items()}
            except (ValueError, UnicodeError):
                raise DiagnosticError("INVALID_FORM", "Use the instructor login form.") from None
        return decode_json(raw)

    @staticmethod
    def _query(environ, allowed):
        try:
            fields = parse_qs(environ.get("QUERY_STRING", ""), keep_blank_values=True, strict_parsing=True, max_num_fields=8)
        except ValueError:
            raise DiagnosticError("INVALID_QUERY", "Use supported query fields once.") from None
        require(set(fields) <= set(allowed) and all(len(v) == 1 and v[0] for v in fields.values()),
            "INVALID_QUERY", "Use supported query fields once.")
        return {k: v[0] for k, v in fields.items()}

    @staticmethod
    def _limit(query):
        raw = query.pop("limit", "50")
        require(raw.isascii() and raw.isdecimal() and len(raw) <= 3, "INVALID_LIMIT", "Use a limit of 1..100.")
        count = int(raw)
        require(1 <= count <= 100, "INVALID_LIMIT", "Use a limit of 1..100.")
        return count

    @staticmethod
    def _team_id(value):
        require(type(value) is str and TEAM_ID.fullmatch(value), "INVALID_TEAM_ID", "Use a valid team identifier.")
        return value

    @staticmethod
    def _cursor(query):
        value = query.pop("before", None)
        if value is not None:
            require(value.isascii() and value.isdecimal() and len(value) <= 18 and int(value) > 0,
                "INVALID_CURSOR", "Use a positive report sequence.")
            return int(value)
        return None

    def dispatch(self, environ):
        method, path = environ.get("REQUEST_METHOD", "GET"), environ.get("PATH_INFO", "")
        require(len(path) <= 512 and len(environ.get("QUERY_STRING", "")) <= 2048,
            "INVALID_REQUEST_TARGET", "Request target is too long.", 414)
        if path == "/instructor" and method == "GET":
            require(not environ.get("QUERY_STRING"), "INVALID_QUERY", "Credentials must never be supplied in a URL.")
            return 200, ASSET.read_bytes(), "text/html; charset=utf-8", []
        if path == "/instructor/login" and method == "POST":
            require(not environ.get("QUERY_STRING"), "INVALID_QUERY", "Credentials must never be supplied in a URL.")
            self._same_origin(environ)
            value = self._body(environ, 4096, form=True)
            exact_object(value, ("token",))
            token = value["token"]
            require(type(token) is str and len(token) <= 512 and token.isascii() and hmac.compare_digest(token, self._secret),
                "UNAUTHORIZED", "Instructor authentication failed.", 401)
            nonce, expiry = secrets.token_urlsafe(24), int(time.time())+self.session_ttl_s
            with self._lock:
                self._prune()
                require(len(self._sessions) < 1000, "SESSION_CAPACITY", "Too many active instructor sessions.", 429)
                self._sessions[nonce] = expiry
            payload = base64.urlsafe_b64encode(canonical_bytes(dict(nonce=nonce, expires=expiry))).decode("ascii").rstrip("=")
            cookie = payload+"."+self._signature(payload)
            return 200, dict(status="authenticated", expires_in_s=self.session_ttl_s), None, [
                ("Set-Cookie", f"{COOKIE}={cookie}; Path=/; Max-Age={self.session_ttl_s}; HttpOnly; Secure; SameSite=Strict")]
        if path == "/instructor/logout" and method == "POST":
            require(not environ.get("QUERY_STRING"), "INVALID_QUERY", "Unexpected query parameters.")
            self._same_origin(environ)
            nonce = self._admin(environ, mutate=True)
            if nonce:
                with self._lock:
                    self._sessions.pop(nonce, None)
            return 200, dict(status="signed_out"), None, [("Set-Cookie", f"{COOKIE}=; Path=/; Max-Age=0; HttpOnly; Secure; SameSite=Strict")]
        if path == "/api/diagnostics/reports" and method == "POST":
            require(not environ.get("QUERY_STRING"), "INVALID_QUERY", "Unexpected query parameters.")
            team, token = self._team(environ)
            report = validate_report(self._body(environ), secrets=(token, self._secret))
            result = self.store.insert(team["team_id"], report)
            return 200 if result["duplicate"] else 201, result, None, []
        if path == "/api/diagnostics/own" and method == "GET":
            team, _ = self._team(environ)
            query = self._query(environ, ("limit", "before"))
            limit = self._limit(query)
            before = self._cursor(query)
            return 200, self.store.own(team["team_id"], before=before, limit=limit), None, []
        if path == "/api/instructor/issues/list" and method == "GET":
            self._admin(environ)
            query = self._query(environ, ("team_id", "category", "version", "status", "limit", "before"))
            limit = self._limit(query)
            before = self._cursor(query)
            if "team_id" in query:
                self._team_id(query["team_id"])
            if "category" in query:
                require(query["category"] in CATEGORIES)
            if "status" in query:
                require(query["status"] in STATUSES)
            if "version" in query:
                query["version"] = text(query["version"], 128)
            return 200, self.store.issues(query, limit, before=before), None, []
        if path == "/api/instructor/issues/detail" and method == "GET":
            self._admin(environ)
            query = self._query(environ, ("team_id", "report_id", "before"))
            before = self._cursor(query)
            exact_object(query, ("team_id", "report_id"))
            return 200, self.store.detail(self._team_id(query["team_id"]), report_id(query["report_id"]), before=before), None, []
        if path == "/api/instructor/issues/status" and method == "POST":
            self._admin(environ, mutate=True)
            require(not environ.get("QUERY_STRING"), "INVALID_QUERY", "Unexpected query parameters.")
            value = self._body(environ, 8192)
            exact_object(value, ("team_id", "report_id", "status", "fixed_commit", "note"))
            require(value["status"] in STATUSES)
            require(value["fixed_commit"] != self._secret, "SENSITIVE_METADATA", "Credential values are not permitted in diagnostic identity fields.")
            return 200, self.store.set_status(self._team_id(value["team_id"]), report_id(value["report_id"]),
                value["status"], digest_or_none(value["fixed_commit"], HEX40), text(value["note"], 2000, secrets=(self._secret,))), None, []
        known = ("/instructor", "/instructor/login", "/instructor/logout", "/api/diagnostics/reports", "/api/diagnostics/own",
            "/api/instructor/issues/list", "/api/instructor/issues/detail", "/api/instructor/issues/status")
        raise DiagnosticError("METHOD_NOT_ALLOWED" if path in known else "NOT_FOUND",
            "Unsupported diagnostics endpoint or method.", 405 if path in known else 404)

    def __call__(self, environ, start_response):
        path = environ.get("PATH_INFO", "/")
        if not (path == "/instructor" or path.startswith("/instructor/") or path.startswith("/api/diagnostics/") or path.startswith("/api/instructor/")):
            return self.leaderboard(environ, start_response)
        try:
            status, value, media, extra = self.dispatch(environ)
        except DiagnosticError as exc:
            status, value, media, extra = exc.status, {"error": {"code": exc.code, "message": str(exc)}}, None, []
        except (OSError, sqlite3.Error):
            status, value, media, extra = 503, {"error": {"code": "SERVICE_UNAVAILABLE", "message": "Diagnostics storage is temporarily unavailable."}}, None, []
        except Exception:
            status, value, media, extra = 500, {"error": {"code": "INTERNAL_ERROR", "message": "Diagnostics request could not be processed."}}, None, []
        body = value if isinstance(value, bytes) else canonical_bytes(value)
        policy = "default-src 'none'; style-src 'unsafe-inline'; connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
        if media == "text/html; charset=utf-8":
            scripts = re.findall(rb"<script>(.*?)</script>", body, re.S)
            hashes = " ".join("'sha256-"+base64.b64encode(hashlib.sha256(script).digest()).decode("ascii")+"'" for script in scripts)
            policy += "; script-src "+hashes
        headers = [("Content-Type", media or "application/json; charset=utf-8"), ("Content-Length", str(len(body))),
            ("Cache-Control", "no-store"), ("X-Content-Type-Options", "nosniff"), ("Content-Security-Policy", policy),
            ("Referrer-Policy", "no-referrer"), ("X-Frame-Options", "DENY"), *extra]
        if status == 401:
            headers.append(("WWW-Authenticate", 'Bearer realm="Diagnostics"'))
        start_response(f"{status} {HTTPStatus(status).phrase}", headers)
        return [body]


def create_app(leaderboard_db, diagnostics_db, admin_secret_file, *, public_origin, session_ttl_s=3600):
    from .enrollment_server import EnrollmentApplication
    return EnrollmentApplication(DiagnosticsApplication(leaderboard_db, diagnostics_db, admin_secret_file,
        public_origin=public_origin, session_ttl_s=session_ttl_s))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("serve", choices=("serve",))
    parser.add_argument("--leaderboard-db", type=Path, required=True)
    parser.add_argument("--diagnostics-db", type=Path, required=True)
    parser.add_argument("--admin-secret-file", type=Path, required=True)
    parser.add_argument("--public-origin", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args(argv)
    app = create_app(args.leaderboard_db, args.diagnostics_db, args.admin_secret_file, public_origin=args.public_origin)
    with make_server(args.host, args.port, app, server_class=ThreadedWSGIServer, handler_class=SafeRequestHandler) as server:
        server.serve_forever()


if __name__ == "__main__":
    main()
