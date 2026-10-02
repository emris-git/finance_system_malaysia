import argparse
import io
import json
import sys
from datetime import date, timedelta
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select

from finance import cli, jobs, ledger, reports
from finance.models import PENDING, Transaction
from finance.parsers import ParsedStatement, ParsedTxn
from finance.quick_entry import parse_device_event
from finance.utils import cycle_bounds, cycle_ending_in, parse_amount, today
from finance.web.app import app


def test_cycle_bounds_payday_25():
    assert cycle_bounds(date(2026, 9, 26), 26) == (date(2026, 9, 26), date(2026, 10, 25))
    assert cycle_bounds(date(2026, 9, 25), 26) == (date(2026, 8, 26), date(2026, 9, 25))
    assert cycle_bounds(date(2026, 1, 10), 26) == (date(2025, 12, 26), date(2026, 1, 25))
    assert cycle_bounds(date(2026, 2, 14), 1) == (date(2026, 2, 1), date(2026, 2, 28))
    assert cycle_ending_in(2026, 8, 26) == (date(2026, 7, 26), date(2026, 8, 25))
    assert cycle_ending_in(2026, 2, 1) == (date(2026, 2, 1), date(2026, 2, 28))


def test_parse_amount_formats():
    assert parse_amount("12,50 RM") == Decimal("12.50")
    assert parse_amount("RM 1,234.50") == Decimal("1234.50")


def mb(*rows):
    return ParsedStatement("maybank", "maybank_csv", [ParsedTxn(d, Decimal(a), t) for d, a, t in rows])


async def test_month_report_after_payday(session, monkeypatch):
    start, end = cycle_bounds(today() - timedelta(days=40))  # a finished month
    await ledger.import_statement(
        session, mb((start, "-50", "GRAB FOOD"), (end, "8000", "IBG CREDIT ACME TECH SALARY")), origin="t"
    )
    text = await reports.month_report(session, start)
    assert "Месяц" in text and "Зарплата: RM 8,000.00" in text and "Остаток месяца" in text

    # the scheduled run on the 26th reports the month that ended yesterday, once
    sent = []

    async def fake_notify(t):
        sent.append(t)

    monkeypatch.setattr(jobs, "notify_owner", fake_notify)
    monkeypatch.setattr(jobs, "today", lambda: end + timedelta(days=1))
    assert await jobs.send_monthly_report() is True
    assert await jobs.send_monthly_report() is False
    assert "Зарплата" in sent[0]


async def test_month_without_salary_nudges(session):
    start, _ = cycle_bounds(today() - timedelta(days=40))
    await ledger.import_statement(session, mb((start, "-50", "GRAB FOOD")), origin="t")
    assert "Зарплаты за этот месяц не видно" in await reports.month_report(session, start)


async def test_monthly_series_groups_by_financial_month(session):
    start, end = cycle_bounds(today())
    prev_start, prev_end = cycle_bounds(start - timedelta(days=1))
    await ledger.import_statement(
        session, mb((start, "-10", "GRAB FOOD"), (prev_end, "-5", "GRAB FOOD")), origin="t"
    )
    *_, prev, current = await reports.monthly_series(session, "MYR", months=3)
    assert (current["start"], current["month"]) == (start, end.strftime("%Y-%m"))
    assert current["expense"] == Decimal("10") and prev["expense"] == Decimal("5")


async def test_monthly_series_converts_rub_expenses_at_monthly_rate(session):
    start, _ = cycle_bounds(today())
    prev_start, prev_end = cycle_bounds(start - timedelta(days=1))
    # previous month: 100 RM -> 2000 RUB (20); current month has no RF transfer, so it reuses that rate
    await ledger.record_fx(session, Decimal("100"), Decimal("2000"), day=prev_start)
    await ledger.add_manual(session, "ru", Decimal("-400"), "кофе", day=prev_end, category_code="food_out")
    await ledger.add_manual(session, "ru", Decimal("-200"), "кофе", day=start, category_code="food_out")
    *_, prev, current = await reports.monthly_series(session, "MYR", months=3)
    assert prev["expense"] == Decimal("20") and prev["rub_rate"] == Decimal("20")
    assert current["expense"] == Decimal("10")
    # the RUB-only view stays in rubles
    *_, rub_current = await reports.monthly_series(session, "RUB", months=3)
    assert rub_current["expense"] == Decimal("200")


