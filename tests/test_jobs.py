from sqlalchemy import func, select

from finance import jobs
from finance.config import get_settings
from finance.models import MailMessage, Transaction
from finance.utils import today


async def test_mail_poll_imports_once(session, monkeypatch):
    sent = []

    async def fake_notify(text):
        sent.append(text)

    csv = f"Date,Description,Debit,Credit\n{today():%d/%m/%Y},GRAB FOOD,25.00,\n".encode()
    items = [
        jobs.MailItem("<a@maybank>", "alerts@maybank2u.com.my", "Your statement", [("stmt.csv", csv)]),
        jobs.MailItem("<b@maybank>", "alerts@maybank2u.com.my", "Your e-Statement is ready", []),
    ]
    seen_known = []

    def fake_fetch(settings, known):
        seen_known.append(set(known))
        return [i for i in items if i.message_id not in known]

    monkeypatch.setattr(jobs, "_fetch_mail", fake_fetch)
    monkeypatch.setattr(jobs, "notify_owner", fake_notify)
    monkeypatch.setattr(get_settings(), "IMAP_USER", "me@gmail.com")
    monkeypatch.setattr(get_settings(), "IMAP_PASSWORD", "app-password")

    assert await jobs.poll_mail() == 1
    assert await session.scalar(select(func.count(Transaction.id))) == 1
    assert any("Из почты" in t for t in sent)
    assert any("без вложения" in t for t in sent)  # link-only statement mail -> nudge

    assert await jobs.poll_mail() == 0  # both messages remembered
    assert seen_known[-1] == {"<a@maybank>", "<b@maybank>"}
    assert await session.scalar(select(func.count(MailMessage.id))) == 2


async def test_weekly_report_sent_once(session, monkeypatch):
    sent = []

    async def fake_notify(text):
        sent.append(text)

    monkeypatch.setattr(jobs, "notify_owner", fake_notify)
    assert await jobs.send_weekly_report() is True
    assert await jobs.send_weekly_report() is False
    assert len(sent) == 1 and "Неделя" in sent[0]
