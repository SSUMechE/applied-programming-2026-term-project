"""Run the unchanged course commands and queue bounded instructor diagnostics.

This module runs in the parent only. It never imports a simulator, changes a
saved plan, edits native evidence, or changes the child's success/failure code.
"""
from __future__ import annotations

import argparse
from collections import deque
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import re
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .protocol import (ARGUMENT_KEYS, SCHEMA_VERSION, canonical_bytes,
                       sanitize_text, validate_report)
from .process_tree import OwnedJob

PACKAGE_VERSION = "term_v3-diagnostics-v1"
DEFAULT_SERVER = "https://phoniness-reenact-veggie.ngrok-free.dev"
COMMANDS = {
    "check": ["check_environment.py"],
    "test": ["-m", "unittest"],
    "plan": ["-m", "physics.plan_student_transport"],
    "review": ["-m", "physics.review_tier_transport"],
}
ROOT = Path(__file__).resolve().parents[1]
CONFIG = "output/diagnostics_config.json"
QUEUE = "output/diagnostics_queue"
LIMIT = 16 * 1024 * 1024


def read_json(path, limit=LIMIT):
    try:
        with Path(path).open("rb") as stream:
            data = stream.read(limit + 1)
        if len(data) > limit:
            return {}
        value = json.loads(data)
        return value if type(value) is dict else {}
    except (OSError, ValueError):
        return {}


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("wb", dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(canonical_bytes(value) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()


def server_url(value):
    parts = urlsplit(value)
    if (parts.scheme not in ("https", "http") or not parts.hostname
            or parts.username or parts.password or parts.query or parts.fragment
            or parts.path not in ("", "/")
            or parts.scheme == "http" and parts.hostname not in ("localhost", "127.0.0.1", "::1")):
        raise ValueError("Use the class HTTPS base URL.")
    return value.rstrip("/")


def option(argv, name):
    key = "--" + name.replace("_", "-")
    found = None
    for i, arg in enumerate(argv):
        if arg == "--":
            break
        if arg.startswith(key + "="):
            found = arg[len(key) + 1:]
        if arg == key:
            found = argv[i + 1] if i + 1 < len(argv) and not argv[i + 1].startswith("--") else True
    return found


def configuration(root, argv=()):
    stored = read_json(root / CONFIG, 4096)
    url = option(argv, "server_url") or os.environ.get("AP_LEADERBOARD_URL") or stored.get("server") or DEFAULT_SERVER
    token_file = (option(argv, "token_file") or os.environ.get("AP_LEADERBOARD_TOKEN_FILE")
                  or stored.get("token_file"))
    token = ""
    try:
        if isinstance(token_file, str):
            raw = Path(token_file).expanduser()
            raw = raw if raw.is_absolute() else root / raw
            token_file = str(raw.resolve())
            token = Path(token_file).read_text(encoding="utf-8").strip()
        else:
            token = os.environ.get("AP_LEADERBOARD_TEAM_TOKEN", "").strip()
        if not token or any(c.isspace() for c in token) or len(token) > 4096:
            token = ""
    except (OSError, UnicodeError):
        token = ""
    return {"server": server_url(str(url)), "token_file": token_file, "token": token}


def child_command(kind, argv):
    if kind not in COMMANDS:
        raise ValueError("Unknown course command.")
    extra = list(argv) if argv or kind != "test" else ["tests.test_student_strategy"]
    return [sys.executable, "-B", *COMMANDS[kind], *extra]


def console(value, *, error=False):
    """A closed viewer or limited terminal encoding must not abort simulation."""
    try:
        stream = sys.stderr if error else sys.stdout
        stream.write(value)
        stream.flush()
    except (OSError, UnicodeError, ValueError):
        pass


def clean_text(value, root, limit, secrets=()):
    value = str(value or "")
    for path, replacement in ((root.resolve(), "<project>"), (Path.home(), "<home>")):
        for spelling in (str(path), path.as_posix()):
            value = re.sub(re.escape(spelling), lambda _: replacement, value, flags=re.I)
    # Quoted traceback/error paths may contain spaces or UNC network prefixes.
    value = re.sub(r"""(["'])((?:[A-Za-z]:[\\/]|\\\\|/)[^"'\r\n]+)\1""",
                   lambda m: m.group(1) + "<path>/" + re.split(r"[\\/]", m.group(2))[-1] + m.group(1), value)
    # Be conservative about unquoted Windows paths, including spaces.
    value = re.sub(r"(?i)(?:[a-z]:[\\/]|\\\\)[^\r\n\"'<>]+",
                   lambda m: "<path>/" + re.split(r"[\\/]", m.group())[-1], value)
    value = re.sub(r"(?<![\w:/])/(?:[^\s/\"'<>]+/)+[^\s\"'<>]*",
                   lambda m: "<path>/" + m.group().rsplit("/", 1)[-1], value)
    return sanitize_text(value, limit, secrets=secrets)


def exit_status(code):
    # CPython's SystemExit conversion uses a signed C int on Windows. Preserve
    # the child's DWORD, including CTRL-C/native exception statuses.
    return code - 2**32 if os.name == "nt" and 2**31 <= code < 2**32 else code


def traceback_only(text, root, secrets=()):
    """Keep stack locations and terminal exception, never source-code lines."""
    selected = []
    exception_type, message = "", ""
    for line in text.splitlines():
        if re.match(r'^\s*File "[^"]+", line \d+(?:, in [\w<>.]+)?$', line):
            selected.append(clean_text(line.strip(), root, 300, secrets))
        else:
            match = re.match(r"^([\w.]+(?:Error|Exception|Rejected|Exceeded|Interrupt|Exit))(?:: (.*))?$", line)
            if match:
                exception_type = match.group(1).rsplit(".", 1)[-1]
                message = match.group(2) or ""
                selected.append(clean_text(line, root, 2048, secrets))
    return "\n".join(selected)[-8192:], exception_type[:128], clean_text(message, root, 2048, secrets)


def metadata_source(root, argv):
    def git(*args):
        result = subprocess.run(["git", "-C", str(root), *args], capture_output=True,
                                text=True, timeout=5)
        return result.stdout.strip() if result.returncode == 0 else None
    commit, dirty, student_hash, sources_hash = None, None, None, None
    try:
        commit = git("rev-parse", "HEAD")
        commit = commit if commit and re.fullmatch("[0-9a-f]{40}", commit) else None
        status = git("status", "--porcelain", "--untracked-files=normal")
        dirty = bool(status) if status is not None else None
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        from leaderboard.v3 import capture_student_snapshot
        student_hash = capture_student_snapshot(root)["snapshot_sha256"]
    except Exception:
        pass
    difficulty = option(argv, "difficulty")
    rule = option(argv, "rule")
    if not isinstance(rule, str) and difficulty in ("easy", "medium", "hard"):
        rule = f"leaderboard/rules/term_v3/ap2026-{difficulty}-beads-term-v3.json"
    if isinstance(rule, str):
        mapping = read_json(root / rule).get("evaluation_sources", {})
        if type(mapping) is dict and mapping:
            actual = {}
            try:
                for name in mapping:
                    path = (root / name).resolve()
                    if not path.is_relative_to(root.resolve()):
                        raise ValueError("source outside project")
                    actual[name] = hashlib.sha256(path.read_bytes()).hexdigest()
                sources_hash = hashlib.sha256(canonical_bytes(actual)).hexdigest()
            except (OSError, ValueError):
                pass
    return dict(package_version=PACKAGE_VERSION, git_commit=commit,
                evaluation_sources_sha256=sources_hash,
                student_snapshot_sha256=student_hash, dirty=dirty)


def environment():
    def version(name):
        try:
            return importlib.metadata.version(name)[:128]
        except importlib.metadata.PackageNotFoundError:
            return None
    return dict(python=platform.python_version(), platform=f"{platform.system()} {platform.release()} {platform.machine()}"[:128],
                torch=version("torch"), genesis=version("genesis-world"), gpu_name=None)


def fingerprint(path):
    try:
        stat = path.stat()
        return stat.st_mtime_ns, stat.st_size, hashlib.sha256(path.read_bytes()).hexdigest() if stat.st_size <= LIMIT else None
    except OSError:
        return None


def observed_files(root, kind, argv):
    output = option(argv, "output")
    if not isinstance(output, str):
        return {}
    out = (root / output).resolve()
    if kind == "check":
        return {"environment": out}
    files = {name: out / name for name in (
        "REVIEW_SUPERVISOR.json", "CPU_PLANNING_RESULT.json", "CPU_PROCESS_RESULT.json",
        "result.json", "LEADERBOARD_STATUS.json", "worker_stdout.log")}
    if kind == "review":
        files["review_worker.log"] = out.with_suffix(".worker.log")
    return files


def fresh_records(files, before):
    result = {}
    for name, path in files.items():
        current = fingerprint(path)
        if current is None or current == before.get(name):
            continue
        if path.suffix == ".log":
            try:
                with path.open("rb") as stream:
                    stream.seek(max(0, path.stat().st_size - 65536))
                    raw = stream.read(65536)
                result[name] = {"traceback_text": raw.decode("utf-8", errors="replace")}
            except OSError:
                pass
        else:
            result[name] = read_json(path)
    return result


def make_report(root, kind, argv, exit_code, duration_s, tail, records, source, token=""):
    secrets = (token,) if token else ()
    tail += "\n".join(records.get(key, {}).get("traceback_text", "")
                      for key in ("worker_stdout.log", "review_worker.log"))
    trace, exception, message = traceback_only(tail, root, secrets)
    supervisor = records.get("REVIEW_SUPERVISOR.json", {})
    cpu = records.get("CPU_PLANNING_RESULT.json", {})
    native = records.get("result.json", {})
    failure = supervisor.get("worker_failure") or cpu.get("failure") or native.get("failure") or {}
    if type(failure) is dict and (failure.get("type") != "CpuWorkerDidNotComplete" or not exception):
        exception = str(failure.get("type") or exception)[:128]
        message = clean_text(failure.get("message") or message, root, 2048, secrets)
    if not trace and isinstance(native.get("traceback"), str):
        trace, parsed_type, parsed_message = traceback_only(native["traceback"], root, secrets)
        exception, message = exception or parsed_type, message or parsed_message
    code = str(supervisor.get("outcome") or cpu.get("status") or "")
    env_record = records.get("environment", {})
    if kind == "check" and env_record.get("schema_version") == "applied-programming-environment-check-v1":
        code = str(env_record.get("status") or code)
        differences = []
        for row in env_record.get("package_versions", []):
            if type(row) is dict and row.get("passed") is False:
                differences.append(str(row.get("package")) + ": expected " + str(row.get("expected"))
                                   + ", found " + str(row.get("actual")))
        for row in env_record.get("SDK_source_checks", []):
            if type(row) is dict and row.get("passed") is False:
                differences.append("SDK source mismatch: " + str(row.get("source")))
        for name, passed in env_record.get("platform_checks", {}).items():
            if passed is False:
                differences.append("Platform mismatch: " + str(name))
        probe = env_record.get("CUDA_probe", {})
        if type(probe) is dict and probe.get("passed") is False:
            exception = str(probe.get("error_type") or exception)[:128]
            differences.append(str(probe.get("message") or "CUDA smoke check failed"))
        if differences:
            message = clean_text("\n".join(differences[:24]), root, 2048, secrets)
    category = "program_error"
    if exit_code in (-2, 130, 3221225786, -1073741510) or exception == "KeyboardInterrupt":
        category, code = "interrupted", "USER_INTERRUPTED"
    elif kind == "check" or any(word in exception + " " + message for word in ("ImportError", "ModuleNotFoundError", "DLL", "CUDA", "OutOfMemory")):
        category = "environment_error"
    elif "PhysicalTransportRejected" in exception or code == "PHYSICAL_REJECTED":
        category, code = "physics_rejection", "PHYSICAL_REJECTED"
        reason = re.search(r"PHYSICAL_REJECTED:\s*([A-Za-z0-9_:,.-]+)", message)
        if reason:
            code += ":" + reason.group(1)
    elif kind == "test":
        category, code = "test_failure", "PROVIDED_TESTS_FAILED"
    elif kind == "plan" and exception in ("ContractError", "PlanningBudgetExceeded", "CpuWorkerDidNotComplete",
                                           "PathNotFound", "NoPathFound", "PlanningFailure"):
        category, code = "planning_failure", code or "CPU_PLAN_FAILED"
    if "NotImplemented" in exception or "NotImplementedError" in tail:
        code = "STUDENT_NOT_IMPLEMENTED"
    if not message:
        # Only known CLI failure lines are admitted, never arbitrary stdout.
        lines = [line.split(": error: ", 1)[1] for line in tail.splitlines() if ": error: " in line]
        message = clean_text(lines[-1] if lines else "Command exited without a completed success receipt. Retain the local output for investigation.", root, 2048, secrets)
    proof = native.get("device_tier_execution") or {}
    prefix = proof.get("failed_phase_prefix") or {}
    phase = prefix.get("purpose") or prefix.get("phase") or prefix.get("stage_id") or ""
    if type(phase) is not str:
        phase = ""
    args = {}
    for key in ARGUMENT_KEYS:
        item = option(argv, key)
        if item is not None:
            args[key] = clean_text(item, root, 240, secrets) if isinstance(item, str) else item
    out = option(argv, "output")
    run_dir = ""
    if isinstance(out, str):
        try:
            run_dir = (root / out).resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            run_dir = ""
    rule_arg = option(argv, "rule")
    rule = read_json(root / rule_arg) if isinstance(rule_arg, str) else {}
    native_id = records.get("LEADERBOARD_STATUS.json", {}).get("run_id")
    try:
        native_id = str(uuid.UUID(native_id))
    except (ValueError, TypeError, AttributeError):
        native_id = None
    plan_sha = supervisor.get("saved_file_sha256")
    if not isinstance(plan_sha, str) or not re.fullmatch("[0-9a-f]{64}", plan_sha):
        saved = option(argv, "saved_tier")
        plan_sha = None
        if isinstance(saved, str):
            path = root / saved
            try:
                plan_sha = hashlib.sha256(path.read_bytes()).hexdigest()
            except OSError:
                pass
    contents = option(argv, "contents") or option(argv, "material") or ("beads" if kind == "review" else None)
    task = option(argv, "task")
    report = dict(schema_version=SCHEMA_VERSION, report_id=str(uuid.uuid4()),
        occurred_at_utc=datetime.now(timezone.utc).isoformat(),
        command=dict(kind=kind, exit_code=exit_code, duration_s=min(duration_s, 604800), arguments=args),
        source=source, environment=environment(),
        context=dict(difficulty=option(argv, "difficulty") if option(argv, "difficulty") in ("easy", "medium", "hard") else None,
            phase=clean_text(phase, root, 128, secrets), rule_id=str(rule.get("rule_id", ""))[:128],
            run_directory=run_dir[:240], contents=contents if contents in ("beads", "empty") else None,
            case_id=Path(task).name[:128] if isinstance(task, str) else "",
            saved_plan_sha256=plan_sha, native_run_id=native_id),
        failure=dict(category=category, code=clean_text(code or "COMMAND_FAILED", root, 128, secrets),
            exception_type=clean_text(exception, root, 128, secrets), message=message, traceback_tail=trace))
    return validate_report(report, secrets=secrets)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def upload(path, config, timeout=4):
    report = validate_report(read_json(path, 32769), secrets=(config["token"],))
    status_path = path.with_suffix(".status.json")
    status = dict(report_id=report["report_id"], status="pending", reason="TEAM_TOKEN_REQUIRED")
    if config["token"]:
        request = Request(server_url(config["server"]) + "/api/diagnostics/reports",
            data=canonical_bytes(report), method="POST",
            headers={"Authorization": "Bearer " + config["token"], "Content-Type": "application/json",
                     "Accept": "application/json", "ngrok-skip-browser-warning": "true"})
        try:
            with build_opener(NoRedirect()).open(request, timeout=timeout) as response:
                answer = json.loads(response.read(32769))
            if (answer.get("status") != "accepted" or answer.get("report_id") != report["report_id"]
                    or answer.get("body_sha256") != hashlib.sha256(canonical_bytes(report)).hexdigest()):
                raise ValueError("Invalid acknowledgment")
            status = dict(report_id=report["report_id"], status="uploaded",
                          sequence=answer.get("sequence"), duplicate=answer.get("duplicate") is True)
        except HTTPError as error:
            status["reason"] = "HTTP_" + str(error.code)
        except (OSError, ValueError, URLError):
            status["reason"] = "UPLOAD_UNAVAILABLE"
    atomic_json(status_path, status)
    return status


def retry(root, config, *, maximum=50, budget_s=20):
    results, started = [], time.monotonic()
    for path in sorted((root / QUEUE).glob("*.json")):
        if path.name.endswith(".status.json") or read_json(path.with_suffix(".status.json")).get("status") == "uploaded":
            continue
        if len(results) >= maximum or time.monotonic() - started >= budget_s:
            break
        try:
            results.append(upload(path, config, timeout=max(.2, min(4, budget_s - (time.monotonic() - started)))))
        except Exception:
            results.append({"report_id": path.stem, "status": "pending", "reason": "LOCAL_REPORT_UNREADABLE"})
        if not config["token"]:
            break
    return results


def stop_owned_child(process):
    """Interrupt our command tree, allow its supplied supervisor to clean up."""
    if process.poll() is not None:
        return
    try:
        if os.name == "nt":
            process.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            os.killpg(process.pid, signal.SIGINT)
        process.wait(timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        if os.name == "nt":
            # Windows descendants are in OwnedJob and close in finally.
            process.kill()
        else:
            os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=15)


def run_course(root, kind, argv):
    root = Path(root).resolve()
    try:
        config = configuration(root, argv)
    except (ValueError, OSError):
        config = {"server": DEFAULT_SERVER, "token_file": None, "token": ""}
    files = observed_files(root, kind, argv)
    before = {name: fingerprint(path) for name, path in files.items()}
    source = metadata_source(root, argv)
    env = os.environ.copy()
    # The existing evaluator reads these same environment variables. No solver
    # option, cache setting, command flag, trajectory or clock is changed.
    if config["token_file"] and not env.get("AP_LEADERBOARD_TOKEN_FILE"):
        env["AP_LEADERBOARD_TOKEN_FILE"] = str(config["token_file"])
    if not env.get("AP_LEADERBOARD_URL"):
        env["AP_LEADERBOARD_URL"] = config["server"]
    started, tail, process, job = time.monotonic(), deque(maxlen=256), None, None
    exit_code = 1
    try:
        job = OwnedJob()
        process = subprocess.Popen(child_command(kind, argv), cwd=root, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace",
            creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP | 0x4) if os.name == "nt" else 0,
            start_new_session=os.name != "nt")
        job.assign(process)
        job.resume(process)
        for line in iter(lambda: process.stdout.readline(8192), ""):
            console(line)
            tail.append(line[-2048:])
        exit_code = process.wait()
    except KeyboardInterrupt:
        if process:
            stop_owned_child(process)
            exit_code = process.returncode if process.returncode is not None else 130
        else:
            exit_code = 130
        tail.append("KeyboardInterrupt: Command interrupted by user.\n")
    except OSError as error:
        if process and process.poll() is None:
            stop_owned_child(process)
        tail.append(type(error).__name__ + ": Unable to launch the supplied command.\n")
        console("Unable to launch the supplied command.\n", error=True)
    finally:
        if job:
            job.close()
        if process and process.stdout:
            process.stdout.close()
    duration = time.monotonic() - started
    # Reporting failure must never turn a child success into failure or hide a
    # child failure. The native receipt remains the scoring authority.
    try:
        if exit_code:
            report = make_report(root, kind, argv, exit_code, duration, "".join(tail),
                                 fresh_records(files, before), source, config["token"])
            path = root / QUEUE / (report["report_id"] + ".json")
            atomic_json(path, report)
            state = upload(path, config)
            console("Diagnostic " + state["status"] + ": " + report["report_id"] + "\n")
        retry(root, config, maximum=2, budget_s=4)
    except (Exception, KeyboardInterrupt):
        console("Diagnostic upload unavailable. Keep this command's local output.\n", error=True)
    return exit_code


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in COMMANDS:
        return run_course(ROOT, argv[0], argv[1:])
    parser = argparse.ArgumentParser(description="Supplied course commands with automatic instructor diagnostics.")
    commands = parser.add_subparsers(dest="action", required=True)
    setup = commands.add_parser("configure", help="Store the team token file path once, never the token.")
    setup.add_argument("--token-file", required=True)
    setup.add_argument("--server", default=DEFAULT_SERVER)
    commands.add_parser("retry", help="Retry queued diagnostics and full-review records without simulation.")
    for name in COMMANDS:
        commands.add_parser(name, help="Forward remaining arguments unchanged to the supplied " + name + " command.")
    args = parser.parse_args(argv)
    if args.action == "configure":
        path = Path(args.token_file).expanduser().resolve()
        if path.is_relative_to(ROOT):
            parser.error("Keep the team token file outside the repository.")
        try:
            config = configuration(ROOT, ["--token-file", str(path), "--server-url", args.server])
            if not config["token"]:
                parser.error("The token file must contain the instructor-provided team token.")
            atomic_json(ROOT / CONFIG, {"server": config["server"], "token_file": str(path)})
        except (OSError, ValueError):
            parser.error("Could not configure the token file and class HTTPS address.")
        print("REPORTING_CONFIGURED: only the token file path is stored in ignored output/.")
        return 0
    if args.action == "retry":
        config = configuration(ROOT)
        outcomes = retry(ROOT, config)
        print(json.dumps({"diagnostics": outcomes, "simulation_launched": False}))
        from leaderboard.auto_record import retry_queue
        result = retry_queue(ROOT / "output/leaderboard_queue", server_url=config["server"],
                             token_file=config["token_file"])
        print(json.dumps(result))
        return 0
    return 2
