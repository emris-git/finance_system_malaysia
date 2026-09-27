"""Scheduled work: weekly/monthly reports and mail autoimport.

Runs inside the API process (APScheduler, SCHEDULER_ENABLED=true) or as a
one-off command for a Railway cron service: `finance job weekly|monthly|mail`.
"""

from __future__ import annotations

import asyncio
import email
import email.policy
import imaplib
import logging
import re
from dataclasses import dataclass, field
from datetime import timedelta
from html import escape

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from finance import ledger, reports
from finance.bot import notify_owner
from finance.config import Settings, get_settings
from finance.db import get_sessionmaker
from finance.models import JobRun, MailMessage
from finance.parsers import ParseError, parse_file
from finance.utils import cycle_bounds, today, week_bounds

log = logging.getLogger("finance.jobs")

STATEMENT_SUBJECT = re.compile(r"statement|penyata|transaction history|e-?statement", re.IGNORECASE)


async def _claim(job: str, period_key: str) -> bool:
    """Record that a report for this period is being sent; False if it already was."""
    async with get_sessionmaker()() as session:
        session.add(JobRun(job=job, period_key=period_key))
        try:
            await session.commit()
        except IntegrityError:
            return False
    return True


async def send_weekly_report(force: bool = False) -> bool:
    start, _ = week_bounds(today() - timedelta(days=7))
    if not force and not await _claim("weekly", start.isoformat()):
        return False
    async with get_sessionmaker()() as session:
        text = await reports.week_report(session)
    await notify_owner(text)
    return True


async def send_monthly_report(force: bool = False) -> bool:
    """Runs on MONTH_START_DAY: reports the financial month that ended yesterday."""
    start, _ = cycle_bounds(today() - timedelta(days=1))
    if not force and not await _claim("monthly", start.isoformat()):
        return False
    async with get_sessionmaker()() as session:
        text = await reports.month_report(session, start)
    await notify_owner(text)
    return True


# --- mail --------------------------------------------------------------------


@dataclass
class MailItem:
    message_id: str
    sender: str
    subject: str
    attachments: list[tuple[str, bytes]] = field(default_factory=list)


def _fetch_mail(settings: Settings, known: set[str]) -> list[MailItem]:
    """Blocking IMAP read: new messages from configured senders, never marked as read."""
    since = (today() - timedelta(days=settings.MAIL_LOOKBACK_DAYS)).strftime("%d-%b-%Y")
    items: list[MailItem] = []
    with imaplib.IMAP4_SSL(settings.IMAP_HOST) as imap:
        imap.login(settings.IMAP_USER, settings.IMAP_PASSWORD)
        imap.select(settings.IMAP_FOLDER, readonly=True)
        numbers: set[bytes] = set()
        for sender in settings.mail_senders:
            _, data = imap.search(None, "SINCE", since, "FROM", f'"{sender}"')
            numbers.update(data[0].split())
        for num in sorted(numbers, key=int):
            _, data = imap.fetch(num, "(BODY.PEEK[])")
            raw = next((part[1] for part in data if isinstance(part, tuple)), None)
            if raw is None:
                continue
            msg = email.message_from_bytes(raw, policy=email.policy.default)
            message_id = str(msg.get("Message-ID") or f"imap-{num.decode()}").strip()
            if message_id in known:
                continue
            item = MailItem(message_id, str(msg.get("From", "")), str(msg.get("Subject", "")))
            for part in msg.iter_attachments():
                name = part.get_filename() or ""
                if name.lower().endswith((".pdf", ".csv")):
                    item.attachments.append((name, part.get_content()))
            items.append(item)
    return items


async def poll_mail() -> int:
    """Import statement attachments from the mailbox. Returns the number of imported files."""
    settings = get_settings()
    if not (settings.IMAP_USER and settings.IMAP_PASSWORD):
        return 0
    async with get_sessionmaker()() as session:
        known = set((await session.scalars(select(MailMessage.message_id))).all())
    items = await asyncio.to_thread(_fetch_mail, settings, known)

    imported = 0
    for item in items:
        status, details = "skipped", []
        async with get_sessionmaker()() as session:
            for filename, data in item.attachments:
                try:
                    statement = parse_file(data, filename, settings.pdf_passwords)
                    result = await ledger.import_statement(
                        session, statement, origin="email", filename=filename, file_bytes=data
                    )
                except (ParseError, ledger.LedgerError) as exc:
                    details.append(f"{filename}: {exc}")
                    await notify_owner(f"📧 Не смог разобрать вложение «{escape(filename)}» из письма «{escape(item.subject)}»: {escape(str(exc))}")
                    continue
                status = "imported"
                imported += 1
                details.append(f"{filename}: +{result.new}")
                if not result.already_imported:
                    await notify_owner("📧 Из почты\n" + reports.format_import(result) + ("\n/review" if result.to_review else ""))
            if not item.attachments and STATEMENT_SUBJECT.search(item.subject):
                await notify_owner(
                    f"📧 Письмо «{escape(item.subject)}» без вложения — похоже, выписку надо скачать в банке и прислать сюда."
                )
            session.add(
                MailMessage(
                    message_id=item.message_id,
                    sender=item.sender[:255],
                    subject=item.subject,
                    status=status,
                    detail="; ".join(details) or None,
                )
            )
            await session.commit()
    return imported


async def safe(job) -> None:
    try:
        await job()
    except Exception:  # scheduler must survive a bad run; the error goes to logs
        log.exception("job %s failed", getattr(job, "__name__", job))


def start_scheduler():
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.cron import CronTrigger
    from apscheduler.triggers.interval import IntervalTrigger

    settings = get_settings()
    scheduler = AsyncIOScheduler(timezone=settings.tz)
    scheduler.add_job(safe, CronTrigger(day_of_week="mon", hour=9, minute=0), args=[send_weekly_report])
    # the morning after payday (MONTH_START_DAY = payday + 1)
    scheduler.add_job(safe, CronTrigger(day=settings.MONTH_START_DAY, hour=9, minute=30), args=[send_monthly_report])
    if settings.IMAP_USER and settings.IMAP_PASSWORD:
        scheduler.add_job(safe, IntervalTrigger(minutes=30), args=[poll_mail])
    scheduler.start()
    return scheduler
