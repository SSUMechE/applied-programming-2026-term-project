"""Public identity projection outside the frozen evaluator and scoring rules."""
from __future__ import annotations

from http import HTTPStatus
import json
import re


PUBLIC_BOARD_PATHS = frozenset(("/api/v3/board", "/api/leaderboard"))
PUBLIC_ROW_FIELDS = (
    "rank", "carried_distance_mm", "completion_ticks", "simulation_seconds",
    "C", "C_exact", "competition_adjustment", "competition_adjustment_exact",
    "population_n", "valid_carry_count", "required_carry_count",
    "outcome", "trust_label", "evaluation_mode",
)
MAX_BOARD_BYTES = 16 * 1024 * 1024


def public_board_rows(rows):
    """Keep score fields and ID suffixes without mutating stored identities."""
    if not isinstance(rows, list):
        raise ValueError("Board rows must be a list.")
    result = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("display_name"), str):
            raise ValueError("Board identity must be text.")
        public = {key: row[key] for key in PUBLIC_ROW_FIELDS if key in row}
        # Redact historic names containing IDs as well as the current ID-only
        # naming scheme. A constant mask reveals no prefix or original length.
        public["display_name"] = re.sub(
            r"\d{5,}", lambda match: "****" + match[0][-4:], row["display_name"])
        result.append(public)
    return result


def public_board(value):
    if not isinstance(value, dict) or "rows" not in value:
        raise ValueError("Expected a board object.")
    public = {**value, "rows": public_board_rows(value["rows"])}
    if "staging_rows" in value:
        public["staging_rows"] = public_board_rows(value["staging_rows"])
    return public


class PublicIdentityApplication:
    """Mask only public board GET responses, after ranking/freeze retrieval.

    Authentication, private team history, instructor APIs and stored records
    pass through unchanged. Keeping this projection outside leaderboard/ also
    preserves the source hashes bound by the released physics rules.
    """
    def __init__(self, application):
        self.application = application

    def __getattr__(self, name):
        return getattr(self.application, name)

    @staticmethod
    def _reply(start_response, code, value, headers=()):
        body = json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False).encode("utf-8")
        kept = [(name, value) for name, value in headers
                if name.lower() not in ("content-type", "content-length", "content-encoding",
                                        "transfer-encoding", "cache-control")]
        kept.extend((("Content-Type", "application/json; charset=utf-8"),
                     ("Content-Length", str(len(body))), ("Cache-Control", "no-store")))
        start_response(f"{code} {HTTPStatus(code).phrase}", kept)
        return [body]

    def __call__(self, environ, start_response):
        if (environ.get("REQUEST_METHOD", "GET") != "GET"
                or environ.get("PATH_INFO", "") not in PUBLIC_BOARD_PATHS):
            return self.application(environ, start_response)
        captured, chunks = {}, []
        size = 0

        def write(data):
            nonlocal size
            if not isinstance(data, bytes):
                raise ValueError("Expected response bytes.")
            size += len(data)
            if size > MAX_BOARD_BYTES:
                raise ValueError("Board response is too large.")
            chunks.append(data)

        def capture(status, headers, exc_info=None):
            captured.update(code=int(status.split()[0]), headers=list(headers))
            return write

        try:
            response = self.application(environ, capture)
            try:
                for chunk in response:
                    write(chunk)
            finally:
                close = getattr(response, "close", None)
                if close:
                    close()
            code = captured["code"]
            headers = captured["headers"]
            # Never forward an unprojected error body or unexpected content.
            # Preserve the original error status without exposing its details.
            if code != 200:
                if code not in HTTPStatus._value2member_map_ or not 400 <= code <= 599:
                    code = 503
                return self._reply(start_response, code, {
                    "error": {"code": "PUBLIC_BOARD_UNAVAILABLE",
                              "message": "The public board is unavailable. Try Refresh."}}, headers)
            media = next((v for k, v in headers if k.lower() == "content-type"), "")
            if media.split(";", 1)[0].strip().lower() != "application/json":
                raise ValueError("Expected a JSON board.")
            value = public_board(json.loads(b"".join(chunks)))
            return self._reply(start_response, 200, value, headers)
        except Exception:
            return self._reply(start_response, 503, {
                "error": {"code": "PUBLIC_BOARD_UNAVAILABLE",
                          "message": "The public board is unavailable. Try Refresh."}})
