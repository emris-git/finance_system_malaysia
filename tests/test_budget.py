"""Budget forecast, the plan, /budget /plan /ask in the bot and the dashboard API."""

import json
from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace
from urllib.parse import urlparse

import pytest
from sqlalchemy import select

import finance.advisor as advisor
from finance import budget, ledger, reports
from finance.bot.handlers import PlanCb
from finance.config import get_settings
from finance.models import EXPENSE, INCOME, PLAN_DONE, PLAN_OPEN, TRANSFER, Account, PlannedItem
from finance.parsers import ParsedStatement, ParsedTxn
from finance.web.auth import magic_link
from test_api import client  # noqa: F401  (fixture)
from test_bot import bot, press, send  # noqa: F401  (fixture)

NOW = date(2026, 9, 29)  # inside the cycle 26 Sep - 25 Oct
D = Decimal


# --- parsing -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, expected",
    [
        ("1800 отель Бали 25.10", ("отель Бали", D("-1800"), date(2026, 10, 25), EXPENSE, None)),
        ("1600 экскурсия ноябрь", ("экскурсия", D("-1600"), date(2026, 11, 15), EXPENSE, None)),
        ("+3000 бонус 12.2026", ("бонус", D("3000"), date(2026, 12, 15), INCOME, None)),
        ("2000 РФ ежемесячно с 03.10 #перевод", ("РФ", D("-2000"), date(2026, 10, 3), TRANSFER, 1)),
        ("iPhone 17 5500 в январе 2027", ("iPhone 17", D("-5500"), date(2027, 1, 15), EXPENSE, None)),
        ("переезд 12 000 rm март", ("переезд", D("-12000"), date(2027, 3, 15), EXPENSE, None)),  # March passed: next year
        ("1200 страховка 15.01 ежегодно", ("страховка", D("-1200"), date(2027, 1, 15), EXPENSE, 12)),
        ("декор 300 в декабре", ("декор", D("-300"), date(2026, 12, 15), EXPENSE, None)),  # "декор" is not December
    ],
)
def test_parse_plan(text, expected):
    d = budget.parse_plan(text, NOW)
    assert (d.title, d.amount, d.due_on, d.kind, d.repeat_months) == expected


def test_parse_plan_needs_amount_and_date():
    assert budget.parse_plan("ноутбук", NOW) is None
    assert budget.parse_plan("500 ноутбук", NOW) is None


def test_add_months_clips_to_month_end():
    assert budget.add_months(date(2026, 1, 31), 1) == date(2026, 2, 28)
    assert budget.add_months(date(2026, 11, 15), 3) == date(2027, 2, 15)


# --- forecast ------------------------------------------------------------------------


async def three_months(session):
    """Three salary cycles: salary 15 000 on the 24th, rent 3 500 on the 25th, food 2 000,
    one trip; then 500 of food in the current cycle. Maybank ends at 25 000."""
    for end_month in (7, 8, 9):
        start = date(2026, end_month - 1, 26)
        await ledger.add_manual(session, "maybank", D("15000"), "ACME TECH SALARY", date(2026, end_month, 24), "salary")
        await ledger.add_manual(session, "maybank", D("-3500"), "RENT NG SOO LI", date(2026, end_month, 25), "housing")
        await ledger.add_manual(session, "maybank", D("-1000"), "KEDAI MAKAN", start, "food")
        await ledger.add_manual(session, "maybank", D("-1000"), "KEDAI MAKAN", start + timedelta(days=15), "food")
    await ledger.add_manual(session, "maybank", D("-3000"), "TRIP.COM", date(2026, 7, 28), "travel")
    await ledger.add_manual(session, "maybank", D("-500"), "KEDAI MAKAN", date(2026, 9, 27), "food")


