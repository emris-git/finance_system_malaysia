from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from finance import ledger, reports
from finance.models import (
    EXPENSE,
    PENDING,
    POSTED,
    REVIEW_P2P,
    REVIEW_UNCATEGORIZED,
    TRANSFER,
    CategoryRule,
    Transaction,
)
from finance.parsers import ParsedStatement, ParsedTxn
from finance.utils import today


def D(v):
    return Decimal(v)


def maybank(*rows):
    return ParsedStatement(
        "maybank", "maybank_csv", [ParsedTxn(booked_on=d, amount=D(a), description=desc) for d, a, desc in rows]
    )


def tng(*rows):
    return ParsedStatement(
        "tng", "tng_pdf", [ParsedTxn(booked_on=d, amount=D(a), description=desc, raw_type=t) for d, a, desc, t in rows]
    )


async def txn_by_desc(session, text):
    return await session.scalar(select(Transaction).where(Transaction.description.contains(text)))


async def test_import_classifies_and_skips_duplicates(session):
    day = today() - timedelta(days=3)
    st = maybank(
        (day, "8000.00", "IBG CREDIT ACME TECH SDN BHD PAYROLL"),
        (day, "-18.90", "STARBUCKS PAVILION"),
        (day, "-18.90", "STARBUCKS PAVILION"),  # second coffee, same day: both real
        (day, "-42.00", "SOME UNKNOWN SHOP"),
    )
    r = await ledger.import_statement(session, st, origin="test", file_bytes=b"file-1")
    assert (r.new, r.duplicate, r.to_review) == (4, 0, 1)

    again = await ledger.import_statement(session, st, origin="test", file_bytes=b"file-1")
    assert again.already_imported
    # same rows inside a different file (overlapping statement) are still skipped
    overlap = await ledger.import_statement(session, st, origin="test", file_bytes=b"file-2")
    assert (overlap.new, overlap.duplicate) == (0, 4)

    salary = await txn_by_desc(session, "ACME TECH")
    assert (salary.kind, salary.category.code) == ("income", "salary")
    unknown = await txn_by_desc(session, "UNKNOWN SHOP")
    assert (unknown.kind, unknown.category.code, unknown.review_reason) == (EXPENSE, "other", REVIEW_UNCATEGORIZED)


async def test_maybank_to_tng_topup_pairs_up(session):
    day = today() - timedelta(days=2)
    await ledger.import_statement(
        session, maybank((day, "-200.00", "DUITNOW TO TNG DIGITAL SDN BHD")), origin="test", file_bytes=b"mb"
    )
    out = await txn_by_desc(session, "TNG DIGITAL")
    assert out.kind == TRANSFER and out.transfer_id is None

    r = await ledger.import_statement(
        session, tng((day + timedelta(days=1), "200.00", "Maybank2u FPX", "Reload")), origin="test", file_bytes=b"tng"
    )
    assert r.matched_transfers == 1
    await session.refresh(out)
    reload = await txn_by_desc(session, "Maybank2u FPX")
    assert out.transfer_id == reload.transfer_id is not None
    assert out.review_reason is None and reload.review_reason is None


async def test_rf_transfer_marked_from_review(session):
    day = today() - timedelta(days=1)
    await ledger.import_statement(
        session, maybank((day, "-1000.00", "DUITNOW TRANSFER TO ALI BIN ABU")), origin="test", file_bytes=b"p2p"
    )
    out = await txn_by_desc(session, "ALI BIN ABU")
    assert out.review_reason == REVIEW_P2P
    assert [t.id for t in await ledger.review_queue(session)] == [out.id]

    transfer = await ledger.mark_fx(session, out, D("21500"))
    assert transfer.kind == "fx" and transfer.rate == D("21.5")
    assert await ledger.review_count(session) == 0
    assert await ledger.previous_fx_to(session, out.merchant)

    s = await reports.summarize(session, day, day, baseline_periods=0)
    assert (s.fx_myr, s.fx_rub, s.fx_rate) == (D("1000"), D("21500"), D("21.50"))
    assert "MYR" not in s.blocks or s.blocks["MYR"].expense == 0  # not counted as spending


