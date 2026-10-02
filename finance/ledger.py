"""Writing to the ledger: imports, manual entries, transfers, review actions."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from finance.classify import USER_RULE_PRIORITY, Classifier, merchant_of, names_owner, suggest_pattern
from finance.config import get_settings
from finance.models import (
    CRYPTO,
    EXPENSE,
    INCOME,
    PENDING,
    POSTED,
    REVERSED,
    REVIEW_AWAITING_PAIR,
    REVIEW_OWN,
    REVIEW_P2P,
    REVIEW_P2P_IN,
    REVIEW_UNCATEGORIZED,
    SAVINGS,
    TRANSFER,
    USER_FACING_REVIEW,
    Account,
    BalanceCheck,
    Category,
    CategoryRule,
    Import,
    Transaction,
    Transfer,
)
from finance.parsers import ParsedStatement, dedup_keys
from finance.utils import today

PENDING_WINDOW = timedelta(days=4)
TRANSFER_WINDOW = timedelta(days=3)
STALE_PAIR_AFTER = timedelta(days=7)
TRANSFER_LIKE = (REVIEW_AWAITING_PAIR, REVIEW_OWN, REVIEW_P2P, REVIEW_P2P_IN)
# accounts that get statements, so the other leg of a transfer shows up by itself
STATEMENT_ACCOUNT_KINDS = ("bank", "ewallet")


class LedgerError(Exception):
    pass


@dataclass
class ImportResult:
    account_name: str
    total: int = 0
    new: int = 0
    duplicate: int = 0
    reconciled: int = 0
    matched_transfers: int = 0
    to_review: int = 0
    period: tuple[date | None, date | None] = (None, None)
    already_imported: bool = False
    refreshed: int = 0
    to_pots: int = 0
    import_id: int | None = None
    new_ids: list[int] = field(default_factory=list)


def _not_deleted():
    return Transaction.deleted_at.is_(None)


async def get_account(session: AsyncSession, code: str) -> Account:
    account = await session.scalar(select(Account).where(Account.code == code))
    if account is None:
        raise LedgerError(f"нет счёта с кодом {code!r}")
    return account


async def get_category(session: AsyncSession, code_or_id: str | int) -> Category:
    column = Category.id if isinstance(code_or_id, int) else Category.code
    category = await session.scalar(select(Category).where(column == code_or_id))
    if category is None:
        raise LedgerError(f"нет категории {code_or_id!r}")
    return category


async def find_category(session: AsyncSession, word: str, kind: str) -> Category | None:
    """"#транспорт" -> Транспорт: by code, name or the start of a word in the name
    ("#доход" -> Прочий доход); a category of the entry's kind wins."""
    word = word.lower().replace("ё", "е")

    def hit(c: Category) -> bool:
        name = c.name.lower().replace("ё", "е")
        words = re.findall(r"\w+", name)
        return c.code == word or name == word or (len(word) >= 3 and any(w.startswith(word) for w in words))

    categories = (await session.scalars(select(Category).where(Category.is_active).order_by(Category.sort))).all()
    found = [c for c in categories if hit(c)]
    return next((c for c in found if c.kind == kind), found[0] if found else None)


# --- imports -----------------------------------------------------------------


async def _find_pending(session: AsyncSession, account_id: int, amount: Decimal, day: date):
    rows = (
        await session.scalars(
            select(Transaction).where(
                Transaction.account_id == account_id,
                Transaction.status == PENDING,
                Transaction.amount == amount,
                Transaction.booked_on.between(day - PENDING_WINDOW, day + PENDING_WINDOW),
                _not_deleted(),
            )
        )
    ).unique().all()
    return min(rows, key=lambda t: abs((t.booked_on - day).days), default=None)


async def _same_row_by_balance(session: AsyncSession, account_id: int, parsed) -> Transaction | None:
    """The stored row with this date, amount and running balance, if exactly one.

    The running balance pins a statement row down even when its description
    changed because the parser now reads more of it.
    """
    if parsed.balance_after is None:
        return None
    rows = (
        await session.scalars(
            select(Transaction).where(
                Transaction.account_id == account_id,
                Transaction.booked_on == parsed.booked_on,
                Transaction.amount == parsed.amount,
                Transaction.balance_after == parsed.balance_after,
                _not_deleted(),
            )
        )
    ).unique().all()
    return rows[0] if len(rows) == 1 else None


