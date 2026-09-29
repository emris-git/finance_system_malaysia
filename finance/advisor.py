"""/ask in the bot: Claude weighs a purchase or a plan against the budget forecast.

The forecast (finance.budget) is computed here and handed over as data. Claude can
re-run it with what-if purchases (`simulate`); its final message is JSON (the answer
and, optionally, a plan item the owner adds with one tap). It never writes to the
ledger itself. The offer is a field of the final answer rather than a tool: newer
models return text written between tool calls as hidden thinking, so everything the
owner sees has to come after the last tool call.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from html import escape

import anthropic
from sqlalchemy.ext.asyncio import AsyncSession

from finance import budget
from finance.config import get_settings
from finance.models import EXPENSE
from finance.utils import today

log = logging.getLogger(__name__)

MAX_TURNS = 6  # model calls per question: tool rounds included
HISTORY = 8  # messages of the conversation kept between questions

SYSTEM = """You are the budget analyst inside the Telegram bot of one person's finance system.
The owner lives in Malaysia; money is in MYR (RM). {month_rule}

The data below is today's budget forecast, computed by the system:
- liquid: money the forecast spends (bank, e-wallet, cash). reserve_pots: savings the owner allows
  to spend if needed (say so explicitly when you use them). protected_pots: goals; never plan to
  spend them, mention them only if nothing else works and say it breaks the goal.
- usual_spending_per_cycle: median of the last cycles per category; share_spent_after_salary: the part
  of a category that is usually paid right after the salary lands (the rent).
- cycles: for each coming cycle the opening money, the salary still expected, the usual spending
  still to come, the planned items, the closing money and low_before_salary (the lowest point, just
  before the salary). RF transfers and trips are NOT in the forecast unless they are planned items.

How to answer a question about a purchase or an expense:
1. Work out the amount and the timing. If the amount is missing, ask one short question instead.
2. Call `simulate` with the purchase (several items to model paying in parts or on another date).
   Money spent in a cycle lowers every later cycle too. A variant fits when, in every cycle from
   the purchase on, low_before_salary stays at or above a cushion of RM {cushion} and nothing goes
   negative.
3. Give the verdict and the concrete options that work, with numbers: pay now; pay right after the
   salary on a given date; a later month; in parts; which categories to cut and by how much (only
   flexible ones: eating out, shopping, subscriptions, fun; not rent or utilities); the reserve pot.
   If no option works, say so plainly and name what would have to change.
4. When one option is clearly the best and is not in the plan yet, offer it as a plan item
   (offer_plan_item, plan_title, plan_amount, plan_date): the owner gets a button to add it.
   Do not offer again what an earlier answer in this talk already offered.

Make every `simulate` call first. Your final message is JSON: `answer` is the whole reply the owner
sees; notes between tool calls are not shown.

Use only the data and the tool results; do not invent numbers or merchants. This is budgeting, not
financial advice: no investments, loans or crypto recommendations.

Write `answer` in Russian. Telegram HTML only: <b>, <i>, <code>; no Markdown, no headings, no tables. Start
with one verdict line (✅ пролезет / ⚠️ впритык / ❌ не пролезет, or a short answer for other
questions), then at most four short options. Keep it under 1200 characters.

