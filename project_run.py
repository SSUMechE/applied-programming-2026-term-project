"""Supplied command entry point with automatic, ungraded failure reporting."""
from diagnostics.client import main, exit_status

if __name__ == "__main__":
    raise SystemExit(exit_status(main()))
