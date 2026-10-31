"""FastAPI application factory for the Kakao skill endpoint."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

import httpx
from fastapi import BackgroundTasks, FastAPI, Request, Response

from app.config import Settings
from app.kakao import (
    BadSkillRequest,
    callback_wait_response,
    parse_bot_event,
    parse_skill_request,
    simple_text_response,
)
from app.types import SkillHandler

logger = logging.getLogger(__name__)

# Kakao renders any non-200 reply as an error bubble, so failures answer 200 with these texts.
PARSE_FAILURE_TEXT = "메시지를 이해하지 못했어요. 다시 한 번 말씀해 주세요."
HANDLER_FAILURE_TEXT = "잠시 문제가 생겼어요. 잠시 후 다시 말씀해 주세요."


def create_app(
    handler: SkillHandler,
    settings: Settings | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> FastAPI:
    """Build the skill server. ``settings`` and ``http_client`` are exposed on ``app.state``;
    their lifecycle stays with the caller."""
    # Only /health, /skill and /webhook are public; the generated API docs are not.
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.state.http_client = http_client

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/skill")
    async def skill(request: Request, background_tasks: BackgroundTasks) -> dict[str, object]:
        try:
            skill_request = parse_skill_request(await request.json())
        # ValueError covers a body that is not valid UTF-8 JSON.
        except (ValueError, BadSkillRequest) as exc:
            logger.warning("Rejected Kakao skill request: %s", exc)
            return simple_text_response(PARSE_FAILURE_TEXT)

        try:
            result = await handler(skill_request)
            if result.callback is not None:
                background_tasks.add_task(_run_callback, result.callback)
                return callback_wait_response()
            return simple_text_response(result.text)
        except Exception:
            logger.exception("Skill handler failed")
            return simple_text_response(HANDLER_FAILURE_TEXT)

    @app.post("/webhook", status_code=204)
    async def webhook(request: Request, background_tasks: BackgroundTasks) -> Response:
        """KakaoTalk bot webhook: acknowledge with 204 at once (Kakao requires 2xx within one
        second); the answer goes out afterwards through the bot callback API."""
        acknowledged = Response(status_code=204)
        try:
            event = parse_bot_event(await request.json())
        except (ValueError, BadSkillRequest) as exc:
            logger.warning("Rejected Kakao bot webhook: %s", exc)
            return acknowledged
        if event is None:
            return acknowledged

        try:
            result = await handler(event)
        except Exception:
            logger.exception("Webhook handler failed")
            return acknowledged
        if result.callback is None:
            logger.error("Webhook handler returned no callback; the message gets no answer")
        else:
            background_tasks.add_task(_run_callback, result.callback)
        return acknowledged

    return app


async def _run_callback(callback: Callable[[], Awaitable[None]]) -> None:
    try:
        await callback()
    except Exception:
        logger.exception("Kakao callback task failed")
