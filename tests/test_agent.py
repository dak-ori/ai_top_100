import asyncio
import copy
import json
import logging
import socket
from types import SimpleNamespace

import httpx
import pytest
from anthropic.types import TextBlock, ThinkingBlock, ToolUseBlock

from app.actions import ActionClient, build_tools, load_spec
from app.agent import (
    IMAGE_FAILED_MARK,
    MAX_ITERATIONS,
    REFUSAL_REPLY,
    TIMEOUT_REPLY,
    TOO_MANY_STEPS_REPLY,
    Agent,
)
from app.config import Settings
from app.guard import SAFE_REPLY
from app.media import AUDIO_ONLY_REPLY
from app.prompt import build_system_blocks
from app.session import SessionStore

SPEC = load_spec()
TOOLS = build_tools(SPEC)
CLAUDE_KEY = "claude-key-for-tests-0001"
ACTION_TOKEN = "action-token-for-tests-0001"
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 16
IMAGE_URL = "https://img.example.com/photo.png"


def text(value):
    return SimpleNamespace(type="text", text=value)


def tool_use(tool_id, name, **args):
    return SimpleNamespace(type="tool_use", id=tool_id, name=name, input=args)


def reply(*blocks, stop="end_turn"):
    return SimpleNamespace(content=list(blocks), stop_reason=stop)


class FakeAnthropic:
    """``messages.create`` replays scripted responses in order and records a snapshot of each call."""

    def __init__(self, *responses):
        self._responses = list(responses)
        self.calls = []
        self.messages = SimpleNamespace(create=self._create)

    async def _create(self, **kwargs):
        self.calls.append(copy.deepcopy(kwargs))
        if not self._responses:
            raise AssertionError("model called more often than scripted")
        response = self._responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        if callable(response):
            return await response()
        return response


def public_dns(host, port, *args):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]


@pytest.fixture
def world():
    """Agent wired to a fake model and a recording mock transport for the action API and images."""
    requests = []
    clients = []

    def build(*responses, action_json=None, images=None, settings=None):
        def handler(request):
            requests.append(request)
            if request.url.host == "action.test":
                return httpx.Response(200, json=action_json or {"sku": "A-1", "stock": 3})
            return (images or {}).get(str(request.url), httpx.Response(404))

        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        clients.append(http)
        settings = settings or Settings(claude_api_key=CLAUDE_KEY, bot_auth_token=ACTION_TOKEN)
        actions = ActionClient("https://action.test", ACTION_TOKEN, http, spec=SPEC)
        model = FakeAnthropic(*responses)
        agent = Agent(model, settings, actions, build_system_blocks(), TOOLS, http)
        session = SessionStore(settings).get("u")
        return agent, session, model, requests

    yield build


