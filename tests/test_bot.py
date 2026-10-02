"""Feed real Telegram updates through the dispatcher; the Bot API is faked."""

from datetime import datetime, timedelta
from decimal import Decimal
from itertools import count

import pytest
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.types import Chat, Message, Update, User
from sqlalchemy import select

from finance import ledger
from finance.bot import get_dispatcher
from finance.bot.handlers import Act
from finance.models import Account, CategoryRule, Transaction
from finance.parsers import ParsedStatement, ParsedTxn
from finance.utils import fmt_day, today

OWNER = 42
_ids = count(1)


class FakeSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls = []

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        if type(method).__name__ in ("SendMessage", "EditMessageText"):
            return Message(
                message_id=next(_ids), date=datetime.now(), chat=Chat(id=OWNER, type="private"),
                text=method.text, from_user=User(id=1, is_bot=True, first_name="bot"),
            ).as_(bot)  # like a real reply: the handler may edit it
        return True

    async def stream_content(self, *args, **kwargs):
        yield b""

    async def close(self):
        pass

    def texts(self):
        return [getattr(c, "text", None) for c in self.calls if getattr(c, "text", None)]


@pytest.fixture
def bot():
    return Bot("123456:TEST", session=FakeSession())


def _user(uid=OWNER):
    return {"id": uid, "is_bot": False, "first_name": "M"}


async def send(bot, text, uid=OWNER):
    n = next(_ids)
    msg = {"message_id": n, "date": int(datetime.now().timestamp()), "chat": {"id": uid, "type": "private"},
           "from": _user(uid), "text": text}
    if text.startswith("/"):
        msg["entities"] = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
    await get_dispatcher().feed_update(bot, Update.model_validate({"update_id": n, "message": msg}, context={"bot": bot}))


async def press(bot, data: Act):
    n = next(_ids)
    cb = {"id": str(n), "from": _user(), "chat_instance": "c", "data": data.pack(),
          "message": {"message_id": n, "date": int(datetime.now().timestamp()),
                      "chat": {"id": OWNER, "type": "private"}, "text": "card"}}
    await get_dispatcher().feed_update(bot, Update.model_validate({"update_id": n, "callback_query": cb}, context={"bot": bot}))


async def test_strangers_are_ignored(bot, session):
    await send(bot, "/help", uid=999)
    assert bot.session.calls == []


async def test_help_and_rub_entry(bot, session):
    await send(bot, "/help")
    assert "Финансовый бот" in bot.session.texts()[-1]

    await send(bot, "1500₽ такси")
    assert "Записал" in bot.session.texts()[-1]
    txn = await session.scalar(select(Transaction).where(Transaction.description == "такси"))
    assert txn.amount == Decimal("-1500") and txn.category.code == "transport"

    await press(bot, Act(a="cur", t=txn.id))  # it was ringgit cash after all
    assert "Наличные RM" in bot.session.texts()[-1]
    await send(bot, "/undo")
    assert "Удалил" in bot.session.texts()[-1]


async def test_multi_line_entries(bot, session):
    await send(bot, "1500 rub такси\n300 rub кофе\nчто-то непонятное\n+5000 rub кэшбэк")
    reply = bot.session.texts()[-1]
    assert "Записал 3 из 4" in reply and "что-то непонятное" in reply
    rows = (await session.scalars(select(Transaction).where(Transaction.description.in_(["такси", "кофе", "кэшбэк"])))).all()
    assert sorted(r.amount for r in rows) == [Decimal("-1500"), Decimal("-300"), Decimal("5000")]


async def test_review_rf_flow(bot, session):
    await ledger.import_statement(
        session,
        ParsedStatement("maybank", "maybank_csv",
                        [ParsedTxn(today() - timedelta(days=1), Decimal("-1000"), "DUITNOW TRANSFER TO ALI")]),
        origin="test",
    )
    txn = await session.scalar(select(Transaction).where(Transaction.description.contains("ALI")))

    await send(bot, "/review")
    assert "Перевод человеку" in bot.session.texts()[-1]
    await press(bot, Act(a="rf", t=txn.id, m=1))
    assert "Сколько ₽" in bot.session.texts()[-1]
    await send(bot, "21 500")
    texts = bot.session.texts()
    assert "курс 21.50" in texts[-2] and "Всё разобрано" in texts[-1]

    await send(bot, "/week")
    await send(bot, "/balance")
    assert "Российский счёт" in bot.session.texts()[-1]


