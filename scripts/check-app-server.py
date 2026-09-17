#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from telegram_codex_bot.app_server import CodexAppServer  # noqa: E402
from telegram_codex_bot.config import _parse_accounts  # noqa: E402


async def main() -> None:
    codex_bin = os.getenv(
        "CODEX_BIN", "/home/ubuntu/.nvm/versions/node/v23.11.1/bin/codex"
    )
    accounts = _parse_accounts(
        os.getenv("CODEX_ACCOUNTS", "default=/home/ubuntu/.codex")
    )
    apps = {
        name: CodexAppServer(codex_bin, Path("/data"), home)
        for name, home in accounts.items()
    }
    try:
        await asyncio.gather(*(app.start() for app in apps.values()))
        for name, app in apps.items():
            result = await app.request(
                "thread/list",
                {"limit": 1, "sortKey": "updated_at", "sortDirection": "desc"},
            )
            print(
                f"{name}: App Server OK; returned "
                f"{len(result.get('data', []))} thread(s)"
            )
    finally:
        await asyncio.gather(
            *(app.close() for app in apps.values()), return_exceptions=True
        )


if __name__ == "__main__":
    asyncio.run(main())
