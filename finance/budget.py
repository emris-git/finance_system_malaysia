"""Budget for the next salary cycles and the plan it rests on.

A cycle is the financial month (MONTH_START_DAY: 26th -> 25th, or calendar months
when it is 1). For each of the
next BUDGET_HORIZON cycles the forecast starts from the spendable money (bank,
wallet and cash in MYR; reserve pots are shown apart), adds the expected salary,
takes away the usual spending and the planned items, and carries the rest over.

Usual spending per category is the median of the last three cycles with full data;
when the same cycle a year earlier is in the data too, the two are averaged
(December is not an ordinary month). Trips (PLANNED_ONLY) are left out of it: a
trip goes into the plan, and past trips would count it twice.

The low point of a cycle is the money left just before the salary lands, the
moment a big purchase is most likely not to fit. Spending that happens after the
salary (the rent due right after payday) is not taken away before it; which part of a
category that is comes from the latest cycle it appeared in.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from html import escape
from statistics import median

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from finance import reports
from finance.config import get_settings
from finance.ledger import STATEMENT_ACCOUNT_KINDS, LedgerError, get_category
from finance.models import (
    EXPENSE,
    INCOME,
    PLAN_CANCELLED,
    PLAN_DONE,
    PLAN_OPEN,
    REVERSED,
    SAVINGS,
    TRANSFER,
    Account,
    Category,
    PlannedItem,
    Transaction,
    Transfer,
)
from finance.utils import RU_MONTHS, RU_MONTHS_GEN, cycle_bounds, fmt_day, fmt_money, today

BASELINE_CYCLES = 3
PLANNED_ONLY = ("travel",)
LIQUID_KINDS = ("bank", "ewallet", "cash")
ZERO = Decimal("0")
# a category paid mostly after the salary is named in the explanation of the low point
LATE_NAMED = Decimal("0.3")
CENT = Decimal("0.01")
# no income in the "salary" category in the past cycles: a new owner sees why the salary column is empty
SALARY_MISSING = (
    "зарплата не найдена — поставь поступлению категорию «Зарплата» (в /review или на дашборде), "
    "и она попадёт в прогноз"
)


# --- dates -------------------------------------------------------------------------


def add_months(day: date, months: int) -> date:
    """Same day `months` later, clipped to the month's end (31 Jan + 1 -> 28 Feb)."""
    index = day.year * 12 + day.month - 1 + months
    year, month = divmod(index, 12)
    month += 1
    for d in (day.day, 30, 29, 28):
        try:
            return date(year, month, d)
        except ValueError:
            continue
    raise ValueError(day)


def cycles_from(day: date, count: int) -> list[tuple[date, date]]:
    out = [cycle_bounds(day)]
    while len(out) < count:
        out.append(cycle_bounds(out[-1][1] + timedelta(days=1)))
    return out


def month_rule() -> str:
    """How a month is counted, for the texts: "с 26-го по 25-е" or "календарный" (MONTH_START_DAY=1)."""
    day = get_settings().MONTH_START_DAY
    return f"с {day}-го по {day - 1}-е" if day > 1 else "календарный"


def cycle_label(start: date, end: date, now: date | None = None) -> str:
    """A cycle is named after the month it ends in ("окт" for 26 Sep - 25 Oct)."""
    label = RU_MONTHS_GEN[end.month - 1]
    if end.year != (now or today()).year:
        label += f" {end.year % 100:02d}"
    return label


def _day_in_cycle(start: date, end: date, day_of_month: int) -> date:
    """The date with this day of month inside [start, end] (the salary day of a cycle)."""
    for month_start in (end.replace(day=1), start.replace(day=1)):
        try:
            candidate = month_start.replace(day=day_of_month)
        except ValueError:
            candidate = add_months(month_start, 1) - timedelta(days=1)
        if start <= candidate <= end:
            return candidate
    return end


# --- the plan ------------------------------------------------------------------------


async def open_plans(session: AsyncSession) -> list[PlannedItem]:
    return list(
        (
            await session.scalars(
                select(PlannedItem).where(PlannedItem.status == PLAN_OPEN).order_by(PlannedItem.due_on, PlannedItem.id)
            )
        ).unique()
    )


