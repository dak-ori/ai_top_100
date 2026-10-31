import hashlib
import json
import logging
import re
from pathlib import Path

import httpx
import pytest

from app.actions import ACTIONS_SPEC_PATH, ActionClient, CallRecord, ForbiddenAction, build_tools, load_spec

ORIGINAL_SPEC_PATH = Path(r"C:\github\ai_top_100\docs\competition\actions.json")
BASE_URL = "https://action.test"
TOKEN = "test-action-token-value"
TC_ID = "L-03"
RESET_PATH = "/api/action-data/reset"
WRITE_NOTE_MARK = "고객이 확인한 값만 보낸다"

SPEC = load_spec()
TOOLS = {tool.name: tool for tool in build_tools(SPEC)}


@pytest.fixture
async def make_client():
    """Build an ``ActionClient`` whose transport replays ``responses`` in order and records requests.
    A request beyond the scripted responses fails the test."""
    http_clients: list[httpx.AsyncClient] = []

    def factory(*responses, spec=None, sleep=None):
        requests: list[httpx.Request] = []
        queue = list(responses)

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            response = queue.pop(0)
            if isinstance(response, Exception):
                raise response
            return response

        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        http_clients.append(http)
        kwargs = {"spec": spec} if spec is not None else {}
        if sleep is not None:
            kwargs["sleep"] = sleep
        return ActionClient(BASE_URL, TOKEN, http, **kwargs), requests

    yield factory
    for http in http_clients:
        await http.aclose()


def recording_sleep():
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)

    return slept, sleep


def op(operation_id, *, params=(), body=None, description=None, tc_scoped=True):
    parameters = [{"name": name, "in": location, "required": True, "schema": {"type": "string"}} for name, location in params]
    if tc_scoped:
        parameters.insert(0, {"name": "X-Aitop100-Test-Case-Id", "in": "header", "required": True, "schema": {"type": "string"}})
    operation = {"summary": f"{operation_id} summary", "operationId": operation_id, "parameters": parameters}
    if description is not None:
        operation["description"] = description
    if body is not None:
        operation["requestBody"] = {"required": True, "content": {"application/json": {"schema": body}}}
    return operation


# R1


def test_bundled_spec_is_verbatim_copy() -> None:
    if not ORIGINAL_SPEC_PATH.exists():
        pytest.skip("original actions.json is only present in the competition checkout")
    copy = hashlib.sha256(ACTIONS_SPEC_PATH.read_bytes()).hexdigest()
    assert copy == hashlib.sha256(ORIGINAL_SPEC_PATH.read_bytes()).hexdigest()


# R2, R3


def test_build_tools_exposes_every_operation_except_reset() -> None:
    names = list(TOOLS)

    assert len(build_tools(SPEC)) == 40
    assert len(set(names)) == 40
    assert all(re.fullmatch(r"api_\d{2}", name) for name in names)
    assert "api_41" not in names
    assert sorted(names) == [f"api_{n:02d}" for n in range(1, 41)]
    assert all(RESET_PATH not in tool.path and RESET_PATH not in tool.description for tool in TOOLS.values())


def test_tool_names_come_from_operation_ids() -> None:
    assert TOOLS["api_07"].method == "GET"
    assert TOOLS["api_07"].path == "/api/orders/{order_no}"
    # The spec key's query hint is not part of the request path.
    assert TOOLS["api_02"].path == "/api/products"


@pytest.mark.parametrize("tool", TOOLS.values(), ids=lambda tool: tool.name)
def test_input_schema_is_a_closed_json_schema_object(tool) -> None:
    schema = tool.input_schema

    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert isinstance(schema["properties"], dict)
    assert all(isinstance(prop, dict) and "type" in prop for prop in schema["properties"].values())
    assert isinstance(schema["required"], list)
    assert set(schema["required"]) <= set(schema["properties"])
    assert "X-Aitop100-Test-Case-Id" not in json.dumps(schema)
    assert json.loads(json.dumps(schema)) == schema


@pytest.mark.parametrize(
    ("name", "required"),
    [
        ("api_07", ["order_no"]),
        ("api_02", ["item"]),
        ("api_22", ["order_no"]),
        ("api_24", ["order_no", "new_address"]),
        ("api_29", ["order_no", "amount", "reason"]),
        ("api_35", ["lane", "reason"]),
    ],
)
def test_required_arguments(name: str, required: list[str]) -> None:
    assert TOOLS[name].input_schema["required"] == required


