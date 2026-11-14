"""Supplied project commands, diagnostics and student-work-preserving updates."""
import sys
import json
from pathlib import Path

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] in ("--help", "-h"):
        print("Maintenance: python -B project_run.py update --help\n")
        print("Local arm practice: python -B project_run.py practice --help\n")
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
            if len(sys.argv) > 1 and sys.argv[1] == 'practice':
                # Isolated physical practice is local only. It never signs in,
                # uploads a score or merges stages into a whole-mission result.
                from physics.review_arm_practice import main as practice_main
                raise SystemExit(practice_main(sys.argv[2:]))
            from diagnostics import client
            # Keep original student arguments in diagnostics. Only the supplied
            # child adapter fills unused internal scene inputs before execution.
            client.COMMANDS['plan'] = ['student_design_input.py', 'plan']
            client.COMMANDS['review'] = ['student_design_input.py', 'review']
            raise SystemExit(client.exit_status(client.main()))
    except UpdateError as exc:
        print(json.dumps({"status": "PROJECT_RUN_REFUSED", "code": exc.code,
                          "message": str(exc)}), file=sys.stderr)
        raise SystemExit(2)
