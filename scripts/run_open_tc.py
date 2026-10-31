"""Replay the public test cases against the assembled agent.

--dry-run  parse knowledge/open_cases.md and print each case as JSON. No network, no keys.
--live     send each case's first message through the real agent and check the result.

WARNING: --live calls the Anthropic API (about one cent per turn, billed to the key owner) and
the real action API, which RECORDS the write requests it makes (refunds, address changes,
escalations ...) under each case's test case id. Replaying a case after it changed data can
produce different answers. Add --reset-first to reset your action data once before the replay;
that wipes the data of ALL test cases for your token. Without --reset-first nothing is reset.

A live replay sends only the first message, so a case that needs the customer's confirmation
before a write is expected to stop at the question. Read the per-check lines, not just PASS/FAIL.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

import httpx
from anthropic import AsyncAnthropic

# `python scripts/run_open_tc.py` puts scripts/ on sys.path, not the repository root.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.actions import CallRecord  # noqa: E402
from app.config import ConfigError, Settings  # noqa: E402
from app.kakao import MAX_SIMPLE_TEXT_LENGTH, parse_skill_request  # noqa: E402
from app.main import assemble  # noqa: E402

DEFAULT_CASES_FILE = ROOT / "knowledge" / "open_cases.md"
# The action API operations that record a customer request (API-22..40 minus the lookups 30 and
# 36 and the escalation 35, which logs a handoff and needs no customer confirmation).
CONFIRMED_WRITES = frozenset(range(22, 41)) - {30, 35, 36}
_RESET_PATH = "/api/action-data/reset"

_CASE_HEADER = re.compile(r"^# (\S+)[ \t]*$", re.MULTILINE)
_API_NUMBER = re.compile(r"API-(\d{2})")
_MEDIA_LINE = re.compile(r"^(?:image|audio):\s*(https?://\S+)\s*$", re.MULTILINE)


@dataclass(frozen=True)
class Case:
    id: str
    account: str
    inquiry: str
    media_urls: tuple[str, ...]
    expected_apis: tuple[int, ...]


@dataclass(frozen=True)
class CaseResult:
    case_id: str
    called: tuple[int, ...]
    missing: tuple[int, ...]
    unconfirmed_writes: tuple[int, ...]
    answer: str
    seconds: float

    @property
    def calls_ok(self) -> bool:
        return not self.missing

    @property
    def confirmation_ok(self) -> bool:
        return not self.unconfirmed_writes

    @property
    def length_ok(self) -> bool:
        return len(self.answer) <= MAX_SIMPLE_TEXT_LENGTH

    @property
    def passed(self) -> bool:
        return self.calls_ok and self.confirmation_ok and self.length_ok


def parse_cases(text: str) -> list[Case]:
    headers = list(_CASE_HEADER.finditer(text))
    cases = []
    for header, following in zip(headers, [*headers[1:], None]):
        block = text[header.end() : following.start() if following else len(text)]
        cases.append(_parse_case(header.group(1), block))
    return cases


def _parse_case(case_id: str, block: str) -> Case:
    metadata_id = _field(block, "id")
    if metadata_id != case_id:
        raise ValueError(f"{case_id}: case metadata id is {metadata_id!r}")
    expected = _section(block, "기대 대응")
    return Case(
        id=case_id,
        account=_field(block, "인입 계정"),
        inquiry=_quoted_text(_section(block, "고객 문의")),
        media_urls=tuple(_MEDIA_LINE.findall(block.partition("#### 시나리오 및 기대 대응")[0])),
        expected_apis=tuple(dict.fromkeys(int(n) for n in _API_NUMBER.findall(expected))),
    )


def _field(block: str, name: str) -> str:
    match = re.search(rf"^- {re.escape(name)}:[ \t]*(.+?)[ \t]*$", block, re.MULTILINE)
    if match is None:
        raise ValueError(f"case metadata has no {name!r} line")
    return match.group(1)


def _section(block: str, title: str) -> str:
    match = re.search(rf"^###### {re.escape(title)}[ \t]*$(.*?)(?=^#{{1,6}} |^---[ \t]*$|\Z)", block, re.MULTILINE | re.DOTALL)
    if match is None:
        raise ValueError(f"case has no '{title}' section")
    return match.group(1)


def _quoted_text(section: str) -> str:
    lines = [re.sub(r"^>[ \t]?", "", line).rstrip() for line in section.splitlines() if line.startswith(">")]
    return "\n".join(line for line in lines if line).strip()


def api_number(tool_name: str) -> int | None:
    match = re.fullmatch(r"api_(\d{2})", tool_name)
    return int(match.group(1)) if match else None


def first_utterance(case: Case) -> str:
    """The first message of a case as the judge sends it: test case id, account, media, question."""
    return "\n".join([f"id: {case.id}", f"인입 계정: {case.account}", *case.media_urls, case.inquiry])


def judge(case: Case, calls: Sequence[CallRecord], answer: str, seconds: float) -> CaseResult:
    called = tuple(dict.fromkeys(n for call in calls if (n := api_number(call.tool)) is not None))
    # A write that never left the server (rejected arguments) changed nothing.
    sent = {n for call in calls if call.status is not None and (n := api_number(call.tool)) is not None}
    return CaseResult(
        case_id=case.id,
        called=called,
        missing=tuple(n for n in case.expected_apis if n not in called),
        # The replay sends one message, so the customer has confirmed nothing yet.
        unconfirmed_writes=tuple(sorted(sent & CONFIRMED_WRITES)),
        answer=answer,
        seconds=seconds,
    )


async def replay(case: Case, handler: Any, actions: Any) -> CaseResult:
    """Run one case as a fresh user whose first message carries the test case id."""
    actions.reset_calls()
    request = parse_skill_request(
        {"userRequest": {"utterance": first_utterance(case), "user": {"id": f"open-tc-{case.id}-{uuid.uuid4().hex[:8]}"}}}
    )
    started = time.perf_counter()
    result = await handler(request)
    seconds = time.perf_counter() - started
    return judge(case, list(actions.calls), result.text, seconds)


async def reset_action_data(http: httpx.AsyncClient, base_url: str, token: str) -> None:
    """The only place in this repository that calls the data reset endpoint (API-41)."""
    response = await http.post(base_url.rstrip("/") + _RESET_PATH, headers={"Authorization": f"Bearer {token}"})
    response.raise_for_status()


def format_report(results: Sequence[CaseResult]) -> str:
    def label(ok: bool) -> str:
        return "PASS" if ok else "FAIL"

    lines = []
    for r in results:
        lines += [
            f"[{label(r.passed)}] {r.case_id}  chars={len(r.answer)}  {r.seconds:.1f}s",
            f"    calls: {_apis(r.called)}",
            f"    {label(r.calls_ok)} expected APIs called" + (f" (missing: {_apis(r.missing)})" if r.missing else ""),
            f"    {label(r.confirmation_ok)} no write without confirmation"
            + (f" (called: {_apis(r.unconfirmed_writes)})" if r.unconfirmed_writes else ""),
            f"    {label(r.length_ok)} answer <= {MAX_SIMPLE_TEXT_LENGTH} chars",
            f"    answer: {r.answer}",
        ]
    passed = sum(r.passed for r in results)
    lines.append(f"SUMMARY: {passed}/{len(results)} PASS")
    return "\n".join(lines)


def _apis(numbers: Sequence[int]) -> str:
    return ", ".join(f"API-{n:02d}" for n in numbers) or "-"


async def run_live(
    cases: Sequence[Case],
    settings: Settings,
    *,
    reset_first: bool,
    claude: Any | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> list[CaseResult]:
    settings.validate()
    owned_claude = claude is None
    client = AsyncAnthropic(api_key=settings.claude_api_key) if owned_claude else claude
    try:
        async with httpx.AsyncClient(transport=transport) as http:
            if reset_first:
                await reset_action_data(http, settings.ACTION_API_BASE_URL, settings.bot_auth_token)
            handler, actions = assemble(settings, http, client)
            return [await replay(case, handler, actions) for case in cases]
    finally:
        if owned_claude:
            await client.close()


def dry_run_listing(cases: Sequence[Case]) -> list[dict[str, object]]:
    return [
        {"id": c.id, "account": c.account, "inquiry": c.inquiry, "apis": [f"API-{n:02d}" for n in c.expected_apis]}
        for c in cases
    ]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="parse and list the cases; no network, no keys")
    mode.add_argument("--live", action="store_true", help="replay against the real Claude and action APIs (costs money, changes data)")
    parser.add_argument("--file", type=Path, default=DEFAULT_CASES_FILE, help="cases file (default: knowledge/open_cases.md)")
    parser.add_argument("--only", nargs="+", metavar="ID", help="replay only these case ids")
    parser.add_argument("--reset-first", action="store_true", help="--live only: reset all action data once before replaying")
    return parser


def main(argv: Sequence[str] | None = None, out: TextIO | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.reset_first and not args.live:
        parser.error("--reset-first requires --live")
    out = sys.stdout if out is None else out
    if hasattr(out, "reconfigure"):
        # Case text is Korean; the Windows console default encoding cannot always show it.
        out.reconfigure(encoding="utf-8")

    try:
        cases = parse_cases(args.file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"cannot read cases from {args.file}: {exc}", file=sys.stderr)
        return 2
    if args.only:
        unknown = sorted(set(args.only) - {c.id for c in cases})
        if unknown:
            print(f"unknown case id(s): {', '.join(unknown)}", file=sys.stderr)
            return 2
        cases = [c for c in cases if c.id in args.only]

    if args.dry_run:
        empty = [c.id for c in cases if not c.inquiry]
        if empty:
            print(f"cases with no customer inquiry: {', '.join(empty)}", file=sys.stderr)
            return 1
        print(json.dumps(dry_run_listing(cases), ensure_ascii=False, indent=2), file=out)
        return 0

    try:
        results = asyncio.run(run_live(cases, Settings.from_env(), reset_first=args.reset_first))
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except httpx.HTTPError as exc:
        print(f"action data reset failed: {exc}", file=sys.stderr)
        return 2
    print(format_report(results), file=out)
    return 0 if all(r.passed for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
