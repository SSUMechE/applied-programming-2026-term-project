"""Local, best-effort guidance. Never create an artifact or change a command result."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import stat
import sys


CPU = {"plan", "plan-arm", "merge-arms"}
SUMMARY = {**dict.fromkeys(CPU, "CPU_PLANNING_RESULT.json"),
           "review": "REVIEW_SUPERVISOR.json", "practice": "PRACTICE_RESULT.json"}
ACTION = {"plan": "CPU plan", "plan-arm": "CPU arm plan", "merge-arms": "CPU arm merge",
          "review": "Full-mission review", "practice": "Single-arm practice"}
PROOF = "device_tier_execution/TIER_DEVICE_RESULT.json"
LIMIT = 4 * 1024 * 1024


def _output(argv):
    value = None
    for i, arg in enumerate(argv):
        if arg == "--":
            break
        if arg == "--output":
            value = argv[i + 1] if i + 1 < len(argv) else None
        elif arg.startswith("--output="):
            value = arg.partition("=")[2]
    if value and not value.startswith("-") and all(ord(c) >= 32 for c in value):
        return Path(value)


def _absent(path):
    try:
        path.lstat()
    except FileNotFoundError:
        return True
    return False


def _regular(path):
    if path is None:
        return False
    try:
        return stat.S_ISREG(path.lstat().st_mode) and path.resolve(strict=True) == path
    except OSError:
        return False


def _read(path):
    if not _regular(path):
        return {}
    try:
        with path.open("rb") as stream:
            raw = stream.read(LIMIT + 1)
    except OSError:
        return {}
    if len(raw) > LIMIT:
        return {}
    try:
        data = json.loads(raw)
    except (ValueError, RecursionError):
        return {}
    return data if isinstance(data, dict) else {}


def watch(root, action, argv):
    """Remember only paths that do not exist yet. All supported commands require fresh output."""
    if action not in SUMMARY or any(arg in ("-h", "--help", "--worker") for arg in argv):
        return None
    try:
        given = _output(argv)
        paths = {}
        existing = False
        if given is not None:
            out = Path(os.path.abspath(Path(root) / given))
            existing = not _absent(out)
            if not existing:
                names = [SUMMARY[action]]
                if action in CPU:
                    names += ["CPU_PROCESS_RESULT.json", "worker_stdout.log"]
                if action == "review":
                    names += [PROOF]
                if action == "practice":
                    names += ["PRACTICE_WORKER.json"]
                paths = {name: out / name for name in names}
                if action in ("review", "practice"):
                    log = out.with_suffix(".worker.log")
                    if _absent(log):
                        paths["worker log"] = log
        return action, paths, existing
    except (OSError, ValueError, RuntimeError):
        return None


def _physical(action, receipt, proof):
    failure = receipt.get("worker_failure" if action == "review" else "failure")
    message = failure.get("message") if isinstance(failure, dict) else None
    if action == "practice":
        worker = receipt.get("worker")
        native = worker.get("native") if isinstance(worker, dict) else None
        proof = native if isinstance(native, dict) else {}
    prefix = proof.get("failed_phase_prefix")
    purpose = prefix.get("purpose") if isinstance(prefix, dict) else None
    # Only repeat labels emitted by the native producer. For settle/release
    # phases, the purpose (e.g. arm_1__carry__release) is not the phase itself.
    stage = None
    if isinstance(purpose, str) and (purpose in ("initial_settle", "practice_initial_settle")
            or re.fullmatch(r"arm_[012]__(approach|carry)(__(endpoint_settle|supported_hold|release|released_settle))?", purpose)):
        stage = purpose
    phase = prefix.get("phase") if isinstance(prefix, dict) else None
    if phase in ("settle", "approach", "carry", "place", "release", "released_settle"):
        actor = prefix.get("active_arm_id")
        info = "phase: " + phase + (", " + actor if actor in ("arm_0", "arm_1", "arm_2") else "")
        stage = stage + " (" + info + ")" if stage else info
    if stage:
        yield "Stage: " + ACTION[action] + " / " + stage
    else:
        yield "Action: " + ACTION[action]
    if not isinstance(message, str) or failure.get("type") != "PhysicalTransportRejected":
        return
    if "handling:unheld_root_displacement_exceeded" in message:
        yield "The unheld cup did not pass its displacement check. Next: inspect its support and nearby motion before changing the path or layout."
    elif "control:acceleration_limit" in message:
        yield "Measured joint acceleration exceeded a control limit. Next: inspect the failed motion and its timing before re-exporting. See README 9.1."


def _early(action, tail, existing):
    text = tail.lower()
    label = ACTION[action]
    if "runtime differs from this rule version" in text:
        return (f"Stage: {label} / rule environment validation",
                "The evaluator runtime did not match the selected rule. Next: activate the course environment and run python -B project_run.py check --cuda.")
    if "use a fresh" in text or "choose a fresh output workspace" in text:
        return (f"Stage: {label} / output validation",
                "The command refused to reuse an output location. Next: keep the old output and choose a fresh name.")
    if "the following arguments are required" in text or "expected one argument" in text or "unrecognized arguments" in text:
        return f"Stage: {label} / command arguments", "The command rejected its arguments. Next: check the first command-line error."
    if "no such file or directory" in text or "cannot find the file specified" in text:
        return f"Action: {label}", "A requested file or directory was not found. Next: check the name in the original error."
    if '"status": "design_input_error"' in text:
        return f"Action: {label}", "The command reported an input error. Next: check the design or input named in the original message."
    if existing:
        return f"Action: {label}", "The requested output already existed. Next: check the original error, not the old result files."
    return f"Action: {label}", "Next: check the original terminal error before starting another run."


def producer_signal(action, line):
    """Recognize THIS child's parent-only emission after it claimed the output.

    CPU workers send all their own stdout to a separate log. Their parents
    print these compact JSON headers only after saving this invocation's result.
    The review parent announces its worker after exclusively creating the log.
    Match the header only, since arm tracebacks can overflow the console tail.
    """
    if not isinstance(line, str):
        return False
    if action == "plan":
        return line.startswith('{"status": "CPU_PLAN_FAILED", "output": "')
    if action in ("plan-arm", "merge-arms"):
        return bool(re.match(r'^\{"status": "CPU_ARM_OPERATION_FAILED", "completed_stages": \d+, "process_wall_s": ', line))
    if action == "review":
        return (line.startswith('{"event": "Applied Programming full mission worker started", "difficulty": "')
                or bool(re.match(r'^\{"outcome": "[A-Z0-9_]+", "review_accepted": (true|false), "full_mission_passed": ', line)))
    return False


def _lines(watched, tail, owned=False):
    action, paths, existing = watched
    # Mere absence at watch time is insufficient: another command may create
    # the same output before our own command even passes argument validation.
    if existing or not (owned or any(producer_signal(action, line) for line in tail.splitlines())):
        yield from _early(action, tail, existing)
        yield "No new result summary confirmed for this command."
        return
    fresh = {name: path for name, path in paths.items() if _regular(path)}
    summary = fresh.get(SUMMARY[action])
    if not summary:
        yield from _early(action, tail, existing)
        yield "No new result summary found for this command."
    else:
        receipt = _read(summary)
        fields = {"review": "worker_failure and outcome", "practice": "failure and failed_checks"}
        if action in CPU and receipt:
            from .cpu_failure_help import cpu_failure_help
            stage, reason, next_check = cpu_failure_help(action, receipt, process=_read(fresh.get("CPU_PROCESS_RESULT.json")))
            yield "Stage: " + stage
            yield "Reason: " + reason
            yield "Next: " + next_check
        elif action not in CPU:
            yield from _physical(action, receipt, _read(fresh.get(PROOF)))
        else:
            yield "Action: " + ACTION[action]
        yield f"Open {summary} (check {fields.get(action, 'failure.type and failure.message')})."
    if PROOF in fresh:
        yield f"Phase detail: {fresh[PROOF]} (check failed_phase_prefix if present)."
    elif not summary:
        for name in ("CPU_PROCESS_RESULT.json", "PRACTICE_WORKER.json", "worker_stdout.log", "worker log"):
            if name in fresh:
                yield f"Worker detail: {fresh[name]}"
                break
    elif action in CPU and isinstance(receipt.get("failure"), dict) and receipt["failure"].get("type") == "CpuWorkerDidNotComplete":
        for name in ("CPU_PROCESS_RESULT.json", "worker_stdout.log"):
            if name in fresh:
                yield f"Worker detail: {fresh[name]}"


def explain(watched, tail="", *, owned=False):
    if watched is None:
        return
    try:
        print("Local failure help:\n" + "\n".join(_lines(watched, tail, owned)), file=sys.stderr, flush=True)
    except Exception:
        # Missing dependencies, a closed console, partial JSON or a disappearing
        # directory must never change the original result or hide its error.
        pass


@contextmanager
def practice_messages(root, argv):
    # Practice resolves its relative output against the invocation's cwd, not
    # the script directory. The root is used only to verify the exit's origin.
    watched = watch(Path.cwd(), "practice", argv)
    try:
        yield
    except SystemExit as exc:
        if exc.code:
            # Both supplied practice parents return 1 only AFTER writing their
            # final summary. The CLI then raises SystemExit at module scope.
            # A parser refusal or exit inside other code is not that return.
            tb = exc.__traceback__
            while tb and tb.tb_next:
                tb = tb.tb_next
            own_exit = (type(exc.code) is int and exc.code == 1 and tb is not None
                        and tb.tb_frame.f_code.co_name == "<module>"
                        and os.path.abspath(tb.tb_frame.f_code.co_filename) == os.path.join(str(root), "project_run.py"))
            explain(watched, owned=own_exit)
        raise
    except Exception:
        explain(watched)
        raise
