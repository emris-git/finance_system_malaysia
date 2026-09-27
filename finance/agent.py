"""What the daily categorization agent (a Claude routine) may do, through the API.

It categorizes uncategorized rows. It never touches transfers or payments to people: those
may be RF currency exchanges and only the owner knows.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from finance.ledger import LedgerError, set_category
from finance.models import REVIEW_P2P, REVIEW_P2P_IN, TRANSFER, Category, Transaction


class AgentRefused(LedgerError):
    pass


def _check_touchable(txn: Transaction) -> None:
    if txn.kind == TRANSFER or txn.review_reason in (REVIEW_P2P, REVIEW_P2P_IN):
        raise AgentRefused("переводы и платежи людям разбирает владелец")


def _add_note(txn: Transaction, marker: str, text: str | None) -> None:
    if not text:
        return
    line = f"{marker} {text.strip()}"[:300]
    txn.note = f"{txn.note} · {line}" if txn.note else line


async def categorize(
    session: AsyncSession,
    txn: Transaction,
    category: Category,
    note: str | None = None,
    remember: bool = False,
) -> int:
    _check_touchable(txn)
    _add_note(txn, "🤖", note)
    return await set_category(session, txn, category, remember=remember)