async def test_rf_typed_in_bot_is_confirmed_by_statement(session):
    day = today() - timedelta(days=2)
    transfer = await ledger.record_fx(session, D("500"), D("10750"), "maybank", day)
    pending = await session.scalar(select(Transaction).where(Transaction.status == PENDING))
    assert pending.transfer_id == transfer.id

    r = await ledger.import_statement(
        session, maybank((day + timedelta(days=1), "-500.00", "DUITNOW TRANSFER TO ALI BIN ABU")),
        origin="test", file_bytes=b"stmt",
    )
    assert (r.new, r.reconciled) == (0, 1)
    await session.refresh(pending)
    assert pending.status == POSTED and pending.transfer_id == transfer.id
    assert "ALI BIN ABU" in pending.description


async def test_rf_merge_when_amount_differs_by_fee(session):
    day = today() - timedelta(days=2)
    await ledger.record_fx(session, D("500"), D("10750"), "maybank", day)
    await ledger.import_statement(
        session, maybank((day, "-500.50", "IBG TRANSFER TO ALI BIN ABU")), origin="test", file_bytes=b"fee"
    )
    posted = await txn_by_desc(session, "IBG TRANSFER")
    assert posted.review_reason == REVIEW_P2P
    (pending,) = await ledger.pending_fx_near(session, posted)
    await ledger.merge_pending_into(session, pending, posted)
    await session.refresh(posted)
    assert posted.transfer.kind == "fx" and posted.transfer.rate == D("21.478521")
    assert await ledger.review_count(session) == 0


async def test_set_category_with_rule_updates_similar_rows(session):
    day = today() - timedelta(days=1)
    await ledger.import_statement(
        session,
        maybank((day, "-30.00", "KEDAI RUNCIT AMINAH 0001"), (day, "-12.00", "KEDAI RUNCIT AMINAH 0002")),
        origin="test", file_bytes=b"kedai",
    )
    first = await txn_by_desc(session, "0001")
    groceries = await ledger.get_category(session, "groceries")
    updated = await ledger.set_category(session, first, groceries, remember=True)
    assert updated == 1
    rule = await session.scalar(select(CategoryRule).where(CategoryRule.origin == "user"))
    assert rule.pattern == r"\bKEDAI.*?\bRUNCIT"
    assert await ledger.review_count(session) == 0


async def test_rub_manual_expense_and_balance(session):
    txn = await ledger.add_manual(session, "ru", D("-1500"), "такси до аэропорта")
    assert (txn.kind, txn.category.code, txn.account.currency) == (EXPENSE, "transport", "RUB")
    await ledger.set_balance(session, "ru", D("20000"))
    balances = {name: amount for name, _, amount, _ in await reports.balances(session)}
    assert balances["Российский счёт"] == D("20000")

    s = await reports.summarize(session, today(), today(), baseline_periods=0)
    assert s.blocks["RUB"].expense == D("1500")
    assert "Расходы в рублях" in reports.format_summary(s, "Тест", "")


async def test_undo_rf_transfer_returns_row_to_review(session):
    day = today() - timedelta(days=1)
    await ledger.import_statement(
        session, maybank((day, "-300.00", "DUITNOW TRANSFER TO BOB")), origin="test", file_bytes=b"bob"
    )
    out = await txn_by_desc(session, "BOB")
    transfer = await ledger.mark_fx(session, out, D("6400"))
    await ledger.unlink_transfer(session, transfer.id)
    await session.commit()
    await session.refresh(out)
    assert out.review_reason == REVIEW_P2P and out.transfer_id is None
    rub_legs = (await session.scalars(select(Transaction).where(Transaction.amount == D("6400")))).unique().all()
    assert all(leg.deleted_at is not None for leg in rub_legs)


async def test_week_report_text(session):
    last_week = today() - timedelta(days=7)
    await ledger.import_statement(
        session,
        maybank((last_week, "-25.00", "GRAB FOOD"), (last_week, "-500.00", "DUITNOW TRANSFER TO ALI")),
        origin="test", file_bytes=b"wk",
    )
    text = await reports.week_report(session)
    assert "Неделя" in text and "Еда вне дома" in text
    assert "Переводы людям без пометки: 1" in text
    assert "/review" in text


