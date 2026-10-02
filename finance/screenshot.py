"""Payment screenshots (TNG, Maybank MAE) sent to the bot: Claude reads the amount,
the receiver and the time. The ledger side is a pending entry that the statement
confirms later, like an Apple Pay purchase."""

from __future__ import annotations

import base64
import json
import logging
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation

import anthropic

from finance.config import get_settings

log = logging.getLogger(__name__)

MEDIA_TYPES = ("image/jpeg", "image/png", "image/webp", "image/gif")

PROMPT = """This is a screenshot from a Malaysian payment app, sent by the app's owner to log a payment.
Read the payment it shows.

- app: "tng" for Touch 'n Go eWallet, "maybank" for Maybank / MAE, "other" for anything else.
- is_payment: true only for a single payment or transfer that went through (a success screen,
  a receipt, a transaction detail). False for a balance, a list of transactions, a failed or
  pending payment, or anything that is not a payment.
- direction: "out" when money left the owner, "in" when the owner received it.
- amount: the amount as digits with a dot, e.g. "15.00"; currency: ISO code, "MYR" for RM.
- counterparty: who was paid (the shop or the receiver's name) exactly as printed.
- remark: the remark / reference / "what's it for" text, or "" when there is none or it only
  repeats the counterparty.
- occurred_at: date and time as "YYYY-MM-DD HH:MM" (screens print DD/MM/YYYY), "" if not shown.
- to_person: true when it is a transfer to a person's wallet or account (e.g. "Transferred",
  "Transfer to Wallet", DuitNow transfer to a name), false when paying a shop or a merchant QR.
- failure_reason: when is_payment is false, a short reason in Russian; otherwise "".
"""

SCHEMA = {
    "type": "object",
    "properties": {
        "is_payment": {"type": "boolean"},
        "app": {"type": "string", "enum": ["tng", "maybank", "other"]},
        "direction": {"type": "string", "enum": ["out", "in"]},
        "amount": {"type": "string"},
        "currency": {"type": "string"},
        "counterparty": {"type": "string"},
        "remark": {"type": "string"},
        "occurred_at": {"type": "string"},
        "to_person": {"type": "boolean"},
        "failure_reason": {"type": "string"},
    },
    "required": [
        "is_payment", "app", "direction", "amount", "currency", "counterparty",
        "remark", "occurred_at", "to_person", "failure_reason",
    ],
    "additionalProperties": False,
}


class ScreenshotError(Exception):
    """Shown to the owner as is (Russian)."""


@dataclass
class Payment:
    amount: Decimal  # positive, in `currency`
    currency: str
    counterparty: str
    remark: str
    occurred_at: datetime | None  # local time of the payment app
    account: str | None  # tng / maybank; None when the app is not one of ours
    to_person: bool
    incoming: bool

    def to_state(self) -> dict:
        """JSON-safe form for the bot's FSM storage."""
        return {
            "amount": str(self.amount),
            "currency": self.currency,
            "counterparty": self.counterparty,
            "remark": self.remark,
            "occurred_at": self.occurred_at.isoformat() if self.occurred_at else None,
            "account": self.account,
            "to_person": self.to_person,
            "incoming": self.incoming,
        }

    @classmethod
    def from_state(cls, data: dict) -> Payment:
        at = data.get("occurred_at")
        return cls(**{**data, "amount": Decimal(data["amount"]), "occurred_at": datetime.fromisoformat(at) if at else None})


def enabled() -> bool:
    return bool(get_settings().ANTHROPIC_API_KEY)


def payment_from(data: dict) -> Payment:
    """The model's answer -> Payment, or ScreenshotError with a reason for the owner."""
    if not data.get("is_payment"):
        raise ScreenshotError(data.get("failure_reason") or "на скрине не видно платежа")
    try:
        amount = abs(Decimal(str(data.get("amount", "")).replace(",", "")))
    except InvalidOperation:
        amount = Decimal(0)
    if not amount:
        raise ScreenshotError("не нашёл сумму")
    counterparty = " ".join(str(data.get("counterparty") or "").split())
    if not counterparty:
        raise ScreenshotError("не нашёл, кому платёж")
    remark = " ".join(str(data.get("remark") or "").split())
    if remark.upper() == counterparty.upper():
        remark = ""
    try:
        occurred_at = datetime.strptime(data.get("occurred_at") or "", "%Y-%m-%d %H:%M")
    except ValueError:
        occurred_at = None
    app = data.get("app")
    return Payment(
        amount=amount,
        currency=(data.get("currency") or "MYR").upper().replace("RM", "MYR"),
        counterparty=counterparty[:120],
        remark=remark[:80],
        occurred_at=occurred_at,
        account=app if app in ("tng", "maybank") else None,
        to_person=bool(data.get("to_person")),
        incoming=data.get("direction") == "in",
    )


async def read_payment(image: bytes, media_type: str) -> Payment:
    settings = get_settings()
    if media_type not in MEDIA_TYPES:
        raise ScreenshotError("нужна картинка: JPG, PNG или WebP")
    client = anthropic.AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY, timeout=60.0)
    try:
        response = await client.messages.create(
            model=settings.SCREENSHOT_MODEL,
            max_tokens=2000,
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {"type": "base64", "media_type": media_type, "data": base64.standard_b64encode(image).decode()},
                    },
                    {"type": "text", "text": PROMPT},
                ],
            }],
        )
    except anthropic.APIStatusError as exc:
        log.warning("screenshot recognition failed: %s %s", exc.status_code, exc.message)
        raise ScreenshotError(f"сервис распознавания ответил ошибкой {exc.status_code}") from exc
    except anthropic.APIConnectionError as exc:
        log.warning("screenshot recognition failed: %s", exc)
        raise ScreenshotError("сервис распознавания недоступен, попробуй позже") from exc
    if response.stop_reason in ("refusal", "max_tokens"):
        raise ScreenshotError("не получилось прочитать скрин")
    text = next((b.text for b in response.content if b.type == "text"), "")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ScreenshotError("не получилось прочитать скрин") from exc
    return payment_from(data)
