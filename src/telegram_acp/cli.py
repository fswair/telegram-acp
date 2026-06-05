from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path

from dotenv import load_dotenv

from .agent import run_telegram_acp_agent
from .config import RuntimeConfig, TelegramCredentials, parse_chat_ref


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="telegram-acp", description="Run a Telegram-to-ACP relay agent."
    )
    parser.add_argument(
        "--target-chat",
        required=True,
        help="Telegram username or numeric chat id to relay.",
    )
    parser.add_argument(
        "--session-name",
        default="telegram_acp",
        help="Telegram session name; maps to <session-name>.session in the working directory.",
    )
    parser.add_argument(
        "--first-response-timeout",
        type=float,
        default=120.0,
        help="Seconds to wait for the first Telegram response before ending the ACP turn.",
    )
    parser.add_argument(
        "--idle-timeout",
        type=float,
        default=4.0,
        help="Seconds to wait between Telegram updates before ending the ACP turn.",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        help="Python logging level.",
    )
    parser.add_argument(
        "--dotenv-path",
        default=".env",
        help="Path to dotenv file containing TELEGRAM_API_ID and TELEGRAM_API_HASH.",
    )
    parser.add_argument(
        "--workdir",
        default=".",
        help="Working directory for session files and temporary downloads.",
    )
    return parser


def runtime_config_from_args(args: argparse.Namespace) -> RuntimeConfig:
    return RuntimeConfig(
        target_chat=parse_chat_ref(args.target_chat),
        session_name=args.session_name,
        first_response_timeout=args.first_response_timeout,
        idle_timeout=args.idle_timeout,
        log_level=args.log_level,
    )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    load_dotenv(args.dotenv_path)
    logging.basicConfig(level=args.log_level.upper())
    config = runtime_config_from_args(args)
    credentials = TelegramCredentials.from_env()
    asyncio.run(
        run_telegram_acp_agent(
            config,
            credentials=credentials,
            workdir=Path(args.workdir).resolve(),
        )
    )
    return 0
