"""Environment-backed settings for the Kakao skill server."""

from __future__ import annotations

import math
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, fields
from pathlib import Path

from dotenv import dotenv_values

DEFAULT_DOTENV_PATH = Path(__file__).resolve().parent.parent / ".env"

# Lookup order within one source: the canonical lowercase name first, then its aliases.
CLAUDE_API_KEY_NAMES = ("claude_api_key", "CLAUDE_API_KEY", "ANTHROPIC_API_KEY")
BOT_AUTH_TOKEN_NAMES = ("bot_auth_token", "BOT_AUTH_TOKEN")

_SECRET_FIELDS = frozenset({"claude_api_key", "bot_auth_token"})
_MASK = "***"


class ConfigError(Exception):
    """Configuration is missing or malformed. Messages name variables, never secret values."""


@dataclass(frozen=True, repr=False)
class Settings:
    # Field names mirror the environment variable names the rest of the service refers to.
    claude_api_key: str | None = None
    bot_auth_token: str | None = None
    CLAUDE_MODEL: str = "claude-haiku-5-5"
    ACTION_API_BASE_URL: str = "https://gyodong-mall-action.aitop100.org"
    TURN_BUDGET_SECONDS: float = 4.2
    SESSION_TTL_SECONDS: float = 3600.0
    MAX_SESSION_TURNS: int = 40

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str] | None = None,
        dotenv_path: str | os.PathLike[str] = DEFAULT_DOTENV_PATH,
    ) -> Settings:
        """Read settings from the environment, then from a local ``.env`` file.

        A value set in the environment (under any alias) always wins over ``.env``.
        A missing ``.env`` file is not an error, and ``os.environ`` is never modified.
        """
        sources: tuple[Mapping[str, str | None], ...] = (
            os.environ if environ is None else environ,
            dotenv_values(dotenv_path),
        )

        def lookup(*names: str) -> str | None:
            for source in sources:
                for name in names:
                    value = source.get(name)
                    if value:
                        return value
            return None

        defaults = cls()
        return cls(
            claude_api_key=lookup(*CLAUDE_API_KEY_NAMES),
            bot_auth_token=lookup(*BOT_AUTH_TOKEN_NAMES),
            CLAUDE_MODEL=lookup("CLAUDE_MODEL") or defaults.CLAUDE_MODEL,
            ACTION_API_BASE_URL=lookup("ACTION_API_BASE_URL") or defaults.ACTION_API_BASE_URL,
            TURN_BUDGET_SECONDS=_positive(
                "TURN_BUDGET_SECONDS", lookup("TURN_BUDGET_SECONDS"), float, defaults.TURN_BUDGET_SECONDS
            ),
            SESSION_TTL_SECONDS=_positive(
                "SESSION_TTL_SECONDS", lookup("SESSION_TTL_SECONDS"), float, defaults.SESSION_TTL_SECONDS
            ),
            MAX_SESSION_TURNS=_positive(
                "MAX_SESSION_TURNS", lookup("MAX_SESSION_TURNS"), int, defaults.MAX_SESSION_TURNS
            ),
        )

    def validate(self) -> None:
        """Raise ``ConfigError`` naming every required variable that is unset."""
        missing = [
            _describe(names)
            for names, value in (
                (CLAUDE_API_KEY_NAMES, self.claude_api_key),
                (BOT_AUTH_TOKEN_NAMES, self.bot_auth_token),
            )
            if not value
        ]
        if missing:
            raise ConfigError("Missing required environment variable(s): " + "; ".join(missing))

    def __repr__(self) -> str:
        parts = []
        for field in fields(self):
            value = getattr(self, field.name)
            if field.name in _SECRET_FIELDS and value is not None:
                value = _MASK
            parts.append(f"{field.name}={value!r}")
        return f"{type(self).__name__}({', '.join(parts)})"


def _describe(names: tuple[str, ...]) -> str:
    canonical, *aliases = names
    return f"{canonical} (or {', '.join(aliases)})"


def _positive[N: (int, float)](name: str, raw: str | None, parse: Callable[[str], N], default: N) -> N:
    if raw is None:
        return default
    try:
        value = parse(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a positive number, got {raw!r}") from exc
    if not (math.isfinite(value) and value > 0):
        raise ConfigError(f"{name} must be a positive number, got {raw!r}")
    return value
