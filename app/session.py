"""Per-user conversation state: test case id, message history, TTL and turn cap."""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from app.config import Settings

_TEST_CASE_ID = re.compile(r"(?<![A-Za-z0-9])[A-Z]-\d{2}(?:-[a-z])?(?![A-Za-z0-9])")


def extract_test_case_id(text: str) -> str | None:
    match = _TEST_CASE_ID.search(text)
    return match.group() if match else None


def _starts_turn(message: dict) -> bool:
    """A turn begins at a user message that carries the customer's words, not tool results."""
    if message["role"] != "user":
        return False
    content = message["content"]
    if isinstance(content, str):
        return True
    return not any(block.get("type") == "tool_result" for block in content)


@dataclass
class Session:
    max_turns: int
    last_active: float
    test_case_id: str | None = None
    messages: list[dict] = field(default_factory=list)
    user_turns: int = 0

    def observe(self, utterance: str) -> None:
        """Apply a test case id found in the utterance. A different id starts a new case with no
        history; the same id, or none, keeps the session as it is."""
        found = extract_test_case_id(utterance)
        if found is None or found == self.test_case_id:
            return
        if self.test_case_id is not None:
            self.messages.clear()
            self.user_turns = 0
        self.test_case_id = found

    def commit_turn(self, turn: list[dict]) -> None:
        """Append one finished turn (user message through final assistant text), then drop the
        oldest whole turns beyond the cap so tool_use/tool_result pairs are never split."""
        self.messages.extend(turn)
        self.user_turns += 1
        while self.user_turns > self.max_turns:
            next_start = next(i for i, m in enumerate(self.messages) if i > 0 and _starts_turn(m))
            del self.messages[:next_start]
            self.user_turns -= 1


class SessionStore:
    def __init__(self, settings: Settings, clock: Callable[[], float] = time.monotonic) -> None:
        self._ttl = settings.SESSION_TTL_SECONDS
        self._max_turns = settings.MAX_SESSION_TURNS
        self._clock = clock
        self._sessions: dict[str, Session] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def get(self, user_id: str) -> Session:
        """The user's live session; a new one when none exists or the last one expired."""
        now = self._clock()
        self._sweep(now)
        session = self._sessions.get(user_id)
        if session is None:
            session = self._sessions[user_id] = Session(self._max_turns, now)
        session.last_active = now
        return session

    def lock(self, user_id: str) -> asyncio.Lock:
        """Serializes one user's requests."""
        return self._locks.setdefault(user_id, asyncio.Lock())

    def _sweep(self, now: float) -> None:
        # Expired sessions go, and so do idle locks, so neither grows with the number of past users.
        # A held lock stays: its holder is mid-request and will refresh the session.
        for user_id in [u for u, s in self._sessions.items() if now - s.last_active > self._ttl]:
            del self._sessions[user_id]
        for user_id in [u for u, lock in self._locks.items() if u not in self._sessions and not lock.locked()]:
            del self._locks[user_id]