async def import_statement(
    session: AsyncSession,
    statement: ParsedStatement,
    *,
    origin: str,
    filename: str | None = None,
    file_bytes: bytes | None = None,
) -> ImportResult:
    """Store parsed rows, skipping ones already in the ledger.

    - A row stored under the same key is a duplicate.
    - A row with the same date, amount and running balance as a stored one is
      that row read better (e.g. a third description line): its description is
      refreshed, so sending an old statement again fixes it without duplicates.
    - A row that matches a pending entry (bot, Apple Pay) confirms it; user
      choices on the pending row (category, RF transfer) survive.
    - A transfer to a Maybank Tabung ("... FUND Holiday") goes to that savings pot.
    """
    account = await get_account(session, statement.account_code)
    result = ImportResult(account_name=account.name, total=len(statement.transactions))
    result.period = statement.period

    sha = hashlib.sha256(file_bytes).hexdigest() if file_bytes else None
    imp = await session.scalar(select(Import).where(Import.file_sha256 == sha)) if sha else None
    result.already_imported = imp is not None
    if imp is None:
        imp = Import(
            account_id=account.id,
            source=statement.source,
            origin=origin,
            filename=filename,
            file_sha256=sha or hashlib.sha256(f"{origin}:{datetime.now().isoformat()}".encode()).hexdigest(),
            period_from=result.period[0],
            period_to=result.period[1],
        )
        session.add(imp)
        await session.flush()

    keys = dedup_keys(account.code, statement.transactions)
    existing = set(
        (await session.scalars(select(Transaction.dedup_key).where(Transaction.dedup_key.in_(keys)))).all()
    )
    classifier = await Classifier.load(session)
    new_rows: list[Transaction] = []
    refreshed: list[Transaction] = []

    for parsed, key in zip(statement.transactions, keys):
        if key in existing:
            result.duplicate += 1
            continue
        existing.add(key)

        same = await _same_row_by_balance(session, account.id, parsed)
        if same is not None and same not in new_rows:
            if same.description != parsed.description:
                same.description = parsed.description
                same.merchant = merchant_of(parsed.description)
                same.dedup_key = key
                if same.review_reason in USER_FACING_REVIEW and same.transfer_id is None:
                    c = classifier.classify(parsed.description, parsed.amount, account.code, parsed.raw_type)
                    same.kind, same.category_id, same.review_reason = c.kind, c.category_id, c.review_reason
                refreshed.append(same)
            else:
                result.duplicate += 1
            continue

        pending = None if parsed.reversed else await _find_pending(
            session, account.id, parsed.amount, parsed.booked_on
        )
        if pending is not None:
            pending.status = POSTED
            pending.dedup_key = key
            pending.booked_on = parsed.booked_on
            pending.description = parsed.description
            pending.merchant = merchant_of(parsed.description)
            pending.external_ref = parsed.external_ref
            pending.balance_after = parsed.balance_after
            pending.import_id = imp.id
            result.reconciled += 1
            continue

        c = classifier.classify(
            parsed.description, parsed.amount, account.code, parsed.raw_type, parsed.category_hint
        )
        row = Transaction(
            account_id=account.id,
            booked_on=parsed.booked_on,
            booked_at=parsed.booked_at,
            amount=parsed.amount,
            description=parsed.description,
            merchant=merchant_of(parsed.description),
            kind=c.kind,
            category_id=c.category_id,
            status=REVERSED if parsed.reversed else POSTED,
            source=statement.source,
            external_ref=parsed.external_ref,
            balance_after=parsed.balance_after,
            dedup_key=key,
            import_id=imp.id,
            review_reason=None if parsed.reversed else c.review_reason,
        )
        session.add(row)
        new_rows.append(row)

    await session.flush()
    result.new = len(new_rows)
    result.refreshed = len(refreshed)
    result.new_ids = [r.id for r in new_rows]
    result.to_pots = await link_pot_transfers(session, account, [*new_rows, *refreshed])
    result.matched_transfers = await match_internal_transfers(session)
    result.to_review = sum(1 for r in new_rows if r.review_reason in USER_FACING_REVIEW)

    imp.rows_total = result.total
    imp.rows_new = (imp.rows_new or 0) + result.new
    imp.rows_duplicate = result.duplicate
    imp.rows_reconciled = (imp.rows_reconciled or 0) + result.reconciled
    result.import_id = imp.id
    await session.commit()
    return result


# --- savings pots ------------------------------------------------------------

# Maybank Tabung: "TRANSFER FROM A/C ALEX MORGAN * 00000001 FUND Holiday" (a top-up)
# or "... BOOSTER Holiday" (the small automatic save-ups).
# into a Tabung: "TRANSFER FROM A/C ALEX MORGAN * 00000001 FUND Holiday" (or BOOSTER);
# back out of it: "FUND TRANSFER TO A/ ALEX MORGAN * 00000001 WTDRW Holiday"
POT_RE = re.compile(
    r"TRANSFER (?:FROM|TO) A/C?.*\b(?:FUND|BOOSTER|WTDRW)\s+(?!TRANSFER\b)([^*]{1,40}?)\s*$", re.IGNORECASE
)


def pot_name_from(description: str) -> str | None:
    m = POT_RE.search(description)
    return m.group(1).strip() if m else None


def _pot_code(name: str) -> str:
    slug = re.sub(r"\W+", "_", name.strip().lower()).strip("_")[:27]
    return f"pot_{slug or 'pot'}"


async def pots(session: AsyncSession) -> list[Account]:
    return list(
        (
            await session.scalars(
                select(Account).where(Account.kind == SAVINGS, Account.is_active).order_by(Account.name)
            )
        ).all()
    )


async def get_or_create_pot(session: AsyncSession, name: str) -> Account:
    code = _pot_code(name)
    pot = await session.scalar(select(Account).where(Account.code == code))
    if pot is None:
        pot = Account(code=code, name=f"Копилка {name.strip()}", currency="MYR", kind=SAVINGS, sort=100)
        session.add(pot)
        await session.flush()
    elif not pot.is_active:
        pot.is_active = True
    return pot


