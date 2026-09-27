"""Scheduled work: weekly and monthly reports.

Runs inside the API process (APScheduler, SCHEDULER_ENABLED=true) or as a
one-off command for a Railway cron service: `finance job weekly|monthly`.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy.exc import IntegrityError

from finance import reports
from finance.bot import notify_owner
from finance.config import get_settings
from finance.db import get_sessionmaker
from finance.models import JobRun
from finance.utils import cycle_bounds, today, week_bounds

log = logging.getLogger("finance.jobs")


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


async def safe(job) -> None:
    try:
        await job()
    except Exception:  # scheduler must survive a bad run; the error goes to logs
        log.exception("job %s failed", getattr(job, "__name__", job))


def start_scheduler():
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.cron import CronTrigger

    settings = get_settings()
    scheduler = AsyncIOScheduler(timezone=settings.tz)
    scheduler.add_job(safe, CronTrigger(day_of_week="mon", hour=9, minute=0), args=[send_weekly_report])
    # the morning after payday (MONTH_START_DAY = payday + 1)
    scheduler.add_job(safe, CronTrigger(day=settings.MONTH_START_DAY, hour=9, minute=30), args=[send_monthly_report])
    scheduler.start()
    return scheduler
