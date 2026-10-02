import asyncio
from datetime import date, datetime, timedelta
from decimal import Decimal
from urllib.parse import urlparse

import httpx
import pytest
from aiogram import Bot

import finance.bot as bot_module
import finance.web.app as app_module
from finance.config import get_settings
from finance.quick_entry import parse_entry, parse_rf_command, parse_rm_command
from finance.utils import today
from finance.web.app import app
from finance.web.auth import magic_link
from test_bot import FakeSession

TOKEN = {"Authorization": "Bearer test-token"}


@pytest.fixture
async def client(session):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_dashboard_needs_login(client):
    assert (await client.get("/api/summary")).status_code == 401
    page = await client.get("/")
    assert "/web" in page.text  # login page points to the bot


async def test_magic_link_sets_session(client):
    link = urlparse(magic_link())
    r = await client.get(f"{link.path}?{link.query}")
    assert r.status_code == 303
    assert (await client.get("/api/summary")).status_code == 200
    assert "Финансы" in (await client.get("/")).text
    bad = await client.get("/auth/magic?token=forged")
    assert bad.headers["location"].startswith("/?expired")


async def test_upload_then_patch_category(client):
    csv = "Date,Description,Debit,Credit,Balance\n" f"{today():%d/%m/%Y},MYSTERY SHOP 42,15.00,,100.00\n"
    r = await client.post("/api/imports", headers=TOKEN, files={"file": ("mb.csv", csv.encode())})
    assert r.status_code == 200, r.text
    assert r.json()["new"] == 1 and r.json()["to_review"] == 1

    items = (await client.get("/api/transactions?review=true", headers=TOKEN)).json()["items"]
    (txn,) = items
    r = await client.patch(f"/api/transactions/{txn['id']}", headers=TOKEN, json={"category": "shopping"})
    assert r.json()["category"] == "shopping" and r.json()["review"] is None



async def test_upload_limits(client, monkeypatch):
    monkeypatch.setattr(app_module, "MAX_FILE_BYTES", 10)
    r = await client.post("/api/imports", headers=TOKEN, files={"file": ("big.csv", b"x" * 11)})
    assert r.status_code == 413
    monkeypatch.undo()
    # CSV the csv module chokes on is a parse error, not a crash
    r = await client.post("/api/imports", headers=TOKEN, files={"file": ("bin.csv", b"a,b\rc\x00d\n\"e\nf")})
    assert r.status_code == 400, r.text


async def test_security_headers(client):
    headers = (await client.get("/")).headers
    assert "frame-ancestors 'none'" in headers["content-security-policy"]
    assert headers["x-content-type-options"] == "nosniff" and headers["referrer-policy"] == "no-referrer"


def _forged_update(chat_id: int) -> dict:
    """/web "from" the owner, answered into whatever chat the sender names."""
    return {
        "update_id": 1,
        "message": {
            "message_id": 1, "date": int(datetime.now().timestamp()),
            "chat": {"id": chat_id, "type": "private"},
            "from": {"id": 42, "is_bot": False, "first_name": "x"},
            "text": "/web", "entities": [{"type": "bot_command", "offset": 0, "length": 4}],
        },
    }


@pytest.mark.parametrize("configured, sent", [(None, None), ("s3cret", None), ("s3cret", "wrong"), ("s3cret", "s3cret")])
async def test_webhook_needs_the_secret(client, monkeypatch, configured, sent):
    settings = get_settings()
    monkeypatch.setattr(settings, "TELEGRAM_BOT_TOKEN", "123456:TEST")
    monkeypatch.setattr(settings, "TELEGRAM_WEBHOOK_SECRET", configured)
    bot = Bot("123456:TEST", session=FakeSession())
    monkeypatch.setattr(bot_module, "get_bot", lambda: bot)
    headers = {"X-Telegram-Bot-Api-Secret-Token": sent} if sent else {}

    r = await client.post("/telegram/webhook", json=_forged_update(999), headers=headers)
    await asyncio.gather(*list(app_module._background))
    if configured and sent == configured:
        assert r.status_code == 200 and bot.session.calls
    else:
        assert r.status_code == 403 and bot.session.calls == []

async def test_apple_pay_event_is_confirmed_by_statement(client):
    r = await client.post("/api/events", headers=TOKEN, json={"amount": "RM18.90", "merchant": "STARBUCKS KLCC"})
    assert r.status_code == 200 and r.json()["category"] == "food"
    assert (await client.post("/api/events", json={"amount": "1", "merchant": "x"})).status_code == 401

    day = today() - timedelta(days=1)
    csv = "Date,Description,Debit,Credit\n" f"{day:%d/%m/%Y},STARBUCKS KLCC KUALA LUMPUR,18.90,\n"
    result = (await client.post("/api/imports", headers=TOKEN, files={"file": ("s.csv", csv.encode())})).json()
    assert (result["new"], result["reconciled"]) == (0, 1)


async def test_mark_fx_via_api(client):
    csv = "Date,Description,Debit,Credit\n" f"{today():%d/%m/%Y},DUITNOW TRANSFER TO ALI,1000.00,\n"
    await client.post("/api/imports", headers=TOKEN, files={"file": ("p.csv", csv.encode())})
    (txn,) = (await client.get("/api/transactions?review=true", headers=TOKEN)).json()["items"]
    assert txn["review"] == "p2p"
    r = await client.post(f"/api/transactions/{txn['id']}/fx", headers=TOKEN, json={"rub_amount": "21000"})
    assert r.json()["rate"] == 21.0
    summary = (await client.get("/api/summary", headers=TOKEN)).json()
    assert summary["fx"]["rub"] == 21000.0 and summary["review_count"] == 0