async def add_plan(
    session: AsyncSession,
    title: str,
    amount: Decimal,
    due_on: date,
    kind: str = EXPENSE,
    category_code: str | None = None,
    repeat_months: int | None = None,
    until: date | None = None,
    note: str | None = None,
) -> PlannedItem:
    """`amount` signed: negative = money out. Income must be positive, spending negative."""
    if kind not in (EXPENSE, TRANSFER, INCOME):
        raise LedgerError(f"неизвестный вид: {kind}")
    if not amount:
        raise LedgerError("сумма не может быть нулевой")
    if (kind == INCOME) != (amount > 0):
        raise LedgerError("доход — положительная сумма, трата или перевод — отрицательная")
    if repeat_months is not None and repeat_months < 1:
        raise LedgerError("повтор — раз в месяц или реже")
    category = await get_category(session, category_code) if category_code else None
    item = PlannedItem(
        title=title.strip()[:200] or "без названия",
        amount=amount.quantize(CENT),
        kind=kind,
        category_id=category.id if category else None,
        due_on=due_on,
        repeat_months=repeat_months,
        until=until,
        note=note,
    )
    session.add(item)
    await session.commit()
    await session.refresh(item, ["category"])
    return item


async def close_plan(session: AsyncSession, item_id: int, status: str) -> PlannedItem:
    if status not in (PLAN_DONE, PLAN_CANCELLED, PLAN_OPEN):
        raise LedgerError(f"неизвестный статус: {status}")
    item = await session.get(PlannedItem, item_id)
    if item is None:
        raise LedgerError("такого пункта плана нет")
    item.status = status
    await session.commit()
    return item


def occurrences(item: PlannedItem, start: date, end: date) -> list[date]:
    """Dates of `item` inside [start, end]."""
    if not item.repeat_months:
        return [item.due_on] if start <= item.due_on <= end else []
    out, k = [], 0
    while True:
        day = add_months(item.due_on, k * item.repeat_months)
        if day > end or (item.until and day > item.until):
            return out
        if day >= start:
            out.append(day)
        k += 1


REPEAT_WORDS = {
    r"ежемесячн\w*|каждый\s+месяц|раз\s+в\s+месяц": 1,
    r"ежеквартальн\w*|раз\s+в\s+квартал": 3,
    r"ежегодн\w*|каждый\s+год|раз\s+в\s+год": 12,
}
MONTH_FORMS = (
    r"январ[ьяе]|янв", r"феврал[ьяе]|фев", r"март[ае]?|мар", r"апрел[ьяе]|апр", r"ма[йяе]", r"июн[ьяе]?",
    r"июл[ьяе]?", r"август[ае]?|авг", r"сентябр[ьяе]|сен", r"октябр[ьяе]|окт", r"ноябр[ьяе]|ноя", r"декабр[ьяе]|дек",
)
MONTH_RE = re.compile(
    r"(?<![\wа-яё])(?:в\s+)?(" + "|".join(f"(?:{f})" for f in MONTH_FORMS) + r")(?:\s+(\d{4}))?(?![\wа-яё])",
    re.IGNORECASE,
)
DATE_RE = re.compile(r"(?<![\d.])(\d{1,2})\.(\d{1,2})(?:\.(\d{4}|\d{2}))?(?![\d.])")
MONTH_YEAR_RE = re.compile(r"(?<![\d.])(\d{1,2})[./](\d{4})(?![\d.])")
NUMBER = r"(\d{1,3}(?:[ \u00a0]\d{3})+|\d+)(?:[.,](\d{1,2}))?"
# the amount goes first ("1800 отель"); otherwise the last number in the text ("iPhone 17 5500")
LAST_NUMBER_RE = re.compile(r"(?<![\w.])()" + NUMBER + r"(?:\s*(?:rm|myr))?(?![\w.])(?!.*(?<![\w.])\d)", re.IGNORECASE)
AMOUNT_RE = re.compile(r"^\s*([+-])?\s*(?:rm\s*)?" + NUMBER + r"(?:\s*(?:rm|myr|ринг\w*))?(?![\w.])", re.IGNORECASE)
TAG_RE = re.compile(r"(?<!\S)#([\wА-Яа-яЁё-]+)")
TRANSFER_TAGS = ("перевод", "рф", "копилка", "transfer")


