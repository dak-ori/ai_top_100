import logging
from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.kakao import SkillRequest, callback_wait_response, simple_text_response
from app.server import HANDLER_FAILURE_TEXT, PARSE_FAILURE_TEXT, create_app
from app.types import HandlerResult, SkillHandler

SECRET = "test-secret-key-value"
BODY = {
    "intent": {"id": "i", "name": "fallback"},
    "userRequest": {"utterance": "배송 언제 와요?", "user": {"id": "user-1"}},
    "action": {"params": {}, "detailParams": {}},
}


class RecordingHandler:
    def __init__(self, result: HandlerResult) -> None:
        self.result = result
        self.requests: list[SkillRequest] = []

    async def __call__(self, request: SkillRequest) -> HandlerResult:
        self.requests.append(request)
        return self.result


@contextmanager
def serve(handler: SkillHandler) -> Iterator[TestClient]:
    settings = Settings(claude_api_key=SECRET, bot_auth_token=SECRET)
    with TestClient(create_app(handler, settings=settings)) as client:
        yield client


@pytest.fixture
def handler() -> RecordingHandler:
    return RecordingHandler(HandlerResult(text="내일 도착 예정이에요."))


@pytest.fixture
def client(handler: RecordingHandler) -> Iterator[TestClient]:
    with serve(handler) as test_client:
        yield test_client


def test_health_reports_ok_without_settings(client: TestClient) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert SECRET not in response.text


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
def test_generated_api_docs_are_not_exposed(path: str, client: TestClient) -> None:
    assert client.get(path).status_code == 404


def test_skill_returns_simple_text_from_handler(client: TestClient, handler: RecordingHandler) -> None:
    response = client.post("/skill", json=BODY)

    assert response.status_code == 200
    assert response.json() == simple_text_response("내일 도착 예정이에요.")
    assert [(r.utterance, r.user_id) for r in handler.requests] == [("배송 언제 와요?", "user-1")]


@pytest.mark.parametrize(
    "send",
    [
        {"json": {"userRequest": {"utterance": "hi"}}},
        {"json": ["not", "an", "object"]},
        {"content": b"{not json", "headers": {"content-type": "application/json"}},
        {"content": b"\xff\xfe", "headers": {"content-type": "application/json"}},
    ],
)
def test_unparseable_request_returns_safe_text(
    send: dict[str, object], client: TestClient, handler: RecordingHandler, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="app.server"):
        response = client.post("/skill", **send)

    assert response.status_code == 200
    assert response.json() == simple_text_response(PARSE_FAILURE_TEXT)
    assert handler.requests == []
    assert "Rejected Kakao skill request" in caplog.text


def test_handler_exception_returns_safe_text_and_logs(caplog: pytest.LogCaptureFixture) -> None:
    async def failing(request: SkillRequest) -> HandlerResult:
        raise RuntimeError("upstream exploded")

    with caplog.at_level(logging.ERROR, logger="app.server"):
        with serve(failing) as client:
            response = client.post("/skill", json=BODY)

    assert response.status_code == 200
    assert response.json() == simple_text_response(HANDLER_FAILURE_TEXT)
    assert "upstream exploded" not in response.text
    assert "Skill handler failed" in caplog.text
    assert "upstream exploded" in caplog.text


def test_empty_handler_text_returns_safe_text() -> None:
    with serve(RecordingHandler(HandlerResult(text=""))) as client:
        response = client.post("/skill", json=BODY)

    assert response.status_code == 200
    assert response.json() == simple_text_response(HANDLER_FAILURE_TEXT)


def test_callback_result_answers_wait_and_runs_in_background() -> None:
    ran: list[str] = []

    async def callback() -> None:
        ran.append("sent")

    with serve(RecordingHandler(HandlerResult(text="", callback=callback))) as client:
        response = client.post("/skill", json=BODY)

    assert response.status_code == 200
    assert response.json() == callback_wait_response()
    assert ran == ["sent"]


def test_callback_exception_is_logged_and_server_keeps_serving(caplog: pytest.LogCaptureFixture) -> None:
    async def callback() -> None:
        raise RuntimeError("callback exploded")

    with caplog.at_level(logging.ERROR, logger="app.server"):
        with serve(RecordingHandler(HandlerResult(text="", callback=callback))) as client:
            first = client.post("/skill", json=BODY)
            second = client.post("/skill", json=BODY)
            health = client.get("/health")

    assert first.json() == callback_wait_response()
    assert second.json() == callback_wait_response()
    assert health.json() == {"status": "ok"}
    assert caplog.text.count("Kakao callback task failed") == 2
    assert "callback exploded" in caplog.text


WEBHOOK_BODY = {
    "op": 0,
    "t": "MESSAGE_CREATE",
    "d": {
        "id": 558185822630215770,
        "content": "배송 언제 와요?",
        "isChannelChatroom": False,
        "botGroupKey": "bgk_xxx",
        "botUserKey": "buk_xxx",
        "timestamp": "2026-06-09T12:34:56.789Z",
        "callbackToken": "cb_token_xxx",
    },
}


def test_webhook_message_is_acknowledged_with_204_and_answered_in_background() -> None:
    ran: list[str] = []

    async def callback() -> None:
        ran.append("sent")

    handler = RecordingHandler(HandlerResult(text="", callback=callback))
    with serve(handler) as client:
        response = client.post("/webhook", json=WEBHOOK_BODY)

    assert response.status_code == 204 and response.content == b""
    (request,) = handler.requests
    assert request.utterance == "배송 언제 와요?"
    assert request.user_id == "buk_xxx"
    assert request.bot_callback_token == "cb_token_xxx"
    assert ran == ["sent"]


@pytest.mark.parametrize(
    "body",
    [{"op": 0, "t": "LEAVE", "d": {"botUserKey": "buk_xxx"}}, {"op": 0, "t": "MESSAGE_CREATE"}, ["not", "a", "dict"]],
)
def test_webhook_other_events_and_bad_frames_are_acknowledged_without_a_turn(
    body: object, handler: RecordingHandler, client: TestClient
) -> None:
    response = client.post("/webhook", json=body)

    assert response.status_code == 204
    assert handler.requests == []
