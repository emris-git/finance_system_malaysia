"""Statement parsers. `parse_file` picks the right one from the file contents."""

from __future__ import annotations

import io

from pypdf import PdfReader
from pypdf.errors import PdfReadError

from finance.parsers.base import ParseError, ParsedStatement, ParsedTxn, dedup_keys
from finance.parsers.maybank import (
    PAGE_BREAK,
    looks_like_maybank,
    parse_maybank_csv,
    parse_maybank_pdf_text,
)
from finance.parsers.tng import looks_like_tng, parse_tng_text

__all__ = [
    "MAX_FILE_BYTES",
    "ParseError",
    "ParsedStatement",
    "ParsedTxn",
    "dedup_keys",
    "parse_file",
    "pdf_pages",
]

# Upload cap for the bot and the API; real statements are well under 1 MB.
MAX_FILE_BYTES = 15 * 1024 * 1024


def pdf_pages(data: bytes, passwords: list[str]) -> list[str]:
    try:
        reader = PdfReader(io.BytesIO(data))
    except PdfReadError as exc:
        raise ParseError(f"не читается как PDF: {exc}") from exc
    if reader.is_encrypted:
        for password in [*passwords, ""]:
            if reader.decrypt(password):
                break
        else:
            raise ParseError("PDF защищён паролем, ни один из настроенных паролей не подошёл")
    return [page.extract_text() or "" for page in reader.pages]


def parse_file(
    data: bytes,
    filename: str = "",
    passwords: list[str] | None = None,
    account_code: str | None = None,
) -> ParsedStatement:
    """Parse a TNG PDF, Maybank PDF or bank CSV.

    `account_code` forces the target account (e.g. a CSV from another bank).
    """
    name = filename.lower()
    if data[:5] == b"%PDF-" or name.endswith(".pdf"):
        pages = pdf_pages(data, passwords or [])
        text = "\n".join(pages)
        # Maybank statements mention "TNG DIGITAL" in top-up rows, so the bank's
        # legal name is checked first; TNG statements only mention "Maybank".
        if "MALAYAN BANKING" in text.upper():
            statement = parse_maybank_pdf_text(PAGE_BREAK.join(pages))
        elif looks_like_tng(text):
            statement = parse_tng_text(text)
        elif looks_like_maybank(text):
            statement = parse_maybank_pdf_text(PAGE_BREAK.join(pages))
        else:
            raise ParseError("не понял, чей это PDF: ни TNG, ни Maybank")
    else:
        try:
            decoded = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            decoded = data.decode("latin-1")
        statement = parse_maybank_csv(decoded)

    if account_code:
        statement.account_code = account_code
    if not statement.transactions:
        raise ParseError("в файле не нашлось ни одной транзакции")
    return statement