@dataclass
class PlanDraft:
    title: str
    amount: Decimal  # signed
    due_on: date
    kind: str
    repeat_months: int | None
    category_tag: str | None


def _future(day: date, now: date, explicit_year: bool) -> date:
    return day if explicit_year or day >= now else day.replace(year=day.year + 1)


def parse_plan(text: str, now: date | None = None) -> PlanDraft | None:
    """"1800 отель Бали 25.10", "1600 экскурсия ноябрь", "+3000 бонус 12.2026",
    "2000 РФ ежемесячно 03.10 #перевод", "1000 копилка Holiday каждый месяц с 26.10 #копилка".

    A month without a day means the middle of it (the cycle that ends in that month).
    Without any date it is None: a plan needs a when.
    """
    now = now or today()
    rest = f" {text.strip()} "
    repeat = None
    for pattern, months in REPEAT_WORDS.items():
        m = re.search(pattern, rest, re.IGNORECASE)
        if m:
            repeat = months
            rest = rest[: m.start()] + " " + rest[m.end():]
            break
    tags = [t.lower() for t in TAG_RE.findall(rest)]
    rest = TAG_RE.sub(" ", rest)

    due = None
    m = DATE_RE.search(rest)
    if m:
        day_, month_, year_ = int(m.group(1)), int(m.group(2)), m.group(3)
        year = int(year_) + (2000 if year_ and len(year_) == 2 else 0) if year_ else now.year
        try:
            due = _future(date(year, month_, day_), now, bool(year_))
        except ValueError:
            return None
        rest = rest[: m.start()] + " " + rest[m.end():]
    else:
        m = MONTH_YEAR_RE.search(rest)
        if m and 1 <= int(m.group(1)) <= 12:
            due = date(int(m.group(2)), int(m.group(1)), 15)
            rest = rest[: m.start()] + " " + rest[m.end():]
        else:
            m = MONTH_RE.search(rest)
            if m:
                word = m.group(1).lower()
                month_ = next(i for i, form in enumerate(MONTH_FORMS, 1) if re.fullmatch(form, word))
                due = _future(date(int(m.group(2) or now.year), month_, 15), now.replace(day=1), bool(m.group(2)))
                rest = rest[: m.start()] + " " + rest[m.end():]
    rest = re.sub(r"(?<![\wа-яё])с(?![\wа-яё])", " ", rest, flags=re.IGNORECASE)  # "каждый месяц с 03.10"

    m = AMOUNT_RE.match(rest) or LAST_NUMBER_RE.search(rest)
    if not m or due is None:
        return None
    value = Decimal(re.sub(r"\s", "", m.group(2)) + "." + (m.group(3) or "0")).quantize(CENT)
    if not value:
        return None
    rest = rest[: m.start()] + " " + rest[m.end():]
    title = " ".join(rest.split()).strip(" -—,")
    incoming = m.group(1) == "+"
    kind = INCOME if incoming else TRANSFER if any(t in TRANSFER_TAGS for t in tags) else EXPENSE
    category_tag = next((t for t in tags if t not in TRANSFER_TAGS), None)
    return PlanDraft(title or "без названия", value if incoming else -value, due, kind, repeat, category_tag)


def plan_line(item: PlannedItem) -> str:
    when = fmt_day(item.due_on)
    if item.repeat_months == 1:
        when = f"каждый месяц с {when}"
    elif item.repeat_months == 12:
        when = f"каждый год с {when}"
    elif item.repeat_months:
        when = f"раз в {item.repeat_months} мес. с {when}"
    if item.until:
        when += f" по {fmt_day(item.until)}"
    kind = {TRANSFER: " · перевод", INCOME: " · доход"}.get(item.kind, "")
    category = f" · {escape(item.category.label)}" if item.category else ""
    return f"{fmt_money(item.amount, signed=True)} · {escape(item.title)} · {when}{kind}{category}"