Today is {today}. Forecast data:
{data}"""

TOOLS = [
    {
        "name": "simulate",
        "description": (
            "Re-run the budget forecast with hypothetical purchases added on top of the plan. Returns, per cycle, "
            "the closing money and the low point before the salary, and the first cycle that goes below zero. "
            "Pass several items to model paying in parts or on different dates."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string"},
                            "amount": {"type": "number", "description": "MYR, positive = money out"},
                            "date": {"type": "string", "description": "YYYY-MM-DD"},
                        },
                        "required": ["title", "amount", "date"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["items"],
            "additionalProperties": False,
        },
        "strict": True,
    }
]

ANSWER_SCHEMA = {
    "type": "object",
    "properties": {
        "answer": {"type": "string", "description": "the whole reply to the owner"},
        "offer_plan_item": {"type": "boolean"},
        "plan_title": {"type": "string", "description": "short, in Russian; empty when no offer"},
        "plan_amount": {"type": "number", "description": "MYR, positive = money out; 0 when no offer"},
        "plan_date": {"type": "string", "description": "YYYY-MM-DD; empty when no offer"},
    },
    "required": ["answer", "offer_plan_item", "plan_title", "plan_amount", "plan_date"],
    "additionalProperties": False,
}


def month_rule(start_day: int) -> str:
    if start_day == 1:
        return 'A "month" is a calendar month.'
    return (
        f'A "month" is the salary cycle: it starts on day {start_day} and ends on day {start_day - 1} '
        "of the next month, and is named after the month it ends in."
    )


class AdvisorError(Exception):
    """Shown to the owner as is (Russian)."""


@dataclass
class Proposal:
    title: str
    amount: Decimal  # positive = money out
    due_on: date

    def to_state(self) -> dict:
        return {"title": self.title, "amount": str(self.amount), "due_on": self.due_on.isoformat()}

    @classmethod
    def from_state(cls, data: dict) -> Proposal:
        return cls(data["title"], Decimal(data["amount"]), date.fromisoformat(data["due_on"]))


@dataclass
class Answer:
    text: str
    proposal: Proposal | None
    history: list[dict]


def enabled() -> bool:
    return bool(get_settings().ANTHROPIC_API_KEY)


def _dumps(data) -> str:
    return json.dumps(data, ensure_ascii=False, default=str, separators=(",", ":"))


def _parse_item(raw: dict) -> tuple[str, Decimal, date]:
    try:
        amount = abs(Decimal(str(raw["amount"]))).quantize(Decimal("0.01"))
        day = date.fromisoformat(str(raw["date"])[:10])
    except (KeyError, InvalidOperation, ValueError) as exc:
        raise ValueError(f"bad item {raw!r}: {exc}") from exc
    if not amount:
        raise ValueError("amount must not be zero")
    return str(raw.get("title") or "покупка")[:120], amount, day


async def _simulate(session: AsyncSession, items: list[dict]) -> str:
    extra = []
    for raw in items:
        title, amount, day = _parse_item(raw)
        extra.append(budget.PlanHit(0, title, -amount, EXPENSE, day))
    f = await budget.forecast(session, extra=extra)
    rows = [
        {"label": c.label, "start": c.start, "end": c.end, "closing": c.closing, "low_before_salary": c.low}
        for c in f.cycles
    ]
    short = next(({"label": c.label, "short_by": -c.low} for c in f.cycles if c.low < 0), None)
    return _dumps({"cycles": rows, "first_cycle_below_zero": short})


def clean_html(text: str) -> str:
    """Telegram rejects unknown tags and a bare "<": escape everything, then bring back
    <b>, <i> and <code>; Markdown bold becomes <b>."""
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = escape(text, quote=False)
    return re.sub(r"&lt;(/?)(b|i|code)&gt;", r"<\1\2>", text).strip()


def _final(response) -> tuple[str, Proposal | None]:
    """The answer and the offered plan item from the final JSON; plain text if it is not JSON."""
    raw = "\n".join(b.text for b in response.content if b.type == "text").strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return clean_html(raw), None
    proposal = None
    if data.get("offer_plan_item"):
        try:
            title, amount, day = _parse_item(
                {"title": data.get("plan_title"), "amount": data.get("plan_amount"), "date": data.get("plan_date")}
            )
            proposal = Proposal(title, amount, day)
        except ValueError:
            log.warning("advisor offered an unreadable plan item: %r", data)
    return clean_html(str(data.get("answer") or "")), proposal


async def ask(session: AsyncSession, question: str, history: list[dict] | None = None, model: str | None = None) -> Answer:
    settings = get_settings()
    f = await budget.forecast(session)
    usual = sum(f.baseline.values(), Decimal(0))
    cushion = max(Decimal(1000), (usual * Decimal("0.1")).quantize(Decimal(1)))
    system = SYSTEM.format(
        month_rule=month_rule(settings.MONTH_START_DAY),
        cushion=cushion,
        today=today().isoformat(),
        data=_dumps(budget.forecast_json(f)),
    )
    history = list(history or [])[-HISTORY:]
    messages = [*history, {"role": "user", "content": question}]
    client = anthropic.AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY, timeout=120.0)
    try:
        for _ in range(MAX_TURNS):
            response = await client.messages.create(
                model=model or settings.BUDGET_MODEL,
                max_tokens=16000,
                system=system,
                tools=TOOLS,
                output_config={"effort": "medium", "format": {"type": "json_schema", "schema": ANSWER_SCHEMA}},
                messages=messages,
            )
            if response.stop_reason in ("refusal", "max_tokens"):
                raise AdvisorError("не получилось разобрать вопрос, попробуй сформулировать иначе")
            if response.stop_reason != "tool_use":
                text, proposal = _final(response)
                if not text:
                    raise AdvisorError("пустой ответ, попробуй ещё раз")
                said = text + (f"\n[offered as a plan item: {proposal.title}, {proposal.amount} MYR, {proposal.due_on}]" if proposal else "")
                turn = [{"role": "user", "content": question}, {"role": "assistant", "content": said}]
                return Answer(text, proposal, [*history, *turn][-HISTORY:])
            messages.append({"role": "assistant", "content": response.content})
            results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                try:
                    if block.name == "simulate":
                        content = await _simulate(session, block.input.get("items") or [])
                    else:
                        raise ValueError(f"unknown tool {block.name}")
                    results.append({"type": "tool_result", "tool_use_id": block.id, "content": content})
                except ValueError as exc:
                    results.append({"type": "tool_result", "tool_use_id": block.id, "content": str(exc), "is_error": True})
            messages.append({"role": "user", "content": results})
    except anthropic.APIStatusError as exc:
        log.warning("budget advisor failed: %s %s", exc.status_code, exc.message)
        raise AdvisorError(f"Claude ответил ошибкой {exc.status_code}") from exc
    except anthropic.APIConnectionError as exc:
        log.warning("budget advisor failed: %s", exc)
        raise AdvisorError("Claude недоступен, попробуй позже") from exc
    raise AdvisorError("слишком длинный разбор, спроси конкретнее")
