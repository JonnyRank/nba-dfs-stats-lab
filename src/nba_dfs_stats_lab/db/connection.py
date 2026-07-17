"""SQLite connections: the writable analytics DB and the read-only ops ATTACH."""

import re
import sqlite3
from pathlib import Path
from urllib.parse import quote

from nba_dfs_stats_lab.config import ANALYTICS_DB, OPS_DB

_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:/")


def get_connection(db_path: Path = ANALYTICS_DB) -> sqlite3.Connection:
    """Open the analytics DB, creating its parent directory if needed.

    `uri=True` matters even though we pass a plain path: it sets SQLITE_OPEN_URI
    on the connection, which is what lets a later `ATTACH 'file:...?mode=ro'`
    be parsed as a URI instead of a literal filename.
    """
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    # A plain path (no "file:" prefix) is still treated as an ordinary filename
    # under uri=True, so no escaping is needed here.
    conn = sqlite3.connect(str(db_path), uri=True)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def read_only_uri(path: Path) -> str:
    """Build a `file:` URI with mode=ro for an absolute path.

    Windows paths need care: backslashes become forward slashes and spaces
    become %20 (e.g. G:\\My Drive\\x.db -> file:///G:/My%20Drive/x.db?mode=ro).
    Drive-letter colons are kept literal — SQLite expects `/G:/`, not `/G%3A/`.
    Relative paths are rejected: prefixing one with a slash would silently
    re-root it at the filesystem root.
    """
    quoted = quote(Path(path).as_posix(), safe="/:")
    if not quoted.startswith("/"):
        if not _WINDOWS_DRIVE.match(quoted):
            raise ValueError(f"ops path must be absolute, got {str(path)!r}")
        quoted = "/" + quoted  # Windows drive-letter paths lack the leading slash
    return f"file://{quoted}?mode=ro"


def attach_ops(conn: sqlite3.Connection, ops_path: Path = OPS_DB, alias: str = "ops") -> None:
    """ATTACH the ops DB read-only. Any write to `ops.*` raises OperationalError.

    The alias is interpolated (ATTACH can't parameterize it), so restrict it to
    a bare identifier.
    """
    if not alias.isidentifier():
        raise ValueError(f"invalid attach alias: {alias!r}")
    conn.execute(f"ATTACH DATABASE ? AS {alias}", (read_only_uri(ops_path),))
