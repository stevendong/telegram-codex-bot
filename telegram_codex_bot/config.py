from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _parse_accounts(raw: str) -> dict[str, Path]:
    accounts: dict[str, Path] = {}
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        if "=" not in entry:
            raise ValueError("CODEX_ACCOUNTS entries must use name=/absolute/path")
        name, path_text = entry.split("=", 1)
        name = name.strip().lower()
        if not name or not name.replace("-", "").replace("_", "").isalnum():
            raise ValueError(f"Invalid Codex account name: {name}")
        path = Path(path_text.strip()).expanduser().resolve()
        if not path.is_dir():
            raise ValueError(f"Codex account directory not found: {path}")
        accounts[name] = path
    if not accounts:
        raise ValueError("CODEX_ACCOUNTS must define at least one account")
    return accounts


@dataclass(frozen=True, slots=True)
class Config:
    telegram_token: str
    allowed_user_ids: frozenset[int]
    codex_bin: str
    codex_cwd: Path
    codex_sandbox: str
    codex_model: str | None
    codex_accounts: dict[str, Path]
    default_account: str
    state_file: Path
    turn_timeout_seconds: int = 3600
    poll_timeout_seconds: int = 45
    max_parallel_turns: int = 4

    @classmethod
    def from_env(cls) -> "Config":
        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        if not token:
            raise ValueError("TELEGRAM_BOT_TOKEN is required")

        raw_ids = os.getenv("TELEGRAM_ALLOWED_USER_IDS", "").strip()
        try:
            allowed_ids = frozenset(
                int(part.strip()) for part in raw_ids.split(",") if part.strip()
            )
        except ValueError as exc:
            raise ValueError("TELEGRAM_ALLOWED_USER_IDS must contain numeric IDs") from exc
        if not allowed_ids:
            raise ValueError("TELEGRAM_ALLOWED_USER_IDS must not be empty")

        cwd = Path(os.getenv("CODEX_CWD", "/data")).expanduser().resolve()
        if not cwd.is_dir():
            raise ValueError(f"CODEX_CWD is not a directory: {cwd}")

        codex_setting = os.getenv("CODEX_BIN", "codex").strip()
        codex_bin = shutil.which(codex_setting) or codex_setting
        if not Path(codex_bin).is_file():
            raise ValueError(f"Codex executable not found: {codex_setting}")

        sandbox = os.getenv("CODEX_SANDBOX", "workspace-write").strip()
        allowed_sandboxes = {"read-only", "workspace-write", "danger-full-access"}
        if sandbox not in allowed_sandboxes:
            raise ValueError(f"Invalid CODEX_SANDBOX: {sandbox}")
        if sandbox == "danger-full-access" and not _env_bool(
            "CODEX_ALLOW_DANGER_FULL_ACCESS"
        ):
            raise ValueError(
                "danger-full-access requires CODEX_ALLOW_DANGER_FULL_ACCESS=true"
            )

        model = os.getenv("CODEX_MODEL", "").strip() or None
        accounts = _parse_accounts(
            os.getenv("CODEX_ACCOUNTS", "default=/home/ubuntu/.codex")
        )
        default_account = os.getenv("CODEX_DEFAULT_ACCOUNT", "default").strip().lower()
        if default_account not in accounts:
            raise ValueError(
                f"CODEX_DEFAULT_ACCOUNT is not present in CODEX_ACCOUNTS: {default_account}"
            )
        state_file = Path(
            os.getenv(
                "CODEX_BOT_STATE_FILE",
                "/var/lib/telegram-codex-bot/state.json",
            )
        ).expanduser()

        return cls(
            telegram_token=token,
            allowed_user_ids=allowed_ids,
            codex_bin=codex_bin,
            codex_cwd=cwd,
            codex_sandbox=sandbox,
            codex_model=model,
            codex_accounts=accounts,
            default_account=default_account,
            state_file=state_file,
            turn_timeout_seconds=int(os.getenv("CODEX_TURN_TIMEOUT_SECONDS", "3600")),
            poll_timeout_seconds=int(os.getenv("TELEGRAM_POLL_TIMEOUT_SECONDS", "45")),
            max_parallel_turns=int(os.getenv("CODEX_MAX_PARALLEL_TURNS", "4")),
        )