async def _mirror_into_pot(session: AsyncSession, row: Transaction, source: Account, pot: Account) -> None:
    """Pots have no statements: the other leg of the transfer is written here."""
    leg = Transaction(
        account_id=pot.id,
        booked_on=row.booked_on,
        amount=-row.amount,
        description=f"{'Из' if row.amount < 0 else 'Обратно в'} {source.name}",
        merchant=pot.name.upper(),
        kind=TRANSFER,
        status=POSTED,
        source="auto",
    )
    session.add(leg)
    await session.flush()
    out, into = (row, leg) if row.amount < 0 else (leg, row)
    await _link(session, "internal", "auto", out, into)


async def link_pot_transfers(session: AsyncSession, account: Account, rows: list[Transaction]) -> int:
    linked = 0
    for row in rows:
        if row.transfer_id or row.status == REVERSED or row.deleted_at is not None:
            continue
        name = pot_name_from(row.description)
        if not name:
            continue
        await _mirror_into_pot(session, row, account, await get_or_create_pot(session, name))
        linked += 1
    return linked


async def move_to_pot(session: AsyncSession, txn: Transaction, pot: Account) -> None:
    """"Мои счета → копилка X" from the review."""
    if txn.transfer_id:
        await unlink_transfer(session, txn.transfer_id)
    await _mirror_into_pot(session, txn, txn.account, pot)
    txn.review_snoozed_until = None
    await session.commit()


async def set_pot_balance(session: AsyncSession, name: str, target: Decimal) -> tuple[Account, Transaction | None]:
    pot = await get_or_create_pot(session, name)
    await session.commit()
    return pot, await set_balance(session, pot.code, target)


# --- transfers ---------------------------------------------------------------


def _is_self_transfer(description: str) -> bool:
    return names_owner(description)


def _pair_account(row: Transaction) -> int | None:
    """The account the owner named for the other leg ("Мои счета → Maybank")."""
    return row.pair_account_id if row.review_reason == REVIEW_OWN else None


async def _leg_in_named_account(session: AsyncSession, row: Transaction) -> Transaction | None:
    """The other leg on the account the owner named. Any posted row with the
    mirrored amount counts, even one the rules took for a purchase: the owner's
    answer beats the classifier. Transfer-looking rows and closer dates first."""
    candidates = (
        await session.scalars(
            select(Transaction).where(
                Transaction.account_id == row.pair_account_id,
                Transaction.amount == -row.amount,
                Transaction.booked_on.between(row.booked_on - TRANSFER_WINDOW, row.booked_on + TRANSFER_WINDOW),
                Transaction.transfer_id.is_(None),
                Transaction.status == POSTED,
                _not_deleted(),
            )
        )
    ).unique().all()
    return min(
        (c for c in candidates if _pair_account(c) in (None, row.account_id)),
        key=lambda c: (c.review_reason not in TRANSFER_LIKE, abs((c.booked_on - row.booked_on).days)),
        default=None,
    )


async def match_internal_transfers(session: AsyncSession) -> int:
    """Pair money leaving one own account with the same amount arriving on another.

    Rows the owner pinned to an account look for the other leg there first.
    Otherwise at least one leg must look internal (e.g. Maybank row mentioning
    TNG, or a TNG reload), or both must name the owner, so a friend paying
    back the same sum is not mistaken for a top-up.
    """
    matched = 0
    pinned = (
        await session.scalars(
            select(Transaction).where(
                Transaction.review_reason == REVIEW_OWN,
                Transaction.pair_account_id.is_not(None),
                Transaction.transfer_id.is_(None),
                Transaction.status != REVERSED,
                _not_deleted(),
            )
        )
    ).unique().all()
    for row in pinned:
        if row.transfer_id:  # already taken as the other leg of an earlier pinned row
            continue
        other = await _leg_in_named_account(session, row)
        if other is not None:
            out, into = (row, other) if row.amount < 0 else (other, row)
            await _link(session, "internal", "auto", out, into)
            matched += 1

    rows = (
        await session.scalars(
            select(Transaction).where(
                Transaction.transfer_id.is_(None),
                Transaction.review_reason.in_(TRANSFER_LIKE),
                Transaction.status != REVERSED,
                _not_deleted(),
            )
        )
    ).unique().all()
    outs = sorted((r for r in rows if r.amount < 0), key=lambda r: r.booked_on)
    ins = [r for r in rows if r.amount > 0]
    used: set[int] = set()

    for out in outs:
        candidates = [
            i
            for i in ins
            if i.id not in used
            and i.account_id != out.account_id
            and i.account.currency == out.account.currency
            and i.amount == -out.amount
            and abs(i.booked_on - out.booked_on) <= TRANSFER_WINDOW
            and _pair_account(out) in (None, i.account_id)
            and _pair_account(i) in (None, out.account_id)
            and (
                {i.review_reason, out.review_reason} & {REVIEW_AWAITING_PAIR, REVIEW_OWN}
                or (_is_self_transfer(i.description) and _is_self_transfer(out.description))
            )
        ]
        if not candidates:
            continue
        best = min(
            candidates,
            key=lambda i: (
                i.review_reason not in (REVIEW_AWAITING_PAIR, REVIEW_OWN),
                abs((i.booked_on - out.booked_on).days),
            ),
        )
        used.add(best.id)
        await _link(session, "internal", "auto", out, best)
        matched += 1
    return matched


