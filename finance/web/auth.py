"""Single-user auth.

Browser: the bot's /web command hands out a signed link valid for 15 minutes;
opening it sets a 30-day session cookie. Machines (CLI, iOS Shortcut, the
categorization agent): `Authorization: Bearer $API_TOKEN`.
"""

from __future__ import annotations

import hmac

from fastapi import HTTPException, Request
from itsdangerous import BadSignature, URLSafeTimedSerializer

from finance.config import get_settings

COOKIE_NAME = "fin_session"
MAGIC_MAX_AGE = 15 * 60
SESSION_MAX_AGE = 30 * 24 * 3600


class LoginDisabled(Exception):
    """SECRET_KEY is not configured."""


def login_enabled() -> bool:
    return bool(get_settings().SECRET_KEY)


def _serializer(salt: str) -> URLSafeTimedSerializer:
    if not login_enabled():
        raise LoginDisabled("SECRET_KEY is not set")
    return URLSafeTimedSerializer(get_settings().SECRET_KEY, salt=salt)


def base_url() -> str:
    return (get_settings().PUBLIC_BASE_URL or "http://localhost:8000").rstrip("/")


def magic_link() -> str:
    token = _serializer("magic").dumps({"u": get_settings().TELEGRAM_OWNER_ID})
    return f"{base_url()}/auth/magic?token={token}"


def check_magic(token: str) -> bool:
    if not login_enabled():
        return False
    try:
        payload = _serializer("magic").loads(token, max_age=MAGIC_MAX_AGE)
    except BadSignature:
        return False
    return payload.get("u") == get_settings().TELEGRAM_OWNER_ID


def new_session_value() -> str:
    return _serializer("session").dumps({"u": get_settings().TELEGRAM_OWNER_ID})


def _valid_session(value: str | None) -> bool:
    if not value or not login_enabled():
        return False
    try:
        payload = _serializer("session").loads(value, max_age=SESSION_MAX_AGE)
    except BadSignature:
        return False
    return payload.get("u") == get_settings().TELEGRAM_OWNER_ID


def _valid_token(request: Request) -> bool:
    expected = get_settings().API_TOKEN
    header = request.headers.get("authorization", "")
    if not expected or not header.lower().startswith("bearer "):
        return False
    return hmac.compare_digest(header[7:].strip(), expected)


def is_authenticated(request: Request) -> bool:
    return _valid_session(request.cookies.get(COOKIE_NAME)) or _valid_token(request)


async def require_user(request: Request) -> None:
    if not is_authenticated(request):
        raise HTTPException(status_code=401, detail="Откройте дашборд по ссылке из бота: /web")


async def require_token(request: Request) -> None:
    if not _valid_token(request):
        raise HTTPException(status_code=401, detail="Bearer API_TOKEN required")
