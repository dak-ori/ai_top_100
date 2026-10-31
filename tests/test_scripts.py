import io
import json
from types import SimpleNamespace

import httpx
import pytest

from app.actions import CallRecord
from app.config import Settings
from scripts import run_open_tc as tc
from scripts import smoke_skill

ACTION_TOKEN = "fake-action-token-5678"
EXPECTED_IDS = [
    "L-03", "L-05", "L-11", "L-20", "L-23", "L-32", "L-33", "L-34", "L-40", "L-47",
    "L-60", "R-05-a", "Y-07-b", "Y-16", "Y-24",
]  # fmt: skip
RESET_PATH = "/api/action-data/reset"


def cases():
    return tc.parse_cases(tc.DEFAULT_CASES_FILE.read_text(encoding="utf-8"))


def by_id():
    return {case.id: case for case in cases()}


def case(expected=(11, 24)):
    return tc.Case("T-01", "회원 로그인 · 홍길동 · 019-****-0000", "문의", (), tuple(expected))


def record(tool, status=200):
    return CallRecord(tool, {}, status, status is not None and status < 400)


# --- parser -------------------------------------------------------------------------------


def test_parser_reads_every_case_in_the_file():
    assert [c.id for c in cases()] == EXPECTED_IDS
    for c in cases():
        assert c.inquiry and c.account.startswith("회원 로그인")


def test_parser_extracts_the_expected_api_numbers_without_duplicates():
    found = by_id()
    assert found["L-03"].expected_apis == (11, 24)
    assert found["L-11"].expected_apis == (11, 3, 4, 6, 29, 35)
    assert all(len(set(c.expected_apis)) == len(c.expected_apis) for c in found.values())
    # These two cases are defences that need no API call.
    assert found["L-20"].expected_apis == found["Y-24"].expected_apis == ()


def test_parser_reads_account_inquiry_and_media():
    found = by_id()
    assert found["L-03"].account == "회원 로그인 · 서지호 · 019-****-5231"
    assert found["L-03"].inquiry == "주소 잘못 넣었어요. 경기 성남시로 바꿔주세요."
    assert found["L-11"].media_urls[0].endswith(".jpg")
    assert found["Y-07-b"].media_urls[0].endswith(".m4a")
    assert found["L-03"].media_urls == ()


def test_first_utterance_carries_the_id_account_media_and_question():
    utterance = tc.first_utterance(by_id()["L-11"])
    lines = utterance.splitlines()
    assert lines[0] == "id: L-11"
    assert lines[1].startswith("인입 계정: 회원 로그인")
    assert lines[2].startswith("https://") and lines[2].endswith(".jpg")
    assert "곰팡이" in utterance


def test_parser_rejects_a_case_whose_metadata_id_differs():
    text = "# A-01\n\ncase metadata\n- id: B-02\n- 인입 계정: x\n"
    with pytest.raises(ValueError, match="A-01"):
        tc.parse_cases(text)


def test_parser_rejects_a_case_without_its_scenario_sections():
    text = "# A-01\n\ncase metadata\n- id: A-01\n- 인입 계정: x\n"
    with pytest.raises(ValueError, match="case has no .* section"):
        tc.parse_cases(text)


# --- dry run ------------------------------------------------------------------------------


def test_dry_run_lists_every_case_as_json():
    out = io.StringIO()
    assert tc.main(["--dry-run"], out) == 0
    listing = json.loads(out.getvalue())
    assert [item["id"] for item in listing] == EXPECTED_IDS
    assert all(item["inquiry"] for item in listing)
    assert next(i for i in listing if i["id"] == "L-03")["apis"] == ["API-11", "API-24"]


def test_dry_run_only_filters_and_unknown_id_fails(capsys):
    out = io.StringIO()
    assert tc.main(["--dry-run", "--only", "L-05"], out) == 0
    assert [item["id"] for item in json.loads(out.getvalue())] == ["L-05"]
    assert tc.main(["--dry-run", "--only", "Z-99"], io.StringIO()) == 2
    assert "Z-99" in capsys.readouterr().err


def test_dry_run_fails_on_an_unreadable_file(tmp_path, capsys):
    assert tc.main(["--dry-run", "--file", str(tmp_path / "missing.md")], io.StringIO()) == 2
    assert "cannot read cases" in capsys.readouterr().err


def test_reset_first_needs_live():
    with pytest.raises(SystemExit) as error:
        tc.main(["--dry-run", "--reset-first"], io.StringIO())
    assert error.value.code == 2


def test_help_warns_about_cost_and_data_changes(capsys):
    with pytest.raises(SystemExit):
        tc.main(["--help"])
    text = capsys.readouterr().out
    assert "--reset-first" in text and "billed" in text and "RECORDS" in text


# --- judging ------------------------------------------------------------------------------