async def _link(
    session: AsyncSession, kind: str, matched_by: str, out: Transaction, into: Transaction, note: str | None = None
) -> Transfer:
    rate = None
    if kind == "fx" and out.amount:
        rate = (into.amount / -out.amount).quantize(Decimal("0.000001"))
    transfer = Transfer(kind=kind, rate=rate, matched_by=matched_by, note=note)
    session.add(transfer)
    await session.flush()
    for leg in (out, into):
        leg.kind = TRANSFER
        leg.category_id = None
        leg.review_reason = None
        leg.transfer_id = transfer.id
    return transfer


async def transfer_legs(session: AsyncSession, transfer_id: int) -> list[Transaction]:
    return list(
        (
            await session.scalars(
                select(Transaction).where(Transaction.transfer_id == transfer_id, _not_deleted())
            )
        ).unique()
    )


async def unlink_transfer(session: AsyncSession, transfer_id: int) -> None:
    """Undo a transfer: bot-created legs go away, statement legs go back to review,
    a ruble expense that was paid back is an expense again."""
    now = datetime.now(timezone.utc)
    for leg in await transfer_legs(session, transfer_id):
        leg.transfer_id = None
        if leg.category_id is not None:
            leg.kind = EXPENSE  # only a paid-back ruble expense keeps its category inside a transfer
        elif leg.source in ("manual", "auto"):  # legs the system wrote itself (RUB side, pot side)
            leg.deleted_at = now
        else:
            leg.kind = TRANSFER
            leg.review_reason = REVIEW_P2P if leg.amount < 0 else REVIEW_P2P_IN
    transfer = await session.get(Transfer, transfer_id)
    if transfer is not None:
        await session.delete(transfer)
    await session.flush()


async def _exchange(
    session: AsyncSession, kind: str, out: Transaction, account: Account, amount: Decimal, merchant: str, note: str | None
) -> Transfer:
    """Money out of `out`, `amount` received on a manually tracked account: the system writes that leg."""
    if out.transfer_id:
        await unlink_transfer(session, out.transfer_id)
    leg = Transaction(
        account_id=account.id,
        booked_on=out.booked_on,
        amount=amount,
        description=f"Перевод из {out.account.name}: {out.merchant or out.description}"[:500],
        merchant=merchant,
        kind=TRANSFER,
        status=POSTED,
        source="manual",
        note=note,
    )
    session.add(leg)
    await session.flush()
    await session.refresh(leg, ["account"])
    transfer = await _link(session, kind, "manual", out, leg, note)
    await session.commit()
    return transfer


async def mark_fx(
    session: AsyncSession, out: Transaction, rub_amount: Decimal, note: str | None = None
) -> Transfer:
    """MYR sent to someone, RUB received on the Russian account."""
    if out.amount >= 0:
        raise LedgerError("РФ-перевод — это списание, а тут поступление")
    if rub_amount <= 0:
        raise LedgerError("сумма в рублях должна быть положительной")
    ru = await get_account(session, "ru")
    return await _exchange(session, "fx", out, ru, rub_amount, "ПЕРЕВОД НА РФ", note)


# --- rubles paid back in ringgit ---------------------------------------------
# Rubles spent for someone else (their English lessons) or handed over to them,
# ringgit given back here: the reverse of an RF transfer. Neither side is the
# owner's spending or income.

PAY_BACK_WINDOW = timedelta(days=90)


async def rub_expenses_to_pay_back(session: AsyncSession, into: Transaction, limit: int = 6) -> list[Transaction]:
    """Recent ruble expenses, newest first: what this ringgit may be paying back."""
    return list(
        (
            await session.scalars(
                select(Transaction)
                .join(Account, Account.id == Transaction.account_id)
                .where(
                    Account.currency == "RUB",
                    Transaction.kind == EXPENSE,
                    Transaction.amount < 0,
                    Transaction.transfer_id.is_(None),
                    Transaction.status != REVERSED,
                    Transaction.booked_on.between(into.booked_on - PAY_BACK_WINDOW, into.booked_on + timedelta(days=7)),
                    _not_deleted(),
                )
                .order_by(Transaction.booked_on.desc(), Transaction.id.desc())
                .limit(limit)
            )
        ).unique()
    )


def _check_pay_back(into: Transaction) -> None:
    if into.amount <= 0:
        raise LedgerError("возврат — это поступление, а тут списание")
    if into.account.currency == "RUB":
        raise LedgerError("это уже рубли")


async def pay_back_rub_expense(session: AsyncSession, into: Transaction, expense: Transaction) -> Transfer:
    """Ringgit received for a ruble expense paid for someone else: both leave the totals.

    The expense keeps its category, so undoing the link makes it an expense again.
    """
    _check_pay_back(into)
    if expense.amount >= 0 or expense.account.currency != "RUB" or expense.kind != EXPENSE:
        raise LedgerError("это не рублёвый расход")
    for row in (into, expense):
        if row.transfer_id:
            await unlink_transfer(session, row.transfer_id)
    category_id = expense.category_id
    transfer = await _link(session, "fx_back", "manual", expense, into)
    expense.category_id = category_id
    await session.commit()
    return transfer


