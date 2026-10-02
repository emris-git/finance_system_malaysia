from datetime import date
from decimal import Decimal

from finance.parsers.maybank import parse_maybank_csv, parse_maybank_pdf_text
from finance.parsers.tng import parse_tng_text

# Newest first, like the TNG app export.
TNG_TEXT = """Touch 'n Go eWallet Transaction History
25/9/2026 Success Payment 202609250000000001 STARBUCKS PAVILION RM15.50 RM84.50
24/9/2026 Success Reload 202609240000000002 Maybank2u FPX RM100.00 RM100.00
23/9/2026 Success DuitNow Transfer 202609230000000003 ALI BIN ABU RM20.00 RM0.00
22/9/2026 Success Receive 202609220000000004 From Wallet JOHN RM5.00 RM20.00
22/9/2026 Reversed Payment 202609220000000005 GRAB RIDES RM12.00 RM15.00
"""


def test_tng_signs_from_balance_chain():
    st = parse_tng_text(TNG_TEXT)
    assert st.account_code == "tng"
    by_ref = {t.external_ref[-1]: t for t in st.transactions}
    assert by_ref["1"].amount == Decimal("-15.50")
    assert by_ref["2"].amount == Decimal("100.00")  # reload
    assert by_ref["3"].amount == Decimal("-20.00")  # transfer out, from balance
    assert by_ref["4"].amount == Decimal("5.00")
    assert by_ref["5"].reversed
    assert by_ref["1"].balance_after == Decimal("84.50")
    assert [t.booked_on for t in st.transactions] == sorted(t.booked_on for t in st.transactions)


def test_tng_oldest_first_order():
    lines = TNG_TEXT.strip().split("\n")
    text = "\n".join([lines[0], *reversed(lines[1:])])
    st = parse_tng_text(text)
    amounts = {t.external_ref[-1]: t.amount for t in st.transactions}
    assert amounts["2"] == Decimal("100.00")
    assert amounts["3"] == Decimal("-20.00")


MAYBANK_CSV = """Account Statement
Date,Description,Debit,Credit,Balance
01/09/2026,SALARY ACME TECH SDN BHD,,8000.00,10500.00
02/09/2026,DUITNOW TO TNG DIGITAL SDN BHD,100.00,,10400.00
03/09/2026,"TRANSFER TO A/C 12345678 ALI BIN ABU",1000.00,,9400.00
"""


def test_maybank_csv_description_is_not_credit_column():
    st = parse_maybank_csv(MAYBANK_CSV)
    amounts = [t.amount for t in st.transactions]
    # regression: "description" contains "cr" and used to be read as Credit
    assert amounts == [Decimal("8000.00"), Decimal("-100.00"), Decimal("-1000.00")]
    assert st.transactions[1].balance_after == Decimal("10400.00")


def test_maybank_csv_signed_amount_column():
    data = "Transaction Date;Transaction Description;Amount\n2026-09-05;GRAB FOOD;-25.40\n2026-09-06;REFUND;10.00\n"
    st = parse_maybank_csv(data)
    assert [t.amount for t in st.transactions] == [Decimal("-25.40"), Decimal("10.00")]
    assert st.transactions[0].booked_on == date(2026, 9, 5)


MAYBANK_PDF = """MALAYAN BANKING BERHAD
STATEMENT DATE : 30/09/26
ENTRY DATE TRANSACTION DESCRIPTION TRANSACTION AMOUNT STATEMENT BALANCE
BEGINNING BALANCE 2,500.00
01/09/26 IBG CREDIT 8,000.00+ 10,500.00
ACME TECH SDN BHD
SALARY SEP
02/09/26 FPX PAYMENT FR A/C 50.00- 10,450.00
SHOPEE
ENDING BALANCE : 10,450.00
"""


def test_maybank_pdf_trailing_sign_and_continuation():
    st = parse_maybank_pdf_text(MAYBANK_PDF)
    assert [t.amount for t in st.transactions] == [Decimal("8000.00"), Decimal("-50.00")]
    assert st.transactions[0].description == "IBG CREDIT ACME TECH SDN BHD SALARY SEP"
    assert st.transactions[1].description == "FPX PAYMENT FR A/C SHOPEE"
    assert st.transactions[0].booked_on == date(2026, 9, 1)


def test_tng_same_day_rows_in_chronological_order():
    st = parse_tng_text(TNG_TEXT)
    same_day = [t for t in st.transactions if t.booked_on == date(2026, 9, 22)]
    # newest-first statement: the Grab row is listed below the receive, so it happened first
    assert [t.external_ref[-1] for t in same_day] == ["5", "4"]


