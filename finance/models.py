"""Ledger schema.

One `Transaction` row is one movement on one account, amount signed in the
account currency (negative = money out). `kind` decides how it counts:

- expense  - spending; a positive expense is a refund and nets out its category
- income   - salary, interest, money received
- transfer - money between own accounts; never counted as expense or income.
             Legs are linked through `Transfer` (internal = same currency,
             fx = MYR sent to someone -> RUB received on the Russian account,
             fx_back = RUB spent or handed over for someone -> MYR given back,
             crypto = MYR paid -> coins received on a crypto wallet).

`review_reason` marks rows the bot should ask about; NULL means nothing to do.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from finance.db import Base

# Transaction.kind
EXPENSE, INCOME, TRANSFER = "expense", "income", "transfer"

# Transaction.review_reason
REVIEW_UNCATEGORIZED = "uncategorized"  # no rule matched the merchant
REVIEW_P2P = "p2p"  # outgoing transfer to a person: RF exchange, expense or own account?
REVIEW_P2P_IN = "p2p_in"  # incoming transfer from a person
REVIEW_AWAITING_PAIR = "awaiting_pair"  # looks internal (Maybank -> TNG), other leg not imported yet
REVIEW_OWN = "own"  # the owner said "between my accounts": never asked again, still paired when the other leg shows up
USER_FACING_REVIEW = (REVIEW_UNCATEGORIZED, REVIEW_P2P, REVIEW_P2P_IN)

# Account.kind for savings pots (Maybank Tabung "FUND <name>", other piggy banks)
SAVINGS = "savings"
# Account.kind for crypto wallets: one account per coin ("Крипто USDT"), currency = the ticker
CRYPTO = "crypto"

# Transaction.status
POSTED, PENDING, REVERSED = "posted", "pending", "reversed"


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True)
    name: Mapped[str] = mapped_column(String(100))
    currency: Mapped[str] = mapped_column(String(10))  # MYR, RUB or a coin ticker (USDT, BTC)
    kind: Mapped[str] = mapped_column(String(16))  # bank / ewallet / cash / savings / crypto
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    sort: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # savings pots only: a reserve the budget may spend, not a goal it must not touch
    spendable: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")


class Category(Base):
    __tablename__ = "categories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True)
    name: Mapped[str] = mapped_column(String(64))
    emoji: Mapped[str] = mapped_column(String(8), default="")
    kind: Mapped[str] = mapped_column(String(8))  # expense / income
    sort: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")

    @property
    def label(self) -> str:
        return f"{self.emoji} {self.name}".strip()


class CategoryRule(Base):
    """Regex over the transaction description.

    A rule sets the category, the kind, or both. `kind` here has two extra
    values that resolve to Transaction.kind=transfer: `internal` (own accounts,
    wait for the other leg) and `p2p` (to a person, ask the user).
    """

    __tablename__ = "category_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    pattern: Mapped[str] = mapped_column(Text)
    category_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id", ondelete="CASCADE"))
    kind: Mapped[str | None] = mapped_column(String(16))
    direction: Mapped[str | None] = mapped_column(String(3))  # in / out
    account_code: Mapped[str | None] = mapped_column(String(32))
    priority: Mapped[int] = mapped_column(Integer, default=100, server_default="100")
    origin: Mapped[str] = mapped_column(String(8), default="user", server_default="user")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    category: Mapped[Category | None] = relationship(lazy="joined")


class Import(Base):
    __tablename__ = "imports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"))
    source: Mapped[str] = mapped_column(String(24))
    origin: Mapped[str] = mapped_column(String(16))  # telegram / api / cli
    filename: Mapped[str | None] = mapped_column(String(255))
    file_sha256: Mapped[str] = mapped_column(String(64), unique=True)
    period_from: Mapped[date | None] = mapped_column(Date)
    period_to: Mapped[date | None] = mapped_column(Date)
    rows_total: Mapped[int] = mapped_column(Integer, default=0)
    rows_new: Mapped[int] = mapped_column(Integer, default=0)
    rows_duplicate: Mapped[int] = mapped_column(Integer, default=0)
    rows_reconciled: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    account: Mapped[Account] = relationship(lazy="joined")


class Transfer(Base):
    __tablename__ = "transfers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(8))  # internal / fx / fx_back / crypto
    rate: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))  # in_amount / out_amount; fx only
    matched_by: Mapped[str] = mapped_column(String(8))  # auto / manual
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Transaction(Base):
    __tablename__ = "transactions"
    __table_args__ = (
        Index("ix_transactions_account_booked", "account_id", "booked_on"),
        Index("ix_transactions_booked_on", "booked_on"),
        Index("ix_transactions_review", "review_reason"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"))
    booked_on: Mapped[date] = mapped_column(Date)
    booked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 8))  # 8 places: coins (0.00061 BTC)
    description: Mapped[str] = mapped_column(Text)
    merchant: Mapped[str] = mapped_column(String(120), default="")
    kind: Mapped[str] = mapped_column(String(12))
    category_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id", ondelete="SET NULL"))
    status: Mapped[str] = mapped_column(String(10), default=POSTED, server_default=POSTED)
    source: Mapped[str] = mapped_column(String(24))
    external_ref: Mapped[str | None] = mapped_column(String(64))
    balance_after: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    dedup_key: Mapped[str | None] = mapped_column(String(64), unique=True)
    import_id: Mapped[int | None] = mapped_column(ForeignKey("imports.id", ondelete="SET NULL"))
    transfer_id: Mapped[int | None] = mapped_column(
        ForeignKey("transfers.id", ondelete="SET NULL"), index=True
    )
    review_reason: Mapped[str | None] = mapped_column(String(24))
    # "Мои счета → Maybank": while review_reason is `own`, the other leg is looked for only there
    pair_account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id", ondelete="SET NULL"))
    # "Skip" in the review: out of the queue until this date
    review_snoozed_until: Mapped[date | None] = mapped_column(Date)
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    account: Mapped[Account] = relationship(lazy="joined", foreign_keys=[account_id])
    category: Mapped[Category | None] = relationship(lazy="joined")
    transfer: Mapped[Transfer | None] = relationship(lazy="joined")

    @property
    def currency(self) -> str:
        return self.account.currency


class JobRun(Base):
    """Guards scheduled reports against double sending after restarts."""

    __tablename__ = "job_runs"
    __table_args__ = (UniqueConstraint("job", "period_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job: Mapped[str] = mapped_column(String(32))
    period_key: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class BalanceCheck(Base):
    """The real balance of a bank or wallet account, typed by the owner between statements.

    The newest of this and the last statement balance is the starting point; rows
    booked after it are added on top (see reports.balances).
    """

    __tablename__ = "balance_checks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    as_of: Mapped[date] = mapped_column(Date)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# PlannedItem.status
PLAN_OPEN, PLAN_DONE, PLAN_CANCELLED = "planned", "done", "cancelled"


class PlannedItem(Base):
    """Money the owner expects to move in a future cycle: a trip, a move, a yearly fee,
    a monthly RF transfer, a bonus. The budget forecast puts it into the cycle of `due_on`.

    `amount` is signed in MYR like a transaction (negative = out). `kind` is expense,
    transfer (out of the spendable money but not spending: RF, a pot) or income.
    `repeat_months` repeats it (1 = monthly) until `until`.
    """

    __tablename__ = "planned_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    kind: Mapped[str] = mapped_column(String(12), default=EXPENSE)
    category_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id", ondelete="SET NULL"))
    due_on: Mapped[date] = mapped_column(Date)
    repeat_months: Mapped[int | None] = mapped_column(Integer)
    until: Mapped[date | None] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(10), default=PLAN_OPEN, server_default=PLAN_OPEN)
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    category: Mapped[Category | None] = relationship(lazy="joined")