async def test_remembered_rule_beats_p2p_for_rent(session):
    day = today() - timedelta(days=40)
    await ledger.import_statement(
        session,
        maybank((day, "-3200.00", "DUITNOW TRANSFER TO SPEEDHOME RENT"), (day + timedelta(days=30), "-3200.00", "DUITNOW TRANSFER TO SPEEDHOME RENT")),
        origin="test", file_bytes=b"rent",
    )
    first = await txn_by_desc(session, "SPEEDHOME")
    assert first.review_reason == REVIEW_P2P
    housing = await ledger.get_category(session, "housing")
    assert await ledger.set_category(session, first, housing, remember=True) == 1
    rule = await session.scalar(select(CategoryRule).where(CategoryRule.origin == "user"))
    assert rule.pattern == r"\bSPEEDHOME.*?\bRENT" and rule.kind == EXPENSE

    # next month's rent is classified by the rule, no review
    await ledger.import_statement(
        session, maybank((today(), "-3200.00", "DUITNOW TRANSFER TO SPEEDHOME RENT")), origin="test", file_bytes=b"rent2"
    )
    assert await ledger.review_count(session) == 0


@pytest.mark.parametrize(
    "first, later, pattern",
    [
        # the merchant sits before "*", the branch code carries digits: another branch still matches
        ("SALE DEBIT SB294-SOUTHLINK BA * KUALA LUMPUR, MY", "SALE DEBIT SB301-SOUTHLINK BA * PETALING JAYA, MY", r"\bSOUTHLINK"),
        ("SALE DEBIT 99 SPEEDMART 1234 * PETALING JAYA, MY", "SALE DEBIT 99 SPEEDMART 2207 * SHAH ALAM, MY", r"\bSPEEDMART"),
        ("JAYA GROCER THE LINC", "JAYA GROCER MIDVALLEY", r"\bJAYA.*?\bGROCER"),
        ("FPX PAYMENT SHOPEE MALAYSIA", "SHOPEE PAY", r"\bSHOPEE"),
        # Maybank IBK boilerplate is not the counterparty: rent to NG SOO LI must not catch every IBK transfer
        ("IBK FUND TFR FR A/C NG SOO LI          * Morgan Alex MB", "IBK FUND TFR FR A/C NG SOO LI * rent MBB CT", r"\bSOO"),
    ],
)
def test_suggest_pattern_matches_other_branches(first, later, pattern):
    import re

    from finance.classify import suggest_pattern

    assert suggest_pattern(first) == pattern
    assert re.search(pattern, first, re.I) and re.search(pattern, later, re.I)


async def test_rule_is_reused_not_duplicated(session):
    day = today() - timedelta(days=1)
    await ledger.import_statement(
        session, maybank((day, "-100.00", "SALE DEBIT SB294-SOUTHLINK BA * KUALA LUMPUR, MY")), origin="test", file_bytes=b"sl"
    )
    txn = await txn_by_desc(session, "SOUTHLINK")
    transport = await ledger.get_category(session, "transport")
    fun = await ledger.get_category(session, "fun")
    rule, _ = await ledger.apply_category(session, txn, transport, remember=True)
    again, _ = await ledger.apply_category(session, txn, fun, remember=True)
    assert again.id == rule.id and again.category_id == fun.id
    assert [r.id for r in await ledger.user_rules(session)] == [rule.id]

    # next statement: another branch lands in the category on import, no review
    await ledger.import_statement(
        session, maybank((today(), "-45.00", "SALE DEBIT SB301-SOUTHLINK BA * PETALING JAYA, MY")), origin="test", file_bytes=b"sl2"
    )
    assert await ledger.review_count(session) == 0
    assert (await ledger.delete_user_rule(session, rule.id)).id == rule.id
    assert await ledger.user_rules(session) == []


async def test_duitnow_qr_is_a_purchase(session):
    day = today() - timedelta(days=1)
    st = tng(
        (day, "-44.90", "Kafe Seni", "DuitNow QR"),
        (day, "-15.00", "LIM WEI JIE", "Transfer to Wallet"),
    )
    await ledger.import_statement(session, st, origin="test", file_bytes=b"qr")
    cafe = await txn_by_desc(session, "Kafe Seni")
    assert (cafe.kind, cafe.category.code, cafe.review_reason) == (EXPENSE, "food", None)
    person = await txn_by_desc(session, "LIM WEI JIE")
    assert (person.kind, person.review_reason) == (TRANSFER, REVIEW_P2P)