async def test_review_category_is_remembered_with_undo(bot, session):
    await ledger.import_statement(
        session,
        ParsedStatement("maybank", "maybank_csv", [
            ParsedTxn(today(), Decimal("-100"), "SALE DEBIT SB294-SOUTHLINK BA * KUALA LUMPUR, MY"),
            ParsedTxn(today(), Decimal("-40"), "SALE DEBIT SB301-SOUTHLINK BA * PETALING JAYA, MY"),
        ]),
        origin="test",
    )
    txn = await session.scalar(select(Transaction).where(Transaction.description.contains("SB294")))
    transport = await ledger.get_category(session, "transport")
    await press(bot, Act(a="cat", t=txn.id, x=transport.id, m=1))
    edits = [c for c in bot.session.calls if type(c).__name__ == "EditMessageText"]
    assert "Запомнил «SOUTHLINK»" in edits[-1].text and "ещё 1 таких уже разобрал" in edits[-1].text
    undo = edits[-1].reply_markup.inline_keyboard[0][0]
    assert undo.text == "↩️ Не запоминать"

    await send(bot, "/rules")
    assert "«SOUTHLINK» → 🚕 Транспорт" in bot.session.texts()[-1]

    await press(bot, Act.unpack(undo.callback_data))
    edits = [c for c in bot.session.calls if type(c).__name__ == "EditMessageText"]
    assert "только эта транзакция" in edits[-1].text
    assert await ledger.user_rules(session) == []


async def test_rf_command_and_month(bot, session):
    await send(bot, "/rf 1000 21500")
    assert "подтвердится сам" in bot.session.texts()[-1]
    await send(bot, "/rf 1000 21500")
    assert "уже записан" in bot.session.texts()[-1]
    await send(bot, "/month")
    assert "На РФ" in bot.session.texts()[-1]
    await send(bot, "/web")
    assert "auth/magic" in bot.session.texts()[-1]


async def test_skip_does_not_come_back_and_own_offers_pots(bot, session):
    day = today() - timedelta(days=30)
    await ledger.import_statement(
        session,
        ParsedStatement("maybank", "maybank_csv", [
            ParsedTxn(day, Decimal("-1500"), "TRANSFER FROM A/C ALEX MORGAN * 00000001"),
            ParsedTxn(day, Decimal("-200"), "DUITNOW TRANSFER TO ALI"),
        ]),
        origin="test",
    )
    rows = {t.description[:8]: t for t in (await session.scalars(select(Transaction))).unique()}
    first, second = rows["TRANSFER"], rows["DUITNOW "]

    await send(bot, "/pot Holiday 0")
    assert "Копилка Holiday" in bot.session.texts()[-1]

    await press(bot, Act(a="skip", t=second.id, m=1))
    texts = bot.session.texts()
    assert "Отложил на неделю" in texts[-2] and "ALI" not in texts[-1]  # next card is the other row

    await press(bot, Act(a="own", t=first.id, m=1))
    markups = [c for c in bot.session.calls if type(c).__name__ == "EditMessageReplyMarkup"]
    buttons = [b.text for row in markups[-1].reply_markup.inline_keyboard for b in row]
    assert buttons == [
        "📱 TNG eWallet", "🇷🇺 Российский счёт", "🪙 Криптокошелёк", "🐷 Копилка Holiday", "↔️ Другой мой счёт"
    ]
    pot = await session.scalar(select(Account).where(Account.code == "pot_holiday"))
    await press(bot, Act(a="topot", t=first.id, x=pot.id, m=1))
    assert "Отложенных 1" in bot.session.texts()[-1]  # queue empty now, the skipped one waits a week


async def test_seed_keeps_category_order(session):
    from finance.bot.handlers import categories_of
    from finance.seed import CATEGORIES, seed

    (await ledger.get_category(session, "other")).sort = 0  # a DB seeded before a category was inserted
    session.add(CategoryRule(pattern="OLD|PATTERN", kind="expense", origin="seed"))  # since edited in seed.py
    await session.commit()
    assert (await seed(session))["rules_removed"] == 1
    codes = [c.code for c in await categories_of(session, "expense")]
    assert codes == [code for code, _, _, kind in CATEGORIES if kind == "expense"]
    assert codes.index("projects") == codes.index("subscriptions") + 1


