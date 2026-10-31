"""Service assembly: settings, the Claude and action clients, and the Kakao skill server.

Run with ``uvicorn --factory app.main:build_app``. A module-level ``app = build_app()`` would
require the API keys at import time, so the app is built only when the server starts.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
from anthropic import AsyncAnthropic
from fastapi import FastAPI

from app.actions import ActionClient, build_tools, load_spec
from app.agent import Agent
from app.config import Settings
from app.handler import TurnHandler
from app.prompt import build_system_blocks
from app.server import create_app
from app.session import SessionStore


def assemble(settings: Settings, http: httpx.AsyncClient, claude: Any) -> tuple[TurnHandler, ActionClient]:
    """Wire the turn handler. The system blocks and tool definitions are built here, once, and
    reused for every request so the prompt cache keeps hitting."""
    spec = load_spec()
    actions = ActionClient(settings.ACTION_API_BASE_URL, settings.bot_auth_token, http, spec=spec)
    agent = Agent(claude, settings, actions, build_system_blocks(), build_tools(spec), http)
    return TurnHandler(agent, SessionStore(settings), settings, http), actions


def build_app(settings: Settings | None = None, claude: Any | None = None) -> FastAPI:
    """Build the skill server. Raises ``ConfigError`` naming the missing variables.

    ``claude`` is for tests; by default one ``AsyncAnthropic`` client serves the app's lifetime.
    """
    settings = Settings.from_env() if settings is None else settings
    settings.validate()

    http = httpx.AsyncClient()
    owned_claude = claude is None
    claude = AsyncAnthropic(api_key=settings.claude_api_key) if owned_claude else claude
    handler, _ = assemble(settings, http, claude)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await http.aclose()
            if owned_claude:
                await claude.close()

    app = create_app(handler, settings, http)
    app.router.lifespan_context = lifespan
    return app
