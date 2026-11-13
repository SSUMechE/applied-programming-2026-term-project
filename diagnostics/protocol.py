"""Ungraded failure-report v1 contract. Standard library only, no execution."""
from datetime import datetime, timezone
import hashlib
import json
import math
import re
import uuid

SCHEMA_VERSION = "ap-diagnostic-report-v1"
MAX_REPORT_BYTES = 32768
CATEGORIES = ("physics_rejection", "planning_failure", "test_failure", "program_error", "environment_error", "interrupted")
STATUSES = ("new", "investigating", "resolved")
ARGUMENT_KEYS = frozenset(("scope", "difficulty", "task", "material", "contents", "design", "strategy",
    "seed", "max_queries", "endpoint_attempts", "wall_limit_s", "edge_timing_hook", "saved_tier", "rule", "output", "cuda"))
HEX40 = re.compile(r"[0-9a-f]{40}\Z")
HEX64 = re.compile(r"[0-9a-f]{64}\Z")
ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|[@-_])")
CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
CREDENTIAL = re.compile(r"(?i)(\b(?:authorization|password|passwd|access[_-]?token|api[_-]?key|token|secret)\b\s*[:=]\s*)(?:bearer\s+)?[^\s,;]+")
BEARER = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")


class DiagnosticError(ValueError):
    def __init__(self, code, message, status=400):
        super().__init__(message)
        self.code, self.status = code, status


def require(condition, code="INVALID_REPORT", message="Report does not match the diagnostics v1 contract.", status=400):
    if not condition:
        raise DiagnosticError(code, message, status)


def canonical_bytes(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def decode_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, "DUPLICATE_KEY", "JSON object keys must be unique.")
            result[key] = value
        return result
    def constant(_):
        raise DiagnosticError("INVALID_JSON", "Nonfinite JSON numbers are not supported.")
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=pairs, parse_constant=constant)
    except DiagnosticError:
        raise
    except (UnicodeError, ValueError, RecursionError):
        raise DiagnosticError("INVALID_JSON", "Use one valid UTF-8 JSON object.") from None


def sanitize_text(value, limit, *, secrets=()):
    """Remove control escapes and obvious credential text before queue/storage.

    Literal credential values known to the caller must be supplied via secrets.
    This is defense in depth, not a general sensitive-data discovery guarantee.
    HTML remains literal text and must be rendered with textContent, never HTML.
    """
    require(type(value) is str and type(limit) is int and limit >= 0)
    value = ANSI.sub("", value).replace("\r\n", "\n").replace("\r", "\n")
    value = CONTROL.sub("", value)
    for secret in sorted((s for s in secrets if type(s) is str and s), key=len, reverse=True):
        value = value.replace(secret, "[redacted]")
    value = BEARER.sub("Bearer [redacted]", value)
    value = CREDENTIAL.sub(lambda m: m.group(1)+"[redacted]", value)
    # Isolated surrogate code points are not valid UTF-8 report text.
    value = value.encode("utf-8", errors="replace").decode("utf-8")
    return value[:limit]


def exact_object(value, keys):
    require(type(value) is dict and set(value) == set(keys))


def text(value, limit, *, secrets=(), nonempty=False):
    require(type(value) is str and len(value) <= limit)
    value = sanitize_text(value, limit, secrets=secrets)
    require(not nonempty or bool(value.strip()))
    return value


def digest_or_none(value, pattern=HEX64):
    require(value is None or type(value) is str and pattern.fullmatch(value))
    return value


def report_id(value):
    require(type(value) is str and len(value) == 36)
    try:
        require(str(uuid.UUID(value)) == value)
    except (ValueError, AttributeError):
        raise DiagnosticError("INVALID_REPORT_ID", "Use a canonical UUID report ID.") from None
    return value


