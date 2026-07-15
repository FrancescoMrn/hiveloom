"""Logging as memory. M2 ships the append-only trace layer; the queryable Hive
index (SQLite) lands in M3.
"""

from hiveloom.logging.hive import Hive, default_db_path
from hiveloom.logging.trace import TraceEvent, TraceWriter, spec_version_hash

__all__ = [
    "Hive",
    "TraceEvent",
    "TraceWriter",
    "default_db_path",
    "spec_version_hash",
]