# --- the forecast ------------------------------------------------------------------


@dataclass
class PlanHit:
    plan_id: int
    title: str
    amount: Decimal  # signed
    kind: str
    day: date
    overdue: bool = False


@dataclass
class CycleForecast:
    start: date
    end: date
    label: str
    salary_day: date | None
    opening: Decimal
    salary: Decimal  # expected salary still to come in this cycle
    spending: Decimal  # usual spending still to come (positive)
    planned: list[PlanHit]
    closing: Decimal
    low: Decimal  # money left just before the salary lands
    spent: Decimal = ZERO  # already spent (the current cycle only)
    by_category: dict[str, Decimal] = field(default_factory=dict)
    usual: Decimal = ZERO  # usual spending of the whole cycle, before what is already spent
    before_salary: Decimal = ZERO  # usual spending still to come before the salary lands
    planned_before: Decimal = ZERO  # planned items dated before the salary (signed)

    @property
    def planned_total(self) -> Decimal:
        return sum((p.amount for p in self.planned), ZERO)


@dataclass
class Forecast:
    today: date
    liquid: list[tuple[str, Decimal]]  # (account, balance) that the forecast spends
    reserve: list[tuple[str, Decimal]]  # spendable pots, not in the forecast
    protected: list[tuple[str, Decimal]]  # goal pots the budget must not touch
    salary: Decimal
    salary_day_of_month: int | None
    baseline: dict[str, Decimal]  # usual spending per category per cycle (seasonality aside)
    baseline_cycles: list[tuple[date, date]]
    late_share: dict[str, Decimal]  # part of a category usually spent after the salary
    history: list[dict]  # the baseline cycles as they were
    fx_per_cycle: Decimal  # MYR sent to Russia per cycle on average, not in the forecast
    travel_per_cycle: Decimal  # trips per cycle on average, not in the forecast
    cycles: list[CycleForecast]
    categories: dict[str, str]  # code -> label
    data_since: date | None
    notes: list[str] = field(default_factory=list)

    @property
    def liquid_total(self) -> Decimal:
        return sum((a for _, a in self.liquid), ZERO)

    @property
    def reserve_total(self) -> Decimal:
        return sum((a for _, a in self.reserve), ZERO)


def _live(start: date, end: date):
    return (
        Transaction.booked_on.between(start, end),
        Transaction.status != REVERSED,
        Transaction.deleted_at.is_(None),
    )


async def _spent(session: AsyncSession, start: date, end: date) -> dict[str, Decimal]:
    """MYR spending per category in [start, end], refunds netted, positive."""
    rows = await session.execute(
        select(Category.code, -func.sum(Transaction.amount))
        .join(Account, Account.id == Transaction.account_id)
        .outerjoin(Category, Category.id == Transaction.category_id)
        .where(*_live(start, end), Transaction.kind == EXPENSE, Account.currency == "MYR")
        .group_by(Category.code)
    )
    return {code or "other": Decimal(v) for code, v in rows if v}


async def _salary(session: AsyncSession, start: date, end: date) -> tuple[Decimal, date | None]:
    total, last = (
        await session.execute(
            select(func.coalesce(func.sum(Transaction.amount), 0), func.max(Transaction.booked_on))
            .join(Account, Account.id == Transaction.account_id)
            .join(Category, Category.id == Transaction.category_id)
            .where(*_live(start, end), Transaction.kind == INCOME, Category.code == "salary", Account.currency == "MYR")
        )
    ).one()
    return Decimal(total), last


async def _fx_out(session: AsyncSession, start: date, end: date) -> Decimal:
    value = await session.scalar(
        select(func.coalesce(-func.sum(Transaction.amount), 0))
        .join(Account, Account.id == Transaction.account_id)
        .join(Transfer, Transfer.id == Transaction.transfer_id)
        .where(*_live(start, end), Transfer.kind == "fx", Account.currency == "MYR", Transaction.amount < 0)
    )
    return Decimal(value)