async def test_own_account_waits_for_that_statement(bot, session):
    day = today() - timedelta(days=5)
    await ledger.import_statement(
        session,
        ParsedStatement("tng", "tng_pdf", [ParsedTxn(day, Decimal("500"), "ALEX MORGAN", raw_type="DUITNOW_RECEIVEFROM")]),
        origin="test",
    )
    txn = await session.scalar(select(Transaction))
    maybank = await ledger.get_account(session, "maybank")
    await press(bot, Act(a="ownacc", t=txn.id, x=maybank.id, m=1))
    assert "↔️ Перевод с Maybank — склею, когда придёт выписка Maybank" in bot.session.texts()[-2]


async def test_rub_entry_with_date_and_category(bot, session):
    day = today() - timedelta(days=2)
    await send(bot, f"1500₽ {day:%d.%m} такси до аэропорта #еда")
    txn = await session.scalar(select(Transaction).where(Transaction.description == "такси до аэропорта"))
    assert (txn.booked_on, txn.category.code, txn.account.code) == (day, "food", "ru")

    await send(bot, f"+50000₽ {day:%d.%m} фриланс #доход")
    txn = await session.scalar(select(Transaction).where(Transaction.description == "фриланс"))
    assert (txn.amount, txn.kind, txn.category.code) == (Decimal("50000"), "income", "other_income")

    await send(bot, "300₽ кофе #непонятно")
    assert "Категория «#непонятно» не подошла" in bot.session.texts()[-1]


def test_long_labels_get_their_own_row():
    from aiogram.utils.keyboard import InlineKeyboardBuilder

    from finance.bot.handlers import fit_rows

    kb = InlineKeyboardBuilder()
    for text in ("➕ Доход", "↔️ Мои счета", "↩️ Мне вернули за…", "⏭ Пропустить", "🗑 Удалить"):
        kb.button(text=text, callback_data="x")
    rows = [[b.text for b in row] for row in fit_rows(kb).inline_keyboard]
    assert rows == [["➕ Доход", "↔️ Мои счета"], ["↩️ Мне вернули за…"], ["⏭ Пропустить", "🗑 Удалить"]]


def _buttons(markup) -> list[str]:
    return [b.text for row in markup.inline_keyboard for b in row]


async def test_review_crypto_purchase(bot, session):
    from finance import reports

    day = today() - timedelta(days=1)
    await ledger.import_statement(
        session,
        ParsedStatement("tng", "tng_pdf", [
            ParsedTxn(day, Decimal("-278.21"), "Payment - ALP*QRCode1321 Shanghai", raw_type="Payment"),
            ParsedTxn(day, Decimal("-100"), "DUITNOW TRANSFER TO ALI", raw_type="DUITNOW_TRANSFERTO"),
        ]),
        origin="test",
    )
    rows = {t.description[:7]: t for t in (await session.scalars(select(Transaction))).unique()}
    qr, p2p = rows["Payment"], rows["DUITNOW"]

    await send(bot, "/review")
    cards = [c for c in bot.session.calls if type(c).__name__ == "SendMessage" and c.reply_markup]
    assert "🪙 Крипто" in _buttons(cards[-1].reply_markup)

    await press(bot, Act(a="crypto", t=qr.id, m=1))
    assert "Сколько пришло на криптокошелёк за RM 278.21" in bot.session.texts()[-1]
    await send(bot, "сколько-то")
    assert "Нужны сумма и монета" in bot.session.texts()[-1]
    await send(bot, "60 usdt")
    texts = bot.session.texts()
    assert "RM 278.21 → USDT 60.00 (RM 4.64 за USDT)" in texts[-2]
    assert "🪙 Крипто" in _buttons(bot.session.calls[-1].reply_markup)  # the next card: the transfer to ALI

    await session.refresh(qr)
    assert (qr.kind, qr.review_reason, qr.transfer.kind) == ("transfer", None, "crypto")

    # a bare number: the coin of the last purchase
    await press(bot, Act(a="crypto", t=p2p.id, m=1))
    assert "Без монеты — USDT" in bot.session.texts()[-1]
    await send(bot, "21,5")
    assert "USDT 21.50" in bot.session.texts()[-2] and "Всё разобрано" in bot.session.texts()[-1]

    balances = {name: (cur, amount) for name, cur, amount, _ in await reports.balances(session)}
    assert balances["Крипто USDT"] == ("USDT", Decimal("81.5"))
    s = await reports.summarize(session, day, day, baseline_periods=0)
    assert s.blocks.get("MYR") is None  # not spending
    assert "🪙 <b>В крипту:</b> RM 378.21 → USDT 81.50" in reports.format_summary(s, "t", "")

    await send(bot, "/setbalance usdt 80")
    assert "Крипто USDT: USDT 80.00 (корректировка −USDT 1.50)" in bot.session.texts()[-1]
    await send(bot, "/setbalance 20000")
    assert "₽ 20 000" in bot.session.texts()[-1]