async def pay_back_rubles(
    session: AsyncSession, into: Transaction, rub_amount: Decimal, note: str | None = None
) -> Transfer:
    """Ringgit received for rubles handed over (a transfer to someone's Russian card)."""
    _check_pay_back(into)
    if rub_amount <= 0:
        raise LedgerError("сумма в рублях должна быть положительной")
    if into.transfer_id:
        await unlink_transfer(session, into.transfer_id)
    ru = await get_account(session, "ru")
    out = Transaction(
        account_id=ru.id,
        booked_on=into.booked_on,
        amount=-rub_amount,
        description=f"Рубли за {into.account.name}: {into.merchant or into.description}"[:500],
        merchant="ВОЗВРАТ РИНГГИТАМИ",
        kind=TRANSFER,
        status=POSTED,
        source="manual",
        note=note,
    )
    session.add(out)
    await session.flush()
    await session.refresh(out, ["account"])
    transfer = await _link(session, "fx_back", "manual", out, into, note)
    await session.commit()
    return transfer


# --- crypto ------------------------------------------------------------------

CRYPTO_TICKER_RE = re.compile(r"^[A-Z][A-Z0-9]{1,9}$")
COIN_PLACES = Decimal("0.00000001")


async def get_or_create_crypto(session: AsyncSession, currency: str) -> Account:
    """The wallet account for one coin: "Крипто USDT", currency USDT."""
    ticker = currency.strip().upper()
    if not CRYPTO_TICKER_RE.match(ticker):
        raise LedgerError(f"не похоже на тикер монеты: {currency!r}")
    code = f"crypto_{ticker.lower()}"
    account = await session.scalar(select(Account).where(Account.code == code))
    if account is None:
        account = Account(code=code, name=f"Крипто {ticker}", currency=ticker, kind=CRYPTO, sort=90)
        session.add(account)
        await session.flush()
    elif not account.is_active:
        account.is_active = True
    return account


async def crypto_tickers(session: AsyncSession) -> list[str]:
    return list((await session.scalars(select(Account.currency).where(Account.kind == CRYPTO))).all())


async def last_crypto_currency(session: AsyncSession) -> str | None:
    """The coin of the latest crypto purchase: the default when only a number is typed."""
    return await session.scalar(
        select(Account.currency)
        .join(Transaction, Transaction.account_id == Account.id)
        .where(Account.kind == CRYPTO, Transaction.transfer_id.is_not(None), _not_deleted())
        .order_by(Transaction.booked_on.desc(), Transaction.id.desc())
        .limit(1)
    )


async def mark_crypto(
    session: AsyncSession, out: Transaction, amount: Decimal, currency: str, note: str | None = None
) -> Transfer:
    """Money paid out (card, QR, a P2P seller), coins received on the crypto wallet."""
    if out.amount >= 0:
        raise LedgerError("покупка крипты — это списание, а тут поступление")
    if out.account.kind == CRYPTO:
        raise LedgerError("это уже строка криптокошелька")
    amount = amount.quantize(COIN_PLACES)
    if amount <= 0:
        raise LedgerError("сколько монет пришло — нужно положительное число")
    wallet = await get_or_create_crypto(session, currency)
    return await _exchange(session, "crypto", out, wallet, amount, "КРИПТО", note)


async def record_fx(
    session: AsyncSession,
    myr_amount: Decimal,
    rub_amount: Decimal,
    from_account: str = "maybank",
    day: date | None = None,
    note: str | None = None,
) -> Transfer:
    """RF transfer typed into the bot, before the bank statement arrives or after it.

    A statement row already imported with that amount near `day` is the MYR leg;
    otherwise the leg is pending and the statement row with the same amount confirms it.
    """
    account = await get_account(session, from_account)
    if day is not None:
        posted = await session.scalars(
            select(Transaction)
            .where(
                Transaction.account_id == account.id,
                Transaction.amount == -abs(myr_amount),
                Transaction.status == POSTED,
                Transaction.transfer_id.is_(None),
                Transaction.booked_on.between(day - TRANSFER_WINDOW, day + TRANSFER_WINDOW),
                _not_deleted(),
            )
            .order_by(Transaction.booked_on)
        )
        found = min(posted.unique(), key=lambda t: abs((t.booked_on - day).days), default=None)
        if found is not None:
            await session.refresh(found, ["account"])
            return await mark_fx(session, found, rub_amount, note)
    out = Transaction(
        account_id=account.id,
        booked_on=day or today(),
        amount=-abs(myr_amount),
        description="Перевод на РФ (ждёт выписку)",
        merchant="ПЕРЕВОД НА РФ",
        kind=TRANSFER,
        status=PENDING,
        source="manual",
        note=note,
    )
    session.add(out)
    await session.flush()
    await session.refresh(out, ["account"])
    return await mark_fx(session, out, rub_amount, note)


