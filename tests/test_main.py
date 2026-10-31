import os
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from fastapi.testclient import TestClient

import app.main as main_module
from app.config import ConfigError, Settings
from app.main import build_app

ROOT = Path(__file__).resolve().parent.parent
CLAUDE_KEY = "fake-claude-key-1234"
ACTION_TOKEN = "fake-action-token-5678"
BOT_TOKEN = "fake-kakao-bot-token-9012"
KEY_VARIABLES = ("claude_api_key", "CLAUDE_API_KEY", "ANTHROPIC_API_KEY", "bot_auth_token", "BOT_AUTH_TOKEN", "kakao_bot_token", "KAKAO_BOT_TOKEN")
REPLY = "배송 문의 도와드릴게요. 주문번호를 알려 주세요."


class FakeClaude:
    def __init__(self):
        self.requests = []
        self.closed = False
        self.messages = SimpleNamespace(create=self.create)

    async def create(self, **kwargs):
        self.requests.append(kwargs)
        return SimpleNamespace(content=[{"type": "text", "text": REPLY}], stop_reason="end_turn")

    async def close(self):
        self.closed = True


def settings(**overrides):
    return Settings(claude_api_key=CLAUDE_KEY, bot_auth_token=ACTION_TOKEN, kakao_bot_token=BOT_TOKEN, **overrides)


def skill_body(user_id="u1", utterance="안녕하세요 배송 문의드려요"):
    return {"userRequest": {"utterance": utterance, "user": {"id": user_id}}}


def test_import_succeeds_without_keys():
    env = {k: v for k, v in os.environ.items() if k not in KEY_VARIABLES}
    result = subprocess.run(
        [sys.executable, "-c", "from app.main import build_app"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr


def test_build_app_without_keys_names_the_variables():
    with pytest.raises(ConfigError) as error:
        build_app(Settings(), claude=FakeClaude())
    message = str(error.value)
    assert "claude_api_key" in message and "bot_auth_token" in message


def test_build_app_with_one_missing_key_does_not_echo_the_other():
    with pytest.raises(ConfigError) as error:
        build_app(Settings(claude_api_key=CLAUDE_KEY), claude=FakeClaude())
    message = str(error.value)
    assert "bot_auth_token" in message and "claude_api_key" not in message
    assert CLAUDE_KEY not in message


def test_build_app_reads_keys_from_the_environment(monkeypatch):
    for name in KEY_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("claude_api_key", CLAUDE_KEY)
    monkeypatch.setenv("bot_auth_token", ACTION_TOKEN)
    monkeypatch.setenv("kakao_bot_token", BOT_TOKEN)
    with TestClient(build_app(claude=FakeClaude())) as client:
        assert client.get("/health").status_code == 200


def test_health_and_one_simple_text_per_skill_request():
    claude = FakeClaude()
    with TestClient(build_app(settings(), claude=claude)) as client:
        assert client.get("/health").json() == {"status": "ok"}
        response = client.post("/skill", json=skill_body())
    assert response.status_code == 200
    body = response.json()
    assert body["version"] == "2.0"
    assert body["template"]["outputs"] == [{"simpleText": {"text": REPLY}}]
    assert claude.requests[0]["model"] == "claude-haiku-5-5"


def test_system_blocks_and_tools_are_built_once_for_all_requests():
    claude = FakeClaude()
    with TestClient(build_app(settings(), claude=claude)) as client:
        client.post("/skill", json=skill_body("u1"))
        client.post("/skill", json=skill_body("u2"))
    first, second = claude.requests
    assert first["system"] is second["system"]
    assert first["tools"] == second["tools"]


def test_the_agent_gets_the_forty_customer_tools_and_no_reset():
    claude = FakeClaude()
    with TestClient(build_app(settings(), claude=claude)) as client:
        client.post("/skill", json=skill_body())
    assert len(claude.requests[0]["tools"]) == 40


def test_clients_live_as_long_as_the_app(monkeypatch):
    created = []

    class RecordingAnthropic(FakeClaude):
        def __init__(self, api_key):
            super().__init__()
            self.api_key = api_key
            created.append(self)

    monkeypatch.setattr(main_module, "AsyncAnthropic", RecordingAnthropic)
    app = build_app(settings())
    http = app.state.http_client
    with TestClient(app) as client:
        client.post("/skill", json=skill_body("u1"))
        client.post("/skill", json=skill_body("u2"))
        assert not http.is_closed and not created[0].closed
    assert len(created) == 1 and created[0].api_key == CLAUDE_KEY
    assert http.is_closed and created[0].closed


def test_an_injected_claude_client_is_left_open():
    claude = FakeClaude()
    with TestClient(build_app(settings(), claude=claude)):
        pass
    assert not claude.closed


def test_render_blueprint_runs_one_always_on_worker():
    text = (ROOT / "render.yaml").read_text(encoding="utf-8")
    service = yaml.safe_load(text)["services"][0]
    env = {entry["key"]: entry for entry in service["envVars"]}

    assert service["runtime"] == "python"
    assert service["plan"] != "free"
    assert service["autoDeploy"] is True
    assert service["healthCheckPath"] == "/health"
    assert service["buildCommand"] == "pip install -r requirements.txt"
    assert "--factory app.main:build_app" in service["startCommand"]
    assert "--host 0.0.0.0 --port $PORT" in service["startCommand"]
    assert "--workers 1" in service["startCommand"]
    assert re.fullmatch(r"3\.12\.\d+", env["PYTHON_VERSION"]["value"])
    assert env["claude_api_key"]["sync"] is False and "value" not in env["claude_api_key"]
    assert env["bot_auth_token"]["sync"] is False and "value" not in env["bot_auth_token"]
    assert env["CLAUDE_MODEL"]["value"] == "claude-haiku-5-5"
    assert env["TURN_BUDGET_SECONDS"]["value"] == "4.2"
    assert not re.search(r"aitop100_[A-Za-z0-9]{6,}|sk-ant-", text)


def test_requirements_are_pinned_exactly():
    lines = [
        line
        for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]
    assert lines and all(re.fullmatch(r"[A-Za-z0-9_.-]+==\d+(\.\d+)*", line) for line in lines)
    assert any(line.startswith("PyYAML==") for line in lines)
