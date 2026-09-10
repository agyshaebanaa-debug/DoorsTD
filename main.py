from __future__ import annotations

import asyncio
import logging

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from td_platform.app import build_child_router, build_root_router, build_dispatcher
from td_platform.config import Settings
from td_platform.db import PlatformDB
from td_platform.security import TokenVault
from td_platform.services import ChildBotManager


async def main() -> None:
    settings = Settings.from_env()
    logging.basicConfig(level=getattr(logging, settings.log_level, logging.INFO), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    root_bot = Bot(settings.root_bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    root_me = await root_bot.get_me()
    platform_db = PlatformDB(settings.data_dir / "platform.db")
    await platform_db.connect()
    vault = TokenVault(settings.token_encryption_key)
    dispatcher = build_dispatcher(None, int(root_me.id))
    manager = ChildBotManager(dispatcher, platform_db, settings.data_dir, vault)
    from td_platform.app import BotRoleMiddleware
    role = BotRoleMiddleware(manager, int(root_me.id))
    dispatcher.message.middleware(role)
    dispatcher.callback_query.middleware(role)
    dispatcher.pre_checkout_query.middleware(role)
    dispatcher.include_router(build_root_router(manager, settings))
    dispatcher.include_router(build_child_router(manager))
    await manager.start()
    try:
        await dispatcher.start_polling(root_bot, handle_signals=False, close_bot_session=False)
    finally:
        await manager.shutdown()
        await platform_db.close()
        await root_bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
