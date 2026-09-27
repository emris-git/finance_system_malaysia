from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, User

from finance.config import get_settings
from finance.db import get_sessionmaker


class OwnerOnlyMiddleware(BaseMiddleware):
    """Financial data: everyone except TELEGRAM_OWNER_ID is ignored."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user: User | None = data.get("event_from_user")
        owner = get_settings().TELEGRAM_OWNER_ID
        if user is None or owner is None or user.id != owner:
            return None
        return await handler(event, data)


class DbSessionMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        async with get_sessionmaker()() as session:
            data["session"] = session
            return await handler(event, data)
