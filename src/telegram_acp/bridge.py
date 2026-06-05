from __future__ import annotations

import asyncio
import base64
import logging
import mimetypes
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, cast
from urllib.parse import unquote, urlparse

from acp import text_block, update_agent_message
from acp.helpers import embedded_blob_resource, resource_block
from acp.schema import (
    AudioContentBlock,
    EmbeddedResourceContentBlock,
    ImageContentBlock,
    ResourceContentBlock,
    TextContentBlock,
)
from pyrogram import Client as TelegramClient
from pyrogram import filters, raw
from pyrogram.handlers import MessageHandler
from pyrogram.types import Message

from .config import TelegramCredentials

if TYPE_CHECKING:
    from .agent import TelegramRelayAgent


ROOT = Path.cwd()
TMP_DIR = ROOT / ".tmp"
PromptBlock = (
    TextContentBlock
    | ImageContentBlock
    | AudioContentBlock
    | ResourceContentBlock
    | EmbeddedResourceContentBlock
)


def guess_suffix(mime_type: str | None, fallback: str = ".bin") -> str:
    if not mime_type:
        return fallback
    guessed = mimetypes.guess_extension(mime_type)
    return guessed or fallback


@dataclass(slots=True)
class IncomingChunk:
    session_id: str | None
    update: Any


