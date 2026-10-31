from pathlib import Path

import pytest

from app.config import ConfigError, Settings

KEY = "test-claude-key-value"
TOKEN = "test-bot-token-value"


@pytest.fixture
def no_dotenv(tmp_path: Path) -> Path:
    return tmp_path / "missing.env"


def test_defaults_when_only_secrets_are_set(no_dotenv: Path) -> None:
    settings = Settings.from_env({"claude_api_key": KEY, "bot_auth_token": TOKEN}, no_dotenv)

    assert settings.claude_api_key == KEY
    assert settings.bot_auth_token == TOKEN
    assert settings.CLAUDE_MODEL == "claude-haiku-5-5"
    assert settings.ACTION_API_BASE_URL == "https://gyodong-mall-action.aitop100.org"
    assert settings.TURN_BUDGET_SECONDS == 4.2
    assert settings.SESSION_TTL_SECONDS == 3600
    assert settings.MAX_SESSION_TURNS == 40
    settings.validate(webhook=False)
    with pytest.raises(ConfigError, match="kakao_bot_token"):
        settings.validate()


def test_overrides_are_parsed(no_dotenv: Path) -> None:
    environ = {
        "CLAUDE_MODEL": "claude-sonnet-5-5",
        "ACTION_API_BASE_URL": "https://example.test",
        "TURN_BUDGET_SECONDS": "3.5",
        "SESSION_TTL_SECONDS": "120",
        "MAX_SESSION_TURNS": "12",
    }
    settings = Settings.from_env(environ, no_dotenv)

    assert settings.CLAUDE_MODEL == "claude-sonnet-5-5"
    assert settings.ACTION_API_BASE_URL == "https://example.test"
    assert settings.TURN_BUDGET_SECONDS == 3.5
    assert settings.SESSION_TTL_SECONDS == 120
    assert settings.MAX_SESSION_TURNS == 12


@pytest.mark.parametrize(
    ("environ", "expected"),
    [
        ({"claude_api_key": "a", "CLAUDE_API_KEY": "b", "ANTHROPIC_API_KEY": "c"}, "a"),
        ({"CLAUDE_API_KEY": "b", "ANTHROPIC_API_KEY": "c"}, "b"),
        ({"ANTHROPIC_API_KEY": "c"}, "c"),
        ({"claude_api_key": "", "ANTHROPIC_API_KEY": "c"}, "c"),
    ],
)
def test_claude_key_alias_precedence(environ: dict[str, str], expected: str, no_dotenv: Path) -> None:
    assert Settings.from_env(environ, no_dotenv).claude_api_key == expected


@pytest.mark.parametrize(
    ("environ", "expected"),
    [
        ({"bot_auth_token": "a", "BOT_AUTH_TOKEN": "b"}, "a"),
        ({"BOT_AUTH_TOKEN": "b"}, "b"),
    ],
)
def test_bot_token_alias_precedence(environ: dict[str, str], expected: str, no_dotenv: Path) -> None:
    assert Settings.from_env(environ, no_dotenv).bot_auth_token == expected


def test_validate_names_missing_variables_without_values(no_dotenv: Path) -> None:
    with pytest.raises(ConfigError) as excinfo:
        Settings.from_env({}, no_dotenv).validate()

    message = str(excinfo.value)
    assert "claude_api_key" in message
    assert "ANTHROPIC_API_KEY" in message
    assert "bot_auth_token" in message


def test_validate_does_not_leak_present_secret(no_dotenv: Path) -> None:
    with pytest.raises(ConfigError) as excinfo:
        Settings.from_env({"claude_api_key": KEY}, no_dotenv).validate()

    message = str(excinfo.value)
    assert "bot_auth_token" in message
    assert "claude_api_key" not in message
    assert KEY not in message


@pytest.mark.parametrize("raw", ["fast", "0", "-1", "nan", "inf"])
def test_invalid_number_raises_config_error(raw: str, no_dotenv: Path) -> None:
    with pytest.raises(ConfigError, match="TURN_BUDGET_SECONDS"):
        Settings.from_env({"TURN_BUDGET_SECONDS": raw}, no_dotenv)


def test_non_integer_turn_limit_raises_config_error(no_dotenv: Path) -> None:
    with pytest.raises(ConfigError, match="MAX_SESSION_TURNS"):
        Settings.from_env({"MAX_SESSION_TURNS": "4.5"}, no_dotenv)


def test_repr_and_str_mask_secrets(no_dotenv: Path) -> None:
    settings = Settings.from_env({"claude_api_key": KEY, "bot_auth_token": TOKEN}, no_dotenv)

    for rendered in (repr(settings), str(settings), f"{settings}"):
        assert KEY not in rendered
        assert TOKEN not in rendered
        assert "claude_api_key='***'" in rendered
        assert "bot_auth_token='***'" in rendered
        assert "CLAUDE_MODEL='claude-haiku-5-5'" in rendered


def test_repr_shows_unset_secrets_as_none(no_dotenv: Path) -> None:
    assert "claude_api_key=None" in repr(Settings.from_env({}, no_dotenv))


def test_dotenv_fills_gaps_but_never_overrides_environment(tmp_path: Path) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        "claude_api_key=from-file\nbot_auth_token=file-token\nCLAUDE_MODEL=file-model\n",
        encoding="utf-8",
    )
    environ = {"claude_api_key": "from-env", "BOT_AUTH_TOKEN": "env-token"}

    settings = Settings.from_env(environ, dotenv)

    assert settings.claude_api_key == "from-env"
    # An environment alias still beats the canonical name in .env.
    assert settings.bot_auth_token == "env-token"
    assert settings.CLAUDE_MODEL == "file-model"
    assert environ == {"claude_api_key": "from-env", "BOT_AUTH_TOKEN": "env-token"}


def test_missing_dotenv_is_not_an_error(no_dotenv: Path) -> None:
    assert not no_dotenv.exists()
    assert Settings.from_env({}, no_dotenv).claude_api_key is None
