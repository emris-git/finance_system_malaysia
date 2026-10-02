"""Telegram bot. Webhook mode runs inside the API (finance.web.app); `python -m finance.bot` polls locally."""

from __future__ import annotations

from functools import lru_cache

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand, InlineKeyboardMarkup

from finance.config import get_settings

COMMANDS = [
    BotCommand(command="week", description="Эта неделя"),
    BotCommand(command="lastweek", description="Прошлая неделя"),
    BotCommand(command="month", description="Этот месяц (или /month 2026-08)"),
    BotCommand(command="review", description="Разобрать непонятные транзакции"),
    BotCommand(command="rf", description="Перевод на РФ: /rf 1000 21500 [24.09]"),
    BotCommand(command="balance", description="Остатки и курс"),
    BotCommand(command="budget", description="Бюджет на полгода вперёд"),
    BotCommand(command="plan", description="Будущие траты: /plan 1800 отель 25.10"),
    BotCommand(command="ask", description="Пролезет ли покупка: спросить Claude"),
    BotCommand(command="pot", description="Копилки: /pot Holiday 15000"),
    BotCommand(command="rules", description="Запомненные правила категорий"),
    BotCommand(command="fix", description="Поменять категорию: /fix acme trading"),
    BotCommand(command="web", description="Ссылка на дашборд"),
    BotCommand(command="undo", description="Удалить последнюю ручную запись"),
    BotCommand(command="help", description="Что умеет бот"),
]


@lru_cache
def get_bot() -> Bot:
    token = get_settings().TELEGRAM_BOT_TOKEN
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")
    return Bot(token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))


@lru_cache
def get_dispatcher() -> Dispatcher:
    from finance.bot.handlers import router
    from finance.bot.middleware import DbSessionMiddleware, OwnerOnlyMiddleware

    dp = Dispatcher()
    dp.update.outer_middleware(OwnerOnlyMiddleware())
    dp.update.middleware(DbSessionMiddleware())
    dp.include_router(router)
    return dp


async def notify_owner(text: str, reply_markup: InlineKeyboardMarkup | None = None) -> None:
    """Send a message to the owner; silently skipped when the bot is not configured."""
    settings = get_settings()
    if not settings.TELEGRAM_BOT_TOKEN or not settings.TELEGRAM_OWNER_ID:
        return
    await get_bot().send_message(
        settings.TELEGRAM_OWNER_ID, text, reply_markup=reply_markup, disable_web_page_preview=True
    )
