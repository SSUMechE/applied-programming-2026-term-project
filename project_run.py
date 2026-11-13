"""Supplied project commands, diagnostics and student-work-preserving updates."""
import sys
import json
from pathlib import Path

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] in ("--help", "-h"):
        print("Maintenance: python -B project_run.py update --help\n")
    if len(sys.argv) > 1 and sys.argv[1] == "update":
        from update_project import main
        raise SystemExit(main(sys.argv[2:], root=Path(__file__).resolve().parent))
    from update_project import activity_lock, checked_path, UpdateError
    try:
        root = Path(__file__).resolve().parent
        with activity_lock(root):
            if checked_path(root, "output/project_updates/transaction.json", internal=True).exists():
                raise UpdateError("PROJECT_UPDATE_RECOVERY_REQUIRED",
                                  "An interrupted update must be recovered before running the project. "
                                  "Run python -B project_run.py update status, or use your retained bootstrap copy.")
            from diagnostics.client import main, exit_status
            raise SystemExit(exit_status(main()))
    except UpdateError as exc:
        print(json.dumps({"status": "PROJECT_RUN_REFUSED", "code": exc.code,
                          "message": str(exc)}), file=sys.stderr)
        raise SystemExit(2)