async def test_own_transfer_pinned_to_account_links_on_its_import(session):
    day = today() - timedelta(days=5)
    await ledger.import_statement(
        session, tng((day, "500.00", "ALEX MORGAN", "DUITNOW_RECEIVEFROM")), origin="test", file_bytes=b"t"
    )
    await ledger.import_statement(
        session,
        ParsedStatement("cash_myr", "manual", [ParsedTxn(day, D("-500.00"), "CASH TRANSFER")]),
        origin="test",
        file_bytes=b"c",
    )
    top_up = await txn_by_desc(session, "ALEX MORGAN")
    cash = await txn_by_desc(session, "CASH TRANSFER")
    maybank_acc = await ledger.get_account(session, "maybank")

    assert not await ledger.mark_internal(session, top_up, maybank_acc)
    # another own-accounts row with the same amount must not take a leg pinned to Maybank
    assert not await ledger.mark_internal(session, cash)
    assert top_up.transfer_id is None

    # the rules take the Maybank row for a purchase; the owner's answer wins
    await ledger.import_statement(
        session, maybank((day + timedelta(days=1), "-500.00", "SALE DEBIT SOME SHOP")), origin="test", file_bytes=b"m"
    )
    out = await txn_by_desc(session, "SOME SHOP")
    assert out.transfer_id is not None and out.transfer_id == top_up.transfer_id
    assert (out.kind, out.category_id, top_up.review_reason) == (TRANSFER, None, None)
    assert cash.transfer_id is None


async def test_own_transfer_to_account_finds_imported_leg(session):
    day = today() - timedelta(days=5)
    await ledger.import_statement(
        session, maybank((day, "-300.00", "SALE DEBIT SOME SHOP")), origin="test", file_bytes=b"m"
    )
    await ledger.import_statement(
        session, tng((day, "300.00", "ALEX MORGAN", "DUITNOW_RECEIVEFROM")), origin="test", file_bytes=b"t"
    )
    top_up = await txn_by_desc(session, "ALEX MORGAN")
    assert await ledger.mark_internal(session, top_up, await ledger.get_account(session, "maybank"))
    assert (await txn_by_desc(session, "SOME SHOP")).transfer_id == top_up.transfer_id


async def test_project_merchants(session):
    day = today() - timedelta(days=1)
    await ledger.import_statement(
        session,
        tng(
            (day, "-25.00", "Payment - RAILWAY 14150000001", "CARDISSUANCE_PAYMENT"),
            (day, "-60.00", "Payment - GODADDY.COM 14150000002", "CARDISSUANCE_PAYMENT"),
            (day, "-90.00", "Payment - ANTHROPIC 14150000003", "CARDISSUANCE_PAYMENT"),
        ),
        origin="test",
        file_bytes=b"p",
    )
    assert (await txn_by_desc(session, "RAILWAY")).category.code == "projects"
    assert (await txn_by_desc(session, "GODADDY")).category.code == "projects"
    assert (await txn_by_desc(session, "ANTHROPIC")).category.code == "subscriptions"


IBK_TO_TNG = "IBK FUND TFR FR A/C MORGAN ALEX * Alex MBB CT"


async def test_maybank_fund_transfer_to_own_tng_pairs_by_itself(session):
    day = today() - timedelta(days=2)
    r = await ledger.import_statement(
        session,
        maybank(
            (day, "-30.00", IBK_TO_TNG),
            # to someone else, the owner's name only in the reference after "*"
            (day, "-50.00", "IBK FUND TFR FR A/C ALI BIN ABU          * Morgan Alex MB"),
        ),
        origin="test",
    )
    assert r.to_review == 1  # only the transfer to ALI: to the owner's own name waits for its pair
    out = await txn_by_desc(session, "MORGAN")
    assert (out.kind, out.review_reason) == (TRANSFER, "awaiting_pair")
    assert (await txn_by_desc(session, "ALI BIN ABU")).review_reason == REVIEW_P2P

    r = await ledger.import_statement(
        session,
        tng(
            (day + timedelta(days=1), "30.00", "MORGAN ALEX", "DUITNOW_RECEIVEFROM"),
            (day, "50.00", "MBB CT", "DUITNOW_RECEIVEFROM"),  # does not name the owner: asked as usual
        ),
        origin="test",
    )
    await session.refresh(out)
    into = await txn_by_desc(session, "MORGAN ALEX")
    assert r.matched_transfers == 1 and out.transfer_id and out.transfer_id == into.transfer_id
    assert into.review_reason is None and await ledger.review_count(session) == 2


