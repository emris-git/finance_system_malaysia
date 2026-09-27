"""Bot handlers. Business logic lives in finance.ledger / finance.reports."""

from __future__ import annotations

from datetime import date
from html import escape

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject, CommandStart, StateFilter
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from finance import ledger, reports
from finance.classify import rule_label, suggest_pattern
from finance.config import get_settings
from finance.models import (
    CRYPTO,
    EXPENSE,
    INCOME,
    REVIEW_AWAITING_PAIR,
    REVIEW_P2P,
    REVIEW_P2P_IN,
    REVIEW_UNCATEGORIZED,
    Account,
    Category,
    Transaction,
)
from finance.parsers import MAX_FILE_BYTES, ParseError, parse_file
from finance.quick_entry import KNOWN_COINS, parse_crypto_amount, parse_entry, parse_rf_command
from finance.utils import cycle_ending_in, fmt_day, fmt_money, to_decimal
from finance.web.auth import login_enabled, magic_link

router = Router()

HELP = """💰 <b>Финансовый бот</b>

<b>Выписки</b> — пришли PDF из TNG или PDF/CSV из Maybank: разберу и загружу без дублей.

<b>Рубли и наличные</b> — просто напиши:
• <code>1500₽ такси</code> — расход с российского счёта
• <code>+5000₽ кэшбэк</code> — поступление в рублях
• <code>25 rm обед вчера</code> — наличные ринггиты
• <code>1500₽ 24.09 такси #транспорт</code> — с датой (ДД.ММ или ДД.ММ.ГГГГ) и категорией

<b>Перевод на РФ</b> (отдал ринггиты — получил рубли):
• в /review нажми 🇷🇺 у перевода и напиши, сколько ₽ пришло
• или сразу: <code>/rf 1000 21500</code> (<code>/rf 1000 21500 tng</code> — если платил с TNG)

<b>Вернули ринггитами за рубли</b> (заплатил рублями за кого-то или перевёл ему рубли): в /review у поступления нажми
• ↩️ «Возврат за расход в ₽» и выбери рублёвый расход — он перестанет считаться твоим
• 🔁 «Возврат за перевод в ₽» и напиши, сколько ₽ отдал

<b>Крипта</b>:
• купил (заплатил ринггиты — монеты пришли на кошелёк): в /review нажми 🪙 и напиши, сколько пришло, например <code>60 USDT</code>
• потратил — как с рублями: <code>15 usdt кофе</code>, <code>0.0001 btc 24.09 подписка #подписки</code>, <code>+5 usdt кэшбэк</code>

<b>Отчёты</b>: /week /lastweek /month /balance, дашборд — /web
Остаток рублёвого счёта: <code>/setbalance 20000</code>, криптокошелька: <code>/setbalance usdt 120</code>
Копилки: /pot, сумма — <code>/pot Holiday 15000</code>
Категорию для незнакомого магазина запоминаю сразу — дальше такие ставлю сам. Список и удаление: /rules

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
    if t.account.kind != CRYPTO:  # a coin is always typed out, nothing to switch
        other = "💵 Это RM наличными" if t.account.currency == "RUB" else "₽ Это рубли"
        kb.button(text=other, callback_data=Act(a="cur", t=t.id))
    kb.button(text="🗑 Удалить", callback_data=Act(a="del", t=t.id))
    return text, fit_rows(kb)


async def send_next_review(message: Message, session: AsyncSession, state: FSMContext) -> None:
    queue = await ledger.review_queue(session, limit=1)
    if not queue:
        later = await ledger.snoozed_count(session)
        text = "🎉 Всё разобрано." + (f" Отложенных {later} — вернутся через неделю." if later else "")
        await message.answer(text)
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
        await message.answer("Формат: <code>/rf 1000 21500</code> — RM ушло, ₽ пришло. Добавь <code>tng</code>, если платил с TNG.")
        return
    myr, rub, account = parsed
    transfer = await ledger.record_fx(session, myr, rub, account)
    await message.answer(
        f"🇷🇺 Записал: {fmt_money(myr)} → {fmt_money(rub, 'RUB')} (курс {transfer.rate:.2f}).\n"
        f"Когда придёт выписка {'TNG' if account == 'tng' else 'Maybank'}, перевод подтвердится сам."
    )


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
    """/pot — list; /pot Holiday 15000 — set the value (creates the pot if needed)."""
    args = (command.args or "").strip()
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
        lines.append(f"{escape(pot.name)} — {fmt_money(balances.get(pot.name, 0))}")
    if not pots:
        lines.append("Пока нет. Переводы Maybank «FUND …» создают их сами.")
    lines += ["", "Задать сумму: <code>/pot Holiday 15000</code>"]
    await message.answer("\n".join(lines))


@router.message(Command("web"))
async def cmd_web(message: Message) -> None:
    if not login_enabled():
        await message.answer("Дашборд выключен: на сервере не задан SECRET_KEY.")
        return
    await message.answer(f'🔗 <a href="{magic_link()}">Открыть дашборд</a> — ссылка действует 15 минут.')


class RuleCb(CallbackData, prefix="r"):
    r: int


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
            markup = kb.as_markup()
        elif was in (REVIEW_P2P, REVIEW_P2P_IN):
            label = rule_label(suggest_pattern(txn.description))
            kb.button(text=f"📌 Всегда так для «{label[:30]}»", callback_data=Act(a="rem", t=txn.id, x=category.id))
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


# --- free text: manual entries ---------------------------------------------------


@router.message(F.text & ~F.text.startswith("/"))
async def on_text(message: Message, session: AsyncSession) -> None:
    entry = parse_entry(message.text, {*KNOWN_COINS, *await ledger.crypto_tickers(session)})
    if entry is None:
        await message.answer(
            "Не понял. Примеры: <code>1500₽ такси</code>, <code>25 rm обед</code>, <code>15 usdt кофе</code>, "
            "<code>/rf 1000 21500</code>. /help"
        )
        return
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
    text, markup = entry_card(txn)
    if entry.category and (category is None or txn.category_id != category.id):
        text += f"\n\n⚠️ Категория «#{escape(entry.category)}» не подошла — поставил по правилам, поменять: 🏷"
    await message.answer(text, reply_markup=markup)
