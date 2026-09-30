"""Budget pools, computed from the ledger.

The charter splits the budget into pools. Spend is recorded as ``debit``
events; transfers as ``budget_moved``. Balances are always recomputed from the
ledger, so they cannot drift from the record. The confirmation pool is
reserved: nothing but the sealed confirmation may draw on it.
"""

from __future__ import annotations

from dataclasses import dataclass

from .charter import Charter
from .ledger import Ledger

POOLS = ("data", "exploration", "experiments", "confirmation")
#: How far the director may move money between pools, relative to the charter.
MAX_MOVE_SHARE = 0.25


@dataclass
class PoolState:
    size: float
    spent: float

    @property
    def left(self) -> float:
        return max(0.0, self.size - self.spent)


class Budget:
    def __init__(self, charter: Charter, ledger: Ledger):
        self._charter = charter
        self._ledger = ledger

    def pools(self) -> dict[str, PoolState]:
        split = self._charter.budget.split
        usd = self._charter.budget.usd
        sizes = {name: usd * getattr(split, name) for name in POOLS}
        spent = dict.fromkeys(POOLS, 0.0)
        for event in self._ledger.of_kind("debit", "budget_moved"):
            data = event["data"]
            if event["kind"] == "debit":
                spent[data["pool"]] = spent.get(data["pool"], 0.0) + float(data["usd"])
            else:
                sizes[data["from"]] -= float(data["usd"])
                sizes[data["to"]] += float(data["usd"])
        return {name: PoolState(size=sizes[name], spent=spent[name]) for name in POOLS}

    def spent(self) -> float:
        return sum(pool.spent for pool in self.pools().values())

    def left(self, pool: str) -> float:
        return self.pools()[pool].left

    def can_afford(self, pool: str, usd: float) -> bool:
        return self.left(pool) >= usd

    def debit(self, pool: str, usd: float, **reason: object) -> None:
        if pool not in POOLS:
            raise ValueError(f"unknown pool {pool!r}")
        self._ledger.append("debit", pool=pool, usd=round(float(usd), 6), **reason)

    def move(self, source: str, target: str, usd: float) -> str | None:
        """Move money between pools; returns a refusal reason or None."""
        if source not in POOLS or target not in POOLS or source == target:
            return "unknown or identical pools"
        if "confirmation" in (source, target):
            return "the confirmation pool is reserved and cannot be moved"
        if usd <= 0:
            return "amount must be positive"
        if usd > self.left(source):
            return f"only ${self.left(source):.4f} left in {source}"
        moved = {name: 0.0 for name in POOLS}
        for event in self._ledger.of_kind("budget_moved"):
            moved[event["data"]["from"]] -= float(event["data"]["usd"])
            moved[event["data"]["to"]] += float(event["data"]["usd"])
        limit = self._charter.budget.usd * MAX_MOVE_SHARE
        if abs(moved[target] + usd) > limit or abs(moved[source] - usd) > limit:
            return f"moves are limited to ±${limit:.2f} per pool against the charter's split"
        self._ledger.append("budget_moved", **{"from": source, "to": target, "usd": usd})
        return None
