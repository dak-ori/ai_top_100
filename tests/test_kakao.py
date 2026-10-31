import json

import httpx
import pytest

from app.kakao import (
    MAX_SIMPLE_TEXT_LENGTH,
    BadSkillRequest,
    BadSkillResponse,
    CallbackError,
    callback_wait_response,
    parse_bot_event,
    parse_skill_request,
    post_callback,
    simple_text_response,
)

CALLBACK_URL = "https://bot-api.kakao.test/callback/abc"


def skill_body(**user_request: object) -> dict[str, object]:
    return {
        "intent": {"id": "i", "name": "fallback"},
        "userRequest": {"utterance": "안녕하세요", "user": {"id": "user-1"}, **user_request},
        "action": {"params": {}, "detailParams": {}},
    }


def response_text(response: dict) -> str:
    return response["template"]["outputs"][0]["simpleText"]["text"]


# --- parse_skill_request ---


def test_parses_standard_payload() -> None:
    request = parse_skill_request(skill_body(callbackUrl=CALLBACK_URL))

    assert request.utterance == "안녕하세요"
    assert request.user_id == "user-1"
    assert request.callback_url == CALLBACK_URL
    assert request.media_urls == []


def test_missing_utterance_and_callback_default() -> None:
    body = {"userRequest": {"user": {"id": "user-1"}}}

    request = parse_skill_request(body)

    assert request.utterance == ""
    assert request.callback_url is None


@pytest.mark.parametrize(
    "body",
    [
        {"userRequest": {"utterance": "hi"}},
        {"userRequest": {"utterance": "hi", "user": {}}},
        {"userRequest": {"utterance": "hi", "user": {"id": ""}}},
        {"userRequest": {"utterance": "hi", "user": {"id": 7}}},
        {"intent": {}},
        ["not", "an", "object"],
    ],
)
def test_missing_user_id_raises_bad_skill_request(body: object) -> None:
    with pytest.raises(BadSkillRequest):
        parse_skill_request(body)


def test_classifies_media_urls_in_utterance() -> None:
    utterance = (
        "사진 https://cdn.test/a/photo.JPG?x=1 이고 녹음은 http://cdn.test/v.m4a. "
        "문서 https://cdn.test/doc.pdf 와 https://cdn.test/page 는 무시"
    )
    request = parse_skill_request(skill_body(utterance=utterance))

    assert request.media_urls == [
        ("image", "https://cdn.test/a/photo.JPG?x=1"),
        ("audio", "http://cdn.test/v.m4a"),
    ]


@pytest.mark.parametrize(
    ("extension", "kind"),
    [(e, "image") for e in ("jpg", "jpeg", "png", "webp", "gif")]
    + [(e, "audio") for e in ("m4a", "mp3", "wav", "aac", "ogg")],
)
def test_every_supported_extension(extension: str, kind: str) -> None:
    url = f"https://cdn.test/file.{extension}"
    assert parse_skill_request(skill_body(utterance=url)).media_urls == [(kind, url)]


def test_collects_media_from_params_and_deduplicates() -> None:
    image = "https://talk.kakaocdn.test/img/1.png"
    audio = "https://talk.kakaocdn.test/voice/2.wav"
    body = skill_body(utterance=f"이거 봐주세요 {image}", params={"media": {"type": "image", "url": image}})
    body["action"] = {
        "params": {"secureimage": f"List({image}, {audio})", "count": 2},
        "detailParams": {"secureimage": {"origin": f"List({audio})", "value": audio}},
    }

    request = parse_skill_request(body)

    assert request.media_urls == [("image", image), ("audio", audio)]


# --- simple_text_response ---


def test_simple_text_shape() -> None:
    assert simple_text_response("안녕하세요") == {
        "version": "2.0",
        "template": {"outputs": [{"simpleText": {"text": "안녕하세요"}}]},
    }


@pytest.mark.parametrize("text", ["", "   \n"])
def test_empty_text_raises(text: str) -> None:
    with pytest.raises(BadSkillResponse):
        simple_text_response(text)


def test_exactly_500_characters_is_untouched() -> None:
    text = "가" * MAX_SIMPLE_TEXT_LENGTH
    assert response_text(simple_text_response(text)) == text


def test_501_characters_without_boundary_gets_ellipsis() -> None:
    text = "가" * (MAX_SIMPLE_TEXT_LENGTH + 1)

    fitted = response_text(simple_text_response(text))

    assert fitted == "가" * 497 + "..."
    assert len(fitted) == MAX_SIMPLE_TEXT_LENGTH


def test_long_text_cuts_at_last_sentence_boundary() -> None:
    first = "주문을 확인했어요." + "가" * 300 + "다."
    text = first + " " + "나" * 300

    fitted = response_text(simple_text_response(text))

    assert fitted == first
    assert len(fitted) <= MAX_SIMPLE_TEXT_LENGTH


@pytest.mark.parametrize("mark", ["?", "!"])
def test_long_text_cuts_at_question_or_exclamation(mark: str) -> None:
    first = "가" * 200 + mark
    assert response_text(simple_text_response(first + "나" * 400)) == first


def test_long_text_cuts_at_newline() -> None:
    first = "가" * 450
    assert response_text(simple_text_response(first + "\n" + "나" * 100)) == first


def test_boundary_past_limit_is_ignored() -> None:
    text = "가" * 520 + "."
    assert response_text(simple_text_response(text)) == "가" * 497 + "..."


def test_callback_wait_response() -> None:
    assert callback_wait_response() == {
        "version": "2.0",
        "useCallback": True,
        "data": {"text": "확인하고 있어요. 잠시만 기다려 주세요."},
    }


# --- post_callback ---


async def test_post_callback_sends_simple_text() -> None:
    sent: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json={"status": "SUCCESS"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        await post_callback(client, CALLBACK_URL, "배송 중이에요.")

    assert len(sent) == 1
    assert sent[0].method == "POST"
    assert str(sent[0].url) == CALLBACK_URL
    assert json.loads(sent[0].content) == simple_text_response("배송 중이에요.")


@pytest.mark.parametrize("status", [400, 500])
async def test_post_callback_non_2xx_raises(status: int) -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(status))
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(CallbackError, match=str(status)):
            await post_callback(client, CALLBACK_URL, "답변")


async def test_post_callback_transport_error_raises_callback_error() -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("unreachable", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(fail)) as client:
        with pytest.raises(CallbackError):
            await post_callback(client, CALLBACK_URL, "답변")


@pytest.mark.parametrize("url", ["http://bot-api.kakao.test/callback/abc", "ftp://x", ""])
async def test_post_callback_rejects_non_https_without_sending(url: str) -> None:
    sent: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(CallbackError):
            await post_callback(client, url, "답변")

    assert sent == []


def test_bot_message_event_collects_media_and_requires_callback_token() -> None:
    event = parse_bot_event(
        {
            "op": 0,
            "t": "MESSAGE_CREATE",
            "d": {
                "content": "사진 보냅니다",
                "botUserKey": "buk_1",
                "callbackToken": "cb_1",
                "attachments": [{"url": "https://cdn.example/mold.jpg"}],
            },
        }
    )
    assert event is not None
    assert (event.utterance, event.user_id, event.bot_callback_token) == ("사진 보냅니다", "buk_1", "cb_1")
    assert event.media_urls == [("image", "https://cdn.example/mold.jpg")]

    with pytest.raises(BadSkillRequest, match="callbackToken"):
        parse_bot_event({"op": 0, "t": "MESSAGE_CREATE", "d": {"content": "hi", "botUserKey": "buk_1"}})
