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
