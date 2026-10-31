import asyncio

import pytest

from app.config import Settings
from app.session import SessionStore, extract_test_case_id


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("id: L-03", "L-03"),
        ("케이스 R-05-a 입니다", "R-05-a"),
        ("Y-07-b", "Y-07-b"),
        ("(Y-16)", "Y-16"),
        ("L-03 그리고 R-05-a", "L-03"),
        ("전화번호 010-1234-5678", None),
        ("주문번호 20240101-001", None),
        ("ESC-20370630-001", None),
        ("AB-12 / L-123 / xL-03", None),
        ("그냥 질문이에요", None),
    ],
)
def test_extract_test_case_id(text, expected):
    assert extract_test_case_id(text) == expected


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def make_store(clock=None, **overrides):
    settings = Settings(**overrides)
    return SessionStore(settings, clock or Clock())


def turn(n, with_tools=False):
    user = {"role": "user", "content": [{"type": "text", "text": f"q{n}"}]}
    final = {"role": "assistant", "content": [{"type": "text", "text": f"a{n}"}]}
    if not with_tools:
        return [user, final]
    use = {"role": "assistant", "content": [{"type": "tool_use", "id": f"t{n}", "name": "api_01", "input": {}}]}
    result = {"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"t{n}", "content": "{}"}]}
    return [user, use, result, final]


def test_same_user_keeps_session_until_ttl():
    clock = Clock()
    store = make_store(clock, SESSION_TTL_SECONDS=100.0)
    first = store.get("u")
    first.commit_turn(turn(1))
    clock.now += 99
    assert store.get("u") is first
    clock.now += 99  # TTL counts from the last activity
    assert store.get("u") is first
    clock.now += 101
    fresh = store.get("u")
    assert fresh is not first and fresh.messages == []


def test_turn_cap_drops_whole_oldest_turn_with_tool_pairs():
    session = make_store(MAX_SESSION_TURNS=2).get("u")
    session.commit_turn(turn(1, with_tools=True))
    session.commit_turn(turn(2, with_tools=True))
    session.commit_turn(turn(3))
    assert session.user_turns == 2
    assert [m["content"][0].get("text") for m in session.messages if m["role"] == "user"][0] == "q2"
    uses = {b["id"] for m in session.messages for b in m["content"] if b["type"] == "tool_use"}
    results = {b["tool_use_id"] for m in session.messages for b in m["content"] if b["type"] == "tool_result"}
    assert uses == results == {"t2"}
    assert session.messages[0]["role"] == "user"


def test_different_test_case_id_resets_history():
    session = make_store().get("u")
    session.observe("L-03 부탁해요")
    session.commit_turn(turn(1))
    session.observe("계속 L-03 이야기")
    session.observe("아이디 없음")
    assert session.test_case_id == "L-03" and len(session.messages) == 2
    session.observe("이제 R-05-a")
    assert session.test_case_id == "R-05-a"
    assert session.messages == [] and session.user_turns == 0


def test_first_id_keeps_earlier_history():
    session = make_store().get("u")
    session.commit_turn(turn(1))
    session.observe("L-03")
    assert session.test_case_id == "L-03" and len(session.messages) == 2


def test_expired_sessions_and_idle_locks_are_released():
    clock = Clock()
    store = make_store(clock, SESSION_TTL_SECONDS=10.0)
    store.get("a")
    store.lock("a")
    clock.now += 11
    store.get("b")
    assert "a" not in store._sessions and "a" not in store._locks


async def test_lock_serializes_same_user_only():
    store = make_store()
    order = []

    async def work(user, name, hold):
        async with store.lock(user):
            order.append(f"{name}+")
            await asyncio.sleep(hold)
            order.append(f"{name}-")

    await asyncio.gather(work("u", "a", 0.02), work("u", "b", 0), work("v", "c", 0))
    assert order.index("a-") < order.index("b+")
    assert order.index("c+") < order.index("a-")
