from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from acp import (
    PROTOCOL_VERSION,
    Agent,
    InitializeResponse,
    LoadSessionResponse,
    NewSessionResponse,
    PromptResponse,
    run_agent,
)
from acp.interfaces import Client as AcpClient
from acp.schema import (
    AgentCapabilities,
    AudioContentBlock,
    ClientCapabilities,
    CloseSessionResponse,
    EmbeddedResourceContentBlock,
    HttpMcpServer,
    ImageContentBlock,
    Implementation,
    ListSessionsResponse,
    McpServerStdio,
    ResourceContentBlock,
    SessionInfo,
    SseMcpServer,
    TextContentBlock,
)

from .bridge import IncomingChunk, TelegramBridge
from .config import RuntimeConfig, TelegramCredentials


class TelegramRelayAgent(Agent):
    def __init__(self, bridge: TelegramBridge) -> None:
        self._conn: AcpClient | None = None
        self._bridge = bridge
        self._sessions: set[str] = set()
        self._session_meta: dict[str, SessionInfo] = {}
        self._prompt_lock = asyncio.Lock()
        self._active_prompt_session_id: str | None = None
        self._cancel_events: dict[str, asyncio.Event] = {}

    def on_connect(self, conn: AcpClient) -> None:
        self._conn = conn

    async def initialize(
        self,
        protocol_version: int,
        client_capabilities: ClientCapabilities | None = None,
        client_info: Implementation | None = None,
        **kwargs: Any,
    ) -> InitializeResponse:
        await self._bridge.start(self)
        return InitializeResponse(
            protocol_version=PROTOCOL_VERSION
            if protocol_version <= PROTOCOL_VERSION
            else PROTOCOL_VERSION,
            agent_capabilities=AgentCapabilities(),
            agent_info=Implementation(
                name="telegram-acp-relay",
                title="Telegram ACP Relay",
                version="0.1.0",
            ),
        )

    async def new_session(
        self,
        cwd: str,
        additional_directories: list[str] | None = None,
        mcp_servers: list[HttpMcpServer | SseMcpServer | McpServerStdio] | None = None,
        **kwargs: Any,
    ) -> NewSessionResponse:
        session_id = uuid4().hex
        self._register_session(
            session_id=session_id,
            cwd=cwd,
            additional_directories=additional_directories,
        )
        return NewSessionResponse(session_id=session_id)

    async def prompt(
        self,
        prompt: list[
            TextContentBlock
            | ImageContentBlock
            | AudioContentBlock
            | ResourceContentBlock
            | EmbeddedResourceContentBlock
        ],
        session_id: str,
        message_id: str | None = None,
        **kwargs: Any,
    ) -> PromptResponse:
        if session_id not in self._sessions:
            self._register_session(
                session_id=session_id, cwd=str(Path.cwd()), additional_directories=None
            )

        cancel_event = asyncio.Event()
        self._cancel_events[session_id] = cancel_event
        self._touch_session(session_id)

        async with self._prompt_lock:
            self._active_prompt_session_id = session_id
            self._bridge.bind_session(session_id)
            await self._bridge.send_prompt_blocks(session_id, prompt)
            await self._bridge.wait_for_turn_updates(session_id, cancel_event)
            self._active_prompt_session_id = None

        self._cancel_events.pop(session_id, None)
        return PromptResponse(
            stop_reason="cancelled" if cancel_event.is_set() else "end_turn",
            user_message_id=message_id,
        )

    async def cancel(self, session_id: str, **kwargs: Any) -> None:
        event = self._cancel_events.get(session_id)
        if event is not None:
            event.set()

    async def load_session(
        self,
        cwd: str,
        session_id: str,
        additional_directories: list[str] | None = None,
        mcp_servers: list[HttpMcpServer | SseMcpServer | McpServerStdio] | None = None,
        **kwargs: Any,
    ) -> LoadSessionResponse:
        self._register_session(
            session_id=session_id,
            cwd=cwd,
            additional_directories=additional_directories,
        )
        return LoadSessionResponse()

    async def list_sessions(
        self,
        additional_directories: list[str] | None = None,
        cursor: str | None = None,
        cwd: str | None = None,
        **kwargs: Any,
    ) -> ListSessionsResponse:
        return ListSessionsResponse(
            sessions=sorted(
                self._session_meta.values(),
                key=lambda item: item.updated_at or "",
                reverse=True,
            )
        )

    async def close_session(
        self, session_id: str, **kwargs: Any
    ) -> CloseSessionResponse:
        self._sessions.discard(session_id)
        self._cancel_events.pop(session_id, None)
        self._session_meta.pop(session_id, None)
        self._bridge.unbind_session(session_id)
        return CloseSessionResponse()

    async def ext_method(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        return {"unsupported": method, "params": params}

    async def ext_notification(self, method: str, params: dict[str, Any]) -> None:
        logging.debug(
            "Ignored extension notification %s with params %s", method, params
        )

    async def push_unsolicited_update(self, item: IncomingChunk) -> None:
        if self._conn is None or item.session_id is None:
            return
        self._touch_session(item.session_id)
        await self._conn.session_update(session_id=item.session_id, update=item.update)

    def _register_session(
        self,
        *,
        session_id: str,
        cwd: str,
        additional_directories: list[str] | None,
    ) -> None:
        self._sessions.add(session_id)
        self._bridge.bind_session(session_id)
        self._session_meta[session_id] = SessionInfo(
            session_id=session_id,
            cwd=cwd,
            title=f"Telegram Relay {session_id[:8]}",
            updated_at=self._now_iso(),
            additional_directories=additional_directories,
        )

    def _touch_session(self, session_id: str) -> None:
        info = self._session_meta.get(session_id)
        if info is None:
            self._session_meta[session_id] = SessionInfo(
                session_id=session_id,
                cwd=str(Path.cwd()),
                title=f"Telegram Relay {session_id[:8]}",
                updated_at=self._now_iso(),
            )
            return
        info.updated_at = self._now_iso()

    def _now_iso(self) -> str:
        return datetime.now(UTC).isoformat()


def build_bridge(
    config: RuntimeConfig,
    credentials: TelegramCredentials,
    *,
    workdir: Path | None = None,
) -> TelegramBridge:
    return TelegramBridge(
        target_chat=config.target_chat,
        session_name=config.session_name,
        credentials=credentials,
        first_response_timeout=config.first_response_timeout,
        idle_timeout=config.idle_timeout,
        workdir=workdir,
    )


def build_agent(
    config: RuntimeConfig,
    credentials: TelegramCredentials,
    *,
    workdir: Path | None = None,
) -> TelegramRelayAgent:
    bridge = build_bridge(config, credentials, workdir=workdir)
    return TelegramRelayAgent(bridge)


async def run_telegram_acp_agent(
    config: RuntimeConfig,
    *,
    credentials: TelegramCredentials | None = None,
    workdir: Path | None = None,
) -> None:
    agent = build_agent(
        config, credentials or TelegramCredentials.from_env(), workdir=workdir
    )
    await run_agent(agent)