async def test_cli_event_from_stdin(session, capsys, monkeypatch):
    # what the Shortcut pipes in: a Dictionary arrives as JSON; the apostrophe never touches the shell
    payload = json.dumps({"amount": "12,50 RM", "merchant": "McDonald's KLCC", "card": "Maybank Visa"})
    monkeypatch.setattr(sys, "stdin", io.StringIO(payload))
    await cli._event(argparse.Namespace(stdin=True, api=None))
    out = capsys.readouterr().out
    assert "RM 12.50" in out and "McDonald's KLCC" in out and "Еда вне дома" in out
    txn = await session.scalar(select(Transaction).where(Transaction.description == "McDonald's KLCC"))
    assert txn.status == PENDING and txn.amount == Decimal("-12.50") and txn.account.code == "maybank"


def test_parse_device_event_variants():
    assert parse_device_event('{"Сумма": 8.9, "Продавец": "ZUS", "Карта": "TNG eWallet"}') == {
        "amount": "8.9", "merchant": "ZUS", "card": "TNG eWallet", "account": None, "income": False,
        "occurred_at": None,
    }
    lines = parse_device_event("amount: RM 15.00\nmerchant: Café: Pavilion\ncard=Maybank")
    assert (lines["amount"], lines["merchant"], lines["card"]) == ("RM 15.00", "Café: Pavilion", "Maybank")
    with pytest.raises(ValueError):
        parse_device_event("Transaction")


async def test_api_event_with_card(session):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.post(
            "/api/events", headers={"Authorization": "Bearer test-token"},
            json={"amount": "8,90 RM", "merchant": "ZUS COFFEE", "card": "TNG eWallet Visa"},
        )
    assert r.status_code == 200, r.text
    assert r.json()["account"] == "tng" and r.json()["amount"] == -8.9


@pytest.mark.parametrize("request_kwargs", [
    {"json": {"amount": 12.5, "merchant": "ZUS COFFEE"}},  # Shortcuts sends a Number field as a JSON number
    {"json": {"Сумма": "RM12.50", "Продавец": "ZUS COFFEE", "Карта или пропуск": "Maybank Visa"}},
    {"json": {"Amount": "RM 12.50", "Merchant": "ZUS COFFEE", "occurred_at": "27 Sep 2026 at 11:45"}},
    {"data": {"amount": "12,50 RM", "merchant": "ZUS COFFEE"}},  # "Form" request body
    {"content": "amount: RM12.50\nmerchant: ZUS COFFEE"},  # "File" body with plain text
])
async def test_api_event_accepts_shortcut_bodies(session, request_kwargs):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.post("/api/events", headers={"Authorization": "Bearer test-token"}, **request_kwargs)
    assert r.status_code == 200, r.text
    assert r.json()["amount"] == -12.5 and r.json()["category"] == "food"


async def test_api_event_rejected_body_reaches_the_bot(session, monkeypatch):
    import finance.bot
    from finance.web import app as web_app

    sent = []

    async def fake_notify(text, reply_markup=None):
        sent.append(text)

    monkeypatch.setattr(finance.bot, "notify_owner", fake_notify)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.post("/api/events", headers={"Authorization": "Bearer test-token"}, json={"Transaction": "ZUS"})
    assert r.status_code == 422 and "сумм" in r.json()["detail"]
    for task in list(web_app._background):
        await task
    assert sent and "не записалась" in sent[0] and "Transaction" in sent[0]
