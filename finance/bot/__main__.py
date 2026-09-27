"""Local development: long polling instead of the webhook.

Polling removes the webhook, so the deployed bot stops receiving updates
until the API service restarts (it sets the webhook again on startup).
"""

import asyncio
import logging

from finance.bot import COMMANDS, get_bot, get_dispatcher


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    bot = get_bot()
    await bot.delete_webhook()
    await bot.set_my_commands(COMMANDS)
    await get_dispatcher().start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
