from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import func, select

from finance import ledger, reports
from finance.models import REVIEW_OWN, SAVINGS, TRANSFER, Account, Transaction
from finance.parsers import ParsedStatement, ParsedTxn
from finance.utils import today

OLD = "TRANSFER FROM A/C ALEX MORGAN * 00000001"
FULL = OLD + " FUND Holiday"


def stmt(desc, day, amount="-1500.00", balance="8421.50"):
    return ParsedStatement("maybank", "maybank_pdf", [ParsedTxn(day, Decimal(amount), desc, balance_after=Decimal(balance))])


async def test_resent_statement_fixes_description_and_fills_the_pot(session):
    day = today() - timedelta(days=32)
    # imported before the parser read the third line: looks like a transfer to a person
    await ledger.import_statement(session, stmt(OLD, day), origin="test", file_bytes=b"aug-v1")
    row = await session.scalar(select(Transaction).where(Transaction.description == OLD))
    assert row.kind == TRANSFER and row.transfer_id is None

    r = await ledger.import_statement(session, stmt(FULL, day), origin="test", file_bytes=b"aug-v2")
    assert (r.new, r.refreshed, r.to_pots) == (0, 1, 1)
    assert await session.scalar(select(func.count(Transaction.id)).where(Transaction.amount == Decimal("-1500"))) == 1

    await session.refresh(row)
    assert row.description == FULL and row.transfer_id and row.review_reason is None
    pot = await session.scalar(select(Account).where(Account.kind == SAVINGS))
    assert pot.name == "Копилка Holiday"
    balances = {name: amount for name, _, amount, _ in await reports.balances(session)}
    assert balances["Копилка Holiday"] == Decimal("1500")

    s = await reports.summarize(session, day, day, baseline_periods=0)
    assert s.savings_net == Decimal("1500") and "Отложил в копилки: RM 1,500.00" in reports.format_summary(s, "t", "")

    # the same file again: nothing new, nothing duplicated
    again = await ledger.import_statement(session, stmt(FULL, day), origin="test", file_bytes=b"aug-v2")
    assert again.already_imported and (again.new, again.refreshed, again.to_pots) == (0, 0, 0)


async def test_pot_value_can_be_set(session):
    pot, adj = await ledger.set_pot_balance(session, "Holiday", Decimal("15000"))
    assert pot.code == "pot_holiday" and adj.amount == Decimal("15000")
    pot2, adj2 = await ledger.set_pot_balance(session, "holiday", Decimal("15250"))
    assert pot2.id == pot.id and adj2.amount == Decimal("250")


async def test_skip_snoozes_and_own_is_final(session):
    day = today() - timedelta(days=25)
    await ledger.import_statement(
        session,
        ParsedStatement("maybank", "maybank_csv", [
            ParsedTxn(day, Decimal("-300"), "DUITNOW TRANSFER TO ALI"),
            ParsedTxn(day, Decimal("-700"), "IBG TRANSFER TO MY OTHER BANK"),
        ]),
        origin="test",
    )
    ali = await session.scalar(select(Transaction).where(Transaction.description.contains("ALI")))
    other = await session.scalar(select(Transaction).where(Transaction.description.contains("OTHER BANK")))
    assert await ledger.review_count(session) == 2

    await ledger.snooze(session, ali)
    assert [t.id for t in await ledger.review_queue(session)] == [other.id]
    assert await ledger.snoozed_count(session) == 1

    # an old row the owner calls "between my accounts" is not asked again
    await ledger.mark_internal(session, other)
    assert other.review_reason == REVIEW_OWN
    assert await ledger.review_queue(session) == []


async def test_booster_rows_go_to_the_same_pot(session):
    day = today() - timedelta(days=32)
    st = ParsedStatement("maybank", "maybank_pdf", [
        ParsedTxn(day, Decimal("-1500.00"), FULL, balance_after=Decimal("8421.50")),
        ParsedTxn(day, Decimal("-5.50"), OLD + " BOOSTER Holiday", balance_after=Decimal("8416.00")),
    ])
    r = await ledger.import_statement(session, st, origin="test", file_bytes=b"aug")
    assert r.to_pots == 2 and await ledger.review_count(session) == 0
    assert [p.name for p in await ledger.pots(session)] == ["Копилка Holiday"]
    balances = {name: amount for name, _, amount, _ in await reports.balances(session)}
    assert balances["Копилка Holiday"] == Decimal("1505.50")


def test_pot_name_patterns():
    assert ledger.pot_name_from(OLD + " BOOSTER Holiday") == "Holiday"
    assert ledger.pot_name_from(OLD + " FUND Holiday") == "Holiday"
    assert ledger.pot_name_from("FUND TRANSFER TO A/ ALEX MORGAN * 00000001 WTDRW Holiday") == "Holiday"
    assert ledger.pot_name_from("INSTANT FUND TRANSFER TO ALI") is None
    assert ledger.pot_name_from("DUITNOW TRANSFER TO ALI") is None


async def test_withdrawal_from_pot(session):
    day = today() - timedelta(days=32)
    st = ParsedStatement("maybank", "maybank_pdf", [
        ParsedTxn(day, Decimal("-1500.00"), FULL, balance_after=Decimal("8421.50")),
        ParsedTxn(
            day + timedelta(days=1),
            Decimal("1000.00"),
            "FUND TRANSFER TO A/ ALEX MORGAN * 00000001 WTDRW Holiday",
            balance_after=Decimal("11874.28"),
        ),
    ])
    r = await ledger.import_statement(session, st, origin="test", file_bytes=b"wtdrw")
    assert r.to_pots == 2 and await ledger.review_count(session) == 0
    balances = {name: amount for name, _, amount, _ in await reports.balances(session)}
    assert balances["Копилка Holiday"] == Decimal("500")
