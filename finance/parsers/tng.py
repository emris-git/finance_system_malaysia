"""TNG eWallet transaction history PDF.

Line format (varies slightly between app versions):
    DD/M/YYYY  Success  TRANSACTION_TYPE  REFERENCE  Description  RMxx.xx  RMxx.xx

Since 2026-09 the export is titled "TNG WALLET TRANSACTION HISTORY" and every
column wraps, so one row spans several lines, from `DD/M/YYYY Success Type`
to `RMxx.xx RMxx.xx`; the reference is split into chunks of 9-11 characters
and a Details column (starting with the date as YYYYMMDD) follows the description.

The statement shows amounts without a sign, so the direction is recovered
from the running wallet balance; the transaction type is the fallback.
"""

from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal

from finance.parsers.base import ParsedStatement, ParsedTxn
from finance.utils import to_decimal

ROW_RE = re.compile(
    r"(\d{1,2}/\d{1,2}/\d{4})\s+"  # date
    r"(Success|Reversed|Failed)\s+"  # status
    r"(\S+(?:\s+\S+)?)\s+"  # transaction type (1-2 words)
    r"(\d{10,20})\s+"  # reference
    r"(.+?)\s+"  # description
    r"RM\s*([\d,.]+)\s+"  # amount
    r"RM\s*([\d,.]+)",  # wallet balance
    re.IGNORECASE,
)

INFLOW_HINT = re.compile(
    r"RELOAD|TOP.?UP|REFUND|RECEIVE|CASHBACK|REWARD|CASH ?OUT|TRANSFER FROM|FROM WALLET",
    re.IGNORECASE,
)

VOID_RE = re.compile(r"VOID|REVERSAL", re.IGNORECASE)

MARKERS = ("TOUCH 'N GO", "TOUCH N GO", "TNG DIGITAL", "TNG EWALLET", "TNG WALLET", "EWALLET")

# wrapped layout
ROW_START_RE = re.compile(r"^(\d{1,2}/\d{1,2}/\d{4})\s+(Success|Reversed|Failed)\b\s*(.*)$", re.IGNORECASE)
ROW_END_RE = re.compile(r"^RM\s*([\d,.]+)\s+RM\s*([\d,.]+)$", re.IGNORECASE)
REF_START_RE = re.compile(r"20\d{9}")  # YYYYMMDD + 3 digits, may be glued to the type
REF_CHUNK_RE = re.compile(r"^[0-9A-Z]+$")
DETAILS_RE = re.compile(r"(?:^|\s)20\d{10,}")
PAGE_NOISE_RE = re.compile(r"^(\*This is a system generated|\+60\d|Date Status Transaction Type)", re.IGNORECASE)


def looks_like_tng(text: str) -> bool:
    upper = text.upper()
    return any(m in upper for m in MARKERS)


def _parse_date(value: str):
    return datetime.strptime(value, "%d/%m/%Y").date()


def _rows_strict(text: str) -> list[dict]:
    rows = []
    for m in ROW_RE.finditer(text):
        date_str, status, raw_type, ref, desc, amount, balance = m.groups()
        try:
            day = _parse_date(date_str)
        except ValueError:
            continue
        rows.append(
            {
                "day": day,
                "status": status,
                "raw_type": raw_type.strip(),
                "ref": ref,
                "desc": desc.strip(" -"),
                "amount": to_decimal(amount),
                "balance": to_decimal(balance),
            }
        )
    return rows


def _rows_loose(text: str) -> list[dict]:
    """Fallback for layouts the strict regex misses: any line starting with a date."""
    rows = []
    for line in text.split("\n"):
        line = line.strip()
        dm = re.match(r"^(\d{1,2}/\d{1,2}/\d{4})\s+", line)
        if not dm:
            continue
        try:
            day = _parse_date(dm.group(1))
        except ValueError:
            continue
        amounts = re.findall(r"RM\s*([\d,.]+)", line, re.IGNORECASE)
        if not amounts:
            continue
        status = "Reversed" if re.search(r"Reversed|Failed", line, re.IGNORECASE) else "Success"
        ref_match = re.search(r"\b(\d{10,20})\b", line)
        desc = line[dm.end():]
        desc = re.sub(r"(Success|Reversed|Failed)", "", desc, flags=re.IGNORECASE)
        desc = re.sub(r"RM\s*[\d,.]+", "", desc)
        desc = re.sub(r"\b\d{10,20}\b", "", desc)
        desc = re.sub(r"\s+", " ", desc).strip(" -")
        rows.append(
            {
                "day": day,
                "status": status,
                "raw_type": "",
                "ref": ref_match.group(1) if ref_match else None,
                "desc": desc or "Unknown",
                "amount": to_decimal(amounts[0]),
                "balance": to_decimal(amounts[1]) if len(amounts) > 1 else None,
            }
        )
    return rows