async def test_forecast_from_history_and_plan(session):
    await three_months(session)
    await ledger.set_pot_balance(session, "Reserve", D("1000"))
    await ledger.set_pot_balance(session, "Holiday", D("7000"))
    reserve = await session.scalar(select(Account).where(Account.code == "pot_reserve"))
    reserve.spendable = True
    await session.commit()
    await budget.add_plan(session, "отель Бали", D("-1800"), date(2026, 10, 10))

    f = await budget.forecast(session, horizon=3, now=NOW)
    assert f.liquid_total == D("25000")
    assert (f.reserve, f.protected) == ([("Копилка Reserve", D("1000"))], [("Копилка Holiday", D("7000"))])
    assert f.baseline == {"food": D("2000"), "housing": D("3500")}  # the trip is not usual spending
    assert (f.salary, f.salary_day_of_month) == (D("15000"), 24)
    assert f.late_share["housing"] == 1 and "food" not in {k for k, v in f.late_share.items() if v}
    assert f.travel_per_cycle == D("1000")

    now, nxt, _ = f.cycles
    assert (now.label, now.salary_day, now.spent, now.spending) == ("окт", date(2026, 10, 24), D("500"), D("5000"))
    assert [h.title for h in now.planned] == ["отель Бали"]
    assert now.closing == D("25000") + 15000 - 5000 - 1800
    # before the salary: the food still to come and the hotel, the rent comes after it
    assert now.low == D("25000") - 1500 - 1800
    assert nxt.opening == now.closing and nxt.spending == D("5500") and nxt.low == now.closing - 2000

    text = budget.format_forecast(f)
    assert "отель Бали" in text and "Не трогаем: Копилка Holiday" in text and "поездки" in text
    # the current month written out: where the end of it and the low point come from
    assert "  на счетах сейчас    25 000.00\n+ зарплата 24 окт     15 000.00\n− обычные траты        5 000.00" in text
    assert "= к 25 окт            33 200.00" in text
    assert "уже потрачено с 26 сен RM 500.00 = RM 5,000.00 ещё впереди" in text
    assert "Дно утром 24 окт, до зарплаты: 25 000.00 − 1 500.00 трат до зарплаты − 1 800.00 плана до неё = <b>RM 21,700.00</b>" in text
    assert "Остальные RM 3,500.00 обычно уходят в день зарплаты или после (🏠 Жильё)" in text


async def test_rent_moved_to_salary_day_counts_after_it(session):
    """Rent paid on the 28th in June and July, then on the 25th: the latest cycle decides."""
    for end_month, rent_day in ((7, date(2026, 6, 28)), (8, date(2026, 7, 28)), (9, date(2026, 9, 25))):
        await ledger.add_manual(session, "maybank", D("15000"), "ACME TECH SALARY", date(2026, end_month, 24), "salary")
        await ledger.add_manual(session, "maybank", D("-3500"), "RENT NG SOO LI", rent_day, "housing")
        await ledger.add_manual(session, "maybank", D("-1000"), "KEDAI MAKAN", date(2026, end_month - 1, 26), "food")
    f = await budget.forecast(session, horizon=1, now=NOW)
    assert f.late_share["housing"] == 1
    assert f.cycles[0].before_salary == D("1000")  # the food only, the rent comes after the salary


async def test_calendar_months_and_missing_salary(session, monkeypatch):
    """MONTH_START_DAY=1 (the template's default): calendar months; a salary without its category
    is not found, and /budget says what to do about it."""
    monkeypatch.setattr(get_settings(), "MONTH_START_DAY", 1)
    for month in (6, 7, 8):
        await ledger.add_manual(session, "maybank", D("-3500"), "RENT NG SOO LI", date(2026, month, 1), "housing")
        await ledger.add_manual(session, "maybank", D("-2000"), "KEDAI MAKAN", date(2026, month, 10), "food")
    await ledger.add_manual(session, "maybank", D("-300"), "KEDAI MAKAN", date(2026, 9, 5), "food")
    f = await budget.forecast(session, horizon=2, now=date(2026, 9, 20))
    assert [(c.start, c.end, c.label) for c in f.cycles] == [
        (date(2026, 9, 1), date(2026, 9, 30), "сен"), (date(2026, 10, 1), date(2026, 10, 31), "окт"),
    ]
    assert f.baseline == {"food": D("2000"), "housing": D("3500")} and f.salary == 0
    assert f.cycles[0].spending == D("5200")  # rent 3 500 and food 2 000 minus the 300 already spent
    text = budget.format_forecast(f)
    assert "Месяц — календарный." in text and "-е" not in text.split("\n")[1]
    assert "⚠️ Зарплата не найдена — поставь поступлению категорию «Зарплата»" in text
    assert budget.SALARY_MISSING in budget.forecast_json(f)["notes"]
    assert 'a calendar month' in advisor.month_rule(1) and "day 26" in advisor.month_rule(26)