async def test_crypto_keeps_coin_places_and_can_be_redone(session):
    from finance import reports
    from finance.utils import fmt_money

    await ledger.import_statement(
        session,
        ParsedStatement("maybank", "maybank_csv", [ParsedTxn(today(), Decimal("-300"), "DUITNOW TRANSFER TO BOB")]),
        origin="test",
    )
    txn = await session.scalar(select(Transaction))
    await ledger.mark_crypto(session, txn, Decimal("60"), "usdt")
    await ledger.mark_crypto(session, txn, Decimal("0.00061"), "BTC")  # changed my mind: it was bitcoin
    balances = {name: amount for name, _, amount, _ in await reports.balances(session)}
    assert balances["Крипто BTC"] == Decimal("0.00061") and "Крипто USDT" not in balances
    assert fmt_money(balances["Крипто BTC"], "BTC") == "BTC 0.00061"

    await ledger.soft_delete(session, txn)  # the wallet leg goes with it
    balances = {name: amount for name, _, amount, _ in await reports.balances(session)}
    assert "Крипто BTC" not in balances


async def test_crypto_spending_typed_like_rubles(bot, session):
    from finance import reports

    await send(bot, "15 usdt кофе")
    card = bot.session.calls[-1]
    assert "Записал: <b>−USDT 15.00</b> · кофе" in card.text and "Крипто USDT" in card.text
    assert _buttons(card.reply_markup) == ["🏷 Категория", "🗑 Удалить"]  # no ₽/RM switch for a coin

    await send(bot, "0,0001 btc вчера подписка #подписки")
    txn = await session.scalar(select(Transaction).where(Transaction.description == "подписка"))
    assert (txn.amount, txn.account.code, txn.category.code, txn.booked_on) == (
        Decimal("-0.0001"), "crypto_btc", "subscriptions", today() - timedelta(days=1)
    )
    assert "−BTC 0.0001" in bot.session.texts()[-1]

    s = await reports.summarize(session, today() - timedelta(days=1), today(), baseline_periods=0)
    text = reports.format_summary(s, "t", "")
    assert "🪙 <b>Расходы в USDT: USDT 15.00</b>" in text and "🪙 <b>Расходы в BTC: BTC 0.0001</b>" in text
    assert s.blocks.get("MYR") is None  # coins never mix into ringgit spending

    await send(bot, "/undo")
    assert "Удалил: −BTC 0.0001 · подписка" in bot.session.texts()[-1]