async def pending_fx_near(session: AsyncSession, txn: Transaction) -> list[Transaction]:
    """Pending RF transfers typed into the bot that this statement row may confirm."""
    return list(
        (
            await session.scalars(
                select(Transaction)
                .join(Transfer, Transfer.id == Transaction.transfer_id)
                .where(
                    Transfer.kind == "fx",
                    Transaction.status == PENDING,
                    Transaction.account_id == txn.account_id,
                    Transaction.amount < 0,
                    Transaction.booked_on.between(txn.booked_on - timedelta(days=7), txn.booked_on + timedelta(days=7)),
                    _not_deleted(),
                )
                .order_by(Transaction.booked_on)
            )
        ).unique()
    )


async def merge_pending_into(session: AsyncSession, pending: Transaction, posted: Transaction) -> None:
    """The statement row is the real MYR leg of a pending RF transfer (amounts may differ by a fee)."""
    if pending.transfer_id is None:
        raise LedgerError("у ожидающей записи нет перевода")
    posted.transfer_id = pending.transfer_id
    posted.kind = TRANSFER
    posted.category_id = None
    posted.review_reason = None
    pending.transfer_id = None
    pending.deleted_at = datetime.now(timezone.utc)
    await session.flush()
    legs = await transfer_legs(session, posted.transfer_id)
    rub = next((leg for leg in legs if leg.amount > 0), None)
    transfer = await session.get(Transfer, posted.transfer_id)
    if rub is not None and transfer is not None and posted.amount:
        transfer.rate = (rub.amount / -posted.amount).quantize(Decimal("0.000001"))
    await session.commit()


async def transfer_targets(session: AsyncSession, txn: Transaction) -> list[Account]:
    """Own accounts with statements this row may have moved money to or from."""
    return list(
        (
            await session.scalars(
                select(Account)
                .where(
                    Account.kind.in_(STATEMENT_ACCOUNT_KINDS),
                    Account.is_active,
                    Account.currency == txn.account.currency,
                    Account.id != txn.account_id,
                )
                .order_by(Account.sort)
            )
        ).all()
    )


async def mark_internal(session: AsyncSession, txn: Transaction, account: Account | None = None) -> bool:
    """"Between my own accounts", optionally naming the other one. Links the other
    leg if it is already imported; a named account's later statement links it on import."""
    if txn.transfer_id:
        await unlink_transfer(session, txn.transfer_id)
    txn.kind = TRANSFER
    txn.category_id = None
    # Owner's answer is final: not asked again, even if the other account is never imported.
    txn.review_reason = REVIEW_OWN
    txn.pair_account_id = account.id if account else None
    await session.flush()
    await match_internal_transfers(session)
    await session.commit()
    return txn.transfer_id is not None


# --- categories and rules ----------------------------------------------------


async def set_category(
    session: AsyncSession, txn: Transaction, category: Category, remember: bool = False
) -> int:
    """Categorize one row; with `remember`, also add a rule and apply it to
    other rows still waiting for review. Returns how many other rows changed."""
    _, updated = await apply_category(session, txn, category, remember)
    return updated


async def apply_category(
    session: AsyncSession, txn: Transaction, category: Category, remember: bool = False
) -> tuple[CategoryRule | None, int]:
    """set_category that also returns the rule it saved, so the bot can offer to undo it."""
    if txn.transfer_id:
        await unlink_transfer(session, txn.transfer_id)
    txn.kind = category.kind
    txn.category_id = category.id
    txn.review_reason = None

    rule, updated = None, 0
    if remember:
        pattern = suggest_pattern(txn.description)
        rule = await session.scalar(
            select(CategoryRule).where(CategoryRule.pattern == pattern, CategoryRule.origin == "user")
        )
        if rule is None:
            rule = CategoryRule(pattern=pattern, priority=USER_RULE_PRIORITY, origin="user")
            session.add(rule)
        rule.category_id = category.id
        # kind is set so the rule also beats the generic "transfer to a person" rule
        # (rent paid by DuitNow becomes Housing, not a transfer to review).
        rule.kind = category.kind
        # money coming back from an expense merchant is a refund in the same category
        rule.direction = None if category.kind == EXPENSE else "in"
        await session.flush()

        regex = re.compile(pattern, re.IGNORECASE)
        sign = Transaction.amount < 0 if category.kind == EXPENSE else Transaction.amount > 0
        others = (
            await session.scalars(
                select(Transaction).where(
                    Transaction.review_reason.in_(USER_FACING_REVIEW),
                    Transaction.transfer_id.is_(None),
                    Transaction.id != txn.id,
                    sign,
                    _not_deleted(),
                )
            )
        ).unique().all()
        for other in others:
            if regex.search(other.description):
                other.kind = category.kind
                other.category_id = category.id
                other.review_reason = None
                updated += 1
    await session.commit()
    return rule, updated


async def user_rules(session: AsyncSession, limit: int = 15) -> list[CategoryRule]:
    return list(
        (
            await session.scalars(
                select(CategoryRule)
                .where(CategoryRule.origin == "user")
                .order_by(CategoryRule.created_at.desc(), CategoryRule.id.desc())
                .limit(limit)
            )
        ).unique()
    )