async def test_what_if_and_short_cycle(session):
    await three_months(session)
    f = await budget.forecast(
        session, horizon=2, now=NOW, extra=[budget.PlanHit(0, "машина", D("-40000"), EXPENSE, date(2026, 10, 5))]
    )
    assert f.cycles[0].low < 0
    assert "не хватает" in budget.format_forecast(f)


async def test_repeating_and_overdue_plans(session):
    await three_months(session)
    await budget.add_plan(session, "РФ", D("-2000"), date(2026, 9, 3), TRANSFER, repeat_months=1)
    await budget.add_plan(session, "забытый счёт", D("-300"), date(2026, 9, 1))
    f = await budget.forecast(session, horizon=3, now=NOW)
    titles = [[(h.title, h.day, h.overdue) for h in c.planned] for c in f.cycles]
    assert titles[0] == [("забытый счёт", NOW, True), ("РФ", date(2026, 10, 3), False)]
    assert titles[1] == [("РФ", date(2026, 11, 3), False)]
    with pytest.raises(ledger.LedgerError):
        await budget.add_plan(session, "доход с минусом", D("-5"), NOW, INCOME)


async def test_balance_check_between_statements(session):
    day = NOW - timedelta(days=30)
    await ledger.import_statement(
        session,
        ParsedStatement("maybank", "maybank_csv", [ParsedTxn(day, D("-10"), "SHOP", balance_after=D("3871.45"))]),
        origin="test",
    )
    await ledger.add_manual(session, "maybank", D("-20"), "COFFEE", NOW - timedelta(days=5), "food")
    balances = {name: (amount, as_of) for name, _, amount, as_of in await reports.balances(session)}
    assert balances["Maybank"] == (D("3851.45"), NOW - timedelta(days=5))  # the statement plus what came after it

    await ledger.check_balance(session, "maybank", D("4210"), NOW - timedelta(days=1))
    await ledger.add_manual(session, "maybank", D("-36"), "LUNCH", NOW, "food")
    balances = {name: amount for name, _, amount, _ in await reports.balances(session)}
    assert balances["Maybank"] == D("4174")
    with pytest.raises(ledger.LedgerError):
        await ledger.check_balance(session, "ru", D("1"))


# --- bot -----------------------------------------------------------------------------


async def test_plan_and_budget_in_the_bot(bot, session):
    await send(bot, "/plan 1800 отель Бали 25.10")
    assert "Заложил" in bot.session.texts()[-1] and "отель Бали" in bot.session.texts()[-1]
    await send(bot, "/plan")
    assert "1. " in bot.session.texts()[-1]
    item = await session.scalar(select(PlannedItem))
    await press(bot, PlanCb(a="done", p=item.id))
    await session.refresh(item)
    assert item.status == PLAN_DONE

    await send(bot, "/plan ноутбук")
    assert "Нужны сумма и когда" in bot.session.texts()[-1]
    await send(bot, "/budget")
    assert "Бюджет на" in bot.session.texts()[-1]

    await send(bot, "/setbalance maybank 4210")
    assert "4,210.00" in bot.session.texts()[-1]
    await send(bot, "/pot Reserve резерв")
    pot = await session.scalar(select(Account).where(Account.code == "pot_reserve"))
    assert pot.spendable


