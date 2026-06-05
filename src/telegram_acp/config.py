from __future__ import annotations

import os
from dataclasses import dataclass


def parse_chat_ref(value: str) -> int | str:
    stripped = value.strip()
    if stripped.startswith("-") and stripped[1:].isdigit():
        return int(stripped)
    if stripped.isdigit():
        return int(stripped)
    return stripped[1:] if stripped.startswith("@") else stripped


def require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


@dataclass(slots=True)
class TelegramCredentials:
    api_id: str
    api_hash: str

    @classmethod
    def from_env(cls) -> "TelegramCredentials":
        return cls(
            api_id=require_env("TELEGRAM_API_ID"),
            api_hash=require_env("TELEGRAM_API_HASH"),
        )


@dataclass(slots=True)
class RuntimeConfig:
    target_chat: int | str
    session_name: str = "telegram_acp"
    first_response_timeout: float = 120.0
    idle_timeout: float = 4.0
    log_level: str = "INFO"
