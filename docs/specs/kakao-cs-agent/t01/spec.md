---
id: t01
loop_id: kakao-cs-agent
title: Service skeleton
kind: feature
review_requirements: [R7]
---

## Context and goal

카카오 i 오픈빌더 스킬 서버의 뼈대를 만든다. 이 task가 끝나면 카카오 스킬 요청을 파싱하고 SimpleText 응답을 만드는 모듈, 환경변수 설정 로더, 핸들러를 주입받는 `create_app(handler)` 서버 팩토리, `/health`, 의존성·테스트·`.gitignore` 기반이 있다. 에이전트 로직(Claude 호출)은 이후 task가 맡는다. 비밀(`.env` 등)이 절대 저장소에 들어가지 않게 하는 기반도 여기서 만든다.

## Requirements

- R1: `requirements.txt`에 런타임 패키지(`fastapi`, `uvicorn`, `httpx`, `anthropic`, `python-dotenv`)와 테스트 패키지(`pytest`, `pytest-asyncio`)를 정확한 버전으로 고정한다(`==`). `python -m pip install -r requirements.txt`가 Python 3.12와 3.14에서 성공해야 하므로 두 버전 모두 휠이 있는 버전을 고른다. `pytest.ini`(또는 `pyproject.toml`)에 `asyncio_mode = auto`와 `testpaths = tests`를 둔다.
- R2: `.gitignore`가 다음 항목을 모두 포함한다(기존 내용은 유지하고 없는 줄만 추가): `.env`, `.env.*`, `api_key.md`, `docs/competition/api.md`, `.venv/`, `__pycache__/`, `.pytest_cache/`.
- R3: `app/config.py`의 `Settings`(dataclass 또는 pydantic)가 환경변수에서 값을 읽는다. 로컬 `.env`는 `python-dotenv`로 읽되(없어도 오류 아님) 이미 설정된 환경변수를 덮어쓰지 않는다. 키: Anthropic API 키(`claude_api_key`, 별칭 `CLAUDE_API_KEY`·`ANTHROPIC_API_KEY`), 액션 토큰(`bot_auth_token`, 별칭 `BOT_AUTH_TOKEN`), `CLAUDE_MODEL`(기본 `claude-haiku-5-5`), `ACTION_API_BASE_URL`(기본 `https://gyodong-mall-action.aitop100.org`), `TURN_BUDGET_SECONDS`(기본 `4.2`), `SESSION_TTL_SECONDS`(기본 `3600`), `MAX_SESSION_TURNS`(기본 `40`). 필수 키가 없으면 `Settings.validate()`가 어느 변수 이름이 없는지 말하는 `ConfigError`를 던지되 값을 메시지에 넣지 않는다. `Settings`의 `repr`/`str`은 키 값을 마스킹한다.
- R4: `app/kakao.py`에 다음을 둔다.
  - `parse_skill_request(body: dict) -> SkillRequest`: `SkillRequest`는 dataclass이며 필드 이름은 정확히 `utterance: str`, `user_id: str`, `callback_url: str | None`, `media_urls: list[tuple[str, str]]`(각 항목은 `(kind, url)`, `kind`는 `"image"` 또는 `"audio"`)이다. `userRequest.utterance`(없으면 빈 문자열), `userRequest.user.id`(없으면 `ConfigError`가 아닌 `BadSkillRequest` 예외), `userRequest.callbackUrl`(없으면 `None`), 발화에 포함된 이미지·음성 URL 후보 목록(`media_urls`; `http(s)://...` 중 확장자가 jpg/jpeg/png/webp/gif면 image, m4a/mp3/wav/aac/ogg면 audio로 분류하고 그 외 URL은 무시)을 담는다. 또 `userRequest.params.media`·`action.params`·`action.detailParams`의 값 안에 같은 형태의 URL 문자열이 있으면 함께 수집한다(문자열 값만, 중복 제거).
  - `simple_text_response(text: str) -> dict`: `{"version": "2.0", "template": {"outputs": [{"simpleText": {"text": <text>}}]}}`. 텍스트가 500자를 넘으면 문장 경계(`.`, `?`, `!`, `다.`, 줄바꿈) 기준으로 500자 이하가 되게 자르고, 경계가 없으면 497자 + `...`로 자른다. 빈 텍스트는 `BadSkillResponse` 예외.
  - `callback_wait_response() -> dict`: `{"version": "2.0", "useCallback": true, "data": {"text": "확인하고 있어요. 잠시만 기다려 주세요."}}`.
  - `async post_callback(client: httpx.AsyncClient, callback_url: str, text: str) -> None`: 콜백 URL에 `simple_text_response(text)`를 POST(JSON). 2xx가 아니면 `CallbackError`를 던진다. URL이 `https://`가 아니면 보내지 않고 `CallbackError`.