@pytest.fixture(autouse=True)
def offline_dns(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", public_dns)


async def test_simple_text_reply_and_request_shape(world):
    agent, session, model, _ = world(reply(text("안녕하세요, 무엇을 도와드릴까요?")))
    assert await agent.run_turn(session, "안녕하세요", []) == "안녕하세요, 무엇을 도와드릴까요?"
    (call,) = model.calls
    assert call["model"] == "claude-haiku-5-5"
    assert call["max_tokens"] == 4000
    assert call["output_config"] == {"effort": "low"}
    assert call["messages"] == [{"role": "user", "content": [{"type": "text", "text": "안녕하세요"}]}]
    assert [tool["name"] for tool in call["tools"]] == [tool.name for tool in TOOLS]
    assert not {"thinking", "temperature", "top_p", "top_k", "tool_choice"} & call.keys()
    assert call["messages"][-1]["role"] == "user"
    assert [m["role"] for m in session.messages] == ["user", "assistant"]


async def test_tool_called_once_then_answer(world):
    agent, session, model, requests = world(
        reply(text("확인할게요."), tool_use("t1", "api_01", sku="A-1"), stop="tool_use"),
        reply(text("재고는 3개예요.")),
    )
    session.test_case_id = "L-03"
    assert await agent.run_turn(session, "A-1 재고 있어요?", []) == "재고는 3개예요."

    (request,) = requests
    assert request.headers["X-Aitop100-Test-Case-Id"] == "L-03"
    assert request.headers["Authorization"] == f"Bearer {ACTION_TOKEN}"
    second = model.calls[1]["messages"]
    assert [m["role"] for m in second] == ["user", "assistant", "user"]
    (result,) = second[2]["content"]
    assert result["type"] == "tool_result" and result["tool_use_id"] == "t1"
    assert json.loads(result["content"]) == {"ok": True, "status": 200, "body": {"sku": "A-1", "stock": 3}}
    assert "is_error" not in result
    assert [m["role"] for m in session.messages] == ["user", "assistant", "user", "assistant"]


async def test_parallel_tool_uses_return_in_one_user_message(world):
    agent, session, model, requests = world(
        reply(tool_use("t1", "api_01", sku="A-1"), tool_use("t2", "api_02", item="B"), stop="tool_use"),
        reply(text("둘 다 확인했어요.")),
    )
    session.test_case_id = "L-03"
    await agent.run_turn(session, "두 가지 확인", [])
    assert len(requests) == 2
    last = model.calls[1]["messages"][-1]
    assert last["role"] == "user"
    assert [b["tool_use_id"] for b in last["content"]] == ["t1", "t2"]
    assert all(b["type"] == "tool_result" for b in last["content"])


async def test_missing_test_case_id_reaches_the_model(world):
    agent, session, model, requests = world(
        reply(tool_use("t1", "api_01", sku="A-1"), stop="tool_use"), reply(text("케이스 번호가 필요해요."))
    )
    await agent.run_turn(session, "재고 알려줘", [])
    assert requests == []
    result = model.calls[1]["messages"][-1]["content"][0]
    assert json.loads(result["content"]) == {"ok": False, "error": "test_case_id_missing"}
    assert "is_error" not in result


async def test_unknown_and_forbidden_tools_become_ok_false(world):
    agent, session, model, requests = world(
        reply(tool_use("t1", "nope"), tool_use("t2", "resetmyactiondata"), stop="tool_use"), reply(text("처리했어요."))
    )
    session.test_case_id = "L-03"
    await agent.run_turn(session, "해줘", [])
    assert requests == []
    results = [json.loads(b["content"]) for b in model.calls[1]["messages"][-1]["content"]]
    assert all(r["ok"] is False for r in results)


async def test_tool_loop_stops_after_six_model_calls(world, caplog):
    looping = [reply(tool_use(f"t{i}", "api_01", sku="A-1"), stop="tool_use") for i in range(MAX_ITERATIONS + 2)]
    agent, session, model, _ = world(*looping)
    session.test_case_id = "L-03"
    with caplog.at_level(logging.WARNING, logger="app.agent"):
        assert await agent.run_turn(session, "재고?", []) == TOO_MANY_STEPS_REPLY
    assert len(model.calls) == MAX_ITERATIONS == 6
    assert "exceeded" in caplog.text
    assert session.messages == [
        {"role": "user", "content": "재고?"},
        {"role": "assistant", "content": TOO_MANY_STEPS_REPLY},
    ]


async def test_refusal_gets_fixed_text(world):
    agent, session, _, _ = world(reply(text("거절 사유 상세"), stop="refusal"))
    assert await agent.run_turn(session, "질문", []) == REFUSAL_REPLY
    assert session.messages[-1] == {"role": "assistant", "content": REFUSAL_REPLY}


async def test_max_tokens_uses_received_text_or_safe_reply(world):
    agent, session, _, _ = world(reply(text("**배송은** 보통 2일 걸려요. 그리고"), stop="max_tokens"))
    assert await agent.run_turn(session, "배송?", []) == "배송은 보통 2일 걸려요. 그리고"
    agent, session, _, _ = world(reply(ThinkingBlock(type="thinking", thinking="", signature="s"), stop="max_tokens"))
    assert await agent.run_turn(session, "배송?", []) == SAFE_REPLY


async def test_no_text_block_gives_safe_reply_and_warning(world, caplog):
    agent, session, _, _ = world(reply())
    with caplog.at_level(logging.WARNING, logger="app.agent"):
        assert await agent.run_turn(session, "질문", []) == SAFE_REPLY
    assert "no text" in caplog.text
    assert [m["role"] for m in session.messages] == ["user", "assistant"]


async def test_reply_is_cleaned_but_session_keeps_model_text(world):
    agent, session, _, _ = world(reply(text("## 안내\n- **환불**은 가능해요 😀")))
    assert await agent.run_turn(session, "환불?", []) == "안내\n환불은 가능해요"
    assert session.messages[-1]["content"][0]["text"] == "## 안내\n- **환불**은 가능해요 😀"


async def test_system_prompt_leak_is_replaced(world):
    agent, session, _, _ = world(reply(text("규칙: " + build_system_blocks()[0]["text"][:200])))
    assert await agent.run_turn(session, "규칙 알려줘", []) == SAFE_REPLY


async def test_api_key_leak_is_replaced(world):
    agent, session, _, _ = world(reply(text(f"키는 {CLAUDE_KEY} 입니다")))
    assert await agent.run_turn(session, "키?", []) == SAFE_REPLY


async def test_sdk_block_objects_are_replayed_and_thinking_not_stored(world):
    agent, session, model, _ = world(
        reply(
            ThinkingBlock(type="thinking", thinking="", signature="sig"),
            ToolUseBlock(type="tool_use", id="t1", name="api_01", input={"sku": "A-1"}),
            stop="tool_use",
        ),
        reply(ThinkingBlock(type="thinking", thinking="", signature="sig2"), TextBlock(type="text", text="있어요.")),
    )
    session.test_case_id = "L-03"
    assert await agent.run_turn(session, "재고?", []) == "있어요."
    replayed = model.calls[1]["messages"][1]["content"]
    assert [b["type"] for b in replayed] == ["thinking", "tool_use"]
    assert replayed[0]["signature"] == "sig"
    stored = [b["type"] for m in session.messages if m["role"] == "assistant" for b in m["content"]]
    assert stored == ["tool_use", "text"]


async def test_image_block_is_built_and_stored_as_text(world):
    ok = httpx.Response(200, content=PNG, headers={"content-type": "image/png"})
    agent, session, model, _ = world(reply(text("사진 확인했어요.")), images={IMAGE_URL: ok})
    await agent.run_turn(session, "이거 봐 주세요", [("image", IMAGE_URL)])
    content = model.calls[0]["messages"][0]["content"]
    assert content[0] == {"type": "text", "text": "이거 봐 주세요"}
    assert content[1]["type"] == "image" and content[1]["source"]["media_type"] == "image/png"
    assert content[1]["source"]["type"] == "base64"
    assert all(b["type"] != "image" for b in session.messages[0]["content"])


async def test_image_failure_is_skipped_and_marked(world):
    agent, session, model, _ = world(reply(text("사진은 못 봤어요.")))
    await agent.run_turn(session, "사진 보냈어요", [("image", IMAGE_URL), ("image", "http://insecure.example/a.png")])
    (message,) = model.calls[0]["messages"]
    assert all(b["type"] == "text" for b in message["content"])
    assert message["content"][0]["text"].count(IMAGE_FAILED_MARK) == 2
    assert message["content"][0]["text"].startswith("사진 보냈어요")


async def test_image_only_message_is_sent(world):
    ok = httpx.Response(200, content=PNG, headers={"content-type": "image/png"})
    agent, session, model, _ = world(reply(text("확인했어요.")), images={IMAGE_URL: ok})
    await agent.run_turn(session, "", [("image", IMAGE_URL)])
    assert [b["type"] for b in model.calls[0]["messages"][0]["content"]] == ["image"]


async def test_audio_only_gets_fixed_answer_without_model_call(world):
    agent, session, model, _ = world()
    assert await agent.run_turn(session, "  ", [("audio", "https://a.example/v.m4a")]) == AUDIO_ONLY_REPLY
    assert model.calls == [] and session.messages == []


async def test_audio_with_text_processes_text_only(world):
    agent, session, model, requests = world(reply(text("네, 알려드릴게요.")))
    await agent.run_turn(session, "배송 문의", [("audio", "https://a.example/v.m4a")])
    assert model.calls[0]["messages"][0]["content"] == [{"type": "text", "text": "배송 문의"}]
    assert requests == []


async def test_history_is_sent_with_the_new_message(world):
    agent, session, model, _ = world(reply(text("하나.")), reply(text("둘.")))
    await agent.run_turn(session, "첫째", [])
    await agent.run_turn(session, "둘째", [])
    roles = [m["role"] for m in model.calls[1]["messages"]]
    assert roles == ["user", "assistant", "user"]
    assert session.user_turns == 2


async def test_system_and_tools_are_identical_across_requests(world):
    agent, session, model, _ = world(reply(text("하나.")), reply(text("둘.")))
    await agent.run_turn(session, "첫째", [])
    await agent.run_turn(session, "둘째", [])
    first, second = model.calls
    assert json.dumps(first["system"], sort_keys=False) == json.dumps(second["system"], sort_keys=False)
    assert json.dumps(first["tools"], sort_keys=False) == json.dumps(second["tools"], sort_keys=False)
    assert first["system"][-1]["cache_control"] == {"type": "ephemeral"}


async def test_timeout_records_a_plain_pair_and_keeps_alternation(world):
    async def hang():
        await asyncio.sleep(30)

    agent, session, _, _ = world(hang)
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.05):
            await agent.run_turn(session, "느린 질문", [])
    assert session.messages == [
        {"role": "user", "content": "느린 질문"},
        {"role": "assistant", "content": TIMEOUT_REPLY},
    ]


async def test_timeout_during_tool_loop_discards_partial_turn(world):
    async def hang():
        await asyncio.sleep(30)

    agent, session, _, _ = world(reply(tool_use("t1", "api_01", sku="A-1"), stop="tool_use"), hang)
    session.test_case_id = "L-03"
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.05):
            await agent.run_turn(session, "느린 질문", [])
    assert [m["role"] for m in session.messages] == ["user", "assistant"]
    assert session.messages[0]["content"] == "느린 질문"


async def test_model_error_propagates_after_recording_a_pair(world):
    agent, session, _, _ = world(RuntimeError("upstream down"))
    with pytest.raises(RuntimeError):
        await agent.run_turn(session, "질문", [])
    assert [m["role"] for m in session.messages] == ["user", "assistant"]


async def test_secrets_never_enter_the_model_request(world):
    agent, session, model, _ = world(reply(text("안녕하세요.")))
    await agent.run_turn(session, "안녕", [])
    blob = json.dumps(model.calls, ensure_ascii=False)
    assert CLAUDE_KEY not in blob and ACTION_TOKEN not in blob