async def _data_since(session: AsyncSession) -> date | None:
    """First day the MYR bank and wallet data covers (the ruble account is typed by hand)."""
    return await session.scalar(
        select(func.min(Transaction.booked_on))
        .join(Account, Account.id == Transaction.account_id)
        .where(Account.kind.in_(STATEMENT_ACCOUNT_KINDS), Account.currency == "MYR", Transaction.deleted_at.is_(None))
    )


def _q(value: Decimal) -> Decimal:
    return Decimal(value).quantize(CENT)


async def forecast(
    session: AsyncSession, horizon: int | None = None, extra: list[PlanHit] | None = None, now: date | None = None
) -> Forecast:
    """`extra`: what-if items on top of the plan (the /ask simulation)."""
    now = now or today()
    horizon = horizon or get_settings().BUDGET_HORIZON
    categories = {c.code: c.label for c in (await session.scalars(select(Category))).all()}
    accounts = {a.name: a for a in (await session.scalars(select(Account).where(Account.is_active))).all()}

    liquid, reserve, protected = [], [], []
    for name, currency, amount, _ in await reports.balances(session):
        account = accounts.get(name)
        if account is None or currency != "MYR":
            continue
        if account.kind in LIQUID_KINDS:
            liquid.append((name, _q(amount)))
        elif account.kind == SAVINGS:
            (reserve if account.spendable else protected).append((name, _q(amount)))

    data_since = await _data_since(session)
    current = cycle_bounds(now)
    past, cursor = [], current
    for _ in range(12):  # look back for up to a year for cycles with full data
        cursor = cycle_bounds(cursor[0] - timedelta(days=1))
        if data_since is None or cursor[0] < data_since:
            break
        past.append(cursor)
        if len(past) == BASELINE_CYCLES:
            break

    history, per_cycle, salaries, salary_days, late_by_cycle = [], [], [], [], []
    fx_total = ZERO
    for start, end in past:
        spent = await _spent(session, start, end)
        salary, salary_on = await _salary(session, start, end)
        per_cycle.append(spent)
        if salary > 0:
            salaries.append(salary)
            salary_days.append(salary_on.day)
            late_by_cycle.append((spent, await _spent(session, salary_on, end)))
        fx = await _fx_out(session, start, end)
        fx_total += fx
        history.append({"start": start, "end": end, "label": cycle_label(start, end, now), "salary": salary,
                        "spending": spent, "fx": fx})

    codes = {code for spent in per_cycle for code in spent}
    baseline = {
        code: _q(Decimal(median([spent.get(code, ZERO) for spent in per_cycle])))
        for code in codes
        if code not in PLANNED_ONLY
    }
    baseline = {code: amount for code, amount in baseline.items() if amount > 0}
    # the latest cycle with the category decides: the rent moved to the salary day stays there
    late_share = {}
    for code in baseline:
        spent, late = next(((sp, lt) for sp, lt in late_by_cycle if sp.get(code, ZERO) > 0), ({}, {}))
        if spent:
            late_share[code] = min(Decimal(1), max(ZERO, (late.get(code, ZERO) / spent[code]).quantize(CENT)))
    salary = _q(Decimal(median(salaries))) if salaries else ZERO
    salary_dom = int(median(salary_days)) if salary_days else None
    n = len(past) or 1
    travel = _q(sum((spent.get(code, ZERO) for spent in per_cycle for code in PLANNED_ONLY), ZERO) / n)

    plans = await open_plans(session)
    result = Forecast(
        today=now, liquid=liquid, reserve=reserve, protected=protected, salary=salary,
        salary_day_of_month=salary_dom, baseline=baseline, baseline_cycles=past, late_share=late_share,
        history=history, fx_per_cycle=_q(fx_total / n), travel_per_cycle=travel, cycles=[],
        categories=categories, data_since=data_since,
    )
    if not past:
        result.notes.append("нет ни одного полного цикла в данных — обычные траты неизвестны")
    elif len(past) < BASELINE_CYCLES:
        result.notes.append(f"обычные траты посчитаны по {len(past)} циклам из {BASELINE_CYCLES}")
    if not salary:
        result.notes.append(SALARY_MISSING)

    opening = result.liquid_total
    for index, (start, end) in enumerate(cycles_from(now, horizon)):
        first = index == 0
        expected = dict(baseline)
        last_year = cycle_bounds(add_months(start, -12))
        if data_since is not None and last_year[0] >= data_since:
            ly = await _spent(session, *last_year)
            expected = {
                code: _q((baseline.get(code, ZERO) + ly.get(code, ZERO)) / 2)
                for code in set(baseline) | set(ly)
                if code not in PLANNED_ONLY
            }

        salary_day = _day_in_cycle(start, end, salary_dom) if salary_dom else None
        spent_now: dict[str, Decimal] = {}
        salary_left = salary
        if first:
            spent_now = await _spent(session, start, now)
            got, _ = await _salary(session, start, now)
            if got > 0:
                salary_left = ZERO

        remaining, before_salary = {}, ZERO
        for code, amount in expected.items():
            already = spent_now.get(code, ZERO)
            left = max(amount - already, ZERO)
            remaining[code] = left
            early = amount * (1 - late_share.get(code, ZERO))
            before_salary += max(early - already, ZERO) if first else early
        spending = _q(sum(remaining.values(), ZERO))

        hits: list[PlanHit] = []
        for item in plans:
            if first and not item.repeat_months and item.due_on < start:
                hits.append(PlanHit(item.id, item.title, item.amount, item.kind, now, overdue=True))
                continue
            for day in occurrences(item, start, end):
                if first and item.repeat_months and day < now:
                    continue  # this month's repeat has most likely gone already
                hits.append(PlanHit(item.id, item.title, item.amount, item.kind, day))
        for hit in extra or []:
            if start <= hit.day <= end or (first and hit.day < start):
                hits.append(hit)

        planned = sum((h.amount for h in hits), ZERO)
        closing = opening + salary_left - spending + planned
        if salary_left and salary_day and salary_day >= now:
            planned_before = sum((h.amount for h in hits if h.day < salary_day), ZERO)
            before_salary = _q(before_salary)
            low = opening - before_salary + planned_before
        else:  # no salary ahead in this cycle: the end of it is the lowest point
            planned_before, before_salary, low = planned, spending, closing
        result.cycles.append(
            CycleForecast(
                start=start, end=end, label=cycle_label(start, end, now), salary_day=salary_day,
                opening=_q(opening), salary=salary_left, spending=spending, planned=hits,
                closing=_q(closing), low=_q(low),
                spent=_q(sum(spent_now.values(), ZERO)), by_category=remaining,
                usual=_q(sum(expected.values(), ZERO)), before_salary=before_salary, planned_before=planned_before,
            )
        )
        opening = closing
    return result


