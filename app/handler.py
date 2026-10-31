"""Kakao skill handler: sessions, time budgets and customer-safe failure text."""

from __future__ import annotations

import asyncio
import logging

import httpx

from app.agent import TIMEOUT_REPLY, Agent
from app.config import Settings
from app.kakao import CallbackError, SkillRequest, post_callback
from app.server import HANDLER_FAILURE_TEXT
from app.session import SessionStore
from app.types import HandlerResult

logger = logging.getLogger(__name__)

# Kakao lets a callback answer arrive within about a minute; stay inside it.
CALLBACK_BUDGET_SECONDS = 50.0


class TurnHandler:
    def __init__(self, agent: Agent, sessions: SessionStore, settings: Settings, http_client: httpx.AsyncClient) -> None:
        self._agent = agent
        self._sessions = sessions
        self._settings = settings
        self._http = http_client

    async def __call__(self, request: SkillRequest) -> HandlerResult:
        if request.callback_url is None:
            return HandlerResult(text=await self._answer(request, self._settings.TURN_BUDGET_SECONDS))
        return HandlerResult(text="", callback=lambda: self._answer_by_callback(request, request.callback_url))

    async def _answer_by_callback(self, request: SkillRequest, callback_url: str) -> None:
        reply = await self._answer(request, CALLBACK_BUDGET_SECONDS)
        try:
            await post_callback(self._http, callback_url, reply)
        except CallbackError as exc:
            # The server has already answered Kakao; there is no customer-facing channel left.
            logger.error("Could not deliver the callback answer: %s", exc)

    async def _answer(self, request: SkillRequest, budget: float) -> str:
        """The reply text for one request. Never raises: failures become customer-safe text and
        the cause goes to the log only."""
        try:
            async with asyncio.timeout(budget):
                async with self._sessions.lock(request.user_id):
                    session = self._sessions.get(request.user_id)
                    session.observe(request.utterance)
                    return await self._agent.run_turn(session, request.utterance, request.media_urls)
        except TimeoutError:
            logger.warning("Turn exceeded its %.1fs budget", budget)
            return TIMEOUT_REPLY
        except Exception:
            logger.exception("Turn failed")
            return HANDLER_FAILURE_TEXT