def _wrapped_row(date_str: str, status: str, lines: list[str], amount: str, balance: str) -> dict | None:
    try:
        day = _parse_date(date_str)
    except ValueError:
        return None
    body = "\n".join(lines)
    ref_start = REF_START_RE.search(body)
    raw_type, ref, rest = "", None, lines
    if ref_start:
        # a wrapped type is split mid-word: CARDISSUANCE_ / PAYMENT
        raw_type = re.sub(r"\s*\n\s*", "", body[: ref_start.start()]).strip()
        rest = body[ref_start.start():].split("\n")
        chunks = []
        while rest and REF_CHUNK_RE.match(rest[0]):
            chunks.append(rest.pop(0))
        ref = "".join(chunks) or None
    text = " ".join(rest)
    details = DETAILS_RE.search(text)
    desc = re.sub(r"\s+", " ", text[: details.start()] if details else text).strip(" -")
    return {
        "day": day,
        "status": status,
        "raw_type": raw_type,
        "ref": ref,
        "desc": desc or raw_type or "Unknown",
        "amount": to_decimal(amount),
        "balance": to_decimal(balance),
    }


def _rows_wrapped(text: str) -> list[dict]:
    rows = []
    block = None
    for line in text.split("\n"):
        line = line.strip()
        start = ROW_START_RE.match(line)
        if start:
            block = (start.group(1), start.group(2), [start.group(3)])
            continue
        if block is None or not line or PAGE_NOISE_RE.match(line):
            continue
        end = ROW_END_RE.match(line)
        if end:
            row = _wrapped_row(*block, end.group(1), end.group(2))
            if row:
                rows.append(row)
            block = None
        else:
            block[2].append(line)
    return rows


def _matches(prev: dict, cur: dict) -> bool:
    if prev["balance"] is None or cur["balance"] is None:
        return False
    return abs(abs(cur["balance"] - prev["balance"]) - cur["amount"]) < Decimal("0.01")


def infer_signs(rows: list[dict]) -> tuple[list[int], list[int]]:
    """+1 / -1 per row (in the given order) from the balance chain, plus the
    row indexes in chronological order.

    Statements may list rows newest-first or oldest-first; whichever order
    explains more balance steps wins.
    """
    oldest_first = sum(_matches(p, c) for p, c in zip(rows, rows[1:]))
    newest_first = sum(_matches(c, p) for p, c in zip(rows, rows[1:]))
    chrono = list(range(len(rows)))
    if newest_first > oldest_first:
        chrono.reverse()

    signs = [0] * len(rows)
    for prev_i, cur_i in zip(chrono, chrono[1:]):
        prev, cur = rows[prev_i], rows[cur_i]
        if _matches(prev, cur) and cur["balance"] != prev["balance"]:
            signs[cur_i] = 1 if cur["balance"] > prev["balance"] else -1
    for i, row in enumerate(rows):
        if signs[i] == 0:
            hint = f"{row['raw_type']} {row['desc']}"
            signs[i] = 1 if INFLOW_HINT.search(hint) else -1
    return signs, chrono


def _pair_voids(rows: list[dict], signs: list[int], chrono: list[int]) -> None:
    """A reversed card payment still moves the balance and the money comes back
    as a separate VOID row. Reports skip reversed rows, so the VOID is marked
    reversed too; otherwise it would count as a refund of a payment never counted."""
    reversed_rows: list[dict] = []
    for i in chrono:
        row = rows[i]
        if row["status"].lower() != "success":
            reversed_rows.append(row)
        elif signs[i] > 0 and VOID_RE.search(f"{row['raw_type']} {row['desc']}"):
            original = next((r for r in reversed_rows if r["amount"] == row["amount"]), None)
            if original is not None:
                reversed_rows.remove(original)
                row["status"] = "Reversed"


def parse_tng_text(text: str) -> ParsedStatement:
    rows = max(_rows_strict(text), _rows_wrapped(text), key=len) or _rows_loose(text)
    signs, chrono = infer_signs(rows)
    _pair_voids(rows, signs, chrono)

    txns: list[ParsedTxn] = []
    seen = set()
    for i in chrono:
        row, sign = rows[i], signs[i]
        if row["amount"] == 0:
            continue
        key = (row["ref"], row["day"], row["amount"])
        if row["ref"] and key in seen:
            continue
        seen.add(key)
        txns.append(
            ParsedTxn(
                booked_on=row["day"],
                amount=row["amount"] * sign,
                description=row["desc"],
                raw_type=row["raw_type"] or None,
                external_ref=row["ref"],
                balance_after=row["balance"],
                reversed=row["status"].lower() != "success",
            )
        )
    # Stable sort: same-day rows keep their chronological order (latest balance last).
    txns.sort(key=lambda t: t.booked_on)
    return ParsedStatement(account_code="tng", source="tng_pdf", transactions=txns)
