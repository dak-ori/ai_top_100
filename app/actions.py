"""Agent tools generated from the action API OpenAPI spec, and the client that calls them."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Awaitable, Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)

ACTIONS_SPEC_PATH = Path(__file__).resolve().parent / "data" / "actions.json"
TEST_CASE_HEADER = "X-Aitop100-Test-Case-Id"
REQUEST_TIMEOUT_SECONDS = 3.0
MAX_RETRY_AFTER_SECONDS = 2
# Bounds memory in the long-running server, where nobody clears the records.
MAX_CALL_RECORDS = 1000

_TEST_CASE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")
_DESCRIPTION_LIMIT = 300
_WRITE_NOTE = (
    "접수(쓰기) 도구: 고객이 확인한 값만 보낸다. "
    "API는 정책을 검사하지 않고 그대로 접수하므로 호출 전에 정책을 확인한다."
)
# httpx resolves dot segments, so these values would address a different path even when encoded.
_UNSAFE_PATH_VALUES = frozenset({"", ".", ".."})
_CLIENT_ERRORS = {400: "bad_request", 404: "not_found"}

Location = Literal["path", "query", "body"]
Sleep = Callable[[float], Awaitable[None]]


class ForbiddenAction(Exception):
    """An operation outside the agent's per-test-case action surface was requested."""


@dataclass(frozen=True)
class ToolDef:
    name: str
    description: str
    input_schema: dict[str, object]
    method: str
    # Request path template; the spec key's query hint (e.g. "?item=") is stripped.
    path: str
    # Tool argument name -> where it goes in the request and its name there.
    arguments: Mapping[str, tuple[Location, str]]
    has_body: bool


@dataclass(frozen=True)
class CallRecord:
    tool: str
    args: dict[str, object]
    # None when no HTTP response arrived: rejected before sending, or a network failure.
    status: int | None
    ok: bool


def load_spec(path: Path = ACTIONS_SPEC_PATH) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def build_tools(spec: dict) -> list[ToolDef]:
    """One tool per test-case-scoped operation. An operation that does not take the test case
    header (the data reset) is not a customer action and is never exposed."""
    return [_build_tool(method, key, op) for method, key, op in _operations(spec) if _is_test_case_scoped(op)]


class ActionClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        http: httpx.AsyncClient,
        *,
        spec: dict | None = None,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        if not token:
            raise ValueError("an action API token is required")
        spec = load_spec() if spec is None else spec
        self._base_url = base_url.rstrip("/")
        self._authorization = f"Bearer {token}"
        self._http = http
        self._sleep = sleep
        self._tools = {tool.name: tool for tool in build_tools(spec)}
        self._forbidden = {
            _tool_name(op): (method.upper(), _request_path(key))
            for method, key, op in _operations(spec)
            if not _is_test_case_scoped(op)
        }
        self._forbidden_routes = frozenset(self._forbidden.values())
        self.calls: list[CallRecord] = []

    async def call(self, tool_name: str, args: dict, test_case_id: str | None) -> dict[str, object]:
        """Run one tool call. Every failure comes back as ``{"ok": False, "error": ...}``;
        only an attempt at a forbidden operation raises ``ForbiddenAction``."""
        if tool_name in self._forbidden:
            raise ForbiddenAction(f"{tool_name} is not exposed to the agent")
        status, result = await self._invoke(tool_name, args, test_case_id)
        self.calls.append(CallRecord(tool_name, dict(args), status, result["ok"] is True))
        del self.calls[:-MAX_CALL_RECORDS]
        return result

    def reset_calls(self) -> None:
        self.calls.clear()

    async def _invoke(
        self, tool_name: str, args: dict, test_case_id: str | None
    ) -> tuple[int | None, dict[str, object]]:
        tool = self._tools.get(tool_name)
        if tool is None:
            return None, {"ok": False, "error": "unknown_tool"}
        if test_case_id is None:
            return None, {"ok": False, "error": "test_case_id_missing"}
        if not _TEST_CASE_ID.fullmatch(test_case_id):
            return None, {"ok": False, "error": "test_case_id_invalid"}

        provided = {name: value for name, value in args.items() if value is not None}
        missing = [name for name in tool.input_schema["required"] if name not in provided]
        if missing:
            return None, {"ok": False, "error": "missing_argument", "missing": missing}
        unexpected = sorted(set(provided) - set(tool.arguments))
        if unexpected:
            return None, {"ok": False, "error": "unexpected_argument", "unexpected": unexpected}

        path, query, body = tool.path, {}, {}
        for name, value in provided.items():
            location, wire_name = tool.arguments[name]
            if location == "path":
                text = str(value)
                if text in _UNSAFE_PATH_VALUES:
                    return None, {"ok": False, "error": "invalid_argument", "invalid": [name]}
                path = path.replace(f"{{{wire_name}}}", quote(text, safe=""))
            elif location == "query":
                query[wire_name] = value
            else:
                body[wire_name] = value

        request = self._http.build_request(
            tool.method,
            self._base_url + path,
            params=query,
            json=body if tool.has_body else None,
            headers={"Authorization": self._authorization, TEST_CASE_HEADER: test_case_id},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        # Last line of defence: whatever the arguments, never send to a forbidden route.
        if (request.method, request.url.path) in self._forbidden_routes:
            raise ForbiddenAction(f"{tool_name} resolved to a forbidden route")

        try:
            response = await self._send(request)
        except httpx.HTTPError as exc:
            logger.warning("Action API %s failed: %s", tool_name, type(exc).__name__)
            return None, {"ok": False, "error": "upstream_error", "status": None}
        return response.status_code, _result(tool_name, response)

    async def _send(self, request: httpx.Request) -> httpx.Response:
        response = await self._http.send(request)
        retry_after = _retry_after(response)
        if response.status_code == 429 and retry_after is not None and retry_after <= MAX_RETRY_AFTER_SECONDS:
            await self._sleep(retry_after)
            response = await self._http.send(request)
        return response


def _result(tool_name: str, response: httpx.Response) -> dict[str, object]:
    status = response.status_code
    if response.is_success:
        if not response.content:
            return {"ok": True, "status": status, "body": None}
        try:
            return {"ok": True, "status": status, "body": response.json()}
        except ValueError:
            logger.warning("Action API %s returned HTTP %s with a non-JSON body", tool_name, status)
            return {"ok": False, "error": "upstream_error", "status": status}
    if status in _CLIENT_ERRORS:
        return {"ok": False, "status": status, "body": None, "error": _CLIENT_ERRORS[status]}
    if status == 429:
        retry_after = _retry_after(response)
        logger.warning("Action API %s rate limited (Retry-After: %s)", tool_name, retry_after)
        return {"ok": False, "status": status, "error": "rate_limited", "retry_after": retry_after}
    logger.warning("Action API %s returned HTTP %s", tool_name, status)
    return {"ok": False, "error": "upstream_error", "status": status}


def _retry_after(response: httpx.Response) -> int | None:
    value = response.headers.get("Retry-After", "").strip()
    return int(value) if value.isdecimal() else None


def _operations(spec: dict) -> Iterator[tuple[str, str, dict]]:
    for key, path_item in spec["paths"].items():
        for method, op in path_item.items():
            yield method, key, op


def _is_test_case_scoped(op: dict) -> bool:
    return any(_is_test_case_header(param) for param in op.get("parameters", []))


def _is_test_case_header(param: dict) -> bool:
    return param["in"] == "header" and param["name"].lower() == TEST_CASE_HEADER.lower()


def _tool_name(op: dict) -> str:
    return re.sub(r"[^a-z0-9]+", "_", op["operationId"].lower())


def _request_path(key: str) -> str:
    return key.partition("?")[0]


def _build_tool(method: str, key: str, op: dict) -> ToolDef:
    properties: dict[str, object] = {}
    required: list[str] = []
    arguments: dict[str, tuple[Location, str]] = {}

    def add(name: str, location: Location, wire_name: str, schema: dict, is_required: bool) -> None:
        if name in arguments:
            raise ValueError(f"{op['operationId']}: duplicate tool argument {name!r}")
        properties[name] = schema
        arguments[name] = (location, wire_name)
        if is_required:
            required.append(name)

    for param in op.get("parameters", []):
        if _is_test_case_header(param):
            continue
        if param["in"] not in ("path", "query"):
            raise ValueError(f"{op['operationId']}: unsupported parameter location {param['in']!r}")
        schema = dict(param["schema"])
        if "description" in param:
            schema["description"] = param["description"]
        add(param["name"], param["in"], param["name"], schema, param.get("required", False))

    request_body = op.get("requestBody")
    if request_body is not None:
        body_schema = request_body["content"]["application/json"]["schema"]
        body_required = set(body_schema.get("required", [])) if request_body.get("required", False) else set()
        for name, schema in body_schema.get("properties", {}).items():
            add(f"body_{name}" if name in arguments else name, "body", name, schema, name in body_required)

    return ToolDef(
        name=_tool_name(op),
        description=_describe(method, key, op),
        input_schema={
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
        method=method.upper(),
        path=_request_path(key),
        arguments=arguments,
        has_body=request_body is not None,
    )


def _describe(method: str, key: str, op: dict) -> str:
    lines = [f"{op['summary']} [{method.upper()} {key}]"]
    description = op.get("description", "").strip()
    if description:
        lines.append(description.split("\n\n", 1)[0][:_DESCRIPTION_LIMIT])
    if method.lower() != "get":
        lines.append(_WRITE_NOTE)
    return "\n".join(lines)
