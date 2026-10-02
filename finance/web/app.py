"""FastAPI app: dashboard, JSON API, uploads, device events, Telegram webhook."""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
from html import escape
from contextlib import asynccontextmanager
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy import func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from finance import agent, budget, ledger, reports
from finance.config import get_settings
from finance.db import get_db
from finance.models import PENDING, REVIEW_UNCATEGORIZED, Account, Category, PlannedItem, Transaction
from finance.parsers import MAX_FILE_BYTES, ParseError, parse_file
from finance.quick_entry import parse_device_event
from finance.utils import cycle_bounds, parse_amount, to_decimal, today
from finance.web.auth import (
    COOKIE_NAME,
    SESSION_MAX_AGE,
    check_magic,
    is_authenticated,
    login_enabled,
    new_session_value,
    require_token,
    require_user,
)

log = logging.getLogger("finance.web")
HERE = Path(__file__).parent
templates = Jinja2Templates(directory=HERE / "templates")
DB = Annotated[AsyncSession, Depends(get_db)]
_background: set[asyncio.Task] = set()


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    scheduler = None
    if settings.TELEGRAM_BOT_TOKEN and settings.PUBLIC_BASE_URL and not settings.TELEGRAM_WEBHOOK_SECRET:
        log.error("TELEGRAM_WEBHOOK_SECRET is not set: the webhook is not registered and the bot stays silent")
    elif settings.TELEGRAM_BOT_TOKEN and settings.PUBLIC_BASE_URL:
        from finance.bot import COMMANDS, get_bot, get_dispatcher

        bot, dp = get_bot(), get_dispatcher()
        await bot.set_webhook(
            f"{settings.PUBLIC_BASE_URL.rstrip('/')}/telegram/webhook",
            secret_token=settings.TELEGRAM_WEBHOOK_SECRET,
            allowed_updates=dp.resolve_used_update_types(),
            drop_pending_updates=False,
        )
        await bot.set_my_commands(COMMANDS)
    if settings.SCHEDULER_ENABLED:
        from finance.jobs import start_scheduler

        scheduler = start_scheduler()
    yield
    if scheduler:
        scheduler.shutdown(wait=False)
    if settings.TELEGRAM_BOT_TOKEN:
        from finance.bot import get_bot

        await get_bot().session.close()


app = FastAPI(title="Finance", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")

# Scripts only from /static; inline style attributes stay allowed (the dashboard uses a few).
# no-referrer keeps the magic link token out of Referer headers.
SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
        "object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
}


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    for name, value in SECURITY_HEADERS.items():
        response.headers.setdefault(name, value)
    return response


# --- pages -------------------------------------------------------------------


@app.get("/healthz")
async def healthz(session: DB):
    await session.execute(text("SELECT 1"))
    return {"ok": True}


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    page = "dashboard.html" if is_authenticated(request) else "login.html"
    return templates.TemplateResponse(request, page, {"login_enabled": login_enabled()})


@app.get("/auth/magic")
async def auth_magic(token: str):
    if not check_magic(token):
        return RedirectResponse("/?expired=1", status_code=303)
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(
        COOKIE_NAME,
        new_session_value(),
        max_age=SESSION_MAX_AGE,
        httponly=True,
        secure=bool(get_settings().PUBLIC_BASE_URL),
        samesite="lax",
    )
    return response


@app.get("/auth/logout")
async def auth_logout():
    response = RedirectResponse("/", status_code=303)
    response.delete_cookie(COOKIE_NAME)
    return response


# --- read API ----------------------------------------------------------------


def _period(start: date | None, end: date | None) -> tuple[date, date]:
    if start and end:
        return start, end
    s, e = cycle_bounds(today())
    return s, min(e, today())


def _txn_json(t: Transaction) -> dict:
    return {
        "id": t.id,
        "date": t.booked_on.isoformat(),
        "account": t.account.code,
        "account_name": t.account.name,
        "currency": t.account.currency,
        "amount": t.amount,
        "description": t.description,
        "merchant": t.merchant,
        "kind": t.kind,
        "category": t.category.code if t.category else None,
        "category_label": t.category.label if t.category else None,
        "status": t.status,
        "review": t.review_reason,
        "transfer": ({"kind": t.transfer.kind, "rate": t.transfer.rate} if t.transfer else None),
        "note": t.note,
        "source": t.source,
    }