async def test_rows_imported_before_are_paired_on_start(session):
    day = today() - timedelta(days=90)
    await ledger.import_statement(session, maybank((day, "-30.00", IBK_TO_TNG)), origin="test")
    await ledger.import_statement(
        session, tng((day, "30.00", "Transfer from MBB", "DUITNOW_RECEIVEFROM")), origin="test"
    )
    out = await txn_by_desc(session, "MORGAN")
    into = await txn_by_desc(session, "Transfer from MBB")
    # as the old rules left them: "FUND TFR" was an unknown purchase, the TNG side a transfer from a person
    out.kind, out.review_reason, out.transfer_id = EXPENSE, REVIEW_UNCATEGORIZED, None
    into.transfer_id = None
    into.kind, into.review_reason = "income", "p2p_in"
    await session.commit()

    assert await ledger.apply_rules_to_review_queue(session) == (1, 1)
    await session.refresh(out)
    await session.refresh(into)
    assert out.transfer_id == into.transfer_id and out.kind == into.kind == TRANSFER
    assert await ledger.review_count(session) == 0


async def test_remembered_rent_by_ibk_leaves_own_transfers_alone(session):
    day = today() - timedelta(days=1)
    await ledger.import_statement(
        session, maybank((day, "-3500.00", "IBK FUND TFR FR A/C NG SOO LI          * Morgan Alex MB")), origin="test"
    )
    rent = await txn_by_desc(session, "NG SOO LI")
    assert rent.review_reason == REVIEW_P2P  # a transfer to a person, not to the owner
    rule, _ = await ledger.apply_category(session, rent, await ledger.get_category(session, "housing"), remember=True)
    await session.commit()

    await ledger.import_statement(
        session,
        maybank((today(), "-3500.00", "IBK FUND TFR FR A/C NG SOO LI * rent MBB CT"), (today(), "-30.00", IBK_TO_TNG)),
        origin="test",
        file_bytes=b"next-month",
    )
    rows = (
        await session.scalars(select(Transaction).order_by(Transaction.id).execution_options(populate_existing=True))
    ).unique().all()
    assert [(r.kind, r.category.code if r.category else None, r.review_reason) for r in rows] == [
        (EXPENSE, "housing", None),
        (EXPENSE, "housing", None),
        (TRANSFER, None, "awaiting_pair"),
    ]


async def test_ruble_expense_paid_back_in_ringgit(session):
    lessons = await ledger.add_manual(session, "ru", D("-5000"), "уроки английского Насти", today(), "education")
    await ledger.import_statement(session, maybank((today(), "1450.00", "CASH DEPOSIT")), origin="test")
    cash = await txn_by_desc(session, "CASH DEPOSIT")
    assert cash.review_reason == REVIEW_UNCATEGORIZED
    assert [e.id for e in await ledger.rub_expenses_to_pay_back(session, cash)] == [lessons.id]

    transfer = await ledger.pay_back_rub_expense(session, cash, lessons)
    assert transfer.kind == "fx_back"
    assert (cash.kind, cash.review_reason, lessons.kind, lessons.category.code) == (TRANSFER, None, TRANSFER, "education")

    s = await reports.summarize(session, today(), today(), baseline_periods=0)
    assert s.blocks.get("RUB") is None and s.blocks.get("MYR") is None  # neither spending nor income
    assert (s.fx_back_rub, s.fx_back_myr) == (D("5000"), D("1450"))
    assert "↩️ <b>Вернули за рубли:</b> ₽ 5 000 → RM 1,450.00" in reports.format_summary(s, "t", "")

    await ledger.soft_delete(session, cash)  # undone: the lessons are an expense again, not deleted
    await session.refresh(lessons)
    assert (lessons.kind, lessons.transfer_id, lessons.deleted_at, lessons.category.code) == (EXPENSE, None, None, "education")


async def test_rubles_handed_over_paid_back_in_ringgit(session):
    day = today() - timedelta(days=2)
    await ledger.import_statement(
        session, tng((day, "465.00", "IVANOVA MARIA", "DUITNOW_RECEIVEFROM")), origin="test"
    )
    back = await txn_by_desc(session, "MARIA")
    await ledger.pay_back_rubles(session, back, D("10000"))
    balances = {name: amount for name, _, amount, _ in await reports.balances(session)}
    assert balances["Российский счёт"] == D("-10000") and back.kind == TRANSFER and back.review_reason is None

    await ledger.apply_category(session, back, await ledger.get_category(session, "other_income"))
    await session.commit()
    balances = {name: amount for name, _, amount, _ in await reports.balances(session)}
    assert "Российский счёт" not in balances  # the ruble leg the bot wrote is gone with the link
