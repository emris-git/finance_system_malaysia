"""Parse free-text entries typed into the bot: "1500₽ такси", "25 rm обед вчера", "+5000 р кэшбэк",
"1500₽ 24.09 такси #транспорт", "15 usdt кофе"."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

from finance.utils import to_decimal, today

RUB_WORDS = r"₽|р\.?|руб\.?|рублей|rub|rur"
MYR_WORDS = r"rm|myr|ринг\w*"

ENTRY_RE = re.compile(
    rf"""^\s*
    (?P<sign>[+-])?\s*
    (?P<pre>{RUB_WORDS}|{MYR_WORDS})?\s*
    (?P<num>\d{{1,3}}(?:[  ]\d{{3}})+(?:[.,]\d{{1,2}})?|\d+(?:[.,]\d{{1,2}})?)
    \s*(?P<post>(?:{RUB_WORDS}|{MYR_WORDS})(?![\wа-яё]))?
    \s*(?P<desc>.*?)\s*$""",
    re.IGNORECASE | re.VERBOSE,
)

# Coins taken as a currency in "15 usdt кофе" even before their wallet exists;
# the bot adds the tickers of the wallets that do.
KNOWN_COINS = ("USDT", "USDC", "BTC", "ETH", "TON", "SOL")

DAY_WORDS = {"позавчера": 2, "вчера": 1, "сегодня": 0}
# DD.MM with a two-digit month, so "7.5 кг" stays a quantity
DATE_RE = re.compile(r"(?<![\d.,/])(\d{1,2})[./](\d{2})(?:[./](\d{4}|\d{2}))?(?![\d.,/])")
TAG_RE = re.compile(r"(?<!\S)#([\wА-Яа-яЁё-]+)")


@dataclass
class Entry:
    amount: Decimal  # signed: negative = expense
    currency: str  # RUB / MYR / a coin ticker
    description: str
    day: date
    explicit_currency: bool
    category: str | None = None  # "#транспорт" -> "транспорт", resolved against the categories by the bot


def _coin_entry_re(coins: Iterable[str]) -> re.Pattern | None:
    tickers = sorted({c.upper() for c in coins}, key=len, reverse=True)
    if not tickers:
        return None
    alt = "|".join(map(re.escape, tickers))
    return re.compile(
        rf"""^\s*
        (?P<sign>[+-])?\s*
        (?:(?P<pre>{alt})\s*)?
        (?P<num>\d{{1,3}}(?:[  ]\d{{3}})+(?:[.,]\d{{1,8}})?|\d+(?:[.,]\d{{1,8}})?)
        \s*(?P<post>(?:{alt})(?![\wа-яё]))?
        \s*(?P<desc>.*?)\s*$""",
        re.IGNORECASE | re.VERBOSE,
    )


def parse_entry(text: str, coins: Iterable[str] = ()) -> Entry | None:
    """`coins`: tickers read as a currency ("15 usdt кофе", "usdt 0,5 кофе"); amounts keep 8 places."""
    coin_re = _coin_entry_re(coins)
    m = coin_re.match(text) if coin_re else None
    if m and (m.group("pre") or m.group("post")):
        if not m.group("desc"):
            return None
        currency = (m.group("pre") or m.group("post")).upper()
        value = _coin_number(m.group("num"))
        explicit = True
    else:
        m = ENTRY_RE.match(text)
        if not m or not m.group("desc"):
            return None
        marker = (m.group("pre") or m.group("post") or "").lower()
        currency = "MYR" if marker and re.fullmatch(MYR_WORDS, marker, re.IGNORECASE) else "RUB"
        value = to_decimal(m.group("num").replace(" ", "").replace(" ", "").replace(",", "."))
        explicit = bool(marker)
    if not value:
        return None

    description = m.group("desc")
    category = None
    tag = TAG_RE.search(description)
    if tag:
        category = tag.group(1).lower()
        description = _cut(description, tag)
    day = today()
    dated = _explicit_day(description)
    if dated:
        day, description = dated
    else:
        for word, back in DAY_WORDS.items():
            if re.search(rf"\b{word}\b", description, re.IGNORECASE):
                day -= timedelta(days=back)
                description = re.sub(rf"\s*\b{word}\b\s*", " ", description, flags=re.IGNORECASE).strip()
                break
    amount = value if m.group("sign") == "+" else -value
    return Entry(amount, currency, description or "Без описания", day, explicit, category)


def _cut(text: str, m: re.Match) -> str:
    return re.sub(r"\s+", " ", text[: m.start()] + " " + text[m.end():]).strip()


def _explicit_day(description: str) -> tuple[date, str] | None:
    """"24.09" / "24.09.2026" / "24/09/26"; a date without a year that would be
    in the future is last year's (typed "30.12" on 2 January)."""
    current = today()
    for m in DATE_RE.finditer(description):
        day, month, year = m.groups()
        years = [int(year) + (2000 if len(year) == 2 else 0)] if year else [current.year, current.year - 1]
        for year_n in years:
            try:
                found = date(year_n, int(month), int(day))
            except ValueError:
                continue
            if year or found <= current:
                return found, _cut(description, m)
    return None