async def test_review_ringgit_paid_back_for_rubles(bot, session):
    lessons = await ledger.add_manual(session, "ru", Decimal("-5000"), "уроки английского", today(), "education")
    day = today() - timedelta(days=1)
    await ledger.import_statement(
        session,
        ParsedStatement("maybank", "maybank_csv", [ParsedTxn(day, Decimal("1450"), "CASH DEPOSIT")]),
        origin="test",
    )
    await ledger.import_statement(
        session,
        ParsedStatement("tng", "tng_pdf", [ParsedTxn(day, Decimal("465"), "IVANOVA MARIA", raw_type="DUITNOW_RECEIVEFROM")]),
        origin="test",
    )
    rows = {t.description[:4]: t for t in (await session.scalars(select(Transaction))).unique()}
    cash, maria = rows["CASH"], rows["IVAN"]

    await send(bot, "/review")
    cards = [c for c in bot.session.calls if type(c).__name__ == "SendMessage" and c.reply_markup]
    buttons = _buttons(cards[-1].reply_markup)
    assert "↩️ Возврат за расход в ₽" in buttons and "🔁 Возврат за перевод в ₽" in buttons

    await press(bot, Act(a="rubexp", t=cash.id, m=1))
    markups = [c for c in bot.session.calls if type(c).__name__ == "EditMessageReplyMarkup"]
    assert _buttons(markups[-1].reply_markup) == [
        f"−₽ 5 000 · уроки английского · {fmt_day(today())}", "✍️ Его нет в записях — ввести ₽"
    ]
    await press(bot, Act(a="rubpick", t=cash.id, x=lessons.id, m=1))
    assert "Возврат за «уроки английского» (₽ 5 000, 3.45 ₽ за RM)" in bot.session.texts()[-2]

    await press(bot, Act(a="rubtr", t=maria.id, m=1))
    assert "Сколько ₽ ты отдал за RM 465.00" in bot.session.texts()[-1]
    await send(bot, "10 000")
    texts = bot.session.texts()
    assert "🔁 ₽ 10 000 → RM 465.00 (21.51 ₽ за RM) — не доход" in texts[-2] and "Всё разобрано" in texts[-1]


async def test_apple_pay_category_from_the_push(bot, session):
    from finance.bot.handlers import event_card

    txn = await ledger.add_manual(session, "maybank", Decimal("-7"), "MYSTERY KIOSK", source="shortcut", review=True)
    _, markup = await event_card(session, txn)
    food = next(row[0] for row in markup.inline_keyboard if row[0].text == "🍜 Еда вне дома")
    await press(bot, Act.unpack(food.callback_data))
    edits = [c for c in bot.session.calls if type(c).__name__ == "EditMessageText"]
    assert "🍜 Еда вне дома" in edits[-1].text and "📌 Запомнил" in edits[-1].text
    assert bot.session.texts()[-1] == edits[-1].text  # no /review card follows
    await session.refresh(txn)
    assert txn.review_reason is None


# --- payment screenshots ---------------------------------------------------------


async def send_photo(bot, uid=OWNER):
    n = next(_ids)
    msg = {"message_id": n, "date": int(datetime.now().timestamp()), "chat": {"id": uid, "type": "private"},
           "from": _user(uid), "photo": [{"file_id": "f", "file_unique_id": "u", "width": 590, "height": 1320}]}
    await get_dispatcher().feed_update(bot, Update.model_validate({"update_id": n, "message": msg}, context={"bot": bot}))


@pytest.fixture
def screen(monkeypatch):
    """The screenshot the bot will "see": the model's JSON answer."""
    import io

    from finance import screenshot

    shown = {}
    monkeypatch.setattr(screenshot, "enabled", lambda: True)
    monkeypatch.setattr(Bot, "download", lambda self, file, *a, **kw: _async(io.BytesIO(b"img")))

    async def read_payment(image, media_type):
        return screenshot.payment_from(shown)

    monkeypatch.setattr(screenshot, "read_payment", read_payment)
    return shown


async def _async(value):
    return value


def tng_transfer(day, **over):
    return {
        "is_payment": True, "app": "tng", "direction": "out", "amount": "15.00", "currency": "MYR",
        "counterparty": "TAN MEI LING", "remark": "TAN MEI LING",
        "occurred_at": f"{day.isoformat()} 19:44", "to_person": True, "failure_reason": "", **over,
    }


def edits(bot):
    return [c for c in bot.session.calls if type(c).__name__ == "EditMessageText"]


