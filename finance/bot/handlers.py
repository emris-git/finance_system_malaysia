"""Bot handlers. Business logic lives in finance.ledger / finance.reports."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from html import escape

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart, StateFilter
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from finance import advisor, budget, ledger, reports, screenshot
from finance.classify import names_owner, rule_label, suggest_pattern
from finance.config import get_settings
from finance.models import (
    CRYPTO,
    EXPENSE,
    INCOME,
    PENDING,
    PLAN_CANCELLED,
    PLAN_DONE,
    POSTED,
    REVIEW_AWAITING_PAIR,
    REVIEW_P2P,
    REVIEW_P2P_IN,
    REVIEW_UNCATEGORIZED,
    USER_FACING_REVIEW,
    Account,
    Category,
    Transaction,
)
from finance.parsers import MAX_FILE_BYTES, ParseError, parse_file
from finance.quick_entry import KNOWN_COINS, parse_crypto_amount, parse_entry, parse_rf_command
from finance.screenshot import Payment, ScreenshotError
from finance.utils import cycle_ending_in, fmt_day, fmt_money, to_decimal, today
from finance.web.auth import login_enabled, magic_link

router = Router()

HELP = """💰 <b>Финансовый бот</b>

<b>Выписки</b> — пришли PDF из TNG или PDF/CSV из Maybank: разберу и загружу без дублей.

<b>Рубли и наличные</b> — просто напиши:
• <code>1500 rub такси</code> — расход с российского счёта (можно и ₽, р, руб)
• <code>+5000 rub кэшбэк</code> — поступление в рублях
• <code>25 rm обед вчера</code> — наличные ринггиты
• <code>1500 rub 24.09 такси #транспорт</code> — с датой (ДД.ММ или ДД.ММ.ГГГГ) и категорией
• несколько записей — каждая с новой строки в одном сообщении

<b>Скрин оплаты</b> — пришли скриншот успешного платежа из TNG или MAE: запишу расход и спрошу категорию, если сам не пойму. Когда придёт выписка, запись подтвердится без дубля.

<b>Перевод на РФ</b> (отдал ринггиты — получил рубли):
• в /review нажми 🇷🇺 у перевода и напиши, сколько ₽ пришло
• или сразу: <code>/rf 1000 21500</code> (<code>/rf 1000 21500 24.09</code> — с датой, <code>… tng</code> — если платил с TNG)

<b>Вернули ринггитами за рубли</b> (заплатил рублями за кого-то или перевёл ему рубли): в /review у поступления нажми
• ↩️ «Возврат за расход в ₽» и выбери рублёвый расход — он перестанет считаться твоим
• 🔁 «Возврат за перевод в ₽» и напиши, сколько ₽ отдал

<b>Крипта</b>:
• купил (заплатил ринггиты — монеты пришли на кошелёк): в /review нажми 🪙 и напиши, сколько пришло, например <code>60 USDT</code>
• потратил — как с рублями: <code>15 usdt кофе</code>, <code>0.0001 btc 24.09 подписка #подписки</code>, <code>+5 usdt кэшбэк</code>

<b>Отчёты</b>: /week /lastweek /month /balance, дашборд — /web
Остаток рублёвого счёта: <code>/setbalance 20000</code>, криптокошелька: <code>/setbalance usdt 120</code>
Остаток Maybank или TNG из приложения, пока нет выписки: <code>/setbalance maybank 4210</code>
Копилки: /pot, сумма — <code>/pot Holiday 15000</code>, можно ли тратить в бюджете — <code>/pot Reserve резерв</code> / <code>/pot Holiday цель</code>

<b>Бюджет</b>:
• /budget — прогноз на полгода: сколько останется к концу каждого месяца и перед зарплатой
• /plan — будущие траты: <code>/plan 1800 отель Бали 25.10</code>, <code>/plan 12000 переезд март 2027</code>, <code>/plan 2000 РФ ежемесячно с 03.10 #перевод</code>
• /ask — спросить Claude, пролезет ли покупка: <code>/ask ноутбук за 6000 в декабре</code>
Категорию для незнакомого магазина запоминаю сразу — дальше такие ставлю сам. Список и удаление: /rules
Ошибся категорией: <code>/fix acme trading</code> — найду транзакцию и дам выбрать другую (просто /fix — последние)
Отложенные (⏭) вернутся через неделю или сразу — кнопкой в конце /review

