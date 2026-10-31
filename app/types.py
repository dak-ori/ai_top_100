"""Contract between the skill server and the turn handler it serves."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from app.kakao import SkillRequest


@dataclass(frozen=True)
class HandlerResult:
    text: str
    # When set, the server answers with a callback-wait response and runs this in the background.
    callback: Callable[[], Awaitable[None]] | None = None


SkillHandler = Callable[[SkillRequest], Awaitable[HandlerResult]]
