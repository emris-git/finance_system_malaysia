"""Maybank statements: CSV export and PDF e-statement.

CSV columns vary (Date, Description, Debit, Credit, Balance or a single
signed Amount); columns are detected from the header row.

The PDF parser follows the Maybank savings/current account e-statement
layout: `DD/MM/YY DESCRIPTION 1,234.56- 9,876.54`, where the trailing sign
is the direction and following lines continue the description. It has only
been checked against synthetic samples; verify it on a real statement.
"""

from __future__ import annotations

import csv
import io
import re
from datetime import date, datetime

from finance.parsers.base import ParseError, ParsedStatement, ParsedTxn
from finance.utils import to_decimal

DATE_FORMATS = ("%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d", "%d %b %Y", "%d/%m/%y", "%d-%m-%y", "%Y%m%d")


def _detect_date_format(value: str) -> str | None:
    for fmt in DATE_FORMATS:
        try:
            datetime.strptime(value.strip(), fmt)
            return fmt
        except ValueError:
            continue
    return None


def _is_debit_header(h: str) -> bool:
    return "debit" in h or re.search(r"\bdr\b", h) is not None or "withdrawal" in h


def _is_credit_header(h: str) -> bool:
    # Word match: "description" contains "cr" and must not be taken for Credit.
    return "credit" in h or re.search(r"\bcr\b", h) is not None or "deposit" in h


def _has_digits(value: str) -> bool:
    return bool(re.search(r"\d", value))