async def user_rule_for(session: AsyncSession, txn: Transaction) -> CategoryRule | None:
    """The bot's own rule for this row's merchant, if one was saved."""
    return await session.scalar(
        select(CategoryRule).where(
            CategoryRule.pattern == suggest_pattern(txn.description), CategoryRule.origin == "user"
        )
    )


async def find_transactions(session: AsyncSession, text: str = "", limit: int = 8) -> list[Transaction]:
    """/fix: the latest categorized rows whose description has the text (all words, any order).
    Transfers are left out: they have their own flows in /review."""
    conditions = [Transaction.kind != TRANSFER, Transaction.status != REVERSED, _not_deleted()]
    for word in text.split():
        conditions.append(Transaction.description.icontains(word, autoescape=True))
    return list(
        (
            await session.scalars(
                select(Transaction)
                .where(*conditions)
                .order_by(Transaction.booked_on.desc(), Transaction.id.desc())
                .limit(limit)
            )
        ).unique()
    )


async def delete_user_rule(session: AsyncSession, rule_id: int) -> CategoryRule | None:
    """Rows the rule already categorized keep their category."""
    rule = await session.get(CategoryRule, rule_id)
    if rule is None or rule.origin != "user":
        return None
    await session.delete(rule)
    await session.commit()
    return rule


async def dismiss_review(session: AsyncSession, txn: Transaction) -> None:
    """Keep as is: expense stays in its category, p2p inflow stays income."""
    if txn.review_reason == REVIEW_P2P:
        other = await get_category(session, "other")
        txn.kind = EXPENSE
        txn.category_id = other.id
    txn.review_reason = None
    await session.commit()


# --- manual entries ----------------------------------------------------------


async def add_manual(
    session: AsyncSession,
    account_code: str,
    amount: Decimal,
    description: str,
    day: date | None = None,
    category_code: str | None = None,
    status: str = POSTED,
    source: str = "manual",
    review: bool = False,
    booked_at: datetime | None = None,
    note: str | None = None,
) -> Transaction:
    """Entry typed by a person (review=False: they see the category right away)
    or pushed by a device, e.g. an Apple Pay automation or a payment screenshot (review=True)."""
    account = await get_account(session, account_code)
    classifier = await Classifier.load(session)
    c = classifier.classify(description, amount, account.code, category_hint=category_code)
    # An entry made by hand is a purchase or income, not a transfer to review.
    kind = c.kind if c.kind != TRANSFER else (EXPENSE if amount < 0 else INCOME)
    category_id = c.category_id
    explicit = classifier.by_code.get(category_code) if category_code else None
    if explicit is not None and explicit.kind == kind:
        category_id = explicit.id
    txn = Transaction(
        account_id=account.id,
        booked_on=day or today(),
        booked_at=booked_at,
        amount=amount,
        description=description,
        merchant=merchant_of(description),
        kind=kind,
        category_id=category_id,
        status=status,
        source=source,
        note=note,
        review_reason=REVIEW_UNCATEGORIZED if review and c.review_reason == REVIEW_UNCATEGORIZED else None,
    )
    if txn.category_id is None:
        fallback = await get_category(session, "other" if amount < 0 else "other_income")
        txn.category_id = fallback.id
    session.add(txn)
    await session.commit()
    await session.refresh(txn, ["account", "category"])
    return txn


SAME_PAYMENT_WITHIN = timedelta(minutes=10)


async def find_recorded(
    session: AsyncSession, account_code: str, amount: Decimal, day: date, at: datetime | None = None
) -> Transaction | None:
    """A row already holding this payment: the statement came first, or the same
    screenshot was sent twice. Same account and amount, a day either way; when
    both sides know the time, it must agree too (two coffees on one day)."""
    account = await get_account(session, account_code)
    rows = (
        await session.scalars(
            select(Transaction).where(
                Transaction.account_id == account.id,
                Transaction.amount == amount,
                Transaction.booked_on.between(day - timedelta(days=1), day + timedelta(days=1)),
                Transaction.status != REVERSED,
                _not_deleted(),
            )
        )
    ).unique().all()
    if at is not None:
        naive = at.replace(tzinfo=None)
        rows = [
            r for r in rows
            if r.booked_at is None
            or abs(r.booked_at.astimezone(get_settings().tz).replace(tzinfo=None) - naive) <= SAME_PAYMENT_WITHIN
        ]
    return min(rows, key=lambda t: abs((t.booked_on - day).days), default=None)


def account_for_card(card: str | None, default: str = "maybank") -> str:
    """Apple Wallet card name -> account code ("TNG eWallet Visa" -> tng)."""
    name = (card or "").lower()
    if "tng" in name or "touch" in name:
        return "tng"
    if "maybank" in name:
        return "maybank"
    return default


async def set_balance(session: AsyncSession, account_code: str, target: Decimal) -> Transaction | None:
    """Adjust a manually tracked account (RUB, cash) to the real balance."""
    account = await get_account(session, account_code)
    current = await session.scalar(
        select(func.coalesce(func.sum(Transaction.amount), 0)).where(
            Transaction.account_id == account.id, Transaction.status != REVERSED, _not_deleted()
        )
    )
    delta = target - Decimal(current)
    if delta == 0:
        return None
    txn = Transaction(
        account_id=account.id,
        booked_on=today(),
        amount=delta,
        description="Корректировка остатка",
        merchant="КОРРЕКТИРОВКА",
        kind=TRANSFER,
        status=POSTED,
        source="manual",
    )
    session.add(txn)
    await session.commit()
    return txn