async def test_ask_needs_the_key(bot, session, monkeypatch):
    monkeypatch.setattr(get_settings(), "ANTHROPIC_API_KEY", None)
    await send(bot, "/ask хочу ноутбук")
    assert "выключен" in bot.session.texts()[-1]


async def test_ask_talk_and_add_the_proposal(bot, session, monkeypatch):
    monkeypatch.setattr(get_settings(), "ANTHROPIC_API_KEY", "test")
    questions = []

    async def fake_ask(session, question, history=None, model=None):
        questions.append((question, len(history or [])))
        turn = [{"role": "user", "content": question}, {"role": "assistant", "content": "ok"}]
        proposal = advisor.Proposal("ноутбук", D("6000"), date(2026, 12, 1))
        return advisor.Answer("⚠️ впритык: <b>лучше в декабре</b>", proposal, [*(history or []), *turn])

    monkeypatch.setattr(advisor, "ask", fake_ask)
    await send(bot, "/ask ноутбук за 6000?")
    await send(bot, "а если в январе?")  # still the same talk, not a cash entry
    assert questions == [("ноутбук за 6000?", 0), ("а если в январе?", 2)]
    edits = [c for c in bot.session.calls if type(c).__name__ == "EditMessageText"]
    assert "лучше в декабре" in edits[-1].text
    assert edits[-1].reply_markup.inline_keyboard[0][0].text.startswith("📌 В план: ноутбук")

    await press(bot, PlanCb(a="add"))
    item = await session.scalar(select(PlannedItem))
    assert (item.title, item.amount, item.due_on, item.status) == ("ноутбук", D("-6000"), date(2026, 12, 1), PLAN_OPEN)
    await press(bot, PlanCb(a="add"))  # a second tap adds nothing
    assert len((await session.scalars(select(PlannedItem))).all()) == 1

    await send(bot, "/done")
    await send(bot, "25 rm обед")
    assert "Записал" in bot.session.texts()[-1]


# --- advisor: the tool loop with a fake Claude ------------------------------------


class FakeClaude:
    def __init__(self, responses, calls):
        self.messages = SimpleNamespace(create=self.create)
        self._responses, self.calls = responses, calls

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return self._responses.pop(0)


def tool_use(id_, name, input_):
    return SimpleNamespace(type="tool_use", id=id_, name=name, input=input_)


def final(**answer):
    """The advisor's last message: JSON with the answer and the offered plan item."""
    data = {"answer": "", "offer_plan_item": False, "plan_title": "", "plan_amount": 0, "plan_date": "", **answer}
    return SimpleNamespace(stop_reason="end_turn", content=[
        SimpleNamespace(type="thinking", thinking=""),  # newer models open with a thinking block
        SimpleNamespace(type="text", text=json.dumps(data, ensure_ascii=False)),
    ])


async def test_advisor_simulates_and_proposes(session, monkeypatch):
    await three_months(session)
    monkeypatch.setattr(get_settings(), "ANTHROPIC_API_KEY", "test")
    calls = []
    responses = [
        SimpleNamespace(stop_reason="tool_use", content=[
            SimpleNamespace(type="thinking", thinking=""),
            tool_use("t1", "simulate", {"items": [{"title": "ноутбук", "amount": 6000, "date": "2026-12-05"}]}),
        ]),
        final(answer="✅ **пролезет** в декабре", offer_plan_item=True, plan_title="ноутбук", plan_amount=6000,
              plan_date="2026-12-05"),
    ]
    monkeypatch.setattr(advisor.anthropic, "AsyncAnthropic", lambda **kw: FakeClaude(responses, calls))

    answer = await advisor.ask(session, "ноутбук за 6000 в декабре?")
    assert answer.text == "✅ <b>пролезет</b> в декабре"
    assert answer.proposal == advisor.Proposal("ноутбук", D("6000.00"), date(2026, 12, 5))
    # the offer stays in the talk, so the next answer does not offer it again
    assert [m["role"] for m in answer.history] == ["user", "assistant"]
    assert "[offered as a plan item: ноутбук" in answer.history[1]["content"]
    assert calls[0]["model"] == get_settings().BUDGET_MODEL and "usual_spending_per_cycle" in calls[0]["system"]
    assert calls[0]["output_config"]["format"]["schema"] == advisor.ANSWER_SCHEMA
    assert calls[1]["messages"][-2]["content"][0].type == "thinking"  # the thinking block goes back unchanged
    result = calls[1]["messages"][-1]["content"][0]
    assert result["tool_use_id"] == "t1" and '"first_cycle_below_zero"' in result["content"] and not result.get("is_error")


