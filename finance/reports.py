"""Read side: period summaries for the bot, weekly/monthly reports, dashboard series."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from html import escape

from sqlalchemy import and_, case, func, literal_column, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from finance.config import get_settings
from finance.ledger import review_count, snoozed_count
from finance.models import (
    CRYPTO,
    EXPENSE,
    INCOME,
    REVERSED,
    REVIEW_P2P,
    SAVINGS,
    Account,
    BalanceCheck,
    Category,
    Transaction,
    Transfer,
)
from finance.utils import (
    RU_MONTHS,
    cycle_bounds,
    fmt_day,
    fmt_money,
    fmt_range,
    pct_change,
    today,
    week_bounds,
)

STATEMENT_SOURCES = ("tng_pdf", "maybank_csv", "maybank_pdf")
STALE_DATA_DAYS = 7
ZERO = Decimal("0")


@dataclass
class CategoryLine:
    code: str
    label: str
    amount: Decimal
    baseline: Decimal | None = None


@dataclass
class CurrencyBlock:
    currency: str
    expense: Decimal = ZERO
    income: Decimal = ZERO
    baseline_expense: Decimal | None = None
    categories: list[CategoryLine] = field(default_factory=list)
    merchants: list[tuple[str, Decimal, int]] = field(default_factory=list)


@dataclass
class Freshness:
    account: str
    last_day: date | None

    @property
    def days_old(self) -> int | None:
        return (today() - self.last_day).days if self.last_day else None


@dataclass
class Summary:
    start: date
    end: date
    blocks: dict[str, CurrencyBlock]
    fx_myr: Decimal = ZERO
    fx_rub: Decimal = ZERO
    fx_count: int = 0
    fx_back_rub: Decimal = ZERO  # rubles spent or handed over for someone ...
    fx_back_myr: Decimal = ZERO  # ... and the ringgit they gave back
    unresolved_count: int = 0
    unresolved_amount: Decimal = ZERO
    savings_net: Decimal = ZERO  # into savings pots minus taken out
    crypto_paid: dict[str, Decimal] = field(default_factory=dict)  # currency -> paid for coins
    crypto_got: dict[str, Decimal] = field(default_factory=dict)  # ticker -> coins received
    review_count: int = 0
    snoozed_count: int = 0
    freshness: list[Freshness] = field(default_factory=list)
    balances: list[tuple[str, str, Decimal, date | None]] = field(default_factory=list)

    @property
    def fx_rate(self) -> Decimal | None:
        return (self.fx_rub / self.fx_myr).quantize(Decimal("0.01")) if self.fx_myr else None


def _live(start: date, end: date):
    return and_(
        Transaction.booked_on.between(start, end),
        Transaction.status != REVERSED,
        Transaction.deleted_at.is_(None),
    )


async def _category_totals(session: AsyncSession, start: date, end: date, kind: str):
    """{(currency, category_code): amount}; expenses as positive numbers."""
    sign = -1 if kind == EXPENSE else 1
    rows = await session.execute(
        select(Account.currency, Category.code, func.sum(Transaction.amount) * sign)
        .join(Account, Account.id == Transaction.account_id)
        .outerjoin(Category, Category.id == Transaction.category_id)
        .where(_live(start, end), Transaction.kind == kind)
        .group_by(Account.currency, Category.code)
    )
    return {(cur, code or "other"): Decimal(amount) for cur, code, amount in rows}


async def summarize(
    session: AsyncSession,
    start: date,
    end: date,
    baseline_periods: int = 4,
    baseline_start: date | None = None,
) -> Summary:
    """Totals for [start, end]; baseline = average of the N periods before it.

    Periods are equal-length by default; `baseline_start` gives the exact start
    when they are not (financial months of 28-31 days).
    """
    length = (end - start).days + 1
    categories = {c.code: c for c in (await session.scalars(select(Category))).all()}

    expenses = await _category_totals(session, start, end, EXPENSE)
    incomes = await _category_totals(session, start, end, INCOME)
    baseline: dict[tuple[str, str], Decimal] = defaultdict(lambda: ZERO)
    if baseline_periods:
        b_start = baseline_start or start - timedelta(days=length * baseline_periods)
        for key, amount in (await _category_totals(session, b_start, start - timedelta(days=1), EXPENSE)).items():
            baseline[key] = amount / baseline_periods

    blocks: dict[str, CurrencyBlock] = {}
    for currency in sorted({cur for cur, _ in [*expenses, *incomes, *baseline]}, key=lambda c: c != "MYR"):
        block = CurrencyBlock(currency=currency)
        lines = []
        for (cur, code), amount in expenses.items():
            if cur != currency or amount == 0:
                continue
            cat = categories.get(code)
            lines.append(
                CategoryLine(code, cat.label if cat else code, amount, baseline.get((cur, code)) if baseline_periods else None)
            )
        block.categories = sorted(lines, key=lambda line: line.amount, reverse=True)
        block.expense = sum((line.amount for line in lines), ZERO)
        block.income = sum((a for (cur, _), a in incomes.items() if cur == currency), ZERO)
        if baseline_periods:
            block.baseline_expense = sum((a for (cur, _), a in baseline.items() if cur == currency), ZERO)
        blocks[currency] = block

    merchant_rows = await session.execute(
        select(Account.currency, Transaction.merchant, -func.sum(Transaction.amount), func.count())
        .join(Account, Account.id == Transaction.account_id)
        .where(_live(start, end), Transaction.kind == EXPENSE)
        .group_by(Account.currency, Transaction.merchant)
        .order_by(func.sum(Transaction.amount).asc())
    )
    for currency, merchant, amount, count in merchant_rows:
        block = blocks.get(currency)
        if block is not None and len(block.merchants) < 5 and amount > 0:
            block.merchants.append((merchant or "—", Decimal(amount), count))

    summary = Summary(start=start, end=end, blocks=blocks)

    fx = (
        await session.execute(
            select(
                func.coalesce(func.sum(case((Transaction.amount < 0, -Transaction.amount), else_=0)), 0),
                func.coalesce(func.sum(case((Transaction.amount > 0, Transaction.amount), else_=0)), 0),
                func.count(func.distinct(Transfer.id)),
            )
            .join(Transfer, Transfer.id == Transaction.transfer_id)
            .where(_live(start, end), Transfer.kind == "fx")
        )
    ).one()
    summary.fx_myr, summary.fx_rub, summary.fx_count = Decimal(fx[0]), Decimal(fx[1]), fx[2]

    back = await session.execute(
        select(Account.currency, func.sum(Transaction.amount))
        .join(Account, Account.id == Transaction.account_id)
        .join(Transfer, Transfer.id == Transaction.transfer_id)
        .where(_live(start, end), Transfer.kind == "fx_back")
        .group_by(Account.currency)
    )
    for currency, amount in back:
        if currency == "RUB":
            summary.fx_back_rub = -Decimal(amount)
        else:
            summary.fx_back_myr += Decimal(amount)

    crypto = await session.execute(
        select(Account.currency, Account.kind, func.sum(Transaction.amount))
        .join(Account, Account.id == Transaction.account_id)
        .join(Transfer, Transfer.id == Transaction.transfer_id)
        .where(_live(start, end), Transfer.kind == "crypto")
        .group_by(Account.currency, Account.kind)
    )
    for currency, kind, amount in crypto:
        if kind == CRYPTO:
            summary.crypto_got[currency] = Decimal(amount)
        else:
            summary.crypto_paid[currency] = summary.crypto_paid.get(currency, ZERO) - Decimal(amount)

    unresolved = (
        await session.execute(
            select(func.count(Transaction.id), func.coalesce(-func.sum(Transaction.amount), 0)).where(
                _live(start, end), Transaction.review_reason == REVIEW_P2P
            )
        )
    ).one()
    summary.unresolved_count, summary.unresolved_amount = unresolved[0], Decimal(unresolved[1])
    summary.savings_net = Decimal(
        await session.scalar(
            select(func.coalesce(func.sum(Transaction.amount), 0))
            .join(Account, Account.id == Transaction.account_id)
            .where(_live(start, end), Account.kind == SAVINGS, Transaction.transfer_id.is_not(None))
        )
    )
    summary.review_count = await review_count(session)
    summary.snoozed_count = await snoozed_count(session)
    summary.freshness = await freshness(session)
    summary.balances = await balances(session)
    return summary


async def freshness(session: AsyncSession) -> list[Freshness]:
    rows = await session.execute(
        select(Account.name, func.max(Transaction.booked_on))
        .join(Transaction, Transaction.account_id == Account.id)
        .where(Transaction.source.in_(STATEMENT_SOURCES), Transaction.deleted_at.is_(None))
        .group_by(Account.name, Account.sort)
        .order_by(Account.sort)
    )
    return [Freshness(name, last) for name, last in rows]


async def known_balance(session: AsyncSession, account: Account) -> tuple[Decimal, date, datetime | None] | None:
    """The newest balance known for sure: the last statement row, or a balance check
    typed later (its creation time comes along: rows added after it that day count on top)."""
    last = (
        await session.execute(
            select(Transaction.balance_after, Transaction.booked_on)
            .where(
                Transaction.account_id == account.id,
                Transaction.balance_after.is_not(None),
                Transaction.deleted_at.is_(None),
            )
            .order_by(Transaction.booked_on.desc(), Transaction.id.desc())
            .limit(1)
        )
    ).first()
    check = await session.scalar(
        select(BalanceCheck)
        .where(BalanceCheck.account_id == account.id)
        .order_by(BalanceCheck.as_of.desc(), BalanceCheck.id.desc())
        .limit(1)
    )
    if check is not None and (last is None or check.as_of > last[1]):
        return Decimal(check.amount), check.as_of, check.created_at
    if last is not None:
        return Decimal(last[0]), last[1], None
    return None


async def balances(session: AsyncSession) -> list[tuple[str, str, Decimal, date | None]]:
    """Statement accounts: the newest known balance (statement or a balance check) plus
    the rows booked after it, so purchases entered before the next statement count.
    Manual accounts: sum of entries."""
    out = []
    accounts = (await session.scalars(select(Account).where(Account.is_active).order_by(Account.sort))).all()
    for account in accounts:
        known = await known_balance(session, account)
        if known is not None:
            amount, day, checked_at = known
            after = Transaction.booked_on > day
            if checked_at is not None:
                after = or_(after, and_(Transaction.booked_on == day, Transaction.created_at > checked_at))
            added, last_day = (
                await session.execute(
                    select(func.coalesce(func.sum(Transaction.amount), 0), func.max(Transaction.booked_on)).where(
                        Transaction.account_id == account.id,
                        Transaction.status != REVERSED,
                        Transaction.deleted_at.is_(None),
                        after,
                    )
                )
            ).one()
            out.append((account.name, account.currency, amount + Decimal(added), max(day, last_day or day)))
            continue
        total, count = (
            await session.execute(
                select(func.coalesce(func.sum(Transaction.amount), 0), func.count(Transaction.id)).where(
                    Transaction.account_id == account.id,
                    Transaction.status != REVERSED,
                    Transaction.deleted_at.is_(None),
                )
            )
        ).one()
        if count:
            out.append((account.name, account.currency, Decimal(total), None))
    return out


async def latest_fx_rate(session: AsyncSession, days: int = 90) -> Decimal | None:
    """RUB per MYR actually received over recent RF transfers."""
    return await fx_rate_between(session, today() - timedelta(days=days), today())


async def fx_rate_between(session: AsyncSession, start: date, end: date) -> Decimal | None:
    myr, rub = (
        await session.execute(
            select(
                func.coalesce(func.sum(case((Transaction.amount < 0, -Transaction.amount), else_=0)), 0),
                func.coalesce(func.sum(case((Transaction.amount > 0, Transaction.amount), else_=0)), 0),
            )
            .join(Transfer, Transfer.id == Transaction.transfer_id)
            .where(_live(start, end), Transfer.kind == "fx")
        )
    ).one()
    return (Decimal(rub) / Decimal(myr)).quantize(Decimal("0.01")) if myr else None


# --- Telegram formatting ------------------------------------------------------


def _trend(current: Decimal, baseline: Decimal | None) -> str:
    if baseline is None:
        return ""
    change = pct_change(current, baseline)
    if change is None:
        return " 🆕" if current > 0 else ""
    if abs(change) < 10:
        return ""
    return f" {'↑' if change > 0 else '↓'}{abs(change)}%"


def format_summary(s: Summary, title: str, baseline_label: str) -> str:
    lines = [f"📊 <b>{escape(title)}</b>", ""]
    myr = s.blocks.get("MYR")
    if myr and (myr.expense or myr.income):
        trend = _trend(myr.expense, myr.baseline_expense).strip()
        vs = f" ({trend} {baseline_label})" if trend and trend != "🆕" else ""
        lines.append(f"<b>Расходы: {fmt_money(myr.expense)}</b>{vs}")
        if myr.income:
            lines.append(f"Доходы: {fmt_money(myr.income)}")
        lines.append("")
        for line in myr.categories[:8]:
            lines.append(f"{escape(line.label)} — {fmt_money(line.amount)}{_trend(line.amount, line.baseline)}")
        if myr.merchants:
            lines += ["", "<b>Больше всего</b>"]
            for merchant, amount, count in myr.merchants[:5]:
                times = f" ({count}×)" if count > 1 else ""
                lines.append(f"{escape(merchant[:32])} — {fmt_money(amount)}{times}")
    else:
        lines.append("Расходов в RM за период нет.")

    if s.fx_count:
        rate = f" (курс {s.fx_rate})" if s.fx_rate else ""
        lines += ["", f"🇷🇺 <b>На РФ:</b> {fmt_money(s.fx_myr)} → {fmt_money(s.fx_rub, 'RUB')}{rate}"]

    if s.fx_back_myr:
        lines += ["", f"↩️ <b>Вернули за рубли:</b> {fmt_money(s.fx_back_rub, 'RUB')} → {fmt_money(s.fx_back_myr)}"]

    if s.crypto_got:
        paid = " + ".join(fmt_money(amount, cur) for cur, amount in s.crypto_paid.items())
        got = " + ".join(fmt_money(amount, coin) for coin, amount in sorted(s.crypto_got.items()))
        lines += ["", f"🪙 <b>В крипту:</b> {paid} → {got}"]

    if s.savings_net:
        verb = "Отложил в копилки" if s.savings_net > 0 else "Взял из копилок"
        lines += ["", f"🐷 {verb}: {fmt_money(abs(s.savings_net))}"]

    rub = s.blocks.get("RUB")
    if rub and (rub.expense or rub.income):
        lines += ["", f"₽ <b>Расходы в рублях: {fmt_money(rub.expense, 'RUB')}</b>"]
        for line in rub.categories[:5]:
            lines.append(f"   {escape(line.label)} — {fmt_money(line.amount, 'RUB')}")

    for coin, block in s.blocks.items():
        if coin in ("MYR", "RUB") or not (block.expense or block.income):
            continue
        lines += ["", f"🪙 <b>Расходы в {escape(coin)}: {fmt_money(block.expense, coin)}</b>"]
        for line in block.categories[:5]:
            lines.append(f"   {escape(line.label)} — {fmt_money(line.amount, coin)}")

    alerts = []
    if s.unresolved_count:
        alerts.append(f"⚠️ Переводы людям без пометки: {s.unresolved_count} на {fmt_money(s.unresolved_amount)}")
    if s.review_count:
        later = f" (и {s.snoozed_count} отложено)" if s.snoozed_count else ""
        alerts.append(f"📝 Ждут разбора: {s.review_count}{later} → /review")
    stale = [f for f in s.freshness if f.days_old is not None and f.days_old > STALE_DATA_DAYS]
    if s.freshness:
        parts = [f"{escape(f.account)} — по {fmt_day(f.last_day)}" for f in s.freshness if f.last_day]
        alerts.append("📥 Данные: " + ", ".join(parts))
    for f in stale:
        alerts.append(f"   ⏰ {escape(f.account)}: {f.days_old} дн. без выписки — пришли свежую")
    if alerts:
        lines += ["", *alerts]
    return "\n".join(lines)


async def week_report(session: AsyncSession, day: date | None = None) -> str:
    """Report for the full week before `day` (default: last week)."""
    start, end = week_bounds((day or today()) - timedelta(days=7))
    summary = await summarize(session, start, end, baseline_periods=4)
    return format_summary(summary, f"Неделя {fmt_range(start, end)}", "к среднему за 4 нед.")


async def current_week_report(session: AsyncSession) -> str:
    start, _ = week_bounds(today())
    end = today()
    summary = await summarize(session, start, end, baseline_periods=0)
    return format_summary(summary, f"Эта неделя, {fmt_range(start, end)}", "")


async def month_report(session: AsyncSession, day: date | None = None) -> str:
    """Financial month containing `day` (default: the current one, to date).

    A finished month is compared with the three before it and checks that the
    salary is in: the report goes out the morning after payday.
    """
    start, end = cycle_bounds(day or today())
    finished = end < today()
    end = min(end, today())
    baseline_start = start
    for _ in range(3):
        baseline_start = cycle_bounds(baseline_start - timedelta(days=1))[0]
    summary = await summarize(
        session, start, end, baseline_periods=3 if finished else 0, baseline_start=baseline_start
    )
    if get_settings().MONTH_START_DAY == 1:
        title = f"{RU_MONTHS[start.month - 1]} {start.year}"
    else:
        title = f"{'Месяц' if finished else 'Этот месяц:'} {fmt_range(start, end)}"
    text = format_summary(summary, title, "к среднему за 3 мес.")

    incomes = await _category_totals(session, start, end, INCOME)
    salary = incomes.get(("MYR", "salary"), ZERO)
    if salary:
        text += f"\n\n💼 Зарплата: {fmt_money(salary)}"
    elif finished:
        text += "\n\n💼 Зарплаты за этот месяц не видно — пришли свежую выписку Maybank"
    myr = summary.blocks.get("MYR")
    if myr and myr.income:
        net = myr.income - myr.expense - summary.fx_myr + summary.fx_back_myr
        back = " + вернули за ₽" if summary.fx_back_myr else ""
        text += f"\n💰 Остаток месяца (доход − расход − РФ{back}): {fmt_money(net, signed=True)}"
    return text


# --- dashboard series ----------------------------------------------------------


async def monthly_series(session: AsyncSession, currency: str, months: int = 12) -> list[dict]:
    """Financial months, oldest first; each keyed by the month it ends in (26 Aug - 25 Sep -> 2026-09)."""
    start_day = get_settings().MONTH_START_DAY
    cycles = [cycle_bounds(today())]
    for _ in range(months - 1):
        cycles.insert(0, cycle_bounds(cycles[0][0] - timedelta(days=1)))

    booked = Transaction.booked_on
    if start_day > 1:
        # rows from the 26th on belong to the month that ends next calendar month
        booked = case(
            (func.extract("day", Transaction.booked_on) >= start_day, Transaction.booked_on + literal_column("interval '1 month'")),
            else_=Transaction.booked_on,
        )
    month = func.to_char(booked, "YYYY-MM")
    rows = await session.execute(
        select(month, Transaction.kind, Category.code, func.sum(Transaction.amount))
        .join(Account, Account.id == Transaction.account_id)
        .outerjoin(Category, Category.id == Transaction.category_id)
        .where(
            _live(cycles[0][0], today()),
            Account.currency == currency,
            Transaction.kind.in_((EXPENSE, INCOME)),
        )
        .group_by(month, Transaction.kind, Category.code)
    )
    by_month: dict[str, dict] = {}
    for start, end in cycles:
        key = end.strftime("%Y-%m")
        by_month[key] = {"month": key, "start": start, "end": end, "expense": ZERO, "income": ZERO, "categories": {}}
    for key, kind, code, amount in rows:
        bucket = by_month.get(key)
        if bucket is None:
            continue
        amount = Decimal(amount)
        if kind == EXPENSE:
            bucket["expense"] += -amount
            bucket["categories"][code or "other"] = bucket["categories"].get(code or "other", ZERO) - amount
        else:
            bucket["income"] += amount
    return list(by_month.values())


async def daily_series(session: AsyncSession, start: date, end: date, currency: str) -> list[dict]:
    rows = await session.execute(
        select(Transaction.booked_on, -func.sum(Transaction.amount))
        .join(Account, Account.id == Transaction.account_id)
        .where(_live(start, end), Account.currency == currency, Transaction.kind == EXPENSE)
        .group_by(Transaction.booked_on)
    )
    totals = {day: Decimal(amount) for day, amount in rows}
    out, cursor = [], start
    while cursor <= end:
        out.append({"day": cursor.isoformat(), "amount": totals.get(cursor, ZERO)})
        cursor += timedelta(days=1)
    return out


def format_import(r) -> str:
    """Bot message for a finance.ledger.ImportResult."""
    start, end = r.period
    period = f": {fmt_range(start, end)}" if start and end else ""
    if r.already_imported and not (r.new or r.refreshed or r.to_pots):
        return f"📥 {escape(r.account_name)}{period} — этот файл уже загружен."
    lines = [f"📥 <b>{escape(r.account_name)}</b>{period}", f"Новых: {r.new} · дублей: {r.duplicate}"]
    if r.refreshed:
        lines.append(f"✏️ Уточнил описания: {r.refreshed}")
    if r.to_pots:
        lines.append(f"🐷 Переводов с копилками: {r.to_pots}")
    if r.reconciled:
        lines.append(f"Подтверждено ожидавших: {r.reconciled}")
    if r.matched_transfers:
        lines.append(f"↔️ Связал переводов между счетами: {r.matched_transfers}")
    if r.to_review:
        lines.append(f"📝 Нужно разобрать: {r.to_review}")
    return "\n".join(lines)