def test_quick_entry():
    assert parse_entry("1500 rub такси").amount == -1500 and parse_entry("1500 rub такси").currency == "RUB"
    e = parse_entry("1 500₽ такси вчера")
    assert (e.amount, e.currency, e.description, e.day) == (-1500, "RUB", "такси", today() - timedelta(days=1))
    e = parse_entry("25.5 rm обед")
    assert (e.amount, e.currency) == (-25.5, "MYR")
    assert parse_entry("+5000р кэшбэк").amount == 5000
    assert parse_entry("привет") is None
    e = parse_entry("1500₽ 24.09.2025 такси #Транспорт")
    assert (e.amount, e.description, e.day, e.category) == (-1500, "такси", date(2025, 9, 24), "транспорт")
    assert parse_entry("200₽ кофе 7.5 кг").description == "кофе 7.5 кг"  # a quantity, not 7 May
    assert parse_entry("300₽ 31.02 кофе").day == today()  # no such date: left in the text
    ahead = today() + timedelta(days=1)
    assert parse_entry(f"100₽ {ahead:%d.%m} ёлка").day.year == ahead.year - 1  # a future day is last year's
    coins = ("USDT", "BTC")
    e = parse_entry("0,00012 btc 24.09.2025 подписка #подписки", coins)
    assert (e.amount, e.currency, e.description, e.day, e.category) == (
        Decimal("-0.00012"), "BTC", "подписка", date(2025, 9, 24), "подписки"
    )
    assert (parse_entry("+5 USDT кэшбэк", coins).amount, parse_entry("usdt 15 кофе", coins).currency) == (5, "USDT")
    assert parse_entry("15 coffee", coins).currency == "RUB"  # not a known coin
    assert parse_entry("15 usdt", coins) is None  # no description, like "1500₽"
    assert parse_rf_command("1000 21500 tng") == (1000, 21500, "tng", None)
    assert parse_rf_command("2000 39373,6 tng 02.03") == (2000, Decimal("39373.6"), "tng", date(today().year, 3, 2))
    assert parse_rf_command("1000") is None
    assert parse_rm_command("59259,9") == (Decimal("59259.9"), None, "maybank", None)
    assert parse_rm_command("59259.9 3100") == (Decimal("59259.9"), Decimal("3100"), "maybank", None)
    assert parse_rm_command("59259.9 12.02") == (Decimal("59259.9"), None, "maybank", date(today().year, 2, 12))
    assert parse_rm_command("59259.9 3100 tng 12.02") == (Decimal("59259.9"), Decimal("3100"), "tng", date(today().year, 2, 12))
    assert parse_rm_command("") is None


async def test_no_secret_key_disables_login(client, monkeypatch):
    from finance.config import get_settings

    link = urlparse(magic_link())
    monkeypatch.setattr(get_settings(), "SECRET_KEY", None)
    r = await client.get(f"{link.path}?{link.query}")
    assert r.headers["location"].startswith("/?expired")  # a link signed earlier no longer works
    assert "SECRET_KEY" in (await client.get("/")).text
    assert (await client.get("/api/summary", headers=TOKEN)).status_code == 200  # API token still works


async def test_unknown_apple_pay_merchant_asks_for_category(client, monkeypatch):
    import finance.bot
    from finance.web import app as web_app

    sent = []

    async def fake_notify(text, reply_markup=None):
        sent.append((text, reply_markup))

    monkeypatch.setattr(finance.bot, "notify_owner", fake_notify)
    await client.post("/api/events", headers=TOKEN, json={"amount": "RM18.90", "merchant": "STARBUCKS KLCC"})
    await client.post("/api/events", headers=TOKEN, json={"amount": "RM7.00", "merchant": "MYSTERY KIOSK"})
    for task in list(web_app._background):
        await task

    ((text, markup),) = sent  # the known merchant went through silently
    assert "какая категория" in text and "MYSTERY KIOSK" in text
    buttons = [row[0].text for row in markup.inline_keyboard]
    assert "🚕 Транспорт" in buttons


async def test_search_finds_a_row_from_any_month_to_fix(client, session):
    from finance import ledger
    from finance.parsers import ParsedStatement, ParsedTxn

    old = today() - timedelta(days=260)
    await ledger.import_statement(
        session,
        ParsedStatement("maybank", "maybank_csv",
                        [ParsedTxn(old, Decimal("-1488.60"), "TRANSFER FROM A/C ACME TRADING SDN. BHD.* Morgan Ale")]),
        origin="test",
    )
    in_month = (await client.get("/api/transactions?q=ACME", headers=TOKEN)).json()
    assert in_month["total"] == 0  # the default period is this month
    (txn,) = (await client.get("/api/transactions?q=acme&anytime=true", headers=TOKEN)).json()["items"]
    r = await client.patch(f"/api/transactions/{txn['id']}", headers=TOKEN, json={"category": "health"})
    assert r.json()["category"] == "health" and r.json()["kind"] == "expense"