# Layout since 2026-09: every column wraps, a row runs until "RMamount RMbalance".
TNG_WRAPPED_TEXT = """TNG WALLET TRANSACTION HISTORY
1 August 2026 - 31 August 2026
Date Status Transaction Type Reference Description Details Amount (RM) Wallet Balance
1/8/2026 Success CARDISSUANCE_
PAYMENT
20260801101
10000010000
TNGOW3MY1
71295320000
111
Payment - ANTHROPIC
14150000000
202608011112128001001700000000
11111
RM20.00 RM80.00
2/8/2026 Success DUITNOW_RECEI
VEFROM
20260802111
21700010100
17129530000
2222
JOHN OWNER 2026080210110000010000TNGOW3
MY171295320000222
RM200.00 RM280.00
*This is a system generated email. Please do not reply to this email.
+603 5022 3888. The operating hours are Monday to Sunday.
2/8/2026 Success Payment 20260802101
10000010000
TNGOW3MY1
71295320000
333
CelcomDigi Mobile Sdn Bhd 202608022112128001101700000000
33333
RM50.00 RM230.00
3/8/2026 Success Receive from Wallet20260803111
21700010300
17106040000
4444
JANE DOE 2026080310110000010000TNGOW3
MY171060420000444
RM1,000.00 RM1,230.00
3/8/2026 Success Cashback 20260803211
22590230017
12955190000
55
In-store Up to RM5 Cashback Blind
Box
2026080310110000010000TNGOW3
MY171295320000555
RM0.01 RM1,230.01
4/8/2026 Reversed CARDISSUANCE_
PAYMENT
20260804101
10000010000
TNGOW3MY1
71295320000
666
Payment - GRAB RIDES-EC
PETALING JAYA
202608041112128001001700000000
66666
RM37.00 RM1,193.01
4/8/2026 Success CARDISSUANCE_
VOID
20260804111
21280450017
12949300000
77
Reversal -  GRAB RIDES-EC
PETALING JAYA
2026080410110000040000TNGOW3
MY171295320000777
RM37.00 RM1,230.01
"""


def test_tng_wrapped_layout():
    from finance.parsers.tng import looks_like_tng

    assert looks_like_tng(TNG_WRAPPED_TEXT)
    st = parse_tng_text(TNG_WRAPPED_TEXT)
    rows = [(t.raw_type, t.description, t.amount, t.balance_after) for t in st.transactions]
    assert rows == [
        ("CARDISSUANCE_PAYMENT", "Payment - ANTHROPIC 14150000000", Decimal("-20.00"), Decimal("80.00")),
        ("DUITNOW_RECEIVEFROM", "JOHN OWNER", Decimal("200.00"), Decimal("280.00")),
        ("Payment", "CelcomDigi Mobile Sdn Bhd", Decimal("-50.00"), Decimal("230.00")),
        ("Receive from Wallet", "JANE DOE", Decimal("1000.00"), Decimal("1230.00")),
        ("Cashback", "In-store Up to RM5 Cashback Blind Box", Decimal("0.01"), Decimal("1230.01")),
        ("CARDISSUANCE_PAYMENT", "Payment - GRAB RIDES-EC PETALING JAYA", Decimal("-37.00"), Decimal("1193.01")),
        ("CARDISSUANCE_VOID", "Reversal - GRAB RIDES-EC PETALING JAYA", Decimal("37.00"), Decimal("1230.01")),
    ]
    assert st.transactions[0].external_ref == "2026080110110000010000TNGOW3MY171295320000111"
    # the reversed payment and its VOID both stay out of spending
    assert [t.reversed for t in st.transactions] == [False] * 5 + [True, True]


def test_owner_id_username_does_not_crash(caplog):
    from finance.config import Settings

    assert Settings(DATABASE_URL="x", TELEGRAM_OWNER_ID="johndoe").TELEGRAM_OWNER_ID is None
    assert "numeric Telegram user id" in caplog.text
    assert Settings(DATABASE_URL="x", TELEGRAM_OWNER_ID="123456").TELEGRAM_OWNER_ID == 123456


def test_maybank_pdf_keeps_the_fund_line():
    text = """MALAYAN BANKING BERHAD
STATEMENT DATE : 31/08/26
25/08/26 TRANSFER FROM A/C 1,500.00- 8,421.50
ALEX MORGAN *
00000001
FUND Holiday
26/08/26 SALE DEBIT 99 SPEEDMART 12.00- 8,409.50
"""
    st = parse_maybank_pdf_text(text)
    assert st.transactions[0].description == "TRANSFER FROM A/C ALEX MORGAN * 00000001 FUND Holiday"
    assert st.transactions[1].description == "SALE DEBIT 99 SPEEDMART"


