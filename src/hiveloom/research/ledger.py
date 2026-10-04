"""The program ledger: an append-only, hash-chained record of everything.

Every action the engine takes, every director call, every paid debit, every
verdict and every read of sealed data is one event. Each event carries the hash
of the one before it, so the record cannot be edited without ``verify``
noticing. Budget balances are computed from the ledger, never stored beside
it, so the two cannot disagree.
"""

from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

GENESIS = "0" * 64


@contextmanager
def exclusive(handle, *, blocking: bool = True):
    """An advisory exclusive lock on an open file (a no-op where there is no flock)."""
    try:
        import fcntl
    except ImportError:  # pragma: no cover - Windows: single-process use only
        yield True
        return
    flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
    try:
        fcntl.flock(handle.fileno(), flags)
    except BlockingIOError:
        yield False
        return
    try:
        yield True
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _hash(previous: str, seq: int, ts: str, kind: str, data: dict[str, Any]) -> str:
    material = json.dumps(
        {"prev": previous, "seq": seq, "ts": ts, "kind": kind, "data": data},
        sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str,
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class Ledger:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def events(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        return [
            json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def append(self, kind: str, **data: Any) -> dict[str, Any]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Read-last-then-append under an exclusive lock: two writers (a running
        # program and a `stop` from another process) must not fork the chain.
        with self.path.open("a", encoding="utf-8") as handle, exclusive(handle):
            existing = self.events()
            previous = existing[-1]["hash"] if existing else GENESIS
            seq = len(existing)
            ts = datetime.now(UTC).isoformat()
            data = json.loads(json.dumps(data, default=str))
            event = {
                "seq": seq, "ts": ts, "kind": kind, "data": data, "prev": previous,
                "hash": _hash(previous, seq, ts, kind, data),
            }
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")
            handle.flush()
        return event

    def verify(self) -> dict[str, Any]:
        """Recompute the chain. ``broken_at`` is the first bad seq, or None."""
        previous = GENESIS
        events = self.events()
        for index, event in enumerate(events):
            expected = _hash(previous, index, event.get("ts", ""), event.get("kind", ""),
                             event.get("data", {}))
            if event.get("seq") != index or event.get("prev") != previous \
                    or event.get("hash") != expected:
                return {"ok": False, "checked": index, "broken_at": index}
            previous = event["hash"]
        return {"ok": True, "checked": len(events), "broken_at": None}

    def of_kind(self, *kinds: str) -> list[dict[str, Any]]:
        return [event for event in self.events() if event["kind"] in kinds]
