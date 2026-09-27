"""Common parser output."""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal


class ParseError(Exception):
    pass


@dataclass
class ParsedTxn:
    booked_on: date
    amount: Decimal  # signed, account currency; negative = money out
    description: str
    raw_type: str | None = None  # statement's own type column (TNG: Payment, Reload, ...)
    external_ref: str | None = None
    balance_after: Decimal | None = None
    reversed: bool = False
    booked_at: datetime | None = None
    category_hint: str | None = None  # a category code chosen upfront (manual entries)


@dataclass
class ParsedStatement:
    account_code: str
    source: str
    transactions: list[ParsedTxn] = field(default_factory=list)

    @property
    def period(self) -> tuple[date | None, date | None]:
        days = [t.booked_on for t in self.transactions]
        return (min(days), max(days)) if days else (None, None)


def normalize_description(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().upper()


def dedup_keys(account_code: str, txns: list[ParsedTxn]) -> list[str]:
    """Stable key per row.

    Two identical coffees on one day are both real, so identical rows get an
    occurrence index. Re-importing the same (or an overlapping) statement
    reproduces the same keys and the rows are skipped.
    """
    seen: Counter[tuple] = Counter()
    keys = []
    for t in txns:
        base = (
            account_code,
            t.booked_on.isoformat(),
            f"{t.amount:.2f}",
            normalize_description(t.description),
            t.external_ref or "",
        )
        n = seen[base]
        seen[base] += 1
        keys.append(hashlib.sha256("|".join((*base, str(n))).encode()).hexdigest())
    return keys