По понедельникам пришлю отчёт за неделю, наутро после зарплаты — за месяц."""


class Act(CallbackData, prefix="t"):
    a: str  # action
    t: int  # transaction id
    x: int = 0  # argument: category id, pending txn id, flag
    m: int = 0  # 1 = inside the /review flow: show the next item afterwards


class RfAmount(StatesGroup):
    waiting = State()


class CryptoAmount(StatesGroup):
    waiting = State()


class RubBack(StatesGroup):
    waiting = State()


# Words /setbalance takes for the RUB and cash accounts, not coin tickers
NOT_COINS = {"CASH", "RM", "MYR", "RU", "RUB", "RUR"}
# /setbalance maybank 4210: the real balance of a statement account between statements
STATEMENT_ACCOUNT_WORDS = {"maybank": "maybank", "mbb": "maybank", "мейбанк": "maybank", "tng": "tng", "тнг": "tng"}
# /pot Reserve резерв: the budget may spend it; цель: it may not
POT_ROLES = {"резерв": True, "цель": False}


# --- rendering ---------------------------------------------------------------


def txn_line(t: Transaction) -> str:
    amount = fmt_money(t.amount, t.account.currency, signed=True)
    return f"<b>{amount}</b> · {escape(t.account.name)} · {fmt_day(t.booked_on)}\n{escape(t.description[:200])}"


async def categories_of(session: AsyncSession, kind: str) -> list[Category]:
    return list(
        (
            await session.scalars(
                select(Category).where(Category.kind == kind, Category.is_active).order_by(Category.sort)
            )
        ).all()
    )


def category_grid(kb: InlineKeyboardBuilder, cats: list[Category], txn_id: int, action: str, mode: int) -> None:
    for c in cats:
        kb.button(text=c.label, callback_data=Act(a=action, t=txn_id, x=c.id, m=mode))


# Telegram splits a row's width evenly and cuts long labels ("💼 …ата") on a phone;
# two buttons fit side by side only when both labels are this short.
PAIR_MAX = 13


def fit_rows(kb: InlineKeyboardBuilder) -> InlineKeyboardMarkup:
    """Lay buttons out in order: two in a row when both are short, else one full-width."""
    rows: list[int] = []
    half_row = False  # the last row holds one short button
    for button in kb.buttons:
        short = len(button.text) <= PAIR_MAX
        if short and half_row:
            rows[-1] = 2
            half_row = False
        else:
            rows.append(1)
            half_row = short
    kb.adjust(*rows)
    return kb.as_markup()


def pay_back_buttons(kb: InlineKeyboardBuilder, t: Transaction, mode: int) -> None:
    """Ringgit given back for rubles: for an expense in rubles or rubles handed over."""
    if t.amount > 0 and t.account.currency == "MYR":
        kb.button(text="↩️ Возврат за расход в ₽", callback_data=Act(a="rubexp", t=t.id, m=mode))
        kb.button(text="🔁 Возврат за перевод в ₽", callback_data=Act(a="rubtr", t=t.id, m=mode))


async def review_card(session: AsyncSession, t: Transaction, left: int) -> tuple[str, InlineKeyboardMarkup]:
    kb = InlineKeyboardBuilder()
    header = f"📝 <b>Разбор</b> · осталось {left}\n\n{txn_line(t)}\n\n"
    reason = t.review_reason

    if reason == REVIEW_P2P or (reason == REVIEW_AWAITING_PAIR and t.amount < 0):
        question = "Перевод человеку — что это?" if reason == REVIEW_P2P else "Не нашёл пару в другом счёте — что это?"
        for p in (await ledger.pending_fx_near(session, t))[:3]:
            kb.button(
                text=f"🔗 РФ-перевод {fmt_day(p.booked_on)}: {fmt_money(p.amount, 'MYR')}",
                callback_data=Act(a="merge", t=t.id, x=p.id, m=1),
            )
        rf_label = "🇷🇺 На РФ-счёт (как раньше)" if await ledger.previous_fx_to(session, t.merchant) else "🇷🇺 На РФ-счёт"
        kb.button(text=rf_label, callback_data=Act(a="rf", t=t.id, m=1))
        kb.button(text="🪙 Крипто", callback_data=Act(a="crypto", t=t.id, m=1))
        kb.button(text="↔️ Мои счета", callback_data=Act(a="own", t=t.id, m=1))
        kb.button(text="🧾 Это расход…", callback_data=Act(a="cats", t=t.id, x=1, m=1))
        kb.button(text="⏭ Пропустить", callback_data=Act(a="skip", t=t.id, m=1))
    elif reason in (REVIEW_P2P_IN, REVIEW_AWAITING_PAIR):
        question = "Входящий перевод — что это?"
        kb.button(text="➕ Доход", callback_data=Act(a="keep", t=t.id, m=1))
        kb.button(text="↔️ Мои счета", callback_data=Act(a="own", t=t.id, m=1))
        kb.button(text="↩️ Мне вернули за…", callback_data=Act(a="cats", t=t.id, x=1, m=1))
        pay_back_buttons(kb, t, 1)
        kb.button(text="⏭ Пропустить", callback_data=Act(a="skip", t=t.id, m=1))
    else:
        question = "Какая категория?"
        kind = EXPENSE if t.amount < 0 else INCOME
        cats = await categories_of(session, kind)
        category_grid(kb, cats, t.id, "cat", 1)
        if t.amount < 0 and t.account.kind != CRYPTO:
            kb.button(text="🪙 Крипто", callback_data=Act(a="crypto", t=t.id, m=1))
        pay_back_buttons(kb, t, 1)
        kb.button(text="✔️ Оставить как есть", callback_data=Act(a="keep", t=t.id, m=1))
        kb.button(text="⏭ Пропустить", callback_data=Act(a="skip", t=t.id, m=1))
        kb.adjust(1)  # a column of full-width buttons: category names are long
        return header + question, kb.as_markup()
    return header + question, fit_rows(kb)


async def event_card(session: AsyncSession, t: Transaction) -> tuple[str, InlineKeyboardMarkup]:
    """Apple Pay purchase the rules did not recognize: ask for the category right away.
    The choice is remembered for the merchant, as in /review; unanswered, it waits there."""
    kb = InlineKeyboardBuilder()
    category_grid(kb, await categories_of(session, EXPENSE if t.amount < 0 else INCOME), t.id, "cat", 0)
    kb.adjust(1)
    return f"💳 Новая покупка — какая категория?\n\n{txn_line(t)}", kb.as_markup()


def entry_card(t: Transaction) -> tuple[str, InlineKeyboardMarkup]:
    category = t.category.label if t.category else "—"
    text = (
        f"✅ Записал: <b>{fmt_money(t.amount, t.account.currency, signed=True)}</b> · {escape(t.description)}\n"
        f"{escape(category)} · {escape(t.account.name)} · {fmt_day(t.booked_on)}"
    )
    kb = InlineKeyboardBuilder()
    kb.button(text="🏷 Категория", callback_data=Act(a="qcats", t=t.id))
    if t.account.code in ("ru", "cash_myr"):  # a coin is always typed out, a card row came from the bank
        other = "💵 Это RM наличными" if t.account.currency == "RUB" else "₽ Это рубли"
        kb.button(text=other, callback_data=Act(a="cur", t=t.id))
    kb.button(text="🗑 Удалить", callback_data=Act(a="del", t=t.id))
    return text, fit_rows(kb)


async def send_next_review(message: Message, session: AsyncSession, state: FSMContext) -> None:
    queue = await ledger.review_queue(session, limit=1)
    if not queue:
        later = await ledger.snoozed_count(session)
        text = "🎉 Всё разобрано." + (f" Отложенных {later} — вернутся через неделю." if later else "")
        kb = InlineKeyboardBuilder()
        if later:
            kb.button(text=f"⏪ Разобрать отложенные ({later})", callback_data=Act(a="later", t=0))
        await message.answer(text, reply_markup=kb.as_markup() if later else None)
        return
    text, markup = await review_card(session, queue[0], await ledger.review_count(session))
    await message.answer(text, reply_markup=markup)


async def load_txn(session: AsyncSession, txn_id: int) -> Transaction | None:
    return await session.scalar(select(Transaction).where(Transaction.id == txn_id, Transaction.deleted_at.is_(None)))


# --- commands ----------------------------------------------------------------


@router.message(CommandStart())
@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(HELP)


@router.message(Command("week"))
async def cmd_week(message: Message, session: AsyncSession) -> None:
    await message.answer(await reports.current_week_report(session))


@router.message(Command("lastweek"))
async def cmd_lastweek(message: Message, session: AsyncSession) -> None:
    await message.answer(await reports.week_report(session))


@router.message(Command("month"))
async def cmd_month(message: Message, session: AsyncSession, command: CommandObject) -> None:
    day = None
    if command.args:
        try:
            requested = date.fromisoformat(command.args.strip()[:7] + "-01")
        except ValueError:
            await message.answer("Формат: /month 2026-08 — месяц, который закончился в августе")
            return
        day = cycle_ending_in(requested.year, requested.month)[0]
    await message.answer(await reports.month_report(session, day))


@router.message(Command("review"))
async def cmd_review(message: Message, session: AsyncSession, state: FSMContext) -> None:
    await state.set_state(None)
    await send_next_review(message, session, state)


@router.message(Command("rf"))
async def cmd_rf(message: Message, session: AsyncSession, command: CommandObject) -> None:
    parsed = parse_rf_command(command.args or "")
    if not parsed:
        await message.answer(
            "Формат: <code>/rf 1000 21500</code> — RM ушло, ₽ пришло. Добавь дату <code>24.09</code>, если перевод был раньше, "
            "и <code>tng</code>, если платил с TNG."
        )
        return
    myr, rub, account, day = parsed
    duplicate = await ledger.find_fx_duplicate(session, myr, account, day)
    if duplicate:
        out, rub_leg = duplicate
        got = f" → {fmt_money(rub_leg.amount, 'RUB')}" if rub_leg else ""
        await message.answer(
            f"🇷🇺 Такой перевод уже записан: {fmt_money(-out.amount)}{got}, {fmt_day(out.booked_on)}. Ничего не добавил.\n"
            "Если это другой обмен на ту же сумму, укажи другую дату."
        )
        return
    transfer = await ledger.record_fx(session, myr, rub, account, day)
    if any(leg.status == PENDING for leg in await ledger.transfer_legs(session, transfer.id)):
        tail = f"Когда придёт выписка {'TNG' if account == 'tng' else 'Maybank'}, перевод подтвердится сам."
    else:
        tail = "Нашёл этот перевод в выписке и привязал."
    await message.answer(f"🇷🇺 Записал: {fmt_money(myr)} → {fmt_money(rub, 'RUB')} (курс {transfer.rate:.2f}).\n{tail}")


@router.message(Command("balance"))
async def cmd_balance(message: Message, session: AsyncSession) -> None:
    lines = ["💼 <b>Остатки</b>"]
    for name, currency, amount, as_of in await reports.balances(session):
        suffix = f" (на {fmt_day(as_of)})" if as_of else ""
        lines.append(f"{escape(name)}: {fmt_money(amount, currency)}{suffix}")
    rate = await reports.latest_fx_rate(session)
    if rate:
        lines += ["", f"🇷🇺 Курс по твоим переводам за 90 дней: {rate} ₽ за RM"]
    await message.answer("\n".join(lines))


@router.message(Command("setbalance"))
async def cmd_setbalance(message: Message, session: AsyncSession, command: CommandObject) -> None:
    words = (command.args or "").split()
    if len(words) == 2 and words[0].lower() in STATEMENT_ACCOUNT_WORDS:
        try:
            amount = to_decimal(words[1])
        except ValueError:
            await message.answer("Формат: <code>/setbalance maybank 4210</code>")
            return
        await ledger.check_balance(session, STATEMENT_ACCOUNT_WORDS[words[0].lower()], amount)
        account = await ledger.get_account(session, STATEMENT_ACCOUNT_WORDS[words[0].lower()])
        await message.answer(
            f"{escape(account.name)}: {fmt_money(amount)} на сегодня. Покупки после этого добавятся сверху, "
            "а когда придёт выписка, остаток возьмётся из неё."
        )
        return
    coin = parse_crypto_amount(command.args or "")
    if coin and coin[1] and coin[1] not in NOT_COINS:
        target, ticker = coin
        try:
            wallet = await ledger.get_or_create_crypto(session, ticker)
        except ledger.LedgerError as exc:
            await message.answer(f"Не получилось: {escape(str(exc))}")
            return
        txn = await ledger.set_balance(session, wallet.code, target)
        note = f"корректировка {fmt_money(txn.amount, ticker, signed=True)}" if txn else "уже совпадает"
        await message.answer(f"{escape(wallet.name)}: {fmt_money(target, ticker)} ({note}).")
        return
    parts = (command.args or "").split()
    account = "cash_myr" if parts and parts[0].lower() in ("cash", "rm", "нал") else "ru"
    try:
        target = to_decimal(parts[-1])
    except (ValueError, IndexError):
        await message.answer("Формат: <code>/setbalance 20000</code> (рубли) или <code>/setbalance cash 150</code>")
        return
    txn = await ledger.set_balance(session, account, target)
    currency = "MYR" if account == "cash_myr" else "RUB"
    note = f"корректировка {fmt_money(txn.amount, currency, signed=True)}" if txn else "уже совпадает"
    await message.answer(f"Остаток: {fmt_money(target, currency)} ({note}).")


@router.message(Command("pot", "pots"))
async def cmd_pot(message: Message, session: AsyncSession, command: CommandObject) -> None:
    """/pot — list; /pot Holiday 15000 — set the value (creates the pot if needed);
    /pot Reserve резерв | цель — whether the budget may spend it."""
    args = (command.args or "").strip()
    name, _, role = args.rpartition(" ")
    if name and role.lower() in POT_ROLES:
        pot = await ledger.get_or_create_pot(session, name)
        pot.spendable = POT_ROLES[role.lower()]
        await session.commit()
        await message.answer(
            f"🐷 {escape(pot.name)}: "
            + ("резерв — бюджет может на неё рассчитывать." if pot.spendable else "цель — бюджет её не трогает.")
        )
        return
    if args:
        name, _, last = args.rpartition(" ")
        try:
            target = to_decimal(last)
        except ValueError:
            name, target = args, None
        if not name:
            await message.answer("Формат: <code>/pot Holiday 15000</code> — название и сколько там сейчас.")
            return
        if target is None:
            pot = await ledger.get_or_create_pot(session, name)
            await session.commit()
        else:
            pot, _ = await ledger.set_pot_balance(session, name, target)
            await message.answer(f"🐷 {escape(pot.name)}: {fmt_money(target)}")
            return
    lines = ["🐷 <b>Копилки</b>"]
    balances = {name: amount for name, _, amount, _ in await reports.balances(session)}
    pots = await ledger.pots(session)
    for pot in pots:
        role = " · резерв" if pot.spendable else ""
        lines.append(f"{escape(pot.name)} — {fmt_money(balances.get(pot.name, 0))}{role}")
    if not pots:
        lines.append("Пока нет. Переводы Maybank «FUND …» создают их сами.")
    lines += ["", "Задать сумму: <code>/pot Holiday 15000</code>. Можно тратить в бюджете: <code>/pot Reserve резерв</code>, нельзя: <code>/pot Holiday цель</code>"]
    await message.answer("\n".join(lines))


@router.message(Command("web"))
async def cmd_web(message: Message) -> None:
    if not login_enabled():
        await message.answer("Дашборд выключен: на сервере не задан SECRET_KEY.")
        return
    await message.answer(f'🔗 <a href="{magic_link()}">Открыть дашборд</a> — ссылка действует 15 минут.')


class RuleCb(CallbackData, prefix="r"):
    r: int


def fix_view(rows: list[Transaction]) -> tuple[str, InlineKeyboardMarkup]:
    lines, kb = [], InlineKeyboardBuilder()
    for n, t in enumerate(rows, 1):
        category = t.category.label if t.category else "без категории"
        lines.append(f"{n}. {txn_line(t)}\n{escape(category)}")
        amount = fmt_money(t.amount, t.account.currency, signed=True)
        expense = t.kind == EXPENSE or t.amount < 0  # a refund sits in an expense category
        kb.button(text=f"{n}. {amount} · {fmt_day(t.booked_on)}", callback_data=Act(a="cats", t=t.id, x=int(expense)))
    return "🏷 Какую поправить?\n\n" + "\n\n".join(lines), fit_rows(kb)


@router.message(Command("fix"))
async def cmd_fix(message: Message, session: AsyncSession, command: CommandObject) -> None:
    query = (command.args or "").strip()
    rows = await ledger.find_transactions(session, query)
    if not rows:
        await message.answer(
            f"Не нашёл «{escape(query)}». Ищу по описанию из выписки, например <code>/fix grab</code>."
            if query else "Пока нечего поправлять."
        )
        return
    text, markup = fix_view(rows)
    await message.answer(text, reply_markup=markup)


async def rules_view(session: AsyncSession) -> tuple[str, InlineKeyboardMarkup | None]:
    rules = await ledger.user_rules(session)
    if not rules:
        return "Своих правил пока нет — они появляются, когда размечаешь транзакции в /review.", None
    lines = ["📌 <b>Мои правила</b> (новые сверху)", ""]
    kb = InlineKeyboardBuilder()
    for i, rule in enumerate(rules, 1):
        target = rule.category.label if rule.category else rule.kind or "—"
        lines.append(f"{i}. «{escape(rule_label(rule.pattern))}» → {escape(target)}")
        kb.button(text=f"🗑 {i}", callback_data=RuleCb(r=rule.id))
    kb.adjust(5)
    lines += ["", "🗑 — удалить правило (уже размеченные транзакции не меняются)"]
    return "\n".join(lines), kb.as_markup()


@router.message(Command("rules"))
async def cmd_rules(message: Message, session: AsyncSession) -> None:
    text, markup = await rules_view(session)
    await message.answer(text, reply_markup=markup)


@router.callback_query(RuleCb.filter())
async def cb_rule_delete(query: CallbackQuery, callback_data: RuleCb, session: AsyncSession) -> None:
    await ledger.delete_user_rule(session, callback_data.r)
    text, markup = await rules_view(session)
    await query.message.edit_text(text, reply_markup=markup)
    await query.answer("Удалил")


@router.message(Command("undo"))
async def cmd_undo(message: Message, session: AsyncSession) -> None:
    txn = await ledger.last_manual(session)
    if txn is None:
        await message.answer("Нечего отменять.")
        return
    await ledger.soft_delete(session, txn)
    await message.answer(f"🗑 Удалил: {fmt_money(txn.amount, txn.account.currency, signed=True)} · {escape(txn.description)}")


# --- payment screenshots --------------------------------------------------------


class Shot(CallbackData, prefix="s"):
    a: str  # acc: record on account x; new: record although a matching row exists
    x: int = 0


def screenshot_card(t: Transaction, cats: list[Category]) -> tuple[str, InlineKeyboardMarkup]:
    note = f" ({escape(t.note)})" if t.note else ""
    text = f"📸 Записал: <b>{fmt_money(t.amount, t.account.currency, signed=True)}</b> · {escape(t.description)}{note}\n"
    where = f"{escape(t.account.name)} · {fmt_day(t.booked_on)}"
    kb = InlineKeyboardBuilder()
    if t.review_reason in USER_FACING_REVIEW:
        text += f"{where}\n\nКакая категория?"
        category_grid(kb, cats, t.id, "cat", 0)
        kb.button(text="🗑 Не записывать", callback_data=Act(a="del", t=t.id))
        kb.adjust(1)
        return text, kb.as_markup()
    text += f"{escape(t.category.label if t.category else '—')} · {where}"
    if t.status == PENDING:
        text += "\nКогда придёт выписка, запись подтвердится без дубля."
    kb.button(text="🏷 Категория", callback_data=Act(a="qcats", t=t.id))
    kb.button(text="🗑 Удалить", callback_data=Act(a="del", t=t.id))
    return text, fit_rows(kb)


async def record_screenshot(
    session: AsyncSession, payment: Payment, account_code: str, force: bool = False
) -> tuple[str, InlineKeyboardMarkup]:
    """Store the payment as an expense, unless the ledger already has it."""
    tz = get_settings().tz
    at = payment.occurred_at.replace(tzinfo=tz) if payment.occurred_at else None
    day = payment.occurred_at.date() if payment.occurred_at else today()
    cats = await categories_of(session, EXPENSE)
    if not force:
        existing = await ledger.find_recorded(session, account_code, -payment.amount, day, at)
        if existing is not None:
            kb = InlineKeyboardBuilder()
            text = f"📸 Этот платёж уже есть:\n{txn_line(existing)}"
            if existing.review_reason in USER_FACING_REVIEW:
                text += "\n\nКакая категория?"
                category_grid(kb, cats, existing.id, "cat", 0)
            else:
                text += f"\n{escape(existing.category.label if existing.category else '—')}"
            kb.button(text="➕ Нет, это другой платёж", callback_data=Shot(a="new"))
            kb.adjust(1)
            return text, kb.as_markup()
    account = await ledger.get_account(session, account_code)
    txn = await ledger.add_manual(
        session,
        account_code,
        -payment.amount,
        payment.counterparty,
        day,
        # a bank or wallet row is confirmed by its statement later, cash is final
        status=PENDING if account.kind in ledger.STATEMENT_ACCOUNT_KINDS else POSTED,
        source="screenshot",
        review=True,
        booked_at=at,
        note=payment.remark or None,
    )
    if payment.to_person and txn.review_reason == REVIEW_UNCATEGORIZED:
        # a person may be paid once for anything: the category is remembered only on request
        txn.review_reason = REVIEW_P2P
        await session.commit()
    return screenshot_card(txn, cats)


@router.message(F.photo | (F.document & F.document.mime_type.startswith("image/")))
async def on_screenshot(message: Message, session: AsyncSession, bot: Bot, state: FSMContext) -> None:
    if not screenshot.enabled():
        await message.answer("📸 Скрины пока не читаю: на сервере не задан ANTHROPIC_API_KEY.")
        return
    file = message.photo[-1] if message.photo else message.document
    if file.file_size and file.file_size > MAX_FILE_BYTES:
        await message.answer("Картинка больше 15 МБ — пришли скрин поменьше.")
        return
    status = await message.answer("🔎 Смотрю скрин…")
    data = (await bot.download(file)).read()
    try:
        payment = await screenshot.read_payment(data, "image/jpeg" if message.photo else message.document.mime_type)
    except ScreenshotError as exc:
        await status.edit_text(f"📸 Не записал: {escape(str(exc))}.")
        return
    shown = f"{fmt_money(payment.amount, payment.currency)} · {escape(payment.counterparty)}"
    if payment.incoming:
        await status.edit_text(f"📸 {shown} — это поступление, его возьму из выписки.")
        return
    if payment.currency != "MYR":
        await status.edit_text(f"📸 {shown} — со скринов записываю только ринггиты.")
        return
    if names_owner(payment.counterparty):
        await status.edit_text(f"📸 {shown} — перевод самому себе, склею его по выпискам.")
        return
    await state.update_data(shot=payment.to_state())
    if payment.account is None:
        kb = InlineKeyboardBuilder()
        for code in ("maybank", "tng", "cash_myr"):
            acc = await ledger.get_account(session, code)
            kb.button(text=acc.name, callback_data=Shot(a="acc", x=acc.id))
        await status.edit_text(f"📸 {shown} — с какого счёта платил?", reply_markup=fit_rows(kb))
        return
    text, markup = await record_screenshot(session, payment, payment.account)
    await status.edit_text(text, reply_markup=markup)


@router.callback_query(Shot.filter())
async def cb_screenshot(query: CallbackQuery, callback_data: Shot, session: AsyncSession, state: FSMContext) -> None:
    saved = (await state.get_data()).get("shot")
    if not saved:
        await query.answer("Скрин устарел — пришли его ещё раз", show_alert=True)
        return
    payment = Payment.from_state(saved)
    if callback_data.a == "acc":
        account = await session.get(Account, callback_data.x)
        if account is None:
            await query.answer("Счёт не найден")
            return
        payment.account = account.code
        await state.update_data(shot=payment.to_state())
    if payment.account is None:
        await query.answer()
        return
    text, markup = await record_screenshot(session, payment, payment.account, force=callback_data.a == "new")
    if callback_data.a == "new":
        await state.update_data(shot=None)  # recorded: a second tap must not add it again
    await query.message.edit_text(text, reply_markup=markup)
    await query.answer()


# --- files ---------------------------------------------------------------------


@router.message(F.document)
async def on_document(message: Message, session: AsyncSession, bot: Bot) -> None:
    doc = message.document
    if doc.file_size and doc.file_size > MAX_FILE_BYTES:
        await message.answer("Файл больше 15 МБ — это точно выписка?")
        return
    buffer = await bot.download(doc)
    data = buffer.read()
    try:
        statement = parse_file(data, doc.file_name or "", get_settings().pdf_passwords)
    except ParseError as exc:
        await message.answer(f"Не смог разобрать файл: {escape(str(exc))}")
        return
    result = await ledger.import_statement(
        session, statement, origin="telegram", filename=doc.file_name, file_bytes=data
    )
    kb = InlineKeyboardBuilder()
    if result.to_review:
        kb.button(text=f"📝 Разобрать ({result.to_review})", callback_data=Act(a="start", t=0))
    await message.answer(reports.format_import(result), reply_markup=kb.as_markup() if result.to_review else None)


# --- review / entry callbacks ----------------------------------------------------


@router.callback_query(Act.filter(F.a == "start"))
async def cb_start(query: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    await query.answer()
    await send_next_review(query.message, session, state)


@router.callback_query(Act.filter(F.a == "later"))
async def cb_unsnooze(query: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    await ledger.unsnooze_all(session)
    await query.message.edit_reply_markup(reply_markup=None)
    await query.answer()
    await send_next_review(query.message, session, state)


@router.callback_query(Act.filter())
async def cb_action(query: CallbackQuery, callback_data: Act, session: AsyncSession, state: FSMContext) -> None:
    txn = await load_txn(session, callback_data.t)
    if txn is None:
        await query.answer("Транзакция не найдена — возможно, уже удалена")
        return
    a, in_review = callback_data.a, callback_data.m == 1
    done: str | None = None
    markup = None

    if a == "skip":
        await ledger.snooze(session, txn)
        done = f"⏭ Отложил на неделю\n{txn_line(txn)}"

    elif a in ("cats", "qcats"):
        kind = EXPENSE if (txn.amount < 0 or callback_data.x == 1) else INCOME
        kb = InlineKeyboardBuilder()
        cats = await categories_of(session, kind)
        category_grid(kb, cats, txn.id, "cat" if a == "cats" else "qcat", callback_data.m)
        kb.adjust(1)
        await query.message.edit_reply_markup(reply_markup=kb.as_markup())
        await query.answer()
        return

    elif a in ("cat", "qcat"):
        category = await ledger.get_category(session, callback_data.x)
        was = txn.review_reason
        # An unknown merchant is remembered right away (undo is one tap); a payment
        # to a person is only remembered on request — it may be a one-off.
        auto = a == "cat" and was == REVIEW_UNCATEGORIZED
        rule, updated = await ledger.apply_category(session, txn, category, remember=auto)
        if a == "qcat":
            await session.refresh(txn, ["category"])
            text, markup = entry_card(txn)
            await query.message.edit_text(text, reply_markup=markup)
            await query.answer("Готово")
            return
        done = f"✅ {escape(category.label)}\n{txn_line(txn)}"
        kb = InlineKeyboardBuilder()
        if rule is not None:
            extra = f", ещё {updated} таких уже разобрал" if updated else ""
            done += f"\n\n📌 Запомнил «{escape(rule_label(rule.pattern))}» → дальше сюда само{extra}"
            kb.button(text="↩️ Не запоминать", callback_data=Act(a="unrule", t=txn.id, x=rule.id))
        else:
            # a payment to a person may be a one-off; a fix may go against a saved rule
            old_rule = await ledger.user_rule_for(session, txn)
            if was in (REVIEW_P2P, REVIEW_P2P_IN) or (old_rule and old_rule.category_id != category.id):
                label = rule_label(suggest_pattern(txn.description))
                kb.button(text=f"📌 Всегда так для «{label[:30]}»", callback_data=Act(a="rem", t=txn.id, x=category.id))
        # a wrong tap is fixed right here
        kb.button(
            text="🏷 Другая категория", callback_data=Act(a="cats", t=txn.id, x=int(category.kind == EXPENSE))
        )
        kb.adjust(1)
        markup = kb.as_markup()

    elif a == "rem":
        category = await ledger.get_category(session, callback_data.x)
        rule, updated = await ledger.apply_category(session, txn, category, remember=True)
        extra = f", ещё {updated} таких уже разобрал" if updated else ""
        await query.message.edit_text(
            f"✅ {escape(category.label)}\n{txn_line(txn)}\n\n📌 Запомнил «{escape(rule_label(rule.pattern))}»{extra}"
        )
        await query.answer("Запомнил")
        return

    elif a == "unrule":
        rule = await ledger.delete_user_rule(session, callback_data.x)
        await query.message.edit_text(
            f"✅ {escape(txn.category.label if txn.category else '')} — только эта транзакция\n{txn_line(txn)}"
            + ("\n\nПравило удалено; уже разобранные по нему остались как есть." if rule else "")
        )
        await query.answer("Не запоминаю")
        return

    elif a == "keep":
        await ledger.dismiss_review(session, txn)
        done = f"✔️ Оставил как есть\n{txn_line(txn)}"

    elif a == "own":
        accounts = await ledger.transfer_targets(session, txn)
        pots = await ledger.pots(session)
        if accounts or pots:
            kb = InlineKeyboardBuilder()
            for acc in accounts:
                icon = "📱" if acc.kind == "ewallet" else "🏦"
                kb.button(text=f"{icon} {acc.name}", callback_data=Act(a="ownacc", t=txn.id, x=acc.id, m=callback_data.m))
            if txn.amount < 0 and txn.account.currency == "MYR":
                # ringgit out, rubles in: the RF transfer, asks how many ₽ arrived
                ru = await ledger.get_account(session, "ru")
                kb.button(text=f"🇷🇺 {ru.name}", callback_data=Act(a="rf", t=txn.id, m=callback_data.m))
            if txn.amount < 0 and txn.account.kind != CRYPTO:
                kb.button(text="🪙 Криптокошелёк", callback_data=Act(a="crypto", t=txn.id, m=callback_data.m))
            for pot in pots:
                kb.button(text=f"🐷 {pot.name}", callback_data=Act(a="topot", t=txn.id, x=pot.id, m=callback_data.m))
            kb.button(text="↔️ Другой мой счёт", callback_data=Act(a="ownx", t=txn.id, m=callback_data.m))
            await query.message.edit_reply_markup(reply_markup=fit_rows(kb))
            await query.answer("С какого счёта?" if txn.amount > 0 else "На какой счёт?")
            return
        paired = await ledger.mark_internal(session, txn)
        done = f"↔️ Перевод между моими счетами{' — пару нашёл' if paired else ''}\n{txn_line(txn)}"

    elif a == "ownx":
        paired = await ledger.mark_internal(session, txn)
        done = f"↔️ Перевод между моими счетами{' — пару нашёл' if paired else ''}\n{txn_line(txn)}"

    elif a == "ownacc":
        account = await session.get(Account, callback_data.x)
        if account is None:
            await query.answer("Счёт не найден")
            return
        paired = await ledger.mark_internal(session, txn, account)
        name = escape(account.name)
        where = f"с {name}" if txn.amount > 0 else f"на {name}"
        status = "пару нашёл" if paired else f"склею, когда придёт выписка {name}"
        done = f"↔️ Перевод {where} — {status}\n{txn_line(txn)}"

    elif a == "topot":
        pot = await session.get(Account, callback_data.x)
        if pot is None:
            await query.answer("Копилка не найдена")
            return
        await ledger.move_to_pot(session, txn, pot)
        done = f"🐷 {'Из' if txn.amount > 0 else 'В'} «{escape(pot.name)}»\n{txn_line(txn)}"

    elif a == "rf":
        await state.set_state(RfAmount.waiting)
        await state.update_data(rf_txn=txn.id, rf_review=in_review)
        await query.message.edit_reply_markup(reply_markup=None)
        await query.message.answer(
            f"🇷🇺 Сколько ₽ пришло за {fmt_money(-txn.amount, txn.account.currency)}? Напиши число, например <code>21500</code>."
        )
        await query.answer()
        return

    elif a == "crypto":
        await state.set_state(CryptoAmount.waiting)
        await state.update_data(crypto_txn=txn.id, crypto_review=in_review)
        await query.message.edit_reply_markup(reply_markup=None)
        coin = await ledger.last_crypto_currency(session)
        await query.message.answer(
            f"🪙 Сколько пришло на криптокошелёк за {fmt_money(-txn.amount, txn.account.currency)}? "
            f"Напиши сумму и монету, например <code>60 {coin or 'USDT'}</code>."
            + (f" Без монеты — {coin}." if coin else "")
        )
        await query.answer()
        return

    elif a == "rubexp":
        expenses = await ledger.rub_expenses_to_pay_back(session, txn)
        if not expenses:
            await ask_rubles(query, state, txn, in_review, "Рублёвых расходов за 3 месяца не нашёл. ")
            return
        kb = InlineKeyboardBuilder()
        for e in expenses:
            label = f"{fmt_money(e.amount, 'RUB', signed=True)} · {e.description[:24]} · {fmt_day(e.booked_on)}"
            kb.button(text=label, callback_data=Act(a="rubpick", t=txn.id, x=e.id, m=callback_data.m))
        kb.button(text="✍️ Его нет в записях — ввести ₽", callback_data=Act(a="rubtr", t=txn.id, m=callback_data.m))
        kb.adjust(1)
        await query.message.edit_reply_markup(reply_markup=kb.as_markup())
        await query.answer("За какой рублёвый расход?")
        return

    elif a == "rubpick":
        expense = await load_txn(session, callback_data.x)
        if expense is None:
            await query.answer("Рублёвый расход не найден")
            return
        try:
            await ledger.pay_back_rub_expense(session, txn, expense)
        except ledger.LedgerError as exc:
            await query.answer(str(exc), show_alert=True)
            return
        rate = -expense.amount / txn.amount
        done = (
            f"↩️ Возврат за «{escape(expense.description)}» ({fmt_money(-expense.amount, 'RUB')}, {rate:.2f} ₽ за RM) — "
            f"ни расход, ни доход\n{txn_line(txn)}"
        )

    elif a == "rubtr":
        await ask_rubles(query, state, txn, in_review)
        return

    elif a == "merge":
        pending = await load_txn(session, callback_data.x)
        if pending is None:
            await query.answer("Ожидающий перевод не найден")
            return
        await ledger.merge_pending_into(session, pending, txn)
        await session.refresh(txn, ["transfer"])
        rate = f" (курс {txn.transfer.rate:.2f})" if txn.transfer and txn.transfer.rate else ""
        done = f"🔗 Привязал к РФ-переводу{rate}\n{txn_line(txn)}"

    elif a == "cur":
        target = "cash_myr" if txn.account.currency == "RUB" else "ru"
        txn.account_id = (await ledger.get_account(session, target)).id
        await session.commit()
        await session.refresh(txn, ["account"])
        text, markup = entry_card(txn)
        await query.message.edit_text(text, reply_markup=markup)
        await query.answer()
        return

    elif a == "del":
        await ledger.soft_delete(session, txn)
        done = f"🗑 Удалено\n{txn_line(txn)}"

    else:
        await query.answer()
        return

    await query.message.edit_text(done, reply_markup=markup)
    await query.answer()
    if in_review:
        await send_next_review(query.message, session, state)


@router.message(StateFilter(RfAmount.waiting), F.text)
async def on_rf_amount(message: Message, session: AsyncSession, state: FSMContext) -> None:
    data = await state.get_data()
    try:
        rub = to_decimal(message.text.replace(" ", "").replace(",", "."))
    except ValueError:
        await message.answer("Нужно число в рублях, например <code>21500</code>. /review — выйти.")
        return
    txn = await load_txn(session, data.get("rf_txn", 0))
    await state.set_state(None)
    if txn is None:
        await message.answer("Транзакция пропала — начни заново: /review")
        return
    try:
        transfer = await ledger.mark_fx(session, txn, rub)
    except ledger.LedgerError as exc:
        await message.answer(f"Не получилось: {escape(str(exc))}")
        return
    await message.answer(
        f"🇷🇺 {fmt_money(-txn.amount, txn.account.currency)} → {fmt_money(rub, 'RUB')} (курс {transfer.rate:.2f})"
    )
    if data.get("rf_review"):
        await send_next_review(message, session, state)


async def ask_rubles(query: CallbackQuery, state: FSMContext, txn: Transaction, in_review: bool, prefix: str = "") -> None:
    await state.set_state(RubBack.waiting)
    await state.update_data(rubback_txn=txn.id, rubback_review=in_review)
    await query.message.edit_reply_markup(reply_markup=None)
    await query.message.answer(
        f"🔁 {prefix}Сколько ₽ ты отдал за {fmt_money(txn.amount, txn.account.currency)}? "
        "Напиши число, например <code>30000</code>."
    )
    await query.answer()


@router.message(StateFilter(RubBack.waiting), F.text)
async def on_rub_back_amount(message: Message, session: AsyncSession, state: FSMContext) -> None:
    data = await state.get_data()
    try:
        rub = to_decimal(message.text.replace(" ", "").replace(",", "."))
    except ValueError:
        await message.answer("Нужно число в рублях, например <code>30000</code>. /review — выйти.")
        return
    txn = await load_txn(session, data.get("rubback_txn", 0))
    await state.set_state(None)
    if txn is None:
        await message.answer("Транзакция пропала — начни заново: /review")
        return
    try:
        await ledger.pay_back_rubles(session, txn, rub)
    except ledger.LedgerError as exc:
        await message.answer(f"Не получилось: {escape(str(exc))}")
        return
    await message.answer(
        f"🔁 {fmt_money(rub, 'RUB')} → {fmt_money(txn.amount, txn.account.currency)} ({rub / txn.amount:.2f} ₽ за RM) — не доход"
    )
    if data.get("rubback_review"):
        await send_next_review(message, session, state)


@router.message(StateFilter(CryptoAmount.waiting), F.text)
async def on_crypto_amount(message: Message, session: AsyncSession, state: FSMContext) -> None:
    data = await state.get_data()
    parsed = parse_crypto_amount(message.text)
    coin = (parsed[1] if parsed else None) or await ledger.last_crypto_currency(session)
    if not parsed or not parsed[0] or not coin:
        await message.answer("Нужны сумма и монета, например <code>60 USDT</code> или <code>0.0012 BTC</code>. /review — выйти.")
        return
    txn = await load_txn(session, data.get("crypto_txn", 0))
    await state.set_state(None)
    if txn is None:
        await message.answer("Транзакция пропала — начни заново: /review")
        return
    amount = parsed[0]
    try:
        await ledger.mark_crypto(session, txn, amount, coin)
    except ledger.LedgerError as exc:
        await message.answer(f"Не получилось: {escape(str(exc))}")
        return
    paid = -txn.amount
    price = fmt_money(paid / amount, txn.account.currency)
    await message.answer(
        f"🪙 {fmt_money(paid, txn.account.currency)} → {fmt_money(amount, coin)} ({price} за {coin})"
    )
    if data.get("crypto_review"):
        await send_next_review(message, session, state)


# --- budget: forecast, plan, /ask ------------------------------------------------


class PlanCb(CallbackData, prefix="p"):
    a: str  # done / cancel: a plan item; add: the /ask proposal; stop: end the /ask talk
    p: int = 0


class AskBudget(StatesGroup):
    active = State()


# a text sent later than this after the last /ask answer is an entry again, not a question
ASK_IDLE = timedelta(minutes=30)

PLAN_HELP = (
    "Добавить: <code>/plan 1800 отель Бали 25.10</code>, <code>/plan 1600 экскурсия ноябрь</code>, "
    "<code>/plan 2000 РФ ежемесячно с 03.10 #перевод</code>, доход — с плюсом: <code>/plan +3000 бонус 12.2026</code>.\n"
    "✅ — оплачено, ✖️ — не будет."
)


@router.message(Command("budget"))
async def cmd_budget(message: Message, session: AsyncSession) -> None:
    await message.answer(budget.format_forecast(await budget.forecast(session)))


async def plans_view(session: AsyncSession) -> tuple[str, InlineKeyboardMarkup | None]:
    items = await budget.open_plans(session)
    lines = ["🗓 <b>План</b>"]
    kb = InlineKeyboardBuilder()
    for i, item in enumerate(items, 1):
        lines.append(f"{i}. {budget.plan_line(item)}")
        kb.button(text=f"✅ {i}", callback_data=PlanCb(a="done", p=item.id))
        kb.button(text=f"✖️ {i}", callback_data=PlanCb(a="cancel", p=item.id))
    if not items:
        lines.append("Пока пусто.")
    lines += ["", PLAN_HELP]
    kb.adjust(4)
    return "\n".join(lines), kb.as_markup() if items else None


async def plan_impact(session: AsyncSession, due_on: date) -> str:
    """How the cycle of a new plan item looks with it."""
    f = await budget.forecast(session)
    cycle = next((c for c in f.cycles if c.start <= due_on <= c.end), f.cycles[0] if due_on < f.cycles[0].start else None)
    if cycle is None:
        return f"Это дальше прогноза ({len(f.cycles)} мес.), учту, когда дойдём."
    warn = " ⚠️" if cycle.low < 0 else ""
    return f"В {cycle.label}: к концу месяца {fmt_money(cycle.closing)}, перед зарплатой {fmt_money(cycle.low)}{warn}. /budget"


@router.message(Command("plan", "plans"))
async def cmd_plan(message: Message, session: AsyncSession, command: CommandObject) -> None:
    args = (command.args or "").strip()
    if not args:
        text, markup = await plans_view(session)
        await message.answer(text, reply_markup=markup)
        return
    draft = budget.parse_plan(args)
    if draft is None:
        await message.answer(f"Нужны сумма и когда. {PLAN_HELP}")
        return
    category = None
    if draft.category_tag and draft.kind == EXPENSE:
        category = await ledger.find_category(session, draft.category_tag, EXPENSE)
    try:
        item = await budget.add_plan(
            session, draft.title, draft.amount, draft.due_on, draft.kind,
            category.code if category else None, draft.repeat_months,
        )
    except ledger.LedgerError as exc:
        await message.answer(f"Не получилось: {escape(str(exc))}")
        return
    await message.answer(f"🗓 Заложил: {budget.plan_line(item)}\n{await plan_impact(session, item.due_on)}")


async def answer_question(message: Message, session: AsyncSession, state: FSMContext, question: str) -> None:
    status = await message.answer("🧮 Считаю…")
    data = await state.get_data()
    try:
        answer = await advisor.ask(session, question, data.get("ask_history"))
    except advisor.AdvisorError as exc:
        await status.edit_text(f"🧮 Не получилось: {escape(str(exc))}.")
        return
    await state.update_data(
        ask_history=answer.history,
        ask_at=datetime.now(timezone.utc).isoformat(),
        ask_proposal=answer.proposal.to_state() if answer.proposal else None,
    )
    kb = InlineKeyboardBuilder()
    if answer.proposal:
        p = answer.proposal
        kb.button(text=f"📌 В план: {p.title[:30]} {fmt_money(p.amount)} {fmt_day(p.due_on)}", callback_data=PlanCb(a="add"))
    kb.button(text="Закончить", callback_data=PlanCb(a="stop"))
    kb.adjust(1)
    try:
        await status.edit_text(answer.text, reply_markup=kb.as_markup())
    except TelegramBadRequest:  # the model's markup did not parse: show it as plain text
        await status.edit_text(escape(answer.text), reply_markup=kb.as_markup())


@router.message(Command("ask"))
async def cmd_ask(message: Message, session: AsyncSession, state: FSMContext, command: CommandObject) -> None:
    if not advisor.enabled():
        await message.answer("🧮 Анализатор выключен: на сервере не задан ANTHROPIC_API_KEY.")
        return
    await state.set_state(AskBudget.active)
    await state.update_data(ask_history=[], ask_proposal=None, ask_at=datetime.now(timezone.utc).isoformat())
    if command.args:
        await answer_question(message, session, state, command.args.strip())
        return
    await message.answer(
        "🧮 Спрашивай про деньги на ближайшие месяцы, например: «хочу ноутбук за 6000 в декабре — пролезет?», "
        "«потяну переезд за 12 000 в марте?», «сколько могу потратить на подарки в декабре?».\n"
        "Пока идёт разговор, обычный текст — это вопрос. Выйти — /done."
    )


@router.message(Command("done"))
async def cmd_done(message: Message, state: FSMContext) -> None:
    await state.set_state(None)
    await state.update_data(ask_history=None, ask_proposal=None)
    await message.answer("Ок, разговор про бюджет закончен.")


@router.message(StateFilter(AskBudget.active), F.text & ~F.text.startswith("/"))
async def on_ask_text(message: Message, session: AsyncSession, state: FSMContext) -> None:
    last = (await state.get_data()).get("ask_at")
    if last and datetime.now(timezone.utc) - datetime.fromisoformat(last) > ASK_IDLE:
        await state.set_state(None)
        await state.update_data(ask_history=None, ask_proposal=None)
        await on_text(message, session)
        return
    await answer_question(message, session, state, message.text)


@router.callback_query(PlanCb.filter())
async def cb_plan(query: CallbackQuery, callback_data: PlanCb, session: AsyncSession, state: FSMContext) -> None:
    if callback_data.a in ("done", "cancel"):
        try:
            await budget.close_plan(session, callback_data.p, PLAN_DONE if callback_data.a == "done" else PLAN_CANCELLED)
        except ledger.LedgerError as exc:
            await query.answer(str(exc), show_alert=True)
            return
        text, markup = await plans_view(session)
        await query.message.edit_text(text, reply_markup=markup)
        await query.answer("Оплачено" if callback_data.a == "done" else "Убрал из плана")
        return
    if callback_data.a == "stop":
        await state.set_state(None)
        await state.update_data(ask_history=None, ask_proposal=None)
        await query.message.edit_reply_markup(reply_markup=None)
        await query.answer("Разговор закончен")
        return
    saved = (await state.get_data()).get("ask_proposal")
    if not saved:
        await query.answer("Предложение устарело — спроси ещё раз", show_alert=True)
        return
    p = advisor.Proposal.from_state(saved)
    item = await budget.add_plan(session, p.title, -p.amount, p.due_on)
    await state.update_data(ask_proposal=None)  # a second tap must not add it again
    await query.message.edit_reply_markup(reply_markup=None)
    await query.message.answer(f"🗓 Заложил: {budget.plan_line(item)}\n{await plan_impact(session, item.due_on)}")
    await query.answer()


# --- free text: manual entries ---------------------------------------------------


async def _add_entry(session: AsyncSession, entry) -> tuple[Transaction, str | None]:
    """Store one parsed entry; returns the transaction and a warning when the #category did not fit."""
    if entry.currency == "RUB":
        account = "ru"
    elif entry.currency == "MYR":
        account = "cash_myr"
    else:
        account = (await ledger.get_or_create_crypto(session, entry.currency)).code
    category = None
    if entry.category:
        category = await ledger.find_category(session, entry.category, EXPENSE if entry.amount < 0 else INCOME)
    txn = await ledger.add_manual(
        session, account, entry.amount, entry.description, entry.day, category.code if category else None
    )
    warn = None
    if entry.category and (category is None or txn.category_id != category.id):
        warn = f"Категория «#{escape(entry.category)}» не подошла — поставил по правилам"
    return txn, warn


