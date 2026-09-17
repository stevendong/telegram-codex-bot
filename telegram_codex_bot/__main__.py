from __future__ import annotations

import asyncio
import logging
import signal

from .agent_service import AgentService
from .app_server import CodexAppServer
from .bot import BOT_COMMANDS, TelegramCodexBot
from .config import Config
from .state import StateStore
from .telegram_api import TelegramAPI, TelegramError

LOG = logging.getLogger(__name__)


async def async_main() -> None:
    config = Config.from_env()
    state = StateStore(config.state_file)
    apps = {
        name: CodexAppServer(config.codex_bin, config.codex_cwd, codex_home)
        for name, codex_home in config.codex_accounts.items()
    }
    service = AgentService(config, state, apps)
    for user_id in config.allowed_user_ids:
        state.ensure_account_agents(
            user_id, service.account_names(), config.default_account
        )
    telegram = TelegramAPI(config.telegram_token)
    bot = TelegramCodexBot(config, state, service, telegram)
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)

    try:
        await asyncio.gather(*(app.start() for app in apps.values()))
    except Exception:
        await asyncio.gather(
            *(app.close() for app in apps.values()), return_exceptions=True
        )
        raise
    try:
        try:
            await telegram.configure_command_menu(BOT_COMMANDS)
        except TelegramError:
            LOG.exception("Could not update Telegram command menu")
        await bot.run(stop_event)
    finally:
        await asyncio.gather(
            *(app.close() for app in apps.values()), return_exceptions=True
        )


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        asyncio.run(async_main())
    except (ValueError, OSError) as exc:
        raise SystemExit(f"Configuration/startup error: {exc}") from exc


if __name__ == "__main__":
    main()
