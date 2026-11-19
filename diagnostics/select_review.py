"""Resolve a final selection without modifying a review or its pinned producer."""
import argparse
import json
from pathlib import Path
import uuid

from leaderboard.auto_record import _read, select_record
from leaderboard.contracts import canonical_bytes, require
from leaderboard.evaluate import REPORT_NAME


def review_record(directory, queue):
    """Use the immutable score export, never an old absolute path or latest history."""
    supervisor, _ = _read(directory / "REVIEW_SUPERVISOR.json")
    require(isinstance(supervisor, dict) and supervisor.get("scope") == "full_mission"
            and supervisor.get("contents") == "beads" and all(supervisor.get(key) is True
            for key in ("review_accepted", "physical_transport_accepted", "full_mission_passed")),
            "SELECTION_FAILED_RUN", "Choose a successful full bead review.")
    require(not (directory / "score_evidence/EXPORT_REVOKED.json").exists(),
            "EXPORT_REVOKED", "This review's export was revoked.")
    report, report_hash = _read(directory / "score_evidence" / REPORT_NAME, 1_048_576)
    export = supervisor.get("score_export")
    run = report.get("run_id") if isinstance(report, dict) else None
    require(isinstance(run, str) and str(uuid.UUID(run)) == run and isinstance(export, dict)
            and export.get("run_id") == run and export.get("evaluation_sha256") == report_hash,
            "SELECTION_REVIEW_MISMATCH", "The exported evaluation differs from its review.")
    path = queue / (run + ".json")
    require(path.is_file() and not path.is_symlink() and path.resolve().parent == queue.resolve(),
            "LOCAL_RECORD_MISSING", "Recover the local record from this review before selecting it.")
    record, _ = _read(path, 1_048_576)
    require(isinstance(record, dict) and record.get("run_id") == run
            and record.get("rule_digest") == supervisor.get("rule_digest") == report.get("rule_digest")
            and canonical_bytes(record.get("evaluation")) == canonical_bytes(report)
            and record.get("student_snapshot") == supervisor.get("student_snapshot") == report.get("student_snapshot"),
            "SELECTION_REVIEW_MISMATCH", "The queued record differs from the selected review.")
    return path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--record", type=Path)
    source.add_argument("--review-dir", type=Path, help="Use this successful full review's run ID.")
    parser.add_argument("--commit", required=True)
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument("--queue-dir", type=Path, default=Path("output/leaderboard_queue"),
                        help="For a custom record queue, use the same directory as record.")
    parser.add_argument("--server-url")
    parser.add_argument("--token-file", type=Path)
    args = parser.parse_args(argv)
    try:
        path = args.record if args.record is not None else review_record(args.review_dir, args.queue_dir)
        result = select_record(path, args.commit, project_root=args.project_root,
                               server_url=args.server_url, token_file=args.token_file)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (OSError, ValueError, KeyError) as error:
        print(json.dumps({"status": "record_action_failed", "code": getattr(error, "code", "LOCAL_OR_NETWORK_ERROR"),
                          "native_physics_launched": False}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
