"""Money formatting and date ranges."""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from finance.config import get_settings

CURRENCY_SYMBOLS = {"MYR": "RM", "RUB": "₽"}

RU_MONTHS_GEN = [
    "янв", "фев", "мар", "апр", "мая", "июн", "июл", "авг", "сен", "окт", "ноя", "дек",
]
RU_MONTHS = [
    "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
    "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь",
]


def to_decimal(value: str | float | int | Decimal) -> Decimal:
    """'RM1,234.50' -> Decimal('1234.50')."""
    if isinstance(value, Decimal):
        return value.quantize(Decimal("0.01"), ROUND_HALF_UP)
    if isinstance(value, (int, float)):
        return Decimal(str(value)).quantize(Decimal("0.01"), ROUND_HALF_UP)
    cleaned = re.sub(r"[^\d.\-]", "", value.replace(",", ""))
    try:
        return Decimal(cleaned).quantize(Decimal("0.01"), ROUND_HALF_UP)
    except InvalidOperation as exc:
        raise ValueError(f"not an amount: {value!r}") from exc


def parse_amount(text: str) -> Decimal:
    """Amount as a phone formats it: "RM12.50", "12,50 RM", "1 234,5", "1,234.50"."""
    cleaned = re.sub(r"[^\d.,\-]", "", text.replace("\u00a0", " "))
    if "," in cleaned and "." in cleaned:
        decimal_sep = "," if cleaned.rfind(",") > cleaned.rfind(".") else "."
        thousands = "." if decimal_sep == "," else ","
        cleaned = cleaned.replace(thousands, "").replace(decimal_sep, ".")
    elif "," in cleaned:
        head, _, tail = cleaned.rpartition(",")
        cleaned = f"{head.replace(',', '')}.{tail}" if len(tail) in (1, 2) else cleaned.replace(",", "")
    return to_decimal(cleaned)


def fmt_money(amount: Decimal | float | int, currency: str = "MYR", signed: bool = False) -> str:
    """RM 1,234.50 / ₽ 12 345 / BTC 0.00061. RUB is shown without kopecks, coins with all their places."""
    amount = Decimal(amount)
    sign = ""
    if amount < 0:
        sign = "−"
    elif signed and amount > 0:
        sign = "+"
    value = abs(amount)
    if currency == "RUB":
        body = f"{value:,.0f}".replace(",", " ")
    elif currency in CURRENCY_SYMBOLS:
        body = f"{value:,.2f}"
    else:  # a coin: up to 8 places, at least 2
        body = f"{value:,.8f}".rstrip("0")
        if len(body.partition(".")[2]) < 2:
            body = f"{value:,.2f}"
    return f"{sign}{CURRENCY_SYMBOLS.get(currency, currency)} {body}"


def today() -> date:
    return datetime.now(get_settings().tz).date()


def week_bounds(day: date) -> tuple[date, date]:
    """Monday..Sunday containing `day`."""
    start = day - timedelta(days=day.weekday())
    return start, start + timedelta(days=6)


def month_bounds(day: date) -> tuple[date, date]:
    start = day.replace(day=1)
    next_month = (start + timedelta(days=32)).replace(day=1)
    return start, next_month - timedelta(days=1)


def cycle_bounds(day: date, start_day: int | None = None) -> tuple[date, date]:
    """Financial month containing `day`: start_day .. (start_day - 1) of the next month."""
    start_day = start_day or get_settings().MONTH_START_DAY
    if day.day >= start_day:
        start = day.replace(day=start_day)
    else:
        start = (day.replace(day=1) - timedelta(days=1)).replace(day=start_day)
    next_start = (start.replace(day=1) + timedelta(days=32)).replace(day=start_day)
    return start, next_start - timedelta(days=1)


def cycle_ending_in(year: int, month: int, start_day: int | None = None) -> tuple[date, date]:
    """The financial month whose last day falls in year-month (26 Jul - 25 Aug for August)."""
    start_day = start_day or get_settings().MONTH_START_DAY
    return cycle_bounds(date(year, month, start_day - 1 if start_day > 1 else 1), start_day)


def fmt_day(d: date) -> str:
    return f"{d.day} {RU_MONTHS_GEN[d.month - 1]}"


def fmt_range(start: date, end: date) -> str:
    if start.month == end.month:
        return f"{start.day}–{end.day} {RU_MONTHS_GEN[end.month - 1]}"
    return f"{fmt_day(start)} – {fmt_day(end)}"


def pct_change(current: Decimal, baseline: Decimal) -> int | None:
    if baseline <= 0:
        return None
    return int(((current - baseline) / baseline * 100).to_integral_value(ROUND_HALF_UP))