def test_judge_passes_when_all_expected_calls_were_made_and_the_answer_fits():
    result = tc.judge(case((11,)), [record("api_11"), record("api_11")], "짧은 답", 1.2)
    assert result.passed and result.called == (11,) and result.missing == ()


def test_judge_fails_on_a_missing_expected_call():
    result = tc.judge(case((11, 24)), [record("api_11")], "어느 주소로 바꿀까요?", 1.0)
    assert result.missing == (24,) and not result.calls_ok and not result.passed


def test_judge_fails_on_a_write_without_confirmation():
    result = tc.judge(case((11, 24)), [record("api_11"), record("api_24")], "접수했어요", 1.0)
    assert result.unconfirmed_writes == (24,) and result.calls_ok and not result.passed


def test_judge_ignores_escalations_lookups_and_unsent_writes():
    calls = [record("api_35"), record("api_30"), record("api_36"), record("api_29", status=None)]
    result = tc.judge(case(()), calls, "이관했어요", 1.0)
    assert result.unconfirmed_writes == () and result.passed


def test_judge_fails_on_an_answer_over_500_chars():
    assert tc.judge(case(()), [], "가" * 500, 1.0).length_ok
    result = tc.judge(case(()), [], "가" * 501, 1.0)
    assert not result.length_ok and not result.passed


def test_report_lists_each_check_and_a_summary():
    ok = tc.judge(case((11,)), [record("api_11")], "답", 0.5)
    bad = tc.judge(case((11, 24)), [record("api_24")], "접수", 0.5)
    report = tc.format_report([ok, bad])
    assert "[PASS] T-01" in report and "[FAIL] T-01" in report
    assert "missing: API-11" in report and "called: API-24" in report
    assert report.splitlines()[-1] == "SUMMARY: 1/2 PASS"


# --- live replay with fakes ---------------------------------------------------------------


class ScriptedClaude:
    """Answers each model call with the next scripted response."""

    def __init__(self, *responses):
        self._responses = list(responses)
        self.messages = SimpleNamespace(create=self.create)

    async def create(self, **kwargs):
        return self._responses.pop(0)


def tool_use(name, tool_input):
    block = {"type": "tool_use", "id": "toolu_1", "name": name, "input": tool_input}
    return SimpleNamespace(content=[block], stop_reason="tool_use")


def say(text):
    return SimpleNamespace(content=[{"type": "text", "text": text}], stop_reason="end_turn")


def live_settings():
    return Settings(claude_api_key="fake-claude-key-1234", bot_auth_token=ACTION_TOKEN)


def run_live(selected, claude, *, reset_first=False):
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(204) if request.url.path == RESET_PATH else httpx.Response(200, json={"order_no": "1"})

    import asyncio

    results = asyncio.run(
        tc.run_live(selected, live_settings(), reset_first=reset_first, claude=claude, transport=httpx.MockTransport(handle))
    )
    return results, requests


def test_live_replay_records_calls_headers_and_verdict():
    claude = ScriptedClaude(tool_use("api_11", {"order_no": "100100033"}), say("어느 주소로 바꿔 드릴까요?"))
    results, requests = run_live([by_id()["L-03"]], claude)

    [result] = results
    assert result.called == (11,) and result.missing == (24,) and not result.passed
    assert result.answer == "어느 주소로 바꿔 드릴까요?"
    [action] = requests
    assert action.headers["X-Aitop100-Test-Case-Id"] == "L-03"
    assert action.headers["Authorization"] == f"Bearer {ACTION_TOKEN}"


def test_live_replay_gives_each_case_its_own_user_and_call_record():
    claude = ScriptedClaude(tool_use("api_20", {"code": "SUBAK15"}), say("있는 코드예요."), say("안내하지 않아요."))
    results, requests = run_live([by_id()["L-60"], by_id()["L-20"]], claude)

    assert [r.called for r in results] == [(20,), ()]
    assert results[0].passed and results[1].passed
    assert [r.headers["X-Aitop100-Test-Case-Id"] for r in requests] == ["L-60"]


def test_reset_is_called_once_only_when_asked():
    selected = [by_id()["L-20"], by_id()["Y-24"]]

    _, requests = run_live(selected, ScriptedClaude(say("a"), say("b")))
    assert not [r for r in requests if r.url.path == RESET_PATH]

    _, requests = run_live(selected, ScriptedClaude(say("a"), say("b")), reset_first=True)
    resets = [r for r in requests if r.url.path == RESET_PATH]
    assert len(resets) == 1 and resets[0].method == "POST" and requests[0] is resets[0]
    assert resets[0].headers["Authorization"] == f"Bearer {ACTION_TOKEN}"
    assert "X-Aitop100-Test-Case-Id" not in resets[0].headers


def test_a_failed_reset_stops_before_any_replay():
    import asyncio

    def refuse(request):
        return httpx.Response(409, json={"code": "NOT_ALLOWED"})

    claude = ScriptedClaude()
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(
            tc.run_live(
                [by_id()["L-20"]], live_settings(), reset_first=True, claude=claude, transport=httpx.MockTransport(refuse)
            )
        )