async def test_screenshot_to_a_person_asks_and_the_statement_confirms(bot, session, screen):
    from finance.models import PENDING, POSTED, REVIEW_P2P

    day = today() - timedelta(days=1)
    screen.update(tng_transfer(day))
    await send_photo(bot)
    card = edits(bot)[-1]
    assert "📸 Записал: <b>−RM 15.00</b> · TAN MEI LING" in card.text and "Какая категория?" in card.text
    assert "(TAN MEI LING)" not in card.text  # the remark only repeated the receiver
    txn = await session.scalar(select(Transaction).where(Transaction.source == "screenshot"))
    assert (txn.account.code, txn.booked_on, txn.status, txn.review_reason) == ("tng", day, PENDING, REVIEW_P2P)
    from finance.config import get_settings
    local = txn.booked_at.astimezone(get_settings().tz)
    assert (local.hour, local.minute) == (19, 44)  # the app's time is Kuala Lumpur time

    food = next(row[0] for row in card.reply_markup.inline_keyboard if row[0].text == "🍜 Еда вне дома")
    await press(bot, Act.unpack(food.callback_data))
    assert "Всегда так для «TAN … MEI»" in edits(bot)[-1].reply_markup.inline_keyboard[0][0].text  # not auto-remembered

    from finance.parsers import ParsedStatement, ParsedTxn
    result = await ledger.import_statement(
        session,
        ParsedStatement("tng", "tng_pdf", [ParsedTxn(day, Decimal("-15.00"), "TAN MEI LING", raw_type="Transfer to Wallet")]),
        origin="test",
    )
    assert (result.new, result.reconciled) == (0, 1)
    await session.refresh(txn)
    assert (txn.status, txn.category.code, txn.kind) == (POSTED, "food", "expense")


async def test_screenshot_of_a_known_shop_needs_no_question(bot, session, screen):
    screen.update(tng_transfer(today(), counterparty="STARBUCKS PAVILION", remark="", to_person=False))
    await send_photo(bot)
    card = edits(bot)[-1]
    assert "🍜 Еда вне дома · TNG eWallet" in card.text and "подтвердится без дубля" in card.text
    assert [b.text for row in card.reply_markup.inline_keyboard for b in row] == ["🏷 Категория", "🗑 Удалить"]


async def test_screenshot_already_in_the_statement(bot, session, screen):
    from finance.parsers import ParsedStatement, ParsedTxn

    day = today() - timedelta(days=2)
    await ledger.import_statement(
        session,
        ParsedStatement("tng", "tng_pdf", [ParsedTxn(day, Decimal("-15.00"), "TAN MEI LING", raw_type="Transfer to Wallet")]),
        origin="test",
    )
    screen.update(tng_transfer(day))
    await send_photo(bot)
    card = edits(bot)[-1]
    assert "уже есть" in card.text and "Какая категория?" in card.text
    assert await session.scalar(select(Transaction).where(Transaction.source == "screenshot")) is None

    again = card.reply_markup.inline_keyboard[-1][0]
    assert again.text == "➕ Нет, это другой платёж"
    from finance.bot.handlers import Shot
    await press(bot, Shot.unpack(again.callback_data))
    assert "📸 Записал" in edits(bot)[-1].text
    await press(bot, Shot.unpack(again.callback_data))  # a second tap adds nothing
    rows = (await session.scalars(select(Transaction).where(Transaction.source == "screenshot"))).all()
    assert len(rows) == 1


async def test_screenshot_from_another_app_asks_the_account(bot, session, screen):
    screen.update(tng_transfer(today(), app="other", to_person=False, counterparty="KEDAI RUNCIT AH HOCK"))
    await send_photo(bot)
    card = edits(bot)[-1]
    assert "с какого счёта" in card.text
    cash = next(b for row in card.reply_markup.inline_keyboard for b in row if b.text == "Наличные RM")
    from finance.bot.handlers import Shot
    await press(bot, Shot.unpack(cash.callback_data))
    txn = await session.scalar(select(Transaction).where(Transaction.source == "screenshot"))
    assert (txn.account.code, txn.status) == ("cash_myr", "posted")


async def test_screenshot_that_is_not_an_expense(bot, session, screen):
    screen.update(tng_transfer(today(), is_payment=False, failure_reason="это список транзакций"))
    await send_photo(bot)
    assert edits(bot)[-1].text == "📸 Не записал: это список транзакций."
    screen.update(tng_transfer(today(), direction="in"))
    await send_photo(bot)
    assert "поступление" in edits(bot)[-1].text
    screen.update(tng_transfer(today(), counterparty="MORGAN ALEX"))
    await send_photo(bot)
    assert "самому себе" in edits(bot)[-1].text
    assert await session.scalar(select(Transaction).where(Transaction.source == "screenshot")) is None