@app.get("/api/meta", dependencies=[Depends(require_user)])
async def api_meta(session: DB):
    accounts = (await session.scalars(select(Account).order_by(Account.sort))).all()
    categories = (await session.scalars(select(Category).order_by(Category.sort))).all()
    cycle = cycle_bounds(today())
    prev_cycle = cycle_bounds(cycle[0] - timedelta(days=1))
    return {
        "accounts": [{"code": a.code, "name": a.name, "currency": a.currency} for a in accounts],
        "categories": [
            {"id": c.id, "code": c.code, "label": c.label, "name": c.name, "emoji": c.emoji, "kind": c.kind}
            for c in categories
        ],
        "review_count": await ledger.review_count(session),
        "fx_rate": await reports.latest_fx_rate(session),
        "today": today().isoformat(),
        "month_start_day": get_settings().MONTH_START_DAY,
        "cycle": {"start": cycle[0], "end": cycle[1]},
        "prev_cycle": {"start": prev_cycle[0], "end": prev_cycle[1]},
    }


@app.get("/api/summary", dependencies=[Depends(require_user)])
async def api_summary(session: DB, start: date | None = None, end: date | None = None):
    start, end = _period(start, end)
    s = await reports.summarize(session, start, end, baseline_periods=3)
    return {
        "start": s.start,
        "end": s.end,
        "blocks": {
            cur: {
                "expense": b.expense,
                "income": b.income,
                "baseline_expense": b.baseline_expense,
                "categories": [
                    {"code": c.code, "label": c.label, "amount": c.amount, "baseline": c.baseline} for c in b.categories
                ],
                "merchants": [{"merchant": m, "amount": a, "count": n} for m, a, n in b.merchants],
            }
            for cur, b in s.blocks.items()
        },
        "fx": {"myr": s.fx_myr, "rub": s.fx_rub, "count": s.fx_count, "rate": s.fx_rate},
        "fx_back": {"myr": s.fx_back_myr, "rub": s.fx_back_rub},
        "crypto": {"paid": s.crypto_paid, "got": s.crypto_got},
        "unresolved": {"count": s.unresolved_count, "amount": s.unresolved_amount},
        "review_count": s.review_count,
        "freshness": [{"account": f.account, "last_day": f.last_day, "days_old": f.days_old} for f in s.freshness],
        "balances": [
            {"account": name, "currency": cur, "amount": amount, "as_of": as_of}
            for name, cur, amount, as_of in s.balances
        ],
    }


@app.get("/api/monthly", dependencies=[Depends(require_user)])
async def api_monthly(session: DB, currency: str = "MYR", months: int = Query(12, ge=1, le=36)):
    return await reports.monthly_series(session, currency, months)


@app.get("/api/daily", dependencies=[Depends(require_user)])
async def api_daily(session: DB, currency: str = "MYR", start: date | None = None, end: date | None = None):
    start, end = _period(start, end)
    return await reports.daily_series(session, start, end, currency)


@app.get("/api/budget", dependencies=[Depends(require_user)])
async def api_budget(session: DB):
    return budget.forecast_json(await budget.forecast(session))


def _plan_json(item: PlannedItem) -> dict:
    return {
        "id": item.id,
        "title": item.title,
        "amount": item.amount,
        "kind": item.kind,
        "category": item.category.code if item.category else None,
        "category_label": item.category.label if item.category else None,
        "due_on": item.due_on,
        "repeat_months": item.repeat_months,
        "until": item.until,
        "status": item.status,
        "note": item.note,
    }


class PlanBody(BaseModel):
    title: str
    amount: Decimal  # positive; the sign follows the kind
    due_on: date
    kind: Literal["expense", "transfer", "income"] = "expense"
    category: str | None = None
    repeat_months: int | None = None
    until: date | None = None
    note: str | None = None


class PlanPatch(BaseModel):
    status: Literal["planned", "done", "cancelled"]


@app.get("/api/plans", dependencies=[Depends(require_user)])
async def api_plans(session: DB):
    return [_plan_json(item) for item in await budget.open_plans(session)]


@app.post("/api/plans", dependencies=[Depends(require_user)])
async def api_add_plan(body: PlanBody, session: DB):
    amount = abs(body.amount) if body.kind == "income" else -abs(body.amount)
    try:
        item = await budget.add_plan(
            session, body.title, amount, body.due_on, body.kind, body.category or None,
            body.repeat_months or None, body.until, body.note or None,
        )
    except ledger.LedgerError as exc:
        raise HTTPException(400, str(exc)) from exc
    return _plan_json(item)


