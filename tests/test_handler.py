import asyncio
import json
import logging

import httpx
from fastapi.testclient import TestClient

import app.handler as handler_module
from app.agent import TIMEOUT_REPLY
from app.config import Settings
from app.handler import TurnHandler
from app.kakao import BOT_CALLBACK_URL, CALLBACK_WAIT_TEXT, SkillRequest
from app.server import HANDLER_FAILURE_TEXT, create_app
from app.session import SessionStore

CALLBACK_URL = "https://bot-api.kakao.test/callback/abc"
SECRET_DETAIL = "Traceback secret-key-value /srv/app/agent.py"


class FakeAgent:
    def __init__(self, behaviour=None):
        self.behaviour = behaviour or self.answer
        self.turns = []

    async def answer(self, session, user_text, media_urls):
        return f"답변: {user_text}"

    async def run_turn(self, session, user_text, media_urls):
        self.turns.append((session.test_case_id, user_text, media_urls))
        return await self.behaviour(session, user_text, media_urls)


def request(utterance="안녕하세요", user_id="u1", callback_url=None, media_urls=()):
    return SkillRequest(utterance, user_id, callback_url, list(media_urls))


def build(agent=None, **settings):
    posts = []

    def transport(req):
        posts.append(req)
        return httpx.Response(200, json={"status": "SUCCESS"})

    http = httpx.AsyncClient(transport=httpx.MockTransport(transport))
    config = Settings(**settings)
    sessions = SessionStore(config)
    agent = agent or FakeAgent()
    return TurnHandler(agent, sessions, config, http), agent, sessions, posts


async def test_direct_reply_without_callback():
    handler, agent, _, _ = build()
    result = await handler(request("배송 문의", media_urls=[("image", "https://i.example/a.png")]))
    assert result.text == "답변: 배송 문의" and result.callback is None
    assert agent.turns == [(None, "배송 문의", [("image", "https://i.example/a.png")])]


async def test_test_case_id_is_extracted_before_the_turn():
    handler, agent, sessions, _ = build()
    await handler(request("id: L-03 재고 알려줘"))
    await handler(request("그럼 이건요?"))
    await handler(request("R-05-a 시작"))
    assert [turn[0] for turn in agent.turns] == ["L-03", "L-03", "R-05-a"]
    assert sessions.get("u1").test_case_id == "R-05-a"


async def test_over_budget_returns_retry_text():
    async def slow(session, text, media):
        await asyncio.sleep(30)

    handler, _, _, _ = build(FakeAgent(slow), TURN_BUDGET_SECONDS=0.05)
    result = await handler(request())
    assert result.text == TIMEOUT_REPLY and result.callback is None


async def test_exception_text_never_reaches_the_customer(caplog):
    async def boom(session, text, media):
        raise RuntimeError(SECRET_DETAIL)

    handler, _, _, _ = build(FakeAgent(boom))
    with caplog.at_level(logging.ERROR, logger="app.handler"):
        result = await handler(request())
    assert result.text == HANDLER_FAILURE_TEXT and "secret" not in result.text.lower()
    assert "Turn failed" in caplog.text


async def test_callback_request_returns_empty_text_and_posts_the_answer_later():
    handler, _, _, posts = build()
    result = await handler(request("문의", callback_url=CALLBACK_URL))
    assert result.text == "" and result.callback is not None
    assert posts == []
    await result.callback()
    (post,) = posts
    assert str(post.url) == CALLBACK_URL
    body = json.loads(post.content)
    assert body["version"] == "2.0"
    assert body["template"]["outputs"][0]["simpleText"]["text"] == "답변: 문의"