async def test_advisor_reports_bad_tool_input(session, monkeypatch):
    monkeypatch.setattr(get_settings(), "ANTHROPIC_API_KEY", "test")
    calls = []
    responses = [
        SimpleNamespace(stop_reason="tool_use", content=[tool_use("t1", "simulate", {"items": [{"title": "x", "amount": 1, "date": "soon"}]})]),
        final(answer="Уточни дату", offer_plan_item=True, plan_title="x", plan_amount=0, plan_date="soon"),
    ]
    monkeypatch.setattr(advisor.anthropic, "AsyncAnthropic", lambda **kw: FakeClaude(responses, calls))
    answer = await advisor.ask(session, "x")
    assert answer.text == "Уточни дату" and answer.proposal is None  # an unreadable offer is dropped
    assert calls[1]["messages"][-1]["content"][0]["is_error"] is True


# --- dashboard API -------------------------------------------------------------------


async def login(client):
    link = urlparse(magic_link())
    await client.get(f"{link.path}?{link.query}")


async def test_budget_api(client, session):  # noqa: F811
    assert (await client.get("/api/budget")).status_code == 401
    await login(client)
    r = await client.post("/api/plans", json={"title": "отель", "amount": "1800", "due_on": "2026-12-10"})
    assert r.status_code == 200 and r.json()["amount"] == -1800
    r = await client.post("/api/plans", json={"title": "бонус", "amount": "3000", "due_on": "2026-12-10", "kind": "income"})
    assert r.json()["amount"] == 3000
    plans = (await client.get("/api/plans")).json()
    assert [p["title"] for p in plans] == ["отель", "бонус"]
    r = await client.patch(f"/api/plans/{plans[0]['id']}", json={"status": "cancelled"})
    assert r.json()["status"] == "cancelled"
    b = (await client.get("/api/budget")).json()
    assert len(b["cycles"]) == get_settings().BUDGET_HORIZON and "liquid_total" in b


async def test_plan_edit_api(client, session):  # noqa: F811
    await login(client)
    r = await client.post("/api/plans", json={"title": "отель", "amount": "5674", "due_on": "2026-12-10", "category": "travel"})
    plan_id = r.json()["id"]
    r = await client.patch(f"/api/plans/{plan_id}", json={"title": "отель Вьетнам", "amount": "6000", "due_on": "2026-12-12"})
    body = r.json()
    assert (body["title"], body["amount"], body["due_on"], body["category"]) == ("отель Вьетнам", -6000, "2026-12-12", "travel")
    # an unsent field stays; null clears; the sign follows the kind
    r = await client.patch(f"/api/plans/{plan_id}", json={"category": None, "repeat_months": 1, "kind": "income"})
    body = r.json()
    assert (body["category"], body["repeat_months"], body["amount"], body["kind"]) == (None, 1, 6000, "income")
    r = await client.patch(f"/api/plans/{plan_id}", json={"repeat_months": 0})
    assert r.status_code == 400
    r = await client.patch(f"/api/plans/{plan_id}", json={"amount": "0"})
    assert r.status_code == 400
    r = await client.patch("/api/plans/99999", json={"title": "x"})
    assert r.status_code == 404
    r = await client.patch(f"/api/plans/{plan_id}", json={"title": "новое имя", "status": "done"})
    assert r.json()["status"] == "done" and r.json()["title"] == "новое имя"