@app.patch("/api/plans/{item_id}", dependencies=[Depends(require_user)])
async def api_patch_plan(item_id: int, body: PlanPatch, session: DB):
    try:
        item = await budget.close_plan(session, item_id, body.status)
    except ledger.LedgerError as exc:
        raise HTTPException(404, str(exc)) from exc
    await session.refresh(item, ["category"])
    return _plan_json(item)


@app.get("/api/transactions", dependencies=[Depends(require_user)])
async def api_transactions(
    session: DB,
    start: date | None = None,
    end: date | None = None,
    account: str | None = None,
    category: str | None = None,
    kind: str | None = None,
    q: str | None = None,
    review: bool = False,
    anytime: bool = False,  # the dashboard search: find a row from any month to fix it
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    conditions = [Transaction.deleted_at.is_(None)]
    if review:
        conditions.append(ledger.review_filter())
    elif not anytime:
        s, e = _period(start, end)
        conditions.append(Transaction.booked_on.between(s, e))
    if account:
        conditions.append(Transaction.account.has(Account.code == account))
    if category:
        conditions.append(Transaction.category.has(Category.code == category))
    if kind:
        conditions.append(Transaction.kind == kind)
    if q:
        like = f"%{q}%"
        conditions.append(or_(Transaction.description.ilike(like), Transaction.note.ilike(like)))

    total = await session.scalar(select(func.count(Transaction.id)).where(*conditions))
    rows = (
        await session.scalars(
            select(Transaction)
            .where(*conditions)
            .order_by(Transaction.booked_on.desc(), Transaction.id.desc())
            .limit(limit)
            .offset(offset)
        )
    ).unique().all()
    return {"total": total, "items": [_txn_json(t) for t in rows]}


# --- write API ---------------------------------------------------------------


class TxnPatch(BaseModel):
    category: str | None = None
    remember: bool = False
    note: str | None = None
    action: str | None = None  # keep / own / delete


class FxBody(BaseModel):
    rub_amount: Decimal


async def _load(session: AsyncSession, txn_id: int) -> Transaction:
    txn = await session.scalar(select(Transaction).where(Transaction.id == txn_id, Transaction.deleted_at.is_(None)))
    if txn is None:
        raise HTTPException(404, "transaction not found")
    return txn


@app.patch("/api/transactions/{txn_id}", dependencies=[Depends(require_user)])
async def api_patch_transaction(txn_id: int, body: TxnPatch, session: DB):
    txn = await _load(session, txn_id)
    if body.note is not None:
        txn.note = body.note or None
        await session.commit()
    if body.category:
        category = await ledger.get_category(session, body.category)
        await ledger.set_category(session, txn, category, remember=body.remember)
    if body.action == "keep":
        await ledger.dismiss_review(session, txn)
    elif body.action == "own":
        await ledger.mark_internal(session, txn)
    elif body.action == "delete":
        await ledger.soft_delete(session, txn)
        return {"deleted": True}
    await session.refresh(txn, ["category", "transfer"])
    return _txn_json(txn)


@app.post("/api/transactions/{txn_id}/fx", dependencies=[Depends(require_user)])
async def api_mark_fx(txn_id: int, body: FxBody, session: DB):
    txn = await _load(session, txn_id)
    try:
        transfer = await ledger.mark_fx(session, txn, to_decimal(body.rub_amount))
    except ledger.LedgerError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"transfer_id": transfer.id, "rate": transfer.rate}


@app.post("/api/imports", dependencies=[Depends(require_user)])
async def api_import(
    session: DB,
    file: UploadFile = File(...),
    account: str | None = Form(None),
    origin: str = Form("api"),
):
    from finance.bot import notify_owner

    data = await file.read(MAX_FILE_BYTES + 1)
    if len(data) > MAX_FILE_BYTES:
        raise HTTPException(413, "файл больше 15 МБ")
    try:
        statement = parse_file(data, file.filename or "", get_settings().pdf_passwords, account)
        result = await ledger.import_statement(
            session, statement, origin=origin[:16], filename=file.filename, file_bytes=data
        )
    except (ParseError, ledger.LedgerError) as exc:
        raise HTTPException(400, str(exc)) from exc
    if not result.already_imported:
        _spawn(notify_owner(reports.format_import(result) + ("\n/review" if result.to_review else "")))
    return {
        "account": result.account_name,
        "new": result.new,
        "duplicate": result.duplicate,
        "reconciled": result.reconciled,
        "matched_transfers": result.matched_transfers,
        "to_review": result.to_review,
        "already_imported": result.already_imported,
    }