@router.message(F.text & ~F.text.startswith("/"))
async def on_text(message: Message, session: AsyncSession) -> None:
    coins = {*KNOWN_COINS, *await ledger.crypto_tickers(session)}
    lines = [ln for ln in message.text.splitlines() if ln.strip()]
    if len(lines) > 1:  # one entry per line
        done, failed = [], []
        for ln in lines:
            entry = parse_entry(ln, coins)
            if entry is None:
                failed.append(f"• <code>{escape(ln.strip())}</code>")
                continue
            txn, warn = await _add_entry(session, entry)
            row = (
                f"• {fmt_money(txn.amount, txn.account.currency, signed=True)} · {escape(txn.description)} · "
                f"{escape(txn.category.label if txn.category else '—')} · {fmt_day(txn.booked_on)}"
            )
            done.append(row + (f"\n  ⚠️ {warn}" if warn else ""))
        text = f"✅ Записал {len(done)} из {len(lines)}:\n" + "\n".join(done) if done else ""
        if failed:
            text += ("\n\n" if text else "") + "❓ Не понял, эти строки не записал:\n" + "\n".join(failed)
        await message.answer(text)
        return
    entry = parse_entry(message.text, coins)
    if entry is None:
        await message.answer(
            "Не понял. Примеры: <code>1500 rub такси</code>, <code>25 rm обед</code>, <code>15 usdt кофе</code>, "
            "<code>/rf 1000 21500</code>. /help"
        )
        return
    txn, warn = await _add_entry(session, entry)
    text, markup = entry_card(txn)
    if warn:
        text += f"\n\n⚠️ {warn}, поменять: 🏷"
    await message.answer(text, reply_markup=markup)
