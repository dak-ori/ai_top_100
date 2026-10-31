"""Kakao payload parsing and SimpleText responses: Open Builder skills and KakaoTalk bot webhooks."""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

MAX_SIMPLE_TEXT_LENGTH = 500
CALLBACK_WAIT_TEXT = "확인하고 있어요. 잠시만 기다려 주세요."
BOT_CALLBACK_URL = "https://kapi.kakao.com/v1/bot/callback"
MESSAGE_CREATE_EVENT = "MESSAGE_CREATE"

_ELLIPSIS = "..."
# "다." ends in "." so it is covered by the "." boundary.
_SENTENCE_BOUNDARIES = (".", "?", "!", "\n")
_URL_PATTERN = re.compile(r"https?://[^\s\"'<>]+")
_URL_TRAILING_PUNCTUATION = ".,;:!?)]}"
_MEDIA_KINDS = {
    **dict.fromkeys(("jpg", "jpeg", "png", "webp", "gif"), "image"),
    **dict.fromkeys(("m4a", "mp3", "wav", "aac", "ogg"), "audio"),
}


class BadSkillRequest(Exception):
    """The request body is not a usable Kakao skill payload."""


class BadSkillResponse(Exception):
    """A SimpleText response cannot be built from the given text."""


class CallbackError(Exception):
    """Delivering a Kakao callback response failed."""


@dataclass
class SkillRequest:
    utterance: str
    user_id: str
    callback_url: str | None
    media_urls: list[tuple[str, str]]
    # Set for KakaoTalk bot webhook messages: the answer goes to the bot callback API.
    bot_callback_token: str | None = None


def parse_bot_event(body: object) -> SkillRequest | None:
    """Parse a KakaoTalk bot webhook DISPATCH frame. Events other than MESSAGE_CREATE only need
    an acknowledgement, so they return None."""
    if not isinstance(body, dict):
        raise BadSkillRequest("request body must be a JSON object")
    if body.get("t") != MESSAGE_CREATE_EVENT:
        return None
    data = body.get("d")
    if not isinstance(data, dict):
        raise BadSkillRequest("d is missing")

    content = data.get("content")
    if content is None:
        content = ""
    if not isinstance(content, str):
        raise BadSkillRequest("d.content must be a string")

    user_key = data.get("botUserKey")
    if not isinstance(user_key, str) or not user_key:
        raise BadSkillRequest("d.botUserKey is missing")

    callback_token = data.get("callbackToken")
    if not isinstance(callback_token, str) or not callback_token:
        raise BadSkillRequest("d.callbackToken is missing")

    return SkillRequest(
        utterance=content,
        user_id=user_key,
        callback_url=None,
        media_urls=_collect_media_urls([content, data]),
        bot_callback_token=callback_token,
    )


def parse_skill_request(body: object) -> SkillRequest:
    if not isinstance(body, dict):
        raise BadSkillRequest("request body must be a JSON object")
    user_request = body.get("userRequest")
    if not isinstance(user_request, dict):
        raise BadSkillRequest("userRequest is missing")

    utterance = user_request.get("utterance")
    if utterance is None:
        utterance = ""
    if not isinstance(utterance, str):
        raise BadSkillRequest("userRequest.utterance must be a string")

    user = user_request.get("user")
    user_id = user.get("id") if isinstance(user, dict) else None
    if not isinstance(user_id, str) or not user_id:
        raise BadSkillRequest("userRequest.user.id is missing")

    callback_url = user_request.get("callbackUrl")
    if callback_url is not None and not isinstance(callback_url, str):
        raise BadSkillRequest("userRequest.callbackUrl must be a string")

    params = user_request.get("params")
    action = body.get("action")
    media_sources = [
        utterance,
        params.get("media") if isinstance(params, dict) else None,
        action.get("params") if isinstance(action, dict) else None,
        action.get("detailParams") if isinstance(action, dict) else None,
    ]
    return SkillRequest(
        utterance=utterance,
        user_id=user_id,
        callback_url=callback_url or None,
        media_urls=_collect_media_urls(media_sources),
    )


def simple_text_response(text: str) -> dict[str, object]:
    text = text.strip()
    if not text:
        raise BadSkillResponse("SimpleText text must not be empty")
    return {"version": "2.0", "template": {"outputs": [{"simpleText": {"text": _fit_simple_text(text)}}]}}


def callback_wait_response() -> dict[str, object]:
    return {"version": "2.0", "useCallback": True, "data": {"text": CALLBACK_WAIT_TEXT}}


async def post_callback(client: httpx.AsyncClient, callback_url: str, text: str) -> None:
    if not callback_url.startswith("https://"):
        raise CallbackError("callback URL must use https")
    payload = simple_text_response(text)
    try:
        response = await client.post(callback_url, json=payload)
    except httpx.HTTPError as exc:
        raise CallbackError(f"Kakao callback request failed: {type(exc).__name__}") from exc
    if not response.is_success:
        raise CallbackError(f"Kakao callback returned HTTP {response.status_code}")


async def post_bot_callback(client: httpx.AsyncClient, bot_token: str, callback_token: str, text: str) -> None:
    payload = {"skillResponse": simple_text_response(text)}
    headers = {"Authorization": f"KakaoAK {bot_token}", "X-Bot-Callback-Token": callback_token}
    try:
        response = await client.post(BOT_CALLBACK_URL, json=payload, headers=headers)
    except httpx.HTTPError as exc:
        raise CallbackError(f"Kakao bot callback request failed: {type(exc).__name__}") from exc
    if not response.is_success:
        raise CallbackError(f"Kakao bot callback returned HTTP {response.status_code}")


def _fit_simple_text(text: str) -> str:
    if len(text) <= MAX_SIMPLE_TEXT_LENGTH:
        return text
    window = text[:MAX_SIMPLE_TEXT_LENGTH]
    boundary = max(window.rfind(mark) for mark in _SENTENCE_BOUNDARIES)
    fitted = window[: boundary + 1].rstrip()
    if fitted:
        return fitted
    return text[: MAX_SIMPLE_TEXT_LENGTH - len(_ELLIPSIS)] + _ELLIPSIS


def _collect_media_urls(sources: list[object]) -> list[tuple[str, str]]:
    found: dict[str, str] = {}
    for text in _iter_strings(sources):
        for match in _URL_PATTERN.finditer(text):
            url = match.group().rstrip(_URL_TRAILING_PUNCTUATION)
            kind = _media_kind(url)
            if kind is not None:
                found.setdefault(url, kind)
    return [(kind, url) for url, kind in found.items()]


def _iter_strings(value: object) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _iter_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _iter_strings(item)


def _media_kind(url: str) -> str | None:
    try:
        path = urlsplit(url).path
    except ValueError:
        # Malformed URLs (e.g. an unbalanced IPv6 bracket) are not media candidates.
        return None
    _, dot, extension = path.rpartition(".")
    return _MEDIA_KINDS.get(extension.lower()) if dot else None
