"""Production-host WSGI entry point with an explicit persistent database path.

The hosting service supplies HTTPS and a production WSGI runner. This module
does not provision a host, generate team credentials or install scoring rules.
"""
import os
from pathlib import Path

from .server import create_app


database = os.environ.get("AP_LEADERBOARD_DB", "")
if not database:
    raise RuntimeError("Set AP_LEADERBOARD_DB to the prepared persistent SQLite database before starting the service")
database_path = Path(database).expanduser().resolve()
if not database_path.is_file():
    raise RuntimeError("Initialize the persistent leaderboard database and install the released rules before serving")
application = create_app(database_path)