def parse_maybank_csv(data: str, account_code: str = "maybank") -> ParsedStatement:
    sample = data[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    try:
        rows = list(csv.reader(io.StringIO(data), dialect))
    except csv.Error as exc:
        raise ParseError(f"не читается как CSV: {exc}") from exc
    statement = ParsedStatement(account_code=account_code, source="maybank_csv")
    if not rows:
        return statement

    header_idx = next(
        (i for i, row in enumerate(rows[:15]) if any("date" in c.lower() for c in row)), 0
    )
    headers = [h.strip().lower() for h in rows[header_idx]]
    data_rows = rows[header_idx + 1:]

    date_col = next((i for i, h in enumerate(headers) if "date" in h), 0)
    desc_col = next(
        (i for i, h in enumerate(headers) if any(k in h for k in ("desc", "particular", "narrative", "detail"))),
        1,
    )
    debit_col = next((i for i, h in enumerate(headers) if i != desc_col and _is_debit_header(h)), None)
    credit_col = next((i for i, h in enumerate(headers) if i != desc_col and _is_credit_header(h)), None)
    amount_col = next(
        (i for i, h in enumerate(headers) if "amount" in h and not _is_debit_header(h) and not _is_credit_header(h)),
        None,
    )
    balance_col = next((i for i, h in enumerate(headers) if "balance" in h), None)
    ref_col = next((i for i, h in enumerate(headers) if "ref" in h), None)

    date_fmt = None
    for row in data_rows[:10]:
        if len(row) > date_col and row[date_col].strip():
            date_fmt = _detect_date_format(row[date_col])
            if date_fmt:
                break
    date_fmt = date_fmt or "%d/%m/%Y"

    def cell(row: list[str], col: int | None) -> str:
        return row[col].strip() if col is not None and len(row) > col else ""

    for row in data_rows:
        try:
            day = datetime.strptime(cell(row, date_col), date_fmt).date()
        except ValueError:
            continue
        description = cell(row, desc_col)
        if not description:
            continue

        amount = None
        if debit_col is not None and credit_col is not None:
            debit, credit = cell(row, debit_col), cell(row, credit_col)
            if _has_digits(debit) and to_decimal(debit) != 0:
                amount = -abs(to_decimal(debit))
            elif _has_digits(credit) and to_decimal(credit) != 0:
                amount = abs(to_decimal(credit))
        elif amount_col is not None:
            raw = cell(row, amount_col)
            if _has_digits(raw):
                value = abs(to_decimal(raw))
                negative = "-" in raw or raw.upper().endswith("DR")
                amount = -value if negative else value
        if not amount:
            continue

        balance = cell(row, balance_col)
        statement.transactions.append(
            ParsedTxn(
                booked_on=day,
                amount=amount,
                description=description,
                external_ref=cell(row, ref_col) or None,
                balance_after=to_decimal(balance) if _has_digits(balance) else None,
            )
        )
    return statement


# --- PDF e-statement ---------------------------------------------------------

PDF_ROW_RE = re.compile(
    r"^(\d{2}/\d{2}(?:/\d{2,4})?)\s+(.*?)\s+([\d,]+\.\d{2})\s*([+-])\s+([\d,]+\.\d{2})\s*(?:DR)?\s*$"
)
STATEMENT_DATE_RE = re.compile(
    r"(?:STATEMENT DATE|TARIKH PENYATA)\s*:?\s*(\d{2}/\d{2}/\d{2,4})", re.IGNORECASE
)
NOISE_RE = re.compile(
    r"BALANCE|BAKI|PAGE|MUKA|ENTRY DATE|TARIKH|URUSNIAGA|MALAYAN BANKING|PIDM|STATEMENT|"
    r"PENYATA|TOTAL|JUMLAH|ACCOUNT|AKAUN|^\s*$|"
    r"^PERHAT\w*\s*/\s*NOTE",  # the notes at the foot of every page ("Perhation / Note")
    re.IGNORECASE,
)
# The statement's closing lines: the row above them does not continue on the next page.
ENDING_RE = re.compile(r"ENDING BALANCE|TOTAL (?:DEBIT|CREDIT)", re.IGNORECASE)
MARKERS = ("MALAYAN BANKING", "MAYBANK")
# Description lines under a row: name, reference, then e.g. "FUND Holiday" for a Tabung pot.
MAX_CONTINUATION = 4
PAGE_BREAK = "\f"


def looks_like_maybank(text: str) -> bool:
    upper = text.upper()
    return any(m in upper for m in MARKERS)


def _pdf_date(value: str, statement_date: date | None) -> date:
    parts = value.split("/")
    if len(parts) == 3:
        fmt = "%d/%m/%y" if len(parts[2]) == 2 else "%d/%m/%Y"
        return datetime.strptime(value, fmt).date()
    ref = statement_date or date.today()
    day = datetime.strptime(f"{value}/{ref.year}", "%d/%m/%Y").date()
    if day > ref:  # December rows on a January statement
        day = day.replace(year=ref.year - 1)
    return day


def parse_maybank_pdf_text(text: str, account_code: str = "maybank") -> ParsedStatement:
    """Rows of the e-statement text, pages separated by PAGE_BREAK.

    The last row on a page may continue on the next one, below the repeated
    page and table headers: "TRANSFER FROM A/C" at the foot of one page,
    "ALEX MORGAN * / 00000001 / BOOSTER Holiday" at the top of the
    next. The lines between the last header line and the first row of a page
    end the previous page's last row.
    """
    statement = ParsedStatement(account_code=account_code, source="maybank_pdf")
    sd = STATEMENT_DATE_RE.search(text)
    statement_date = _pdf_date(sd.group(1), None) if sd else None

    current: ParsedTxn | None = None
    used = 0  # description lines added to `current`
    open_ = False  # the next line may still continue `current`
    ended = False  # the statement's closing lines came after `current`
    carried: list[str] | None = None  # lines on a new page that may end `current`

    def take_carried() -> None:
        if current is not None and carried:
            current.description = " ".join([current.description, *carried[-(MAX_CONTINUATION - used):]])

    for page in text.split(PAGE_BREAK):
        carried = [] if current is not None and not ended and used < MAX_CONTINUATION else None
        open_ = False
        for line in page.split("\n"):
            line = line.strip()
            m = PDF_ROW_RE.match(line)
            if m:
                date_str, desc, amount, sign, balance = m.groups()
                try:
                    day = _pdf_date(date_str, statement_date)
                except ValueError:
                    continue
                take_carried()
                value = to_decimal(amount)
                current = ParsedTxn(
                    booked_on=day,
                    amount=-value if sign == "-" else value,
                    description=desc.strip(),
                    balance_after=to_decimal(balance),
                )
                statement.transactions.append(current)
                used, open_, ended, carried = 0, True, False, None
            elif ENDING_RE.search(line):
                take_carried()
                open_, ended, carried = False, True, None
            elif NOISE_RE.search(line):
                open_ = False
                if carried is not None:
                    carried.clear()  # page or table header: the continuation comes after it
            elif carried is not None:
                carried.append(line)
            elif open_ and used < MAX_CONTINUATION:
                current.description = f"{current.description} {line}".strip()
                used += 1
    return statement