def test_live_without_keys_fails_naming_the_variables(monkeypatch, capsys):
    monkeypatch.setattr(tc.Settings, "from_env", classmethod(lambda cls: cls()))
    assert tc.main(["--live", "--only", "L-20"], io.StringIO()) == 2
    err = capsys.readouterr().err
    assert "claude_api_key" in err and "bot_auth_token" in err


def test_live_exit_code_follows_the_verdicts(monkeypatch):
    async def fake_run_live(selected, settings, *, reset_first):
        assert reset_first is True
        return [tc.judge(c, [], "답", 0.1) for c in selected]

    monkeypatch.setattr(tc.Settings, "from_env", classmethod(lambda cls: live_settings()))
    monkeypatch.setattr(tc, "run_live", fake_run_live)
    out = io.StringIO()
    assert tc.main(["--live", "--only", "L-20", "Y-24", "--reset-first"], out) == 0
    assert out.getvalue().splitlines()[-1] == "SUMMARY: 2/2 PASS"
    assert tc.main(["--live", "--only", "L-03", "--reset-first"], io.StringIO()) == 1


def test_only_the_replay_script_knows_the_reset_endpoint():
    from pathlib import Path

    app_dir = Path(tc.ROOT) / "app"
    offenders = [
        p.name
        for p in app_dir.rglob("*")
        if p.is_file() and p.suffix in {".py", ".md", ".yaml"} and "action-data/reset" in p.read_text(encoding="utf-8")
    ]
    assert offenders == []


# --- smoke script -------------------------------------------------------------------------

GOOD_BODY = {"version": "2.0", "template": {"outputs": [{"simpleText": {"text": "배송 문의 도와드릴게요."}}]}}


def smoke(skill_response, health_status=200, argv_url="https://svc.example.com/"):
    seen = []

    def handle(request):
        seen.append(request)
        if request.url.path == "/health":
            return httpx.Response(health_status, json={"status": "ok"})
        return skill_response(request)

    code = smoke_skill.main([argv_url], transport=httpx.MockTransport(handle))
    return code, seen


def test_smoke_passes_on_a_valid_skill_response(capsys):
    code, seen = smoke(lambda request: httpx.Response(200, json=GOOD_BODY))
    out = capsys.readouterr().out
    assert code == 0 and "[PASS] GET /health" in out and "[PASS] POST /skill" in out
    assert [r.url.path for r in seen] == ["/health", "/skill"]
    payload = json.loads(seen[1].content)
    assert payload["userRequest"]["utterance"] == "안녕하세요 배송 문의드려요"
    assert payload["userRequest"]["user"]["id"]


@pytest.mark.parametrize(
    "body",
    [
        {**GOOD_BODY, "version": "1.0"},
        {"version": "2.0", "template": {"outputs": GOOD_BODY["template"]["outputs"] * 2}},
        {"version": "2.0", "template": {"outputs": [{"basicCard": {}}]}},
        {"version": "2.0", "template": {"outputs": [{"simpleText": {"text": "가" * 501}}]}},
        {"version": "2.0", "useCallback": True, "data": {"text": "기다려 주세요"}},
        ["not", "an", "object"],
    ],
)
def test_smoke_rejects_a_malformed_skill_response(body, capsys):
    code, _ = smoke(lambda request: httpx.Response(200, json=body))
    assert code == 1 and "[FAIL] POST /skill" in capsys.readouterr().out


def test_smoke_accepts_exactly_500_chars():
    body = {"version": "2.0", "template": {"outputs": [{"simpleText": {"text": "가" * 500}}]}}
    assert smoke(lambda request: httpx.Response(200, json=body))[0] == 0


def test_smoke_fails_on_server_errors_and_non_json(capsys):
    assert smoke(lambda request: httpx.Response(500, text="boom"))[0] == 1
    assert smoke(lambda request: httpx.Response(200, text="<html>"))[0] == 1
    assert smoke(lambda request: httpx.Response(200, json=GOOD_BODY), health_status=503)[0] == 1
    assert "status=503" in capsys.readouterr().out


def test_smoke_reports_an_unreachable_server(capsys):
    def unreachable(request):
        raise httpx.ConnectError("refused")

    code = smoke_skill.main(["https://svc.example.com"], transport=httpx.MockTransport(unreachable))
    assert code == 1 and "[FAIL] GET /health: ConnectError" in capsys.readouterr().out


def test_smoke_fails_when_slower_than_the_kakao_limit(monkeypatch, capsys):
    monkeypatch.setattr(smoke_skill, "KAKAO_LIMIT_SECONDS", 0.0)
    code, _ = smoke(lambda request: httpx.Response(200, json=GOOD_BODY))
    assert code == 1 and "over Kakao" in capsys.readouterr().out
