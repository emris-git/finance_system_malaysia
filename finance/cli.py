"""Command line: `finance <command>` (or `python -m finance.cli`).

    finance seed                         accounts, categories, default rules
    finance parse FILE [--password PW]   print parsed rows as JSON (no DB)
    finance import FILE [--account CODE] import into the DB named by DATABASE_URL
    finance import FILE --api URL        upload to a deployed API (needs API_TOKEN)
    finance report week|lastweek|month [--send]
    finance job weekly|monthly [--force]
    finance event --stdin                card payment from the iPhone (Apple Pay automation over SSH)
    finance link                         dashboard login link (valid 15 min)
    finance demo                         fill an empty DB with sample data
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path


def _print_json(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


async def _seed() -> None:
    from finance.db import get_sessionmaker
    from finance.seed import seed

    from finance.ledger import apply_rules_to_review_queue

    async with get_sessionmaker()() as session:
        print("added:", await seed(session))
        changed, paired = await apply_rules_to_review_queue(session)
        print(f"review queue: {changed} rows reclassified, {paired} transfers paired")


def _parse(args) -> None:
    from dataclasses import asdict

    from finance.config import get_settings
    from finance.parsers import parse_file

    passwords = [args.password] if args.password else get_settings().pdf_passwords
    data = Path(args.file).read_bytes()
    statement = parse_file(data, args.file, passwords, args.account)
    _print_json({"account": statement.account_code, "source": statement.source,
                 "transactions": [asdict(t) for t in statement.transactions]})
    print(f"{len(statement.transactions)} rows", file=sys.stderr)


async def _import(args) -> None:
    data = Path(args.file).read_bytes()
    if args.api:
        import httpx

        token = os.environ.get("API_TOKEN")
        if not token:
            sys.exit("API_TOKEN is not set")
        form = {"origin": "cli", **({"account": args.account} if args.account else {})}
        response = httpx.post(
            args.api.rstrip("/") + "/api/imports",
            headers={"Authorization": f"Bearer {token}"},
            files={"file": (Path(args.file).name, data)},
            data=form,
            timeout=120,
        )
        response.raise_for_status()
        _print_json(response.json())
        return

    from finance import ledger, reports
    from finance.config import get_settings
    from finance.db import get_sessionmaker
    from finance.parsers import parse_file

    statement = parse_file(data, args.file, get_settings().pdf_passwords, args.account)
    async with get_sessionmaker()() as session:
        result = await ledger.import_statement(
            session, statement, origin="cli", filename=Path(args.file).name, file_bytes=data
        )
    print(reports.format_import(result).replace("<b>", "").replace("</b>", ""))


async def _report(args) -> None:
    from finance import reports
    from finance.bot import notify_owner
    from finance.db import get_sessionmaker

    async with get_sessionmaker()() as session:
        if args.period == "week":
            text = await reports.current_week_report(session)
        elif args.period == "lastweek":
            text = await reports.week_report(session)
        else:
            text = await reports.month_report(session)
    if args.send:
        await notify_owner(text)
    print(text)


async def _job(args) -> None:
    from finance import jobs

    job = jobs.send_weekly_report if args.name == "weekly" else jobs.send_monthly_report
    print("sent" if await job(args.force) else "already sent")


async def _event(args) -> None:
    """Card payment from the iPhone automation: posts to FINANCE_API_URL, or writes to DATABASE_URL.

    With --stdin the transaction comes from the "Input" of the Shortcuts SSH action,
    so shop names never pass through the shell command line.
    """
    from finance.config import CLIENT_ENV, ClientSettings
    from finance.ledger import account_for_card
    from finance.quick_entry import parse_device_event
    from finance.utils import fmt_money, parse_amount

    if args.stdin:
        raw = sys.stdin.read()
        try:
            event = parse_device_event(raw)
        except ValueError as exc:
            # keep what the phone sent: the Shortcut runs unattended, this is the only trace
            CLIENT_ENV.parent.mkdir(parents=True, exist_ok=True)
            (CLIENT_ENV.parent / "last_event.txt").write_text(raw)
            sys.exit(f"не понял транзакцию: {exc}. Получено: {raw[:300]!r}")
    else:
        if not args.amount:
            sys.exit("нужен --amount или --stdin")
        event = {"amount": args.amount, "merchant": args.merchant or "", "card": args.card,
                 "account": args.account, "income": args.income}

    merchant = event["merchant"] or "Apple Pay"
    account = event["account"] or account_for_card(event["card"])
    value = abs(parse_amount(event["amount"]))
    amount = value if event["income"] else -value

    client = ClientSettings()
    api = args.api or client.FINANCE_API_URL
    if api:
        import httpx

        if not client.API_TOKEN:
            sys.exit("API_TOKEN is not set")
        response = httpx.post(
            api.rstrip("/") + "/api/events",
            headers={"Authorization": f"Bearer {client.API_TOKEN}"},
            json={"amount": str(value), "merchant": merchant, "account": account, "income": event["income"]},
            timeout=20,
        )
        response.raise_for_status()
        label = response.json().get("category_label") or "—"
    else:
        from finance import ledger
        from finance.db import get_sessionmaker
        from finance.models import PENDING

        async with get_sessionmaker()() as session:
            txn = await ledger.add_manual(
                session, account, amount, merchant, status=PENDING, source="shortcut", review=True
            )
            label = txn.category.label if txn.category else "—"
    print(f"✅ {fmt_money(amount)} · {merchant} → {label}")


async def _demo() -> None:
    from finance.db import get_sessionmaker
    from finance.demo import fill_demo

    async with get_sessionmaker()() as session:
        print(await fill_demo(session))


def main() -> None:
    parser = argparse.ArgumentParser(prog="finance", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("seed")
    p = sub.add_parser("parse")
    p.add_argument("file")
    p.add_argument("--password")
    p.add_argument("--account")
    p = sub.add_parser("import")
    p.add_argument("file")
    p.add_argument("--account")
    p.add_argument("--api", help="base URL of the deployed service")
    p = sub.add_parser("report")
    p.add_argument("period", choices=["week", "lastweek", "month"])
    p.add_argument("--send", action="store_true", help="also send to Telegram")
    p = sub.add_parser("job")
    p.add_argument("name", choices=["weekly", "monthly"])
    p.add_argument("--force", action="store_true")
    p = sub.add_parser("event", help="card payment pushed from the phone")
    p.add_argument("--stdin", action="store_true", help="read the transaction (JSON or key: value lines) from stdin")
    p.add_argument("--amount", help='as the phone formats it: "RM12.50", "12,50 RM"')
    p.add_argument("--merchant")
    p.add_argument("--card", help="Wallet card name; picks the account")
    p.add_argument("--account", help="account code, overrides --card")
    p.add_argument("--income", action="store_true", help="money in (refund)")
    p.add_argument("--api", help="base URL of the deployed service (default: FINANCE_API_URL)")
    sub.add_parser("link")
    sub.add_parser("demo")
    args = parser.parse_args()

    if args.command == "parse":
        _parse(args)
    elif args.command == "link":
        from finance.web.auth import login_enabled, magic_link

        if not login_enabled():
            sys.exit("SECRET_KEY is not set")
        print(magic_link())
    else:
        handler = {
            "seed": lambda: _seed(),
            "import": lambda: _import(args),
            "report": lambda: _report(args),
            "job": lambda: _job(args),
            "event": lambda: _event(args),
            "demo": lambda: _demo(),
        }[args.command]
        asyncio.run(handler())


if __name__ == "__main__":
    main()