def test_descriptions_separate_reads_from_writes() -> None:
    order = TOOLS["api_07"].description
    assert "주문 조회" in order
    assert "[GET /api/orders/{order_no}]" in order
    assert WRITE_NOTE_MARK not in order

    search = TOOLS["api_02"].description
    assert "[GET /api/products?item=]" in search
    assert "SKU의 품목 접두사로 검색합니다." in search

    refund = TOOLS["api_29"].description
    assert "환불 신청" in refund
    assert "[POST /api/refunds]" in refund
    assert WRITE_NOTE_MARK in refund

    for tool in TOOLS.values():
        assert f"[{tool.method} " in tool.description
        assert (WRITE_NOTE_MARK in tool.description) == (tool.method != "GET")


def test_description_keeps_first_paragraph_up_to_300_chars() -> None:
    spec = {"paths": {"/api/things": {"get": op("API-90", description="가" * 400 + "\n\n두 번째 문단")}}}

    (tool,) = build_tools(spec)

    assert "가" * 300 in tool.description
    assert "가" * 301 not in tool.description
    assert "두 번째 문단" not in tool.description


async def test_body_property_colliding_with_parameter_is_prefixed(make_client) -> None:
    body = {
        "type": "object",
        "properties": {"order_no": {"type": "string"}, "memo": {"type": "string"}},
        "required": ["order_no"],
    }
    spec = {"paths": {"/api/orders/{order_no}/notes": {"post": op("API-91", params=[("order_no", "path")], body=body)}}}
    client, requests = make_client(httpx.Response(200, json={}), spec=spec)

    (tool,) = build_tools(spec)
    assert tool.name == "api_91"
    assert set(tool.input_schema["properties"]) == {"order_no", "body_order_no", "memo"}
    assert tool.input_schema["required"] == ["order_no", "body_order_no"]

    result = await client.call("api_91", {"order_no": "A1", "body_order_no": "B2", "memo": "m"}, TC_ID)

    assert result["ok"] is True
    assert requests[0].url.path == "/api/orders/A1/notes"
    assert json.loads(requests[0].content) == {"order_no": "B2", "memo": "m"}


async def test_reset_is_forbidden(make_client) -> None:
    reset = SPEC["paths"][RESET_PATH]["post"]
    reset_name = re.sub(r"[^a-z0-9]+", "_", reset["operationId"].lower())
    client, requests = make_client()

    for test_case_id in (TC_ID, None):
        with pytest.raises(ForbiddenAction):
            await client.call(reset_name, {}, test_case_id)

    assert requests == []
    assert client.calls == []


async def test_operations_without_test_case_header_are_never_exposed(make_client) -> None:
    spec = {
        "paths": {
            "/api/things/{id}": {"get": op("API-01", params=[("id", "path")])},
            "/api/wipe": {"post": op("wipeEverything", tc_scoped=False)},
        }
    }
    client, requests = make_client(spec=spec)

    assert [tool.name for tool in build_tools(spec)] == ["api_01"]
    with pytest.raises(ForbiddenAction):
        await client.call("wipeeverything", {}, TC_ID)
    assert requests == []


async def test_no_argument_can_resolve_to_a_forbidden_route(make_client) -> None:
    spec = {
        "paths": {
            "/api/{area}/reset": {"post": op("API-01", params=[("area", "path")])},
            RESET_PATH: {"post": op("resetMyActionData", tc_scoped=False)},
        }
    }
    client, requests = make_client(spec=spec)

    with pytest.raises(ForbiddenAction):
        await client.call("api_01", {"area": "action-data"}, TC_ID)
    assert requests == []


# R4


async def test_injects_auth_and_test_case_headers(make_client) -> None:
    client, requests = make_client(httpx.Response(200, json={"order_no": "100100003"}))

    result = await client.call("api_07", {"order_no": "100100003"}, TC_ID)

    assert result == {"ok": True, "status": 200, "body": {"order_no": "100100003"}}
    (request,) = requests
    assert request.method == "GET"
    assert str(request.url) == f"{BASE_URL}/api/orders/100100003"
    assert request.headers["Authorization"] == f"Bearer {TOKEN}"
    assert request.headers["X-Aitop100-Test-Case-Id"] == TC_ID
    assert request.content == b""
    assert request.extensions["timeout"] == {"connect": 3.0, "read": 3.0, "write": 3.0, "pool": 3.0}


