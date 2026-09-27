"""Sample data for trying the dashboard and bot locally (`finance demo`). Refuses a non-empty DB."""

from __future__ import annotations

import random
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from finance import ledger
from finance.models import Transaction
from finance.parsers import ParsedStatement, ParsedTxn
from finance.utils import today

MERCHANTS = [
    ("GRAB RIDES KUALA LUMPUR", 8, 28, "tng"),
    ("GRABFOOD ORDER", 18, 55, "tng"),
    ("STARBUCKS PAVILION", 14, 22, "tng"),
    ("JAYA GROCER THE LINC", 40, 180, "maybank"),
    ("99 SPEEDMART", 6, 30, "tng"),
    ("FAMILYMART KLCC", 5, 18, "tng"),
    ("SHOPEE MALAYSIA", 20, 160, "maybank"),
    ("CLAUDE.AI SUBSCRIPTION", 90, 90, "maybank"),
    ("ANYTIME FITNESS", 150, 150, "maybank"),
    ("GSC PAVILION", 22, 45, "tng"),
    ("KEDAI RUNCIT AMINAH", 10, 40, "tng"),
    ("NASI KANDAR PELITA", 12, 30, "tng"),
]


async def fill_demo(session: AsyncSession, days: int = 330) -> str:
    if await session.scalar(select(func.count(Transaction.id))):
        return "DB is not empty — demo data not added"
    rng = random.Random(7)
    start = today() - timedelta(days=days)
    maybank: list[ParsedTxn] = []
    tng: list[ParsedTxn] = []

    day = start
    while day <= today():
        if day.day == 25:
            maybank.append(ParsedTxn(day, Decimal("8000.00"), "IBG CREDIT ACME TECH SDN BHD SALARY"))
        if day.day == 1:
            maybank.append(ParsedTxn(day, Decimal("-3200.00"), "DUITNOW TRANSFER TO SPEEDHOME RENT"))
            maybank.append(ParsedTxn(day, Decimal("-89.00"), "CELCOMDIGI POSTPAID"))
        if day.weekday() == 0:
            amount = Decimal(rng.choice([200, 300, 400]))
            maybank.append(ParsedTxn(day, -amount, "DUITNOW TO TNG DIGITAL SDN BHD"))
            tng.append(ParsedTxn(day, amount, "Maybank2u FPX", raw_type="Reload"))
        if day.day in (5, 20) and rng.random() < 0.8:
            maybank.append(ParsedTxn(day, Decimal(rng.choice([1000, 1500, 2000])) * -1, "DUITNOW TRANSFER TO IVAN PETROV"))
        for _ in range(rng.randint(1, 4)):
            name, lo, hi, account = rng.choice(MERCHANTS)
            value = Decimal(str(round(rng.uniform(lo, hi), 2)))
            (tng if account == "tng" else maybank).append(ParsedTxn(day, -value, name, raw_type="Payment"))
        day += timedelta(days=1)

    await ledger.import_statement(session, ParsedStatement("maybank", "maybank_csv", maybank), origin="demo")
    await ledger.import_statement(session, ParsedStatement("tng", "tng_pdf", tng), origin="demo")

    # Mark most RF transfers as done so the RUB side has data; leave the recent ones for /review.
    queue = await ledger.review_queue(session, limit=500)
    for txn in queue:
        if "IVAN PETROV" in txn.description and txn.booked_on < today() - timedelta(days=20):
            rate = Decimal(str(round(rng.uniform(19.5, 22.5), 2)))
            await ledger.mark_fx(session, txn, (-txn.amount * rate).quantize(Decimal("1")))
    for i in range(60):
        d = today() - timedelta(days=rng.randint(0, days))
        text, lo, hi = rng.choice([("такси", 300, 900), ("кафе", 800, 3000), ("продукты", 1500, 6000), ("подарок маме", 2000, 8000)])
        await ledger.add_manual(session, "ru", Decimal(-rng.randint(lo, hi)), text, d)
    return f"demo: {len(maybank)} Maybank rows, {len(tng)} TNG rows, RUB entries added"