def parse_rf_command(args: str) -> tuple[Decimal, Decimal, str] | None:
    """`/rf 1000 21500 [tng]` -> (myr, rub, account)."""
    parts = args.replace(",", ".").split()
    if len(parts) < 2:
        return None
    try:
        myr, rub = to_decimal(parts[0]), to_decimal(parts[1])
    except ValueError:
        return None
    account = "tng" if len(parts) > 2 and parts[2].lower() in ("tng", "тнг") else "maybank"
    if myr <= 0 or rub <= 0:
        return None
    return myr, rub, account


CRYPTO_AMOUNT_RE = re.compile(
    r"^(?:(?P<pre>[A-Za-z][A-Za-z0-9]{1,9})\s*)?(?P<num>\d[\d\s.,]*)(?:\s*(?P<post>[A-Za-z][A-Za-z0-9]{1,9}))?$"
)


def parse_crypto_amount(text: str) -> tuple[Decimal, str | None] | None:
    """`60 USDT`, `0,00061 btc`, `USDT 60`, `60` -> (amount, ticker or None).

    Coins are not rounded to cents. A lone comma is the decimal point unless
    it groups thousands ("1,500").
    """
    m = CRYPTO_AMOUNT_RE.match(text.strip())
    if not m or (m["pre"] and m["post"]):
        return None
    amount = _coin_number(m["num"])
    if amount is None:
        return None
    ticker = m["pre"] or m["post"]
    return amount, ticker.upper() if ticker else None


def _coin_number(text: str) -> Decimal | None:
    """"0,00061" -> 0.00061, "1,500" -> 1500, "1 500.25" -> 1500.25; not rounded to cents."""
    number = re.sub(r"\s", "", text)
    if "," in number and "." in number:
        number = number.replace(",", "")
    elif "," in number:
        head, _, tail = number.rpartition(",")
        thousands = len(tail) == 3 and head.replace(",", "") not in ("", "0")
        number = number.replace(",", "") if thousands else f"{head.replace(',', '')}.{tail}"
    try:
        return Decimal(number)
    except InvalidOperation:
        return None


EVENT_KEYS = {
    "amount": "amount", "сумма": "amount",
    "merchant": "merchant", "продавец": "merchant", "магазин": "merchant",
    "card": "card", "карта": "card", "карта или пропуск": "card", "pass": "card",
    "account": "account", "счёт": "account", "счет": "account",
    "income": "income",
    "occurred_at": "occurred_at", "date": "occurred_at", "дата": "occurred_at",
}


def parse_device_event(raw: str | dict) -> dict:
    """A Wallet transaction from the iPhone automation: `finance event --stdin` or POST /api/events.

    The Shortcut sends a Dictionary, which arrives as JSON (amounts may be numbers);
    "key: value" lines and form fields (a dict) work too. Keys may be English or
    Russian, any case (amount/сумма, merchant/продавец, card/карта).
    """
    if isinstance(raw, dict):
        data = raw
    else:
        raw = raw.strip()
        try:
            data = json.loads(raw)
        except ValueError:
            data = None
    if isinstance(data, dict):
        items = list(data.items())
    else:
        items = []
        for line in raw.splitlines():
            m = re.match(r"\s*([^:=]+?)\s*[:=]\s*(.*)$", line)
            if m:
                items.append((m.group(1), m.group(2)))

    fields: dict = {}
    for key, value in items:
        name = EVENT_KEYS.get(str(key).strip().lower())
        if name and value not in (None, ""):
            fields[name] = value
    if "amount" not in fields:
        raise ValueError("нет суммы (amount / сумма)")
    return {
        "amount": str(fields["amount"]),
        "merchant": str(fields.get("merchant", "")).strip(),
        "card": str(fields["card"]) if "card" in fields else None,
        "account": str(fields["account"]) if "account" in fields else None,
        "income": str(fields.get("income", "")).lower() in ("true", "1", "yes", "да"),
        "occurred_at": _event_time(fields.get("occurred_at")),
    }


def _event_time(value) -> datetime | None:
    """ISO time of the payment; anything else is dropped: the event is live, so today is right anyway."""
    try:
        return datetime.fromisoformat(str(value).strip()) if value else None
    except ValueError:
        return None
