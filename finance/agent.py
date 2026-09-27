"""What the daily categorization agent (a Claude routine) may do, through the API.

It stores receipt emails, matches them to ledger rows and categorizes
uncategorized rows. It never touches transfers or payments to people: those
may be RF currency exchanges and only the owner knows.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from finance.ledger import LedgerError, set_category
from finance.models import (
    RECEIPT_MATCHED,
    RECEIPT_NEW,
    REVIEW_P2P,
    REVIEW_P2P_IN,
    TRANSFER,
    Category,
    Receipt,
    Transaction,
)

MAX_BODY = 20_000


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


async def save_receipt(
    session: AsyncSession, message_id: str, sender: str, subject: str, received_at: datetime, body: str
) -> tuple[Receipt, bool]:
    existing = await session.scalar(select(Receipt).where(Receipt.message_id == message_id))
    if existing:
        return existing, False
    receipt = Receipt(
        message_id=message_id[:255],
        sender=sender[:255],
        subject=subject,
        received_at=received_at,
        body=body[:MAX_BODY],
        status=RECEIPT_NEW,
    )
    session.add(receipt)
    await session.commit()
    return receipt, True


async def resolve_receipt(
    session: AsyncSession,
    receipt: Receipt,
    status: str,
    txn: Transaction | None = None,
    category: Category | None = None,
    summary: str | None = None,
    remember: bool = False,
) -> None:
    if status == RECEIPT_MATCHED:
        if txn is None:
            raise LedgerError("для matched нужен transaction_id")
        _check_touchable(txn)
        taken = await session.scalar(
            select(Receipt.id).where(
                Receipt.transaction_id == txn.id, Receipt.status == RECEIPT_MATCHED, Receipt.id != receipt.id
            )
        )
        if taken:
            raise AgentRefused(f"к транзакции {txn.id} уже привязан чек {taken}")
        _add_note(txn, "🧾", summary)
        receipt.transaction_id = txn.id
        if category is not None:
            await set_category(session, txn, category, remember=remember)
    receipt.status = status
    receipt.summary = summary
    receipt.resolved_at = datetime.now(timezone.utc)
    await session.commit()