def _t(value: Decimal) -> str:
    """Thousands with one decimal for the phone-wide table: 15450 -> 15.5."""
    sign = "−" if value < 0 else ""
    return f"{sign}{abs(value) / 1000:.1f}"


def _n(value: Decimal) -> str:
    """Money without the currency, for the column of a sum: 8926 -> 8 926.00."""
    sign = "−" if value < 0 else ""
    return sign + f"{abs(value):,.2f}".replace(",", " ")


def _sum_block(rows: list[tuple[str, Decimal]]) -> str:
    """A written-out sum: sign and label on the left, the number right-aligned."""
    width = max(len(_n(v)) for _, v in rows)
    return "\n".join(f"{label:<22}{_n(v).rjust(width)}" for label, v in rows)


def explain_cycle(f: Forecast, c: CycleForecast) -> list[str]:
    """How the end of a cycle and its low point come out, number by number."""
    name = f"{RU_MONTHS[c.end.month - 1]} ({fmt_day(c.start)} – {fmt_day(c.end)})"
    first = c is f.cycles[0]
    rows = [("  на счетах сейчас" if first else "  с прошлого месяца", c.opening)]
    if c.salary:
        rows.append((f"+ зарплата {fmt_day(c.salary_day)}" if c.salary_day else "+ зарплата", c.salary))
    rows.append(("− обычные траты", c.spending))
    if c.planned:
        rows.append(("− план" if c.planned_total <= 0 else "+ план", abs(c.planned_total)))
    rows.append((f"= к {fmt_day(c.end)}", c.closing))
    lines = [f"<b>{name}</b>", f"<pre>{escape(_sum_block(rows))}</pre>"]

    if first and c.spent:
        lines.append(f"Обычные траты: {fmt_money(c.usual)} за месяц − уже потрачено с {fmt_day(c.start)} "
                     f"{fmt_money(c.spent)} = {fmt_money(c.spending)} ещё впереди.")
    else:
        lines.append(f"Обычные траты: {fmt_money(c.usual)} за месяц.")
    if c.planned:
        lines.append("План: " + ", ".join(f"{escape(h.title)} {fmt_money(abs(h.amount))} ({fmt_day(h.day)})" for h in c.planned) + ".")
    if c.salary and c.salary_day:
        after = c.spending - c.before_salary
        parts = [f"{_n(c.opening)} − {_n(c.before_salary)} трат до зарплаты"]
        if c.planned_before:
            parts.append(f"− {_n(-c.planned_before)} плана до неё" if c.planned_before < 0 else f"+ {_n(c.planned_before)} плана до неё")
        warn = " ⚠️" if c.low < 0 else ""
        lines.append(f"Дно утром {fmt_day(c.salary_day)}, до зарплаты: {' '.join(parts)} = <b>{fmt_money(c.low)}</b>{warn}.")
        if after > 0:
            late = [f.categories.get(code, code) for code, share in sorted(f.late_share.items(), key=lambda kv: -kv[1]) if share >= LATE_NAMED]
            which = f" ({escape(', '.join(late))})" if late else ""
            lines.append(f"<i>Остальные {fmt_money(after)} обычно уходят в день зарплаты или после{which}, до неё не вычитаются.</i>")
    return lines


