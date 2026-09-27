"""Rule-based classification: kind (expense/income/transfer), category, review flag."""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from finance.config import get_settings
from finance.models import (
    EXPENSE,
    INCOME,
    REVIEW_AWAITING_PAIR,
    REVIEW_P2P,
    REVIEW_P2P_IN,
    REVIEW_UNCATEGORIZED,
    TRANSFER,
    Category,
    CategoryRule,
)

USER_RULE_PRIORITY = 1000


@dataclass
class Classification:
    kind: str
    category_id: int | None
    review_reason: str | None


@dataclass
class _Rule:
    regex: re.Pattern
    category_id: int | None
    kind: str | None
    direction: str | None
    account_code: str | None


def names_owner(description: str) -> bool:
    """The counterparty is the owner (OWNER_NAME), in any word order: Maybank prints
    "ALEX MORGAN" or "MORGAN ALEX" depending on the product.

    Only the part before "*" counts: after it comes the sender's own reference,
    and a payment to someone else may carry the owner's name there
    ("IBK FUND TFR FR A/C NG SOO LI * Morgan Alex MB").
    """
    words = re.findall(r"\w+", (get_settings().OWNER_NAME or "").upper())
    counterparty = description.split("*", 1)[0]
    found = set(re.findall(r"\w+", counterparty.upper()))
    return bool(words) and all(w in found for w in words)


def merchant_of(description: str) -> str:
    """Description without references, card numbers and noise."""
    text = re.sub(r"\b\d{6,}\b", " ", description.upper())
    text = re.sub(r"[*#]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip(" -.,")
    return text[:120]


# Bank boilerplate that says how money moved, not where it went.
GENERIC_WORDS = {
    "DUITNOW", "TRANSFER", "TRF", "IBG", "INSTANT", "FUND", "FUNDS", "INTERBANK", "PAYMENT", "PAYMENTS",
    "FPX", "CREDIT", "DEBIT", "ACCOUNT", "POS", "PURCHASE", "SALE", "BILL", "QR", "CARD", "VISA",
    "MASTERCARD", "TO", "FROM", "FOR", "THE", "REF", "SDN", "BHD", "A/C",
    "IBK", "TFR", "MBB",  # Maybank internet banking: "IBK FUND TFR FR A/C <name> * <ref> MBB CT"
    # country and cities printed on card rows
    "MALAYSIA", "BERHAD", "KUALA", "LUMPUR", "SELANGOR", "PETALING",
}


def _meaningful_words(text: str) -> list[str]:
    words = []
    for token in re.findall(r"[A-Za-zА-Яа-яЁё0-9][\w.&'/-]*", text):
        for part in re.split(r"[-/]", token):
            part = part.strip(".'")
            # branch/terminal codes (SB294) and short fragments (BA, MY) say nothing about the merchant
            if len(part) < 3 or re.search(r"\d", part) or part.upper() in GENERIC_WORDS:
                continue
            words.append(part.upper())
    return words


def suggest_pattern(description: str) -> str:
    """Regex for "remember this merchant": up to two meaningful words, in order.

    Card rows look like `SALE DEBIT SB294-SOUTHLINK BA * KUALA LUMPUR, MY`: the
    merchant is before the `*` (after it comes the city or an order id). Words
    are joined loosely, so another branch or a word in between still matches.
    """
    text = description.upper()
    words = _meaningful_words(text.split("*", 1)[0]) or _meaningful_words(text)
    if not words:
        return re.escape(merchant_of(description)[:30])
    return ".*?".join(rf"\b{re.escape(w)}" for w in words[:2])


def rule_label(pattern: str) -> str:
    """Human form of a rule pattern for the bot: `\\bJAYA.*?\\bGROCER` -> `JAYA … GROCER`."""
    return pattern.replace(".*?", " … ").replace("\\b", "").replace("\\", "")


class Classifier:
    def __init__(self, rules: list[CategoryRule], categories: list[Category]):
        self.categories = {c.id: c for c in categories}
        self.by_code = {c.code: c for c in categories}
        ordered = sorted(rules, key=lambda r: (-r.priority, r.id or 0))
        self.rules: list[_Rule] = []
        for r in ordered:
            try:
                regex = re.compile(r.pattern, re.IGNORECASE)
            except re.error:
                continue
            self.rules.append(_Rule(regex, r.category_id, r.kind, r.direction, r.account_code))

    @classmethod
    async def load(cls, session: AsyncSession) -> Classifier:
        rules = (await session.scalars(select(CategoryRule))).unique().all()
        categories = (await session.scalars(select(Category))).all()
        return cls(list(rules), list(categories))

    def _applicable(self, direction: str, account_code: str):
        for r in self.rules:
            if r.direction not in (None, direction):
                continue
            if r.account_code not in (None, account_code):
                continue
            yield r

    def _category_for(self, text: str, direction: str, account_code: str, kind: str) -> int | None:
        for r in self._applicable(direction, account_code):
            if r.category_id is None or not r.regex.search(text):
                continue
            category = self.categories.get(r.category_id)
            if category and category.kind == kind:
                return r.category_id
        return None

    def classify(
        self,
        description: str,
        amount: Decimal,
        account_code: str,
        raw_type: str | None = None,
        category_hint: str | None = None,
    ) -> Classification:
        direction = "in" if amount > 0 else "out"
        text = f"{raw_type or ''} {description}"

        kind_rule = next(
            (r for r in self._applicable(direction, account_code) if r.kind and r.regex.search(text)),
            None,
        )
        rule_kind = kind_rule.kind if kind_rule else None

        # money sent to or received from the owner's own name: e.g. Maybank -> TNG
        # ("IBK FUND TFR FR A/C MORGAN ALEX"); pairs up with the other leg by itself
        if rule_kind == "internal" or (rule_kind in ("p2p", "p2p_in") and names_owner(description)):
            return Classification(TRANSFER, None, REVIEW_AWAITING_PAIR)
        if rule_kind == "own":
            return Classification(TRANSFER, None, None)
        if rule_kind == "p2p":
            return Classification(TRANSFER, None, REVIEW_P2P)

        if rule_kind in (EXPENSE, INCOME):
            kind = rule_kind
        else:
            kind = EXPENSE if direction == "out" else INCOME

        if category_hint and category_hint in self.by_code and self.by_code[category_hint].kind == kind:
            return Classification(kind, self.by_code[category_hint].id, None)

        category_id = (kind_rule.category_id if kind_rule else None) or self._category_for(
            text, direction, account_code, kind
        )
        if rule_kind == "p2p_in":
            fallback = self.by_code.get("other_income")
            return Classification(INCOME, category_id or (fallback.id if fallback else None), REVIEW_P2P_IN)
        if category_id:
            return Classification(kind, category_id, None)

        fallback = self.by_code.get("other" if kind == EXPENSE else "other_income")
        return Classification(kind, fallback.id if fallback else None, REVIEW_UNCATEGORIZED)
