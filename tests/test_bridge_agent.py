from __future__ import annotations

import base64
import os
import tempfile
import unittest
from pathlib import Path
from typing import cast
from unittest.mock import patch

from acp import text_block
from acp.helpers import embedded_blob_resource, resource_block
from acp.schema import ResourceContentBlock
from pyrogram import raw
from pyrogram import Client as TelegramClient

from telegram_acp.agent import TelegramRelayAgent
from telegram_acp.bridge import TelegramBridge
from telegram_acp.cli import build_parser, runtime_config_from_args
from telegram_acp.config import TelegramCredentials, parse_chat_ref


class FakeTelegramClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object, object | None]] = []
        self.handlers = []
        self.started = False
        self.invoked = []

    def add_handler(self, handler) -> None:
        self.handlers.append(handler)

    async def start(self) -> None:
        self.started = True

    async def send_message(self, chat_id, text) -> None:
        self.calls.append(("send_message", chat_id, text))

    async def send_photo(self, chat_id, path, caption="") -> None:
        self.calls.append(("send_photo", chat_id, Path(path).suffix))

    async def send_video(self, chat_id, path, caption="") -> None:
        self.calls.append(("send_video", chat_id, Path(path).suffix))

    async def send_animation(self, chat_id, path, caption="") -> None:
        self.calls.append(("send_animation", chat_id, Path(path).suffix))

    async def send_document(self, chat_id, path, caption="", file_name=None) -> None:
        self.calls.append(("send_document", chat_id, file_name or Path(path).name))

    async def resolve_peer(self, chat_id):
        self.calls.append(("resolve_peer", chat_id, None))
        return raw.types.InputPeerSelf()

    async def invoke(self, request) -> bool:
        self.invoked.append(request)
        self.calls.append(
            ("invoke", request.QUALNAME, getattr(request, "max_id", None))
        )
        return True


class BridgeAgentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.fake_client = FakeTelegramClient()
        self.bridge = TelegramBridge(
            target_chat="@target_bot",
            session_name="telegram_acp",
            credentials=TelegramCredentials(api_id="1", api_hash="hash"),
            first_response_timeout=0.01,
            idle_timeout=0.01,
            telegram_client_factory=lambda: cast(TelegramClient, self.fake_client),
        )

    async def test_start_registers_handler(self) -> None:
        agent = TelegramRelayAgent(self.bridge)
        await self.bridge.start(agent)
        self.assertTrue(self.fake_client.started)
        self.assertEqual(len(self.fake_client.handlers), 1)

    async def test_send_text_and_embedded_blob(self) -> None:
        await self.bridge.send_prompt_blocks(
            "session-1",
            [
                text_block("merhaba"),
                resource_block(
                    embedded_blob_resource(
                        uri="telegram://upload/example.gif",
                        blob=base64.b64encode(b"GIF89a").decode("ascii"),
                        mime_type="image/gif",
                    )
                ),
            ],
        )
        self.assertEqual(
            self.fake_client.calls[0], ("send_message", "@target_bot", "merhaba")
        )
        self.assertEqual(self.fake_client.calls[1][0], "send_animation")

    async def test_send_local_resource_link(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as handle:
            handle.write(b"hello")
            file_path = Path(handle.name)
        try:
            block = ResourceContentBlock(
                type="resource_link",
                name="hello.txt",
                uri=file_path.as_uri(),
                mime_type="text/plain",
            )
            await self.bridge.send_prompt_blocks("session-2", [block])
            self.assertEqual(self.fake_client.calls[-1][0], "send_document")
            self.assertEqual(self.fake_client.calls[-1][2], "hello.txt")
        finally:
            file_path.unlink(missing_ok=True)

    async def test_agent_lists_and_closes_sessions(self) -> None:
        agent = TelegramRelayAgent(self.bridge)
        new_session = await agent.new_session(cwd="/tmp/demo")
        listed = await agent.list_sessions()
        self.assertEqual(len(listed.sessions), 1)
        self.assertEqual(listed.sessions[0].session_id, new_session.session_id)
        await agent.close_session(new_session.session_id)
        listed_after = await agent.list_sessions()
        self.assertEqual(listed_after.sessions, [])

    async def test_incoming_message_marks_chat_as_read(self) -> None:
        class DummyAgent:
            async def push_unsolicited_update(self, item) -> None:
                return None

        self.bridge._agent = DummyAgent()  # type: ignore
        self.bridge.bind_session("session-3")

        class DummyMessage:
            outgoing = False
            id = 42
            text = "pong"
            caption = None
            photo = None
            animation = None
            video = None
            document = None

        await self.bridge._on_message(self.fake_client, DummyMessage())  # type: ignore
        self.assertEqual(
            self.fake_client.calls[-1], ("invoke", "functions.messages.ReadHistory", 42)
        )


class ParseChatRefTests(unittest.TestCase):
    def test_numeric_and_username_refs(self) -> None:
        self.assertEqual(parse_chat_ref("-100123"), -100123)
        self.assertEqual(parse_chat_ref("12345"), 12345)
        self.assertEqual(parse_chat_ref("@demo_bot"), "demo_bot")
        self.assertEqual(parse_chat_ref("demo_bot"), "demo_bot")


class CliTests(unittest.TestCase):
    def test_parser_builds_runtime_config(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            [
                "--target-chat",
                "@demo_bot",
                "--session-name",
                "custom_session",
                "--first-response-timeout",
                "30",
                "--idle-timeout",
                "2",
                "--log-level",
                "DEBUG",
            ]
        )
        config = runtime_config_from_args(args)
        self.assertEqual(config.target_chat, "demo_bot")
        self.assertEqual(config.session_name, "custom_session")
        self.assertEqual(config.first_response_timeout, 30.0)
        self.assertEqual(config.idle_timeout, 2.0)
        self.assertEqual(config.log_level, "DEBUG")

    def test_parser_falls_back_to_env_target_chat(self) -> None:
        parser = build_parser()
        args = parser.parse_args([])
        with patch.dict(os.environ, {"TELEGRAM_TARGET_CHAT": "@env_bot"}, clear=False):
            config = runtime_config_from_args(args)
        self.assertEqual(config.target_chat, "env_bot")


if __name__ == "__main__":
    unittest.main()