def validate_report(value, *, secrets=()):
    exact_object(value, ("schema_version", "report_id", "occurred_at_utc", "command", "source", "context", "failure", "environment"))
    require(value["schema_version"] == SCHEMA_VERSION)
    rid = report_id(value["report_id"])
    occurred = text(value["occurred_at_utc"], 40)
    try:
        stamp = datetime.fromisoformat(occurred.replace("Z", "+00:00"))
        require(stamp.tzinfo is not None and stamp.utcoffset().total_seconds() == 0)
        occurred = stamp.astimezone(timezone.utc).isoformat()
    except (ValueError, OverflowError):
        raise DiagnosticError("INVALID_TIMESTAMP", "Use an ISO timestamp with an explicit UTC offset.") from None
    command = value["command"]
    exact_object(command, ("kind", "exit_code", "duration_s", "arguments"))
    require(command["kind"] in ("check", "test", "plan", "review"))
    require(type(command["exit_code"]) is int and -(2**31) <= command["exit_code"] <= 2**32-1 and command["exit_code"] != 0)
    require(type(command["duration_s"]) in (int, float) and 0 <= command["duration_s"] <= 604800 and math.isfinite(command["duration_s"]))
    arguments = command["arguments"]
    require(type(arguments) is dict and set(arguments) <= ARGUMENT_KEYS)
    normalized_args = {}
    for key, item in arguments.items():
        require(item is None or type(item) in (str, bool, int, float))
        if type(item) is str:
            item = text(item, 240, secrets=secrets)
        if type(item) in (int, float):
            require(abs(item) <= 1e12 and math.isfinite(item))
        normalized_args[key] = item
    source = value["source"]
    exact_object(source, ("package_version", "git_commit", "evaluation_sources_sha256", "student_snapshot_sha256", "dirty"))
    require(source["dirty"] is None or type(source["dirty"]) is bool)
    source = dict(package_version=text(source["package_version"], 128, secrets=secrets, nonempty=True),
        git_commit=digest_or_none(source["git_commit"], HEX40),
        evaluation_sources_sha256=digest_or_none(source["evaluation_sources_sha256"]),
        student_snapshot_sha256=digest_or_none(source["student_snapshot_sha256"]), dirty=source["dirty"])
    context = value["context"]
    exact_object(context, ("difficulty", "phase", "rule_id", "run_directory", "contents", "case_id", "saved_plan_sha256", "native_run_id"))
    require(context["difficulty"] in (None, "easy", "medium", "hard"))
    require(context["contents"] in (None, "beads", "empty"))
    run = text(context["run_directory"], 240, secrets=secrets).replace("\\", "/")
    require(not run.startswith("/") and ":" not in run and ".." not in run.split("/"))
    context = dict(difficulty=context["difficulty"], contents=context["contents"], run_directory=run,
        phase=text(context["phase"], 128, secrets=secrets), rule_id=text(context["rule_id"], 128, secrets=secrets),
        case_id=text(context["case_id"], 128, secrets=secrets), saved_plan_sha256=digest_or_none(context["saved_plan_sha256"]),
        native_run_id=None if context["native_run_id"] is None else report_id(context["native_run_id"]))
    failure = value["failure"]
    exact_object(failure, ("category", "code", "exception_type", "message", "traceback_tail"))
    require(failure["category"] in CATEGORIES)
    failure = dict(category=failure["category"], **{key: text(failure[key], size, secrets=secrets)
        for key, size in (("code", 128), ("exception_type", 128), ("message", 2048), ("traceback_tail", 8192))})
    environment = value["environment"]
    exact_object(environment, ("python", "platform", "torch", "genesis", "gpu_name"))
    environment = {key: None if item is None and key not in ("python", "platform") else text(item, 128, secrets=secrets)
        for key, item in environment.items()}
    result = dict(schema_version=SCHEMA_VERSION, report_id=rid, occurred_at_utc=occurred,
        command=dict(kind=command["kind"], exit_code=command["exit_code"], duration_s=command["duration_s"], arguments=normalized_args),
        source=source, context=context, failure=failure, environment=environment)
    encoded = canonical_bytes(result)
    require(len(encoded) <= MAX_REPORT_BYTES, "REPORT_TOO_LARGE", "Report exceeds the 32 KiB diagnostics limit.", 413)
    require(not any(secret.encode("utf-8") in encoded for secret in secrets if type(secret) is str and secret),
        "SENSITIVE_METADATA", "Credential values are not permitted in diagnostic identity fields.")
    return result


def report_signature(value):
    """Group stable error identities, omitting clocks, messages and student edits."""
    signature = dict(version=value["source"]["package_version"], source=value["source"]["evaluation_sources_sha256"],
        command=value["command"]["kind"], category=value["failure"]["category"], code=value["failure"]["code"],
        exception_type=value["failure"]["exception_type"], **{k: value["context"][k]
        for k in ("phase", "case_id", "difficulty", "contents")})
    return hashlib.sha256(canonical_bytes(signature)).hexdigest()