def format_forecast(f: Forecast) -> str:
    """The /budget message: the current month written out, a table of all months, the plan and what is left out."""
    salary = f", зарплата около {f.salary_day_of_month}-го" if f.salary_day_of_month else ""
    lines = [f"📅 <b>Бюджет на {len(f.cycles)} мес.</b>", f"Месяц — {month_rule()}{salary}."]
    if SALARY_MISSING in f.notes:
        lines.append(f"⚠️ {SALARY_MISSING[0].upper()}{SALARY_MISSING[1:]}.")
    money = ", ".join(f"{escape(name)} {fmt_money(a)}" for name, a in f.liquid)
    lines.append(f"На счетах: {fmt_money(f.liquid_total)} ({money})")
    if f.reserve:
        lines.append("Резерв (в расчёт не входит): " + ", ".join(f"{escape(n)} {fmt_money(a)}" for n, a in f.reserve))
    if f.protected:
        lines.append("Не трогаем: " + ", ".join(f"{escape(n)} {fmt_money(a)}" for n, a in f.protected))

    if f.cycles:
        lines += ["", *explain_cycle(f, f.cycles[0])]

    rows = [("мес", "старт", "+зп", "−трат", "план", "=итог", "дно")]
    for c in f.cycles:
        rows.append((
            RU_MONTHS_GEN[c.end.month - 1], _t(c.opening), _t(c.salary) if c.salary else "—", _t(c.spending),
            (("+" if c.planned_total > 0 else "") + _t(c.planned_total)) if c.planned else "—",
            _t(c.closing), _t(c.low) + ("!" if c.low < 0 else ""),
        ))
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    table = "\n".join(
        " ".join(cell.ljust(widths[i]) if i == 0 else cell.rjust(widths[i]) for i, cell in enumerate(row)) for row in rows
    )
    lines += ["", "<b>Все месяцы</b>, тыс. RM", f"<pre>{escape(table)}</pre>",
              "<i>итог = старт + зп − траты ± план; итог месяца — старт следующего. "
              "Дно — сколько останется утром перед зарплатой.</i>"]

    negative = [c for c in f.cycles if c.low < 0]
    if negative:
        c = negative[0]
        lines.append(f"⚠️ {RU_MONTHS[c.end.month - 1]}: перед зарплатой не хватает {fmt_money(-c.low)}")

    plans = [(c, h) for c in f.cycles for h in c.planned]
    if plans:
        lines += ["", "<b>План</b>"]
        for c, h in plans:
            late = " (просрочено)" if h.overdue else ""
            lines.append(f"• {RU_MONTHS_GEN[c.end.month - 1]}: {fmt_money(h.amount, signed=True)} {escape(h.title)}, {fmt_day(h.day)}{late}")

    usual = sorted(f.baseline.items(), key=lambda kv: kv[1], reverse=True)
    if usual:
        top = ", ".join(f"{escape(f.categories.get(code, code))} {fmt_money(a)}" for code, a in usual[:4])
        lines += ["", f"Обычные траты — медиана за {len(f.baseline_cycles)} прошлых месяца: "
                      f"{fmt_money(sum(f.baseline.values(), ZERO))}; больше всего {top}…"]
    left_out = []
    if f.fx_per_cycle:
        left_out.append(f"переводы на РФ (обычно {fmt_money(f.fx_per_cycle)} в месяц)")
    if f.travel_per_cycle:
        left_out.append(f"поездки (в среднем {fmt_money(f.travel_per_cycle)})")
    if left_out:
        lines.append("Не в прогнозе, пока не заложены в план: " + ", ".join(left_out) + ".")
    for note in f.notes:
        if note != SALARY_MISSING:
            lines.append(f"ℹ️ {escape(note)}")
    lines += ["", "Заложить трату: <code>/plan 1800 отель 25.10</code>, спросить: /ask"]
    return "\n".join(lines)