# The foot of one page and the top of the next (June 2026 statement): the last
# row's name, reference and pot line are printed under the next page's headers.
PAGE_FOOT = """21/06/26 SALE DEBIT 99 SPEEDMART 5.00- 43.21
22/06/26 TRANSFER FROM A/C 2.50- 40.71
Perhation / Note
(1) Semua maklumat dan baki yang dinyatakan di sini akan dianggap betul melainkan Bank telah dimaklumkan
tempoh 21 hari.
All items and balances shown will be considered correct unless the Bank is notified in writing
Please notify us of any change of address in writing.
Interest refers to the money earned from your Conventional account (s).
"""
PAGE_TOP = """Maybank Islamic Berhad (787435-M)
15th Floor, Tower A, Dataran Maybank, 1, Jalan Maarof, 59000 Kuala Lumpur
000010 IBS CONTOH MUKA/ 頁 /PAGE : 10
TARIKH PENYATA 結單日期 : 30/06/26
STATEMENT DATE
ALEX MORGAN
A-1-01 RESIDENSI CONTOH ,JALAN NOMBOR AKAUN 戶號 : 000000-000000
CONTOH 1 ,KUALA LUMPUR ,50000
WP KUALA LUMPUR ,MYS
PROTECTED BY PIDM UP TO RM250,000 FOR EACH DEPOSITOR SAVINGS ACCOUNT-I
URUSNIAGA AKAUN/ 戶口進支項 /ACCOUNT TRANSACTIONS
TARIKH MASUK BUTIR URUSNIAGA JUMLAH URUSNIAGA BAKI PENYATA
進支日期 進支項說明 银碼 結單存餘
ENTRY DATE TRANSACTION DESCRIPTION TRANSACTION AMOUNT STATEMENT BALANCE
"""


def test_maybank_pdf_row_continues_on_the_next_page():
    from finance.ledger import pot_name_from

    text = "MALAYAN BANKING BERHAD\n" + PAGE_FOOT + "\f" + PAGE_TOP + """ALEX MORGAN *
00000001
BOOSTER Holiday
23/06/26 SALE DEBIT GRAB 12.00- 28.71
GRAB KUALA LUMPUR
"""
    st = parse_maybank_pdf_text(text)
    assert [t.description for t in st.transactions] == [
        "SALE DEBIT 99 SPEEDMART",
        "TRANSFER FROM A/C ALEX MORGAN * 00000001 BOOSTER Holiday",
        "SALE DEBIT GRAB GRAB KUALA LUMPUR",
    ]
    assert pot_name_from(st.transactions[1].description) == "Holiday"


def test_maybank_pdf_continuation_split_between_pages():
    text = PAGE_FOOT.replace("40.71\n", "40.71\nALEX MORGAN *\n") + "\f" + PAGE_TOP + """00000001
FUND Holiday
ENDING BALANCE : 40.71
TOTAL DEBIT : 7.50
"""
    st = parse_maybank_pdf_text(text)
    assert st.transactions[1].description == "TRANSFER FROM A/C ALEX MORGAN * 00000001 FUND Holiday"


def test_maybank_pdf_next_page_starting_with_a_row_adds_nothing():
    text = PAGE_FOOT.replace("40.71\n", "40.71\nALEX MORGAN *\n00000001\nFUND Holiday\n") + "\f" + PAGE_TOP + (
        "23/06/26 SALE DEBIT GRAB 12.00- 28.71\nENDING BALANCE : 28.71\n\f" + PAGE_TOP + "SOME ADVERT TEXT\n"
    )
    st = parse_maybank_pdf_text(text)
    assert [t.description for t in st.transactions] == [
        "SALE DEBIT 99 SPEEDMART",
        "TRANSFER FROM A/C ALEX MORGAN * 00000001 FUND Holiday",
        "SALE DEBIT GRAB",
    ]


def test_crypto_amount():
    from finance.quick_entry import parse_crypto_amount

    assert parse_crypto_amount("60 usdt") == (Decimal("60"), "USDT")
    assert parse_crypto_amount("USDT 60") == (Decimal("60"), "USDT")
    assert parse_crypto_amount("0,00061 btc") == (Decimal("0.00061"), "BTC")
    assert parse_crypto_amount("1,500 USDT") == (Decimal("1500"), "USDT")
    assert parse_crypto_amount("1 500.25") == (Decimal("1500.25"), None)
    assert parse_crypto_amount("usdt") is None
    assert parse_crypto_amount("60 USDT BTC") is None


def test_screenshot_answer_to_payment():
    from datetime import datetime

    import pytest

    from finance.screenshot import Payment, ScreenshotError, payment_from

    answer = {
        "is_payment": True, "app": "tng", "direction": "out", "amount": "1,015.00", "currency": "RM",
        "counterparty": "  TAN  MEI LING ", "remark": "tan mei ling", "occurred_at": "2026-09-28 19:44",
        "to_person": True, "failure_reason": "",
    }
    p = payment_from(answer)
    assert (p.amount, p.currency, p.counterparty, p.remark, p.account) == (Decimal("1015.00"), "MYR", "TAN MEI LING", "", "tng")
    assert p.occurred_at == datetime(2026, 9, 28, 19, 44) and p.to_person and not p.incoming
    assert Payment.from_state(p.to_state()) == p

    assert payment_from({**answer, "app": "other", "occurred_at": ""}).account is None
    assert payment_from({**answer, "occurred_at": ""}).occurred_at is None
    with pytest.raises(ScreenshotError, match="баланс"):
        payment_from({**answer, "is_payment": False, "failure_reason": "это экран баланса"})
    with pytest.raises(ScreenshotError, match="сумму"):
        payment_from({**answer, "amount": "—"})