async def test_bot_webhook_request_answers_through_the_bot_callback_api():
    handler, _, _, posts = build(kakao_bot_token="bot-token")
    bot_request = SkillRequest("문의", "buk_1", None, [], bot_callback_token="cb_1")
    result = await handler(bot_request)
    assert result.text == "" and result.callback is not None
    assert posts == []
    await result.callback()
    (post,) = posts
    assert str(post.url) == BOT_CALLBACK_URL
    assert post.headers["Authorization"] == "KakaoAK bot-token"
    assert post.headers["X-Bot-Callback-Token"] == "cb_1"
    body = json.loads(post.content)
    assert body["skillResponse"]["template"]["outputs"][0]["simpleText"]["text"] == "답변: 문의"


async def test_callback_failure_sends_safe_text_without_details():
    async def boom(session, text, media):
        raise RuntimeError(SECRET_DETAIL)

    handler, _, _, posts = build(FakeAgent(boom))
    result = await handler(request(callback_url=CALLBACK_URL))
    await result.callback()
    text = json.loads(posts[0].content)["template"]["outputs"][0]["simpleText"]["text"]
    assert text == HANDLER_FAILURE_TEXT and "secret" not in posts[0].content.decode().lower()


async def test_callback_timeout_sends_timeout_text(monkeypatch):
    async def slow(session, text, media):
        await asyncio.sleep(30)

    monkeypatch.setattr(handler_module, "CALLBACK_BUDGET_SECONDS", 0.05)
    handler, _, _, posts = build(FakeAgent(slow))
    result = await handler(request(callback_url=CALLBACK_URL))
    await result.callback()
    assert json.loads(posts[0].content)["template"]["outputs"][0]["simpleText"]["text"] == TIMEOUT_REPLY


async def test_callback_delivery_failure_is_logged_not_raised(caplog):
    handler, _, _, _ = build()
    handler._http = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    result = await handler(request(callback_url=CALLBACK_URL))
    with caplog.at_level(logging.ERROR, logger="app.handler"):
        await result.callback()
    assert "Could not deliver" in caplog.text and "HTTP 500" in caplog.text


async def test_insecure_callback_url_is_logged_not_raised(caplog):
    handler, _, _, posts = build()
    result = await handler(request(callback_url="http://bot.example/cb"))
    with caplog.at_level(logging.ERROR, logger="app.handler"):
        await result.callback()
    assert posts == [] and "Could not deliver" in caplog.text


async def test_same_user_requests_are_serialized():
    order = []

    async def work(session, text, media):
        order.append(f"{text}+")
        await asyncio.sleep(0.02)
        order.append(f"{text}-")
        return text

    handler, _, _, _ = build(FakeAgent(work))
    await asyncio.gather(
        handler(request("a", user_id="u1")), handler(request("b", user_id="u1")), handler(request("c", user_id="u2"))
    )
    assert order.index("a-") < order.index("b+")
    assert order.index("c+") < order.index("a-")


async def test_waiting_for_the_user_lock_counts_against_the_budget():
    async def slow(session, text, media):
        await asyncio.sleep(0.3)
        return text

    handler, _, _, _ = build(FakeAgent(slow), TURN_BUDGET_SECONDS=0.2)
    first, second = await asyncio.gather(handler(request("a")), handler(request("b")))
    assert first.text == TIMEOUT_REPLY and second.text == TIMEOUT_REPLY


def test_wired_into_the_server_for_both_paths():
    handler, _, _, posts = build()

    def body(callback_url=None):
        user_request = {"utterance": "문의", "user": {"id": "u9"}}
        if callback_url:
            user_request["callbackUrl"] = callback_url
        return {"userRequest": user_request, "action": {}}

    with TestClient(create_app(handler)) as client:
        direct = client.post("/skill", json=body()).json()
        assert direct["template"]["outputs"][0]["simpleText"]["text"] == "답변: 문의"
        waiting = client.post("/skill", json=body(CALLBACK_URL)).json()
        assert waiting["useCallback"] is True and waiting["data"]["text"] == CALLBACK_WAIT_TEXT
    assert [str(post.url) for post in posts] == [CALLBACK_URL]
