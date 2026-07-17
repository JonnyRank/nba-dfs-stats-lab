"""SQLite connections: the writable analytics DB and the read-only ops ATTACH."""

import re
import sqlite3
from pathlib import Path
from urllib.parse import quote

from nba_dfs_stats_lab.config import ANALYTICS_DB, OPS_DB

_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:/")


def get_connection(db_path: Path = ANALYTICS_DB) -> sqlite3.Connection:
    """
    Open the analytics database, creating its parent directory when needed.
    
    Parameters:
        db_path (Path): Path to the analytics database.
    
    Returns:
        sqlite3.Connection: A connection with foreign-key enforcement and WAL journaling enabled.
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
    """
    Build a read-only SQLite `file:` URI for an absolute filesystem path.
    
    Parameters:
        path (Path): Absolute path to the SQLite database.
    
    Returns:
        str: URL-encoded SQLite URI ending with `?mode=ro`.
    
    Raises:
        ValueError: If `path` is not absolute.
    """
    quoted = quote(Path(path).as_posix(), safe="/:")
    if not quoted.startswith("/"):
        if not _WINDOWS_DRIVE.match(quoted):
            raise ValueError(f"ops path must be absolute, got {str(path)!r}")
        quoted = "/" + quoted  # Windows drive-letter paths lack the leading slash
    return f"file://{quoted}?mode=ro"


def attach_ops(conn: sqlite3.Connection, ops_path: Path = OPS_DB, alias: str = "ops") -> None:
    """
    Attach the operations database to a SQLite connection in read-only mode.
    
    Parameters:
        ops_path (Path): Filesystem path to the operations database.
        alias (str): Identifier used to reference the attached database.
    
    Raises:
        ValueError: If `alias` is not a valid identifier.
    """
    if not alias.isidentifier():
        raise ValueError(f"invalid attach alias: {alias!r}")
    conn.execute(f"ATTACH DATABASE ? AS {alias}", (read_only_uri(ops_path),))