@app.post("/api/events", dependencies=[Depends(require_token)])
async def api_event(request: Request, session: DB):
    """Apple Pay automation: the Mac CLI, or the Shortcut itself via "Get Contents of URL".

    The body is taken in whatever shape the Shortcut builds it (JSON with numbers,
    Russian or capitalized keys, a form, "key: value" text): amount, merchant,
    card or account, optional occurred_at and income. A rejected body goes to the
    bot as is: the Shortcut runs unattended and nobody reads its response.
    """
    from finance.bot import notify_owner

    if request.headers.get("content-type", "").startswith(("multipart/form-data", "application/x-www-form-urlencoded")):
        raw: str | dict = {k: v for k, v in (await request.form()).items() if isinstance(v, str)}
    else:
        raw = (await request.body()).decode("utf-8", "replace")
    try:
        event = parse_device_event(raw)
        value = abs(parse_amount(event["amount"]))
        if not value:
            raise ValueError(f"сумма не распознана: {event['amount']!r}")
    except ValueError as exc:
        shown = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)
        log.warning("device event rejected: %s; body: %.500s", exc, shown)
        _spawn(notify_owner(
            f"⚠️ Покупка с телефона не записалась: {escape(str(exc))}\nПришло: <code>{escape(shown[:300])}</code>"
        ))
        raise HTTPException(422, f"не понял транзакцию: {exc}") from exc

    occurred_at = event["occurred_at"]
    day = occurred_at.astimezone(get_settings().tz).date() if occurred_at else today()
    txn = await ledger.add_manual(
        session,
        event["account"] or ledger.account_for_card(event["card"]),
        value if event["income"] else -value,
        event["merchant"] or "Apple Pay",
        day,
        status=PENDING,
        source="shortcut",
        review=True,
    )
    if txn.review_reason == REVIEW_UNCATEGORIZED:
        from finance.bot.handlers import event_card

        text, markup = await event_card(session, txn)
        _spawn(notify_owner(text, markup))
    return {
        "id": txn.id,
        "amount": txn.amount,
        "account": txn.account.code,
        "category": txn.category.code if txn.category else None,
        "category_label": txn.category.label if txn.category else None,
    }


# --- categorization agent (Claude routine) -----------------------------------


class AgentCategorize(BaseModel):
    transaction_id: int
    category: str
    note: str | None = None
    remember: bool = False


class NotifyIn(BaseModel):
    text: str


@app.post("/api/agent/categorize", dependencies=[Depends(require_token)])
async def api_agent_categorize(body: AgentCategorize, session: DB):
    txn = await _load(session, body.transaction_id)
    try:
        category = await ledger.get_category(session, body.category)
        updated = await agent.categorize(session, txn, category, body.note, body.remember)
    except agent.AgentRefused as exc:
        raise HTTPException(409, str(exc)) from exc
    except ledger.LedgerError as exc:
        raise HTTPException(400, str(exc)) from exc
    await session.refresh(txn, ["category"])
    return {**_txn_json(txn), "also_updated": updated}


@app.post("/api/notify", dependencies=[Depends(require_token)])
async def api_notify(body: NotifyIn):
    from finance.bot import notify_owner

    await notify_owner(escape(body.text[:3500]))
    return {"ok": True}


# --- Telegram ----------------------------------------------------------------


def _spawn(coro) -> None:
    task = asyncio.create_task(coro)
    _background.add(task)
    task.add_done_callback(_background.discard)


@app.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    settings = get_settings()
    secret = settings.TELEGRAM_WEBHOOK_SECRET
    if not settings.TELEGRAM_BOT_TOKEN:
        raise HTTPException(404)
    # The body names the sender, so only the secret proves an update came from Telegram:
    # without one, anyone could post an update "from" the owner.
    header = request.headers.get("x-telegram-bot-api-secret-token", "")
    if not secret or not hmac.compare_digest(header.encode(), secret.encode()):
        raise HTTPException(403)
    from aiogram.types import Update

    from finance.bot import get_bot, get_dispatcher

    bot = get_bot()
    update = Update.model_validate(await request.json(), context={"bot": bot})
    # Answer Telegram right away; a big PDF import may take longer than its timeout.
    _spawn(get_dispatcher().feed_update(bot, update))
    return JSONResponse({"ok": True})
