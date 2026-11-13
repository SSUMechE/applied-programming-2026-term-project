"""Submit one supplied-evaluator receipt using the team's own credential.

Use the existing course environment. This module installs nothing and does not
run a simulator. Repeated attempts send identical bytes for server idempotency.
"""
from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from leaderboard.contracts import decode_json

SUBMISSION_SCHEMA = "ap-leaderboard-submission-v1"
SUBMISSION_KEYS = {"schema_version", "rule_id", "rule_digest", "commit_id", "evaluation"}


class SubmissionError(ValueError):
    pass


def checked_payload(value):
    """Reject diagnostics and partial/local review files before any network IO."""
    if type(value) is not dict or set(value) != SUBMISSION_KEYS:
        raise SubmissionError("Use the complete submission JSON produced by leaderboard.evaluate")
    if value.get("schema_version") != SUBMISSION_SCHEMA or type(value.get("evaluation")) is not dict:
        raise SubmissionError("Unsupported submission schema")
    for key, length in (("rule_digest", 64), ("commit_id", 40)):
        text = value.get(key)
        if type(text) is not str or len(text) != length or any(c not in "0123456789abcdef" for c in text):
            raise SubmissionError("Invalid " + key)
    if type(value.get("rule_id")) is not str or not value["rule_id"]:
        raise SubmissionError("Missing rule ID")
    try:
        body = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
        if len(body) > 1_048_576:
            raise SubmissionError("Submission exceeds the1MiB service limit")
        return body
    except (ValueError, TypeError) as error:
        raise SubmissionError("Submission must contain finite JSON data") from error


def token_value(*, token_env=None, token_file=None):
    if token_env and token_file:
        raise SubmissionError("Choose a token environment variable or token file")
    if token_file:
        token = Path(token_file).read_text(encoding="utf-8").strip()
    else:
        name = token_env or "AP_LEADERBOARD_TEAM_TOKEN"
        token = os.environ.get(name, "").strip()
    if not token or any(c.isspace() for c in token):
        raise SubmissionError("A nonempty team token is required")
    return token


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


def submit_payload(value, server_url, token, *, timeout_s=15., retries=2):
    body = checked_payload(value)
    parsed = urlsplit(server_url)
    if (parsed.scheme not in ("http", "https") or not parsed.hostname
            or parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment):
        raise SubmissionError("Use the leaderboard HTTP(S) base URL without credentials, query or fragment")
    if parsed.scheme == "http" and parsed.hostname not in ("localhost", "127.0.0.1", "::1"):
        raise SubmissionError("Use HTTPS for a remote leaderboard. HTTP is limited to local development")
    if (type(timeout_s) not in (int, float) or not math.isfinite(timeout_s) or not 1 <= timeout_s <= 60
            or type(retries) is not int or not 0 <= retries <= 5):
        raise SubmissionError("Timeout must be1..60 seconds and retries0..5")
    if type(token) is not str or not token or any(c.isspace() for c in token):
        raise SubmissionError("A nonempty team token is required")
    endpoint = server_url.rstrip("/") + "/api/submissions"
    opener = build_opener(_NoRedirect())
    for attempt in range(retries+1):
        request = Request(endpoint, data=body, method="POST", headers={
            "Content-Type": "application/json; charset=utf-8", "Accept": "application/json",
            "Authorization": "Bearer " + token})
        try:
            with opener.open(request, timeout=timeout_s) as response:
                raw = response.read(2_000_001)
                if len(raw) > 2_000_000:
                    raise SubmissionError("Leaderboard response exceeds the supplied client limit")
                answer = json.loads(raw.decode("utf-8"))
                if type(answer) is not dict:
                    raise SubmissionError("Leaderboard response must be a JSON object")
                return answer
        except HTTPError as error:
            # Authentication/schema failures are not retried. Error output must
            # never include the request headers or the credential.
            if error.code in (502, 503, 504) and attempt < retries:
                time.sleep(min(.25 * 2**attempt, 2.))
                continue
            raw = error.read(65536)
            try:
                detail = json.loads(raw.decode("utf-8"))
                error_detail = detail.get("error", detail)
                message = str(error_detail.get("message", "request rejected") if type(error_detail) is dict else error_detail)[:500]
                message = message.replace(token, "[redacted]")
            except (ValueError, UnicodeError, AttributeError):
                message = "request rejected"
            raise SubmissionError("HTTP " + str(error.code) + ": " + message) from None
        except (URLError, TimeoutError, ConnectionError) as error:
            if attempt < retries:
                time.sleep(min(.25 * 2**attempt, 2.))
                continue
            raise SubmissionError("Leaderboard connection failed after bounded retries") from None
        except (ValueError, UnicodeError) as error:
            if isinstance(error, SubmissionError):
                raise
            raise SubmissionError("Leaderboard returned invalid JSON") from None
    raise SubmissionError("Leaderboard connection failed")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--submission", type=Path, required=True)
    parser.add_argument("--server-url", required=True)
    tokens = parser.add_mutually_exclusive_group()
    tokens.add_argument("--token-env", help="Environment variable name. Default AP_LEADERBOARD_TEAM_TOKEN")
    tokens.add_argument("--token-file", type=Path, help="Private team-token file supplied by the instructor")
    parser.add_argument("--timeout-s", type=float, default=15.)
    parser.add_argument("--retries", type=int, default=2)
    args = parser.parse_args(argv)
    try:
        value = decode_json(args.submission.read_bytes())
        token = token_value(token_env=args.token_env, token_file=args.token_file)
        result = submit_payload(value, args.server_url, token, timeout_s=args.timeout_s, retries=args.retries)
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return 0
    except (OSError, ValueError) as error:
        print(json.dumps({"status": "SUBMISSION_REJECTED", "message": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