async def test_query_and_body_arguments(make_client) -> None:
    client, requests = make_client(httpx.Response(200, json=[]), httpx.Response(200, json={"refund_id": "R1"}))

    await client.call("api_13", {"name": "서지호 & co"}, TC_ID)
    await client.call("api_29", {"order_no": "100100003", "amount": 5000, "reason": "파손"}, TC_ID)

    search, refund = requests
    assert search.url.path == "/api/members"
    assert search.url.params["name"] == "서지호 & co"
    assert refund.method == "POST"
    assert refund.url.path == "/api/refunds"
    assert json.loads(refund.content) == {"order_no": "100100003", "amount": 5000, "reason": "파손"}
    assert refund.headers["Content-Type"] == "application/json"


async def test_write_without_body_arguments_sends_empty_json_object(make_client) -> None:
    client, requests = make_client(httpx.Response(200, json={"request_id": "X"}))

    await client.call("api_22", {"order_no": "100100003"}, TC_ID)

    assert json.loads(requests[0].content) == {}


@pytest.mark.parametrize(
    ("test_case_id", "error"),
    [
        (None, "test_case_id_missing"),
        ("", "test_case_id_invalid"),
        ("-L03", "test_case_id_invalid"),
        ("L 03", "test_case_id_invalid"),
        ("L-03\n", "test_case_id_invalid"),
        ("A" * 101, "test_case_id_invalid"),
    ],
)
async def test_bad_test_case_id_makes_no_request(make_client, test_case_id, error) -> None:
    client, requests = make_client()

    result = await client.call("api_07", {"order_no": "100100003"}, test_case_id)

    assert result == {"ok": False, "error": error}
    assert requests == []


async def test_valid_test_case_id_shapes(make_client) -> None:
    ids = ["R-05-a", "Y-07.b_1", "A" * 100]
    client, requests = make_client(*(httpx.Response(200, json={}) for _ in ids))

    for test_case_id in ids:
        assert (await client.call("api_07", {"order_no": "1"}, test_case_id))["ok"] is True

    assert [request.headers["X-Aitop100-Test-Case-Id"] for request in requests] == ids


@pytest.mark.parametrize(
    "order_no",
    ["../action-data/reset", "../../api/action-data/reset", "1?x=1", "1#frag", "a/b", "%2e%2e%2f"],
)
async def test_path_arguments_cannot_change_the_path(make_client, order_no: str) -> None:
    client, requests = make_client(httpx.Response(404))

    await client.call("api_24", {"order_no": order_no, "new_address": "서울"}, TC_ID)

    (request,) = requests
    segments = request.url.raw_path.decode("ascii").split("/")
    assert segments[:3] == ["", "api", "orders"]
    assert segments[4:] == ["address-change-requests"]
    assert len(segments) == 5
    assert request.url.query == b""


@pytest.mark.parametrize("order_no", ["..", ".", ""])
async def test_dot_segment_path_arguments_are_rejected(make_client, order_no: str) -> None:
    client, requests = make_client()

    result = await client.call("api_24", {"order_no": order_no, "new_address": "서울"}, TC_ID)

    assert result == {"ok": False, "error": "invalid_argument", "invalid": ["order_no"]}
    assert requests == []


async def test_missing_arguments_make_no_request(make_client) -> None:
    client, requests = make_client()

    result = await client.call("api_29", {"order_no": "100100003", "reason": None}, TC_ID)

    assert result == {"ok": False, "error": "missing_argument", "missing": ["amount", "reason"]}
    assert requests == []


async def test_unexpected_arguments_make_no_request(make_client) -> None:
    client, requests = make_client()

    result = await client.call("api_07", {"order_no": "1", "member_id": "MB-1"}, TC_ID)

    assert result == {"ok": False, "error": "unexpected_argument", "unexpected": ["member_id"]}
    assert requests == []


async def test_unknown_tool(make_client) -> None:
    client, requests = make_client()

    for name in ("api_99", "api_41", "API-07"):
        assert await client.call(name, {}, TC_ID) == {"ok": False, "error": "unknown_tool"}
    assert requests == []


async def test_success_without_body(make_client) -> None:
    client, _ = make_client(httpx.Response(204))

    assert await client.call("api_39", {"sku": "GAL-STD-1"}, TC_ID) == {"ok": True, "status": 204, "body": None}


@pytest.mark.parametrize(("status", "error"), [(400, "bad_request"), (404, "not_found")])
async def test_bodyless_client_errors(make_client, status: int, error: str) -> None:
    client, _ = make_client(httpx.Response(status))

    result = await client.call("api_07", {"order_no": "999"}, TC_ID)

    assert result == {"ok": False, "status": status, "body": None, "error": error}


