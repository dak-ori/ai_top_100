"""Smoke-test a deployed skill server: GET /health, then POST one Kakao payload to /skill.

    python scripts/smoke_skill.py https://<service>.onrender.com

The /skill request runs one real agent turn, so it calls the Anthropic API (about one cent).
Exits 0 when both endpoints answer correctly in time, 1 otherwise.
"""

from __future__ import annotations

import argparse
import sys
import time
import uuid
from collections.abc import Sequence

import httpx

UTTERANCE = "안녕하세요 배송 문의드려요"
MAX_TEXT_LENGTH = 500
# Kakao drops a skill response that takes longer than this.
KAKAO_LIMIT_SECONDS = 5.0
REQUEST_TIMEOUT_SECONDS = 15.0


def skill_payload() -> dict[str, object]:
    return {"userRequest": {"utterance": UTTERANCE, "user": {"id": f"smoke-{uuid.uuid4().hex[:12]}"}}}


def response_problems(body: object) -> list[str]:
    """What is wrong with a skill response; empty when it is one SimpleText of at most 500 chars."""
    if not isinstance(body, dict):
        return ["response is not a JSON object"]
    problems = []
    if body.get("version") != "2.0":
        problems.append(f"version is {body.get('version')!r}, expected '2.0'")
    template = body.get("template")
    outputs = template.get("outputs") if isinstance(template, dict) else None
    if not isinstance(outputs, list) or len(outputs) != 1:
        count = len(outputs) if isinstance(outputs, list) else "no"
        return [*problems, f"expected exactly 1 output, found {count}"]
    simple_text = outputs[0].get("simpleText") if isinstance(outputs[0], dict) else None
    text = simple_text.get("text") if isinstance(simple_text, dict) else None
    if not isinstance(text, str) or not text.strip():
        return [*problems, "the output is not a SimpleText with text"]
    if len(text) > MAX_TEXT_LENGTH:
        problems.append(f"text is {len(text)} chars, over {MAX_TEXT_LENGTH}")
    return problems


def smoke(base_url: str, client: httpx.Client) -> bool:
    base_url = base_url.rstrip("/")
    ok = True

    started = time.perf_counter()
    try:
        health = client.get(f"{base_url}/health")
    except httpx.HTTPError as exc:
        print(f"[FAIL] GET /health: {type(exc).__name__}: {exc}")
        return False
    elapsed = time.perf_counter() - started
    healthy = health.status_code == 200
    ok &= healthy
    print(f"[{'PASS' if healthy else 'FAIL'}] GET /health  status={health.status_code}  {elapsed:.2f}s")

    started = time.perf_counter()
    try:
        response = client.post(f"{base_url}/skill", json=skill_payload())
    except httpx.HTTPError as exc:
        print(f"[FAIL] POST /skill: {type(exc).__name__}: {exc}")
        return False
    elapsed = time.perf_counter() - started
    problems = [] if response.status_code == 200 else [f"status is {response.status_code}, expected 200"]
    if response.status_code == 200:
        try:
            body = response.json()
        except ValueError:
            problems.append("response is not JSON")
        else:
            problems += response_problems(body)
            if not problems:
                print(f"        answer: {body['template']['outputs'][0]['simpleText']['text']}")
    if elapsed > KAKAO_LIMIT_SECONDS:
        problems.append(f"took {elapsed:.2f}s, over Kakao's {KAKAO_LIMIT_SECONDS:.0f}s limit")
    ok &= not problems
    print(f"[{'PASS' if not problems else 'FAIL'}] POST /skill  status={response.status_code}  {elapsed:.2f}s")
    for problem in problems:
        print(f"        - {problem}")
    return ok


def main(argv: Sequence[str] | None = None, transport: httpx.BaseTransport | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("url", help="service base URL, e.g. https://<service>.onrender.com")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        # The answer is Korean; the Windows console default encoding cannot always show it.
        sys.stdout.reconfigure(encoding="utf-8")
    with httpx.Client(transport=transport, timeout=REQUEST_TIMEOUT_SECONDS) as client:
        return 0 if smoke(args.url, client) else 1


if __name__ == "__main__":
    raise SystemExit(main())
