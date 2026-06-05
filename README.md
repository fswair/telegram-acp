# Telegram ACP

`telegram-acp` exposes a small ACP relay package for a Kurigram userbot.

Flow:

1. An ACP client sends a prompt turn with text and/or media blocks.
2. The relay sends those blocks to a configured Telegram chat.
3. Incoming messages from that Telegram chat are streamed back to ACP as `session/update` events.

Supported directions:

- ACP -> Telegram: text, image, video, gif, file
- Telegram -> ACP: text, image, video, gif, file

Notes:

- The relay is designed for one target chat and one active ACP conversation at a time.
- Telegram videos/gifs/files are returned to ACP as embedded binary resources.
- Telegram photos are returned as ACP image blocks.
- Incoming Telegram messages are acknowledged with raw MTProto `ReadHistory`.

## Credentials

The runtime reads credentials from `.env`:

```env
TELEGRAM_API_ID=...
TELEGRAM_API_HASH=...
```

> You can create a Telegram app from [here](https://my.telegram.org/auth) to obtain an API id and hash.

## CLI

```bash
uv run telegram-acp --target-chat @target_bot --session-name telegram_acp
```

Useful flags:

- `--target-chat @bot_or_chat_id`
- `--session-name telegram_acp`
- `--first-response-timeout 120`
- `--idle-timeout 4`
- `--dotenv-path .env`
- `--workdir .`

The command runs over ACP stdio, so an ACP-capable client should spawn it directly.

## SDK

Programmatic use lives under `src/telegram_acp`:

- `telegram_acp.build_bridge(...)`
- `telegram_acp.build_agent(...)`
- `telegram_acp.run_telegram_acp_agent(...)`