class TelegramBridge:
    def __init__(
        self,
        *,
        target_chat: int | str,
        session_name: str,
        credentials: TelegramCredentials,
        first_response_timeout: float,
        idle_timeout: float,
        workdir: Path | None = None,
        telegram_client_factory: Callable[[], TelegramClient] | None = None,
    ) -> None:
        self.target_chat = target_chat
        self.first_response_timeout = first_response_timeout
        self.idle_timeout = idle_timeout
        self.workdir = workdir or ROOT
        self._tmp_dir = self.workdir / ".tmp"
        self.client = (
            telegram_client_factory()
            if telegram_client_factory is not None
            else TelegramClient(
                session_name,
                api_id=credentials.api_id,
                api_hash=credentials.api_hash,
                workdir=str(self.workdir),
            )
        )
        self._started = False
        self._active_session_id: str | None = None
        self._agent: TelegramRelayAgent | None = None
        self._session_queues: dict[str, asyncio.Queue[IncomingChunk]] = {}

    async def start(self, agent: "TelegramRelayAgent") -> None:
        if self._started:
            self._agent = agent
            return
        self._tmp_dir.mkdir(exist_ok=True)
        self._agent = agent
        self.client.add_handler(
            MessageHandler(self._on_message, filters.chat(self.target_chat))
        )
        await self.client.start()
        self._started = True
        logging.info("Telegram client started for target chat %s", self.target_chat)

    def bind_session(self, session_id: str) -> None:
        self._session_queues.setdefault(session_id, asyncio.Queue())
        self._active_session_id = session_id

    def unbind_session(self, session_id: str) -> None:
        self._session_queues.pop(session_id, None)
        if self._active_session_id == session_id:
            self._active_session_id = None

    async def clear_session_queue(self, session_id: str) -> None:
        queue = self._session_queues.setdefault(session_id, asyncio.Queue())
        while not queue.empty():
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                break

    async def send_prompt_blocks(
        self, session_id: str, blocks: list[PromptBlock]
    ) -> None:
        self.bind_session(session_id)
        await self.clear_session_queue(session_id)
        for block in blocks:
            await self._send_block(block)

    async def wait_for_turn_updates(
        self, session_id: str, cancel_event: asyncio.Event
    ) -> list[Any]:
        queue = self._session_queues.setdefault(session_id, asyncio.Queue())
        updates: list[Any] = []
        deadline = time.monotonic() + self.first_response_timeout
        while not cancel_event.is_set():
            timeout = max(0.0, deadline - time.monotonic())
            if timeout == 0:
                break
            try:
                item = await asyncio.wait_for(queue.get(), timeout=timeout)
            except asyncio.TimeoutError:
                break
            updates.append(item.update)
            deadline = time.monotonic() + self.idle_timeout
        return updates

    async def _send_block(self, block: PromptBlock) -> None:
        if isinstance(block, TextContentBlock):
            if block.text.strip():
                await self.client.send_message(self.target_chat, block.text)
            return

        if isinstance(block, ImageContentBlock):
            await self._send_base64_file(
                data=block.data,
                mime_type=block.mime_type,
                preferred_name="image",
                preferred_kind="image",
            )
            return

        if isinstance(block, AudioContentBlock):
            await self._send_base64_file(
                data=block.data,
                mime_type=block.mime_type,
                preferred_name="audio",
                preferred_kind="document",
            )
            return

        if isinstance(block, EmbeddedResourceContentBlock):
            await self._send_embedded_resource(block)
            return

        if isinstance(block, ResourceContentBlock):
            if block.uri.startswith(("http://", "https://")):
                await self.client.send_message(self.target_chat, block.uri)
                return
            if block.uri.startswith("file://") or block.uri.startswith("/"):
                path = self._path_from_uri(block.uri)
                await self._send_file_path(
                    path=path,
                    mime_type=block.mime_type or mimetypes.guess_type(str(path))[0],
                    preferred_name=block.name or path.name,
                )
                return
            raise ValueError(
                f"Unsupported resource link URI for Telegram relay: {block.uri}"
            )

        raise ValueError(f"Unsupported ACP content block: {type(block).__name__}")

    async def _send_embedded_resource(
        self, block: EmbeddedResourceContentBlock
    ) -> None:
        resource = block.resource
        if hasattr(resource, "text"):
            text = getattr(resource, "text", "")
            if text:
                await self.client.send_message(self.target_chat, text)
            return

        blob = getattr(resource, "blob", None)
        mime_type = getattr(resource, "mime_type", None)
        uri = getattr(resource, "uri", None) or "resource"
        preferred_name = Path(uri).name or "resource"
        if not isinstance(blob, str):
            raise ValueError("Embedded resource blob must be a base64 string")
        await self._send_base64_file(
            data=blob,
            mime_type=mime_type,
            preferred_name=preferred_name,
            preferred_kind=self._kind_from_mime(mime_type),
        )

    def _path_from_uri(self, uri: str) -> Path:
        if uri.startswith("/"):
            return Path(uri)
        parsed = urlparse(uri)
        if parsed.scheme != "file":
            raise ValueError(f"Unsupported URI scheme: {uri}")
        return Path(unquote(parsed.path))

    async def _send_file_path(
        self, path: Path, mime_type: str | None, preferred_name: str
    ) -> None:
        if not path.exists():
            raise FileNotFoundError(path)
        kind = self._kind_from_mime(mime_type)
        if kind == "image":
            await self.client.send_photo(self.target_chat, str(path))
            return
        if kind == "video":
            await self.client.send_video(self.target_chat, str(path))
            return
        if kind == "animation":
            await self.client.send_animation(self.target_chat, str(path))
            return
        await self.client.send_document(
            self.target_chat, str(path), file_name=preferred_name
        )

    async def _send_base64_file(
        self,
        *,
        data: str,
        mime_type: str | None,
        preferred_name: str,
        preferred_kind: str,
    ) -> None:
        self._tmp_dir.mkdir(exist_ok=True)
        raw_bytes = base64.b64decode(data)
        suffix = guess_suffix(mime_type)
        with tempfile.NamedTemporaryFile(
            dir=self._tmp_dir, suffix=suffix, delete=False
        ) as handle:
            handle.write(raw_bytes)
            temp_path = Path(handle.name)
        try:
            if preferred_kind == "image":
                await self.client.send_photo(
                    self.target_chat, str(temp_path), caption=""
                )
            elif preferred_kind == "video":
                await self.client.send_video(
                    self.target_chat, str(temp_path), caption=""
                )
            elif preferred_kind == "animation":
                await self.client.send_animation(
                    self.target_chat, str(temp_path), caption=""
                )
            else:
                await self.client.send_document(
                    self.target_chat,
                    str(temp_path),
                    caption="",
                    file_name=f"{preferred_name}{suffix if not preferred_name.endswith(suffix) else ''}",
                )
        finally:
            temp_path.unlink(missing_ok=True)

    def _kind_from_mime(self, mime_type: str | None) -> str:
        if not mime_type:
            return "document"
        if mime_type.startswith("image/"):
            return "animation" if mime_type == "image/gif" else "image"
        if mime_type.startswith("video/"):
            return "video"
        return "document"

    async def _on_message(self, _client: TelegramClient, message: Message) -> None:
        if message.outgoing:
            return
        session_id = self._active_session_id
        if session_id is None:
            return
        updates = await self._message_to_updates(message)
        queue = self._session_queues.setdefault(session_id, asyncio.Queue())
        for update in updates:
            item = IncomingChunk(session_id=session_id, update=update)
            await queue.put(item)
            if self._agent is not None:
                await self._agent.push_unsolicited_update(item)
        await self._send_seen(message.id)

    async def _send_seen(self, max_id: int) -> None:
        peer = await self.client.resolve_peer(self.target_chat)
        if isinstance(peer, raw.types.InputPeerChannel):
            channel = raw.types.InputChannel(
                channel_id=peer.channel_id, access_hash=peer.access_hash
            )
            request = raw.functions.channels.ReadHistory(channel=channel, max_id=max_id)
        else:
            request = raw.functions.messages.ReadHistory(
                peer=cast(raw.base.InputPeer, peer), max_id=max_id
            )
        await self.client.invoke(request)

    async def _message_to_updates(self, message: Message) -> list[Any]:
        updates: list[Any] = []
        text = message.text or message.caption
        if text:
            updates.append(update_agent_message(text_block(text)))

        if message.photo:
            updates.append(await self._download_photo_update(message))
        elif message.animation:
            updates.append(
                await self._download_blob_update(message, media_name="animation")
            )
        elif message.video:
            updates.append(
                await self._download_blob_update(message, media_name="video")
            )
        elif message.document:
            updates.append(
                await self._download_blob_update(message, media_name="document")
            )
        return updates

    async def _download_photo_update(self, message: Message) -> Any:
        self._tmp_dir.mkdir(exist_ok=True)
        assert message.photo is not None
        file_path = await self.client.download_media(
            message.photo,
            file_name=str(self._tmp_dir / f"{message.id}_photo"),
            in_memory=False,
            block=True,
        )
        if not file_path:
            raise RuntimeError("Failed to download photo from Telegram")
        try:
            path = Path(cast(str, file_path))
            data = path.read_bytes()
            mime_type = mimetypes.guess_type(str(path))[0] or "image/jpeg"
            encoded = base64.b64encode(data).decode("ascii")
            return update_agent_message(
                ImageContentBlock(
                    type="image",
                    data=encoded,
                    mime_type=mime_type,
                    uri=f"telegram://photo/{message.id}",
                )
            )
        finally:
            Path(cast(str, file_path)).unlink(missing_ok=True)

    async def _download_blob_update(self, message: Message, media_name: str) -> Any:
        self._tmp_dir.mkdir(exist_ok=True)
        file_path = await self.client.download_media(
            message,
            file_name=str(self._tmp_dir / f"{message.id}_{media_name}"),
            in_memory=False,
            block=True,
        )
        if not file_path:
            raise RuntimeError(f"Failed to download {media_name} from Telegram")
        try:
            path = Path(cast(str, file_path))
            data = base64.b64encode(path.read_bytes()).decode("ascii")
            mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            resource = embedded_blob_resource(
                uri=f"telegram://{media_name}/{message.id}/{path.name}",
                blob=data,
                mime_type=mime_type,
            )
            return update_agent_message(resource_block(resource))
        finally:
            Path(cast(str, file_path)).unlink(missing_ok=True)