async def test_screenshot_without_a_key(bot, session):
    await send_photo(bot)
    assert "ANTHROPIC_API_KEY" in bot.session.texts()[-1]


async def test_fix_finds_a_row_and_changes_its_category(bot, session):
    await ledger.import_statement(
        session,
        ParsedStatement("maybank", "maybank_csv", [
            ParsedTxn(today(), Decimal("-1488.60"), "TRANSFER FROM A/C ACME TRADING SDN. BHD.* Morgan Ale"),
            ParsedTxn(today(), Decimal("-40"), "SALE DEBIT SB294-SOUTHLINK BA * KUALA LUMPUR, MY"),
        ]),
        origin="test",
    )
    txn = await session.scalar(select(Transaction).where(Transaction.description.contains("ACME")))
    fun, health = await ledger.get_category(session, "fun"), await ledger.get_category(session, "health")
    await press(bot, Act(a="cat", t=txn.id, x=fun.id, m=1))  # the wrong tap
    edits = [c for c in bot.session.calls if type(c).__name__ == "EditMessageText"]
    buttons = [b.text for row in edits[-1].reply_markup.inline_keyboard for b in row]
    assert "🏷 Другая категория" in buttons

    await send(bot, "/fix acme  trading")
    assert "Какую поправить" in bot.session.texts()[-1] and "SOUTHLINK" not in bot.session.texts()[-1]
    button = bot.session.calls[-1].reply_markup.inline_keyboard[0][0]
    await press(bot, Act.unpack(button.callback_data))
    markups = [c for c in bot.session.calls if type(c).__name__ == "EditMessageReplyMarkup"]
    grid = [b.text for row in markups[-1].reply_markup.inline_keyboard for b in row]
    assert health.label in grid

    await press(bot, Act(a="cat", t=txn.id, x=health.id))
    await session.refresh(txn)
    assert txn.category.code == "health" and txn.kind == "expense"

    await send(bot, "/fix nothing-like-this")
    assert "Не нашёл" in bot.session.texts()[-1]


async def test_fix_against_a_saved_rule_offers_to_move_it(bot, session):
    await ledger.import_statement(
        session,
        ParsedStatement("maybank", "maybank_csv",
                        [ParsedTxn(today(), Decimal("-100"), "SALE DEBIT SB294-SOUTHLINK BA * KUALA LUMPUR, MY")]),
        origin="test",
    )
    txn = await session.scalar(select(Transaction).where(Transaction.description.contains("SB294")))
    fun, transport = await ledger.get_category(session, "fun"), await ledger.get_category(session, "transport")
    await press(bot, Act(a="cat", t=txn.id, x=fun.id, m=1))  # remembered SOUTHLINK → fun
    await press(bot, Act(a="cat", t=txn.id, x=transport.id))
    edits = [c for c in bot.session.calls if type(c).__name__ == "EditMessageText"]
    move = edits[-1].reply_markup.inline_keyboard[0][0]
    assert move.text.startswith("📌 Всегда так")
    await press(bot, Act.unpack(move.callback_data))
    rule = (await ledger.user_rules(session))[0]
    await session.refresh(rule)
    assert rule.category_id == transport.id


async def test_snoozed_rows_come_back_on_request(bot, session):
    await ledger.import_statement(
        session,
        ParsedStatement("maybank", "maybank_csv",
                        [ParsedTxn(today(), Decimal("-200"), "DUITNOW TRANSFER TO ALI")]),
        origin="test",
    )
    txn = await session.scalar(select(Transaction).where(Transaction.description.contains("ALI")))
    await press(bot, Act(a="skip", t=txn.id, m=1))
    assert "Отложенных 1" in bot.session.texts()[-1]
    button = bot.session.calls[-1].reply_markup.inline_keyboard[0][0]
    assert button.text == "⏪ Разобрать отложенные (1)"

    await press(bot, Act.unpack(button.callback_data))
    assert "Перевод человеку" in bot.session.texts()[-1] and "ALI" in bot.session.texts()[-1]
    assert await ledger.snoozed_count(session) == 0