- R5: `app/server.py`의 `create_app(handler, settings=None, http_client=None) -> FastAPI`. `handler`는 `async def handler(request: SkillRequest) -> HandlerResult`를 만족하는 객체이며 `HandlerResult`는 `text: str`과 선택적 `callback: Callable[[], Awaitable[None]] | None`을 가진 dataclass이다(`app/types.py` 등 자유 위치). 라우트: `GET /health`는 `{"status": "ok"}`(키 값이나 설정은 노출하지 않음), `POST /skill`은 본문을 `parse_skill_request`로 파싱해 `handler`를 호출하고 `simple_text_response(result.text)`를 반환한다. 파싱 실패(`BadSkillRequest`)는 HTTP 200으로 안전한 안내문("메시지를 이해하지 못했어요. 다시 한 번 말씀해 주세요.")을 SimpleText로 반환하고 사유는 서버 로그에 남긴다(카카오는 비-200을 오류 말풍선으로 처리하므로). 핸들러가 예외를 던지면 같은 방식으로 안전한 안내문("잠시 문제가 생겼어요. 잠시 후 다시 말씀해 주세요.")을 200으로 반환하고 예외는 로그(`logger.exception`, 비밀 제외)에 남긴다.
- R6: 핸들러가 `callback`을 가진 결과를 반환하면 `POST /skill`은 즉시 `callback_wait_response()`를 반환하고 `callback`을 FastAPI `BackgroundTasks`로 실행한다. 이때 callback이 던지는 예외는 로그에 남기고 서버를 죽이지 않는다.
- R7: (검토) 위 모듈에 비밀 값(키·토큰)이 하드코딩되거나 로그·예외 메시지·`/health` 응답에 노출되는 경로가 없고, 오류 상태가 침묵하지 않고 명시적으로 처리된다.

## Scope and constraints

- 만들 파일: `requirements.txt`, `pytest.ini`(또는 `pyproject.toml`), `.gitignore`(수정), `app/__init__.py`, `app/config.py`, `app/kakao.py`, `app/server.py`, `app/types.py`(선택), `tests/__init__.py`(필요 시), `tests/test_config.py`, `tests/test_kakao.py`, `tests/test_server.py`.
- 메타 제약(비밀 보호, SimpleText 500자, 테스트는 네트워크·키 없이 통과, Python 3.12·3.14 호환)을 모두 따른다.
- 테스트는 `fastapi.testclient.TestClient` 또는 `httpx.ASGITransport`와 가짜 핸들러를 쓴다.

## Out of scope

- Claude 호출, 액션 API 호출, 프롬프트, 세션 저장, Render 설정, `app/main.py`(t05)는 만들지 않는다.
- `.env` 파일을 만들거나 읽어서 내용을 출력하지 않는다.

## Prerequisite inputs

- 없음(첫 task). 카카오 스킬 페이로드는 표준 형식 `{"intent":{...},"userRequest":{"utterance":"...","user":{"id":"..."},"callbackUrl":"..."},"action":{"params":{},"detailParams":{}}}`를 가정한다.

<!-- devlyn:verification -->
## Verification

- `python -m pip install -r requirements.txt` 성공(R1).
- `python -m pytest -q tests/test_config.py` — 환경변수 별칭 우선순위, 필수 키 누락 시 변수 이름만 말하는 `ConfigError`, 값 마스킹, `.env`가 환경변수를 덮어쓰지 않음(R3).
- `python -m pytest -q tests/test_kakao.py` — 파싱(utterance·user id·callbackUrl·미디어 URL 분류와 중복 제거, user id 누락 시 `BadSkillRequest`), 500자 초과 자르기(문장 경계, 경계 없음, 정확히 500자와 501자), 빈 텍스트 예외, 콜백 비-2xx와 `http://` URL 거부(R4).
- `python -m pytest -q tests/test_server.py` — `/health`, 정상 `/skill` 응답 형식, 파싱 실패·핸들러 예외가 200 + 안전한 안내문, 콜백 결과면 `useCallback` 응답과 백그라운드 실행, 콜백 예외가 서버를 죽이지 않음(R5, R6).
- `.gitignore`에 R2의 일곱 항목이 모두 있다는 확인(테스트 또는 명령).
- R7은 검토로 확인한다.

## Deliverable and cleanup

- 위 파일들을 커밋 가능한 상태로 남긴다. `__pycache__`, `.pytest_cache`, 임시 가상환경은 stage하지 않는다.
