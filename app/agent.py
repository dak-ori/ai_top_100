"""One customer turn: model call, tool loop, and a guarded plain-text reply."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import httpx

from app.actions import ActionClient, ForbiddenAction
from app.config import Settings
from app.guard import SAFE_REPLY, clean_reply
from app.media import AUDIO_ONLY_REPLY, MediaError, load_image
from app.session import Session

logger = logging.getLogger(__name__)

MAX_TOKENS = 700
MAX_ITERATIONS = 6
MAX_IMAGES = 3

TOO_MANY_STEPS_REPLY = "확인하는 데 시간이 걸리고 있어요. 담당자에게 넘겨 두었으니 잠시 후 다시 말씀해 주세요."
REFUSAL_REPLY = "이 문의는 제가 바로 답하기 어려워요. 담당자에게 확인해 드릴게요."
TIMEOUT_REPLY = "답변을 준비하는 데 시간이 걸리고 있어요. 같은 내용을 한 번 더 보내 주세요."
EMPTY_MESSAGE_REPLY = "메시지를 확인하지 못했어요. 다시 한 번 말씀해 주세요."
IMAGE_FAILED_MARK = "[이미지를 불러오지 못함]"
IMAGE_KEPT_MARK = "[이미지 첨부]"
NO_TEXT_PLACEHOLDER = "[첨부 파일]"
_THINKING_TYPES = frozenset({"thinking", "redacted_thinking"})


class Agent:
    def __init__(
        self,
        client: Any,
        settings: Settings,
        actions: ActionClient,
        system_blocks: list[dict],
        tools: list,
        http_client: httpx.AsyncClient,
    ) -> None:
        self._client = client
        self._settings = settings
        self._actions = actions
        self._http = http_client
        # Built once and reused byte for byte on every request, so the prompt cache keeps hitting.
        self._system = system_blocks
        self._tools = [_tool_param(tool) for tool in tools]
        self._system_text = "\n".join(block["text"] for block in system_blocks)
        self._secrets = [s for s in (settings.claude_api_key, settings.bot_auth_token) if s]

    async def run_turn(self, session: Session, user_text: str, media_urls: list[tuple[str, str]]) -> str:
        image_urls = [url for kind, url in media_urls if kind == "image"]
        if not user_text.strip() and not image_urls:
            if any(kind == "audio" for kind, _ in media_urls):
                return AUDIO_ONLY_REPLY
            return EMPTY_MESSAGE_REPLY

        try:
            user_message, stored_user = await self._user_message(user_text, image_urls)
            turn, reply = await self._converse(session, user_message)
        except (asyncio.CancelledError, Exception):
            # Keep user/assistant alternation intact for the next turn, then let the failure continue.
            session.commit_turn(_pair(user_text, TIMEOUT_REPLY))
            raise
        reply = clean_reply(reply, system_text=self._system_text, secrets=self._secrets)
        if turn is None:
            turn = _pair(user_text, reply)
        else:
            turn = _without_thinking(turn)
            turn[0] = stored_user
        session.commit_turn(turn)
        return reply

    async def _user_message(self, user_text: str, image_urls: list[str]) -> tuple[dict, dict]:
        """The message sent to the model, and the text-only version kept in the session."""
        results = await asyncio.gather(
            *(load_image(self._http, url) for url in image_urls[:MAX_IMAGES]), return_exceptions=True
        )
        images = []
        text = user_text.strip()
        failed = len(image_urls) - len(results)
        for result in results:
            if isinstance(result, MediaError):
                failed += 1
            elif isinstance(result, BaseException):
                raise result
            else:
                images.append(result)
        if failed:
            text = "\n".join(part for part in (text, " ".join([IMAGE_FAILED_MARK] * failed)) if part)
        text_block = [{"type": "text", "text": text}] if text else []
        kept = [{"type": "text", "text": " ".join([IMAGE_KEPT_MARK] * len(images))}] if images else []
        return (
            {"role": "user", "content": [*text_block, *images]},
            {"role": "user", "content": [*text_block, *kept]},
        )

    async def _converse(self, session: Session, user_message: dict) -> tuple[list[dict] | None, str]:
        """Run the model/tool loop. Returns the finished turn (None when it ended abnormally and the
        caller stores a plain pair instead) and the unfiltered reply text."""
        turn = [user_message]
        for _ in range(MAX_ITERATIONS):
            response = await self._client.messages.create(
                model=self._settings.CLAUDE_MODEL,
                max_tokens=MAX_TOKENS,
                system=self._system,
                tools=self._tools,
                messages=[*session.messages, *turn],
                output_config={"effort": "low"},
            )
            content = [_block_dict(block) for block in response.content]
            text = "".join(b["text"] for b in content if b["type"] == "text").strip()
            stop_reason = response.stop_reason

            if stop_reason == "tool_use":
                tool_uses = [b for b in content if b["type"] == "tool_use"]
                if tool_uses:
                    results = await asyncio.gather(*(self._run_tool(b, session.test_case_id) for b in tool_uses))
                    turn += [{"role": "assistant", "content": content}, {"role": "user", "content": list(results)}]
                    continue
                logger.warning("Model stopped for tool_use without a tool_use block")
            elif stop_reason == "refusal":
                logger.warning("Model refused the request")
                return None, REFUSAL_REPLY
            elif stop_reason == "max_tokens":
                logger.warning("Model reply was cut at max_tokens (text=%s)", bool(text))
                return None, text or SAFE_REPLY
            elif stop_reason in ("end_turn", "stop_sequence") and text:
                turn.append({"role": "assistant", "content": content})
                return turn, text

            if not text:
                logger.warning("Model returned no text (stop_reason=%s)", stop_reason)
                return None, SAFE_REPLY
            return None, text

        logger.warning("Tool loop exceeded %d model calls", MAX_ITERATIONS)
        return None, TOO_MANY_STEPS_REPLY

    async def _run_tool(self, block: dict, test_case_id: str | None) -> dict:
        name = block["name"]
        try:
            result = await self._actions.call(name, block["input"], test_case_id)
            payload = json.dumps(result, ensure_ascii=False)
        except ForbiddenAction:
            logger.warning("Model asked for a forbidden tool: %s", name)
            payload = json.dumps({"ok": False, "error": "forbidden_action"})
        except Exception:
            logger.exception("Tool %s failed unexpectedly", name)
            payload = json.dumps({"ok": False, "error": "tool_failed"})
        return {"type": "tool_result", "tool_use_id": block["id"], "content": payload}


def _pair(user_text: str, reply: str) -> list[dict]:
    return [
        {"role": "user", "content": user_text.strip() or NO_TEXT_PLACEHOLDER},
        {"role": "assistant", "content": reply},
    ]


def _without_thinking(turn: list[dict]) -> list[dict]:
    """The turn minus its thinking blocks. They are only needed while the tool loop runs; the stored
    user message differs from the one the model saw, and a thinking block replayed after such an
    edit is rejected."""
    return [
        {**message, "content": [b for b in message["content"] if b["type"] not in _THINKING_TYPES]}
        if message["role"] == "assistant"
        else message
        for message in turn
    ]


def _tool_param(tool: Any) -> dict:
    if isinstance(tool, dict):
        return tool
    return {"name": tool.name, "description": tool.description, "input_schema": tool.input_schema}


def _block_dict(block: Any) -> dict:
    """A response content block as a plain dict that can be sent back in the next request."""
    if isinstance(block, dict):
        return block
    if hasattr(block, "model_dump"):
        return block.model_dump(exclude_none=True)
    return {key: value for key, value in vars(block).items() if value is not None}