async def test_429_with_short_retry_after_retries_once(make_client) -> None:
    slept, sleep = recording_sleep()
    client, requests = make_client(
        httpx.Response(429, headers={"Retry-After": "2"}),
        httpx.Response(200, json={"order_no": "1"}),
        sleep=sleep,
    )

    result = await client.call("api_07", {"order_no": "1"}, TC_ID)

    assert result == {"ok": True, "status": 200, "body": {"order_no": "1"}}
    assert slept == [2]
    assert len(requests) == 2
    assert all(request.headers["X-Aitop100-Test-Case-Id"] == TC_ID for request in requests)


async def test_429_twice_is_rate_limited(make_client) -> None:
    slept, sleep = recording_sleep()
    client, requests = make_client(
        httpx.Response(429, headers={"Retry-After": "1"}),
        httpx.Response(429, headers={"Retry-After": "1"}),
        sleep=sleep,
    )

    result = await client.call("api_07", {"order_no": "1"}, TC_ID)

    assert result == {"ok": False, "status": 429, "error": "rate_limited", "retry_after": 1}
    assert slept == [1]
    assert len(requests) == 2


@pytest.mark.parametrize(("headers", "retry_after"), [({"Retry-After": "3"}, 3), ({}, None), ({"Retry-After": "soon"}, None)])
async def test_429_without_short_retry_after_is_not_retried(make_client, headers, retry_after) -> None:
    slept, sleep = recording_sleep()
    client, requests = make_client(httpx.Response(429, headers=headers), sleep=sleep)

    result = await client.call("api_07", {"order_no": "1"}, TC_ID)

    assert result == {"ok": False, "status": 429, "error": "rate_limited", "retry_after": retry_after}
    assert slept == []
    assert len(requests) == 1


@pytest.mark.parametrize("status", [500, 503, 401])
async def test_server_errors_are_upstream_errors(make_client, status: int) -> None:
    client, _ = make_client(httpx.Response(status, json={"code": "X", "message": "m"}))

    assert await client.call("api_07", {"order_no": "1"}, TC_ID) == {
        "ok": False,
        "error": "upstream_error",
        "status": status,
    }


async def test_non_json_success_body_is_an_upstream_error(make_client) -> None:
    client, _ = make_client(httpx.Response(200, text="<html>"))

    assert await client.call("api_07", {"order_no": "1"}, TC_ID) == {
        "ok": False,
        "error": "upstream_error",
        "status": 200,
    }


@pytest.mark.parametrize(
    "exc", [httpx.ReadTimeout("timed out"), httpx.ConnectTimeout("timed out"), httpx.ConnectError("refused")]
)
async def test_network_failures_do_not_raise(make_client, exc) -> None:
    client, _ = make_client(exc)

    result = await client.call("api_07", {"order_no": "1"}, TC_ID)

    assert result == {"ok": False, "error": "upstream_error", "status": None}
    assert client.calls == [CallRecord("api_07", {"order_no": "1"}, None, False)]


async def test_empty_token_is_rejected() -> None:
    async with httpx.AsyncClient() as http:
        with pytest.raises(ValueError, match="token"):
            ActionClient(BASE_URL, "", http)


# R5


async def test_token_never_leaks_into_results_records_or_logs(make_client, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    client, _ = make_client(
        httpx.Response(200, json={}),
        httpx.Response(404),
        httpx.Response(429, headers={"Retry-After": "60"}),
        httpx.Response(500),
        httpx.ReadTimeout("timed out"),
    )

    results = [await client.call("api_07", {"order_no": "1"}, TC_ID) for _ in range(5)]
    results.append(await client.call("api_07", {}, None))
    results.append(await client.call("nope", {}, TC_ID))
    with pytest.raises(ForbiddenAction) as forbidden:
        await client.call("resetmyactiondata", {}, TC_ID)

    rendered = [json.dumps(results, ensure_ascii=False), repr(client.calls), caplog.text, str(forbidden.value)]
    assert all(TOKEN not in text for text in rendered)


async def test_calls_are_recorded_and_can_be_reset(make_client) -> None:
    client, _ = make_client(httpx.Response(200, json={}), httpx.Response(404))
    args = {"order_no": "1"}

    await client.call("api_07", args, TC_ID)
    await client.call("api_10", {"shipment_id": "S"}, TC_ID)
    await client.call("api_07", {}, TC_ID)
    args["order_no"] = "mutated"

    assert client.calls == [
        CallRecord("api_07", {"order_no": "1"}, 200, True),
        CallRecord("api_10", {"shipment_id": "S"}, 404, False),
        CallRecord("api_07", {}, None, False),
    ]

    client.reset_calls()
    assert client.calls == []