async def check_balance(session: AsyncSession, account_code: str, amount: Decimal, day: date | None = None) -> BalanceCheck:
    """The real balance of a bank or wallet account, read in its app before the statement came.

    No correction row: the next statement takes over, and a correction would count twice then.
    """
    account = await get_account(session, account_code)
    if account.kind not in STATEMENT_ACCOUNT_KINDS or account.currency != "MYR":
        raise LedgerError("так задаётся остаток Maybank или TNG; рубли и наличные — /setbalance без названия счёта")
    check = BalanceCheck(account_id=account.id, as_of=day or today(), amount=amount)
    session.add(check)
    await session.commit()
    return check


async def soft_delete(session: AsyncSession, txn: Transaction) -> None:
    if txn.transfer_id:
        await unlink_transfer(session, txn.transfer_id)
    txn.deleted_at = datetime.now(timezone.utc)
    await session.commit()


async def last_manual(session: AsyncSession) -> Transaction | None:
    return await session.scalar(
        select(Transaction)
        .where(Transaction.source == "manual", _not_deleted())
        .order_by(Transaction.created_at.desc(), Transaction.id.desc())
        .limit(1)
    )


# --- review queue ------------------------------------------------------------


SNOOZE_DAYS = 7


def _needs_answer():
    stale = today() - STALE_PAIR_AFTER
    return (
        (Transaction.review_reason.in_(USER_FACING_REVIEW))
        | ((Transaction.review_reason == REVIEW_AWAITING_PAIR) & (Transaction.booked_on < stale))
    ) & (Transaction.status != REVERSED) & _not_deleted()


def review_filter():
    not_snoozed = Transaction.review_snoozed_until.is_(None) | (Transaction.review_snoozed_until <= today())
    return _needs_answer() & not_snoozed


async def snoozed_count(session: AsyncSession) -> int:
    return await session.scalar(
        select(func.count(Transaction.id)).where(_needs_answer(), Transaction.review_snoozed_until > today())
    ) or 0


async def snooze(session: AsyncSession, txn: Transaction, days: int = SNOOZE_DAYS) -> None:
    """"Пропустить": out of the queue for a week, then asked again."""
    txn.review_snoozed_until = today() + timedelta(days=days)
    await session.commit()


async def unsnooze_all(session: AsyncSession) -> int:
    """"Разобрать отложенные": skipped rows come back to the queue now, not in a week."""
    rows = (
        await session.scalars(select(Transaction).where(_needs_answer(), Transaction.review_snoozed_until > today()))
    ).unique().all()
    for row in rows:
        row.review_snoozed_until = None
    await session.commit()
    return len(rows)


async def review_queue(session: AsyncSession, limit: int = 20) -> list[Transaction]:
    return list(
        (
            await session.scalars(
                select(Transaction)
                .where(review_filter())
                .order_by(Transaction.booked_on.desc(), Transaction.id.desc())
                .limit(limit)
            )
        ).unique()
    )


async def review_count(session: AsyncSession) -> int:
    return await session.scalar(select(func.count(Transaction.id)).where(review_filter())) or 0


async def previous_fx_to(session: AsyncSession, merchant: str) -> bool:
    """Has money to this counterparty been marked as an RF transfer before?"""
    if not merchant:
        return False
    found = await session.scalar(
        select(Transaction.id)
        .join(Transfer, Transfer.id == Transaction.transfer_id)
        .where(Transfer.kind == "fx", Transaction.merchant == merchant, Transaction.amount < 0, _not_deleted())
        .limit(1)
    )
    return found is not None


async def apply_rules_to_review_queue(session: AsyncSession) -> tuple[int, int]:
    """Re-run rules over rows still waiting for an answer (after rules or OWNER_NAME change);
    runs on every start, after `finance seed`. Returns (rows changed, transfers paired).

    Uncategorized rows take what the rules say now. A transfer to or from a person
    that names the owner becomes a transfer between own accounts; such rows are not
    classified again, since the statement's type column (TNG "DUITNOW_RECEIVEFROM")
    is not stored.
    """
    classifier = await Classifier.load(session)
    rows = (
        await session.scalars(
            select(Transaction).where(
                Transaction.review_reason.in_(USER_FACING_REVIEW),
                Transaction.transfer_id.is_(None),
                Transaction.status != REVERSED,
                _not_deleted(),
            )
        )
    ).unique().all()
    changed = 0
    for row in rows:
        if row.review_reason in (REVIEW_P2P, REVIEW_P2P_IN):
            if names_owner(row.description):
                row.kind, row.category_id, row.review_reason = TRANSFER, None, REVIEW_AWAITING_PAIR
                changed += 1
            continue
        c = classifier.classify(row.description, row.amount, row.account.code)
        if c.review_reason != REVIEW_UNCATEGORIZED:
            row.kind, row.category_id, row.review_reason = c.kind, c.category_id, c.review_reason
            changed += 1
    await session.flush()
    paired = await match_internal_transfers(session)
    await session.commit()
    return changed, paired
