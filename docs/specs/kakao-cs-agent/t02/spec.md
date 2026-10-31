---
id: t02
loop_id: kakao-cs-agent
title: Action API client and tools
kind: feature
review_requirements: [R6]
---

## Context and goal

액션 API(40개 엔드포인트, reset 제외)를 Claude 도구로 노출하고 호출하는 클라이언트를 만든다. 도구 정의는 저장소에 복사한 `actions.json`에서 자동 생성해 명세와 어긋나지 않게 한다. 호출은 항상 `Authorization: Bearer` 토큰과 `X-Aitop100-Test-Case-Id` 헤더를 붙이고, 실패(400/404 본문 없음, 429, 네트워크)를 모델이 읽을 수 있는 명시적 결과로 돌려준다.

## Requirements

- R1: `C:\github\ai_top_100\docs\competition\actions.json`을 `app/data/actions.json`으로 그대로 복사해 저장소에 넣는다(내용을 바꾸지 않는다).
- R2: `app/actions.py`의 `build_tools(spec: dict) -> list[ToolDef]`가 OpenAPI의 각 `(path, method)`에서 도구를 만든다. 이름은 `operationId`를 소문자·언더스코어로 바꾼 `api_01` … `api_41` 형식(예: `API-07` → `api_07`), 설명은 `summary`(및 `description`이 있으면 첫 문단 최대 300자)와 `[GET /api/orders/{order_no}]` 형태의 메서드·경로를 포함한다. 입력 스키마는 JSON Schema 객체이며 path·query 파라미터와 요청 본문 속성을 평평하게 합치고(이름 충돌 시 본문 속성 이름 앞에 `body_`를 붙임), `X-Aitop100-Test-Case-Id` 헤더 파라미터는 스키마에서 제외한다(클라이언트가 주입). `required`는 필수 path·query·본문 속성만 포함한다. `additionalProperties: false`.
- R3: `API-41`(`POST /api/action-data/reset`)은 `build_tools` 결과에 포함되지 않고, 어떤 경로로도 `ActionClient`가 호출하지 않는다(호출을 시도하면 `ForbiddenAction`).
- R4: `ActionClient`(생성 인자: `base_url`, `token`, `httpx.AsyncClient`)의 `async call(tool_name: str, args: dict, test_case_id: str | None) -> dict`:
  - `test_case_id`가 `None`이거나 패턴 `^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$`와 맞지 않으면 HTTP 호출 없이 `{"ok": false, "error": "test_case_id_missing"}`(잘못된 형식이면 `"test_case_id_invalid"`)를 반환한다.
  - 알 수 없는 도구 이름은 `{"ok": false, "error": "unknown_tool"}`. 필수 인자 누락은 HTTP 호출 없이 `{"ok": false, "error": "missing_argument", "missing": [...]}`.
  - path 파라미터는 URL 인코딩해 채운다(`/`, `?`, `..` 등으로 다른 경로를 만들 수 없다). query는 인코딩해 붙인다. 본문 속성은 JSON 본문으로 보낸다.
  - 성공(2xx) 시 `{"ok": true, "status": <코드>, "body": <파싱된 JSON, 본문이 없으면 null>}`.
  - 400/404는 본문이 없으므로 `{"ok": false, "status": 400|404, "body": null, "error": "bad_request"|"not_found"}`.
  - 429는 `Retry-After`(초)를 읽는다. 2초 이하면 그만큼 기다려 최대 1회 재시도하고, 그보다 길거나 재시도도 429면 `{"ok": false, "status": 429, "error": "rate_limited", "retry_after": <초>}`. 5xx와 네트워크 오류·타임아웃(요청당 3초)은 `{"ok": false, "error": "upstream_error", "status": <코드 또는 null>}`이며 예외를 밖으로 던지지 않는다.
  - 모든 요청에 `Authorization: Bearer <token>`과 `X-Aitop100-Test-Case-Id`를 붙인다. 토큰 값은 어떤 반환값·예외·로그에도 나타나지 않는다.
- R5: `ActionClient`는 호출 기록을 `calls: list[CallRecord]`(도구 이름, 인자, 상태 코드, ok 여부 — 토큰 제외)로 남겨 이후 검증 스크립트가 쓸 수 있게 한다. 기록은 요청 단위로 비울 수 있는 `reset_calls()`를 제공한다.
- R6: (검토) 도구 설명이 모델에게 "어떤 상황에 쓰는지"를 충분히 알려 주며 쓰기(접수) 도구와 조회 도구를 구분할 수 있다(설명에 `[POST ...]`/`[GET ...]`와 summary가 있고, 쓰기 도구에 "고객이 확인한 값만 보낸다"는 안내가 붙어 있다). 도구 정의 생성 로직이 명세 변경에 자동 대응한다(하드코딩 목록이 없다).

## Scope and constraints

- 만들 파일: `app/data/actions.json`, `app/actions.py`, `tests/test_actions.py`, 필요하면 `tests/conftest.py`.
- 테스트는 `httpx.MockTransport`로 한다. 실제 네트워크 호출 금지. 대기(`Retry-After`) 테스트는 시간을 주입(`sleep` 함수 인자)해 실제로 기다리지 않는다.
- t01의 `app/config.py`를 재사용한다(`Settings`에서 `base_url`, 토큰). 토큰을 코드에 넣지 않는다.
- 메타 제약을 모두 따른다.

## Out of scope

- 어떤 도구를 언제 쓸지 결정하는 로직(프롬프트·에이전트 루프는 t03·t04), 세션, TC ID 추출(t04).
- 액션 API의 응답 필드별 해석.

## Prerequisite inputs

- t01: `app/config.py`의 `Settings`, `requirements.txt`(`httpx`, `pytest`, `pytest-asyncio` 포함), `pytest.ini`.
- `actions.json`: `paths.<path>.<method>`에 `operationId`(`API-NN`), `summary`, `parameters`(헤더 `X-Aitop100-Test-Case-Id` 포함), `requestBody.content["application/json"].schema`가 있다. 41개 오퍼레이션이며 `API-41`이 reset.

<!-- devlyn:verification -->
## Verification

- `python -m pytest -q tests/test_actions.py` — 다음을 모두 포함한다.
  - 복사본이 원본과 동일(해시 비교, R1).
  - `build_tools` 결과가 40개이고 `api_41`이 없으며 모든 이름이 `api_NN`, 각 스키마가 유효한 JSON Schema 객체, 헤더 파라미터가 스키마에 없음, `API-24`는 `order_no`·`new_address` 필수, `API-29`는 `order_no`·`amount`·`reason` 필수, `API-35`는 `lane`·`reason` 필수(R2, R3).
  - reset을 직접 부르려 하면 `ForbiddenAction`(R3).
  - 헤더 주입, TC ID 누락·형식 오류 시 HTTP 호출 없음, path 인코딩(`../`가 들어간 인자가 경로를 바꾸지 못함), 200/404/400/429/5xx/타임아웃 각 반환 형태, 429 재시도 1회와 긴 `Retry-After`, 필수 인자 누락, 알 수 없는 도구(R4).
  - 토큰 문자열이 반환값·로그 캡처에 나타나지 않음, 호출 기록과 `reset_calls()`(R5).

## Deliverable and cleanup

- 위 파일을 남긴다. 캐시 디렉터리는 stage하지 않는다.