def forecast_json(f: Forecast) -> dict:
    """For the dashboard and for Claude: plain numbers and ISO dates."""
    return {
        "today": f.today,
        "liquid": [{"account": n, "amount": a} for n, a in f.liquid],
        "liquid_total": f.liquid_total,
        "reserve_pots": [{"pot": n, "amount": a} for n, a in f.reserve],
        "protected_pots": [{"pot": n, "amount": a} for n, a in f.protected],
        "salary": f.salary,
        "salary_day_of_month": f.salary_day_of_month,
        "usual_spending_per_cycle": {f.categories.get(k, k): v for k, v in sorted(f.baseline.items(), key=lambda kv: -kv[1])},
        "usual_spending_total": sum(f.baseline.values(), ZERO),
        "share_spent_after_salary": {f.categories.get(k, k): v for k, v in f.late_share.items() if v},
        "rf_transfers_per_cycle_not_in_forecast": f.fx_per_cycle,
        "trips_per_cycle_not_in_forecast": f.travel_per_cycle,
        "past_cycles": [
            {
                "label": h["label"], "start": h["start"], "end": h["end"], "salary": h["salary"], "rf_transfers": h["fx"],
                "spending": {f.categories.get(k, k): v for k, v in sorted(h["spending"].items(), key=lambda kv: -kv[1])},
                "spending_total": sum(h["spending"].values(), ZERO),
            }
            for h in f.history
        ],
        "cycles": [
            {
                "label": c.label, "start": c.start, "end": c.end, "salary_day": c.salary_day,
                "opening": c.opening, "salary": c.salary, "usual_spending_left": c.spending, "spent_so_far": c.spent,
                "planned": [
                    {"id": h.plan_id, "title": h.title, "amount": h.amount, "kind": h.kind, "date": h.day, "overdue": h.overdue}
                    for h in c.planned
                ],
                "planned_total": c.planned_total, "closing": c.closing, "low_before_salary": c.low,
                "usual_spending_month": c.usual, "usual_spending_before_salary": c.before_salary,
                "planned_before_salary": c.planned_before,
            }
            for c in f.cycles
        ],
        "data_since": f.data_since,
        "notes": f.notes,
    }
