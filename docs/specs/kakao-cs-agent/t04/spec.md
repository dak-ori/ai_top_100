---
id: t04
loop_id: kakao-cs-agent
title: Agent loop, session, media and output guard
kind: feature
review_requirements: [R4, R8]
---

## Context and goal

고객 발화 한 건을 받아 Claude Haiku 5.5가 도구(액션 API)를 호출하며 답을 만들고, 그 답을 카카오 규칙(말풍선 1개, 500자 이하, 평문)에 맞게 정리해 돌려주는 에이전트 층을 만든다. 세션(대화 기록, TC ID), 이미지 입력, 지연 한도(카카오 5초), 콜백 폴백, 출력 유출 방지까지 포함한다. 이 task가 끝나면 `TurnHandler`를 `create_app(handler)`에 넣어 실제 서비스가 된다(조립은 t05).

## Requirements

- R1: `app/session.py`.
  - `extract_test_case_id(text: str) -> str | None`: 정규식 `(?<![A-Za-z0-9])[A-Z]-\d{2}(?:-[a-z])?(?![A-Za-z0-9])`로 첫 일치를 반환(예: `id: L-03` → `L-03`, `R-05-a`, `Y-07-b`, `Y-16`). 전화번호·주문번호·`ESC-20370630-001` 같은 문자열에서 오탐하지 않는다.
  - `SessionStore`(주입 가능한 시계 함수): 키는 카카오 사용자 id. 세션은 `test_case_id`, 대화 메시지 목록(Anthropic 메시지 dict), 마지막 활동 시각, 사용자 턴 수를 가진다. TTL(`Settings.SESSION_TTL_SECONDS`) 경과 시 새 세션. 사용자 턴 수가 `MAX_SESSION_TURNS`를 넘으면 가장 오래된 **턴 묶음**(사용자 메시지부터 최종 어시스턴트 텍스트까지, tool_use와 tool_result 쌍 포함)을 통째로 제거해 tool_use/tool_result 쌍이 깨지지 않게 한다. 새 발화에서 이전과 **다른** TC ID가 나오면 새 케이스로 보고 기록을 비우고 TC ID를 교체한다. 같은 TC ID가 반복되거나 발화에 ID가 없으면 기존 값을 유지한다. 사용자별 `asyncio.Lock`을 제공해 같은 사용자의 요청을 직렬화한다.
- R2: `app/guard.py`의 `clean_reply(text: str, *, system_text: str, secrets: Iterable[str]) -> str`.
  - 평문화: 마크다운 강조(`**`, `__`, `` ` ``)·제목 `#`·인용 `>`·표 구분·목록 기호(`- `, `* `, `• `, 번호 목록 머리표는 유지하지 않고 문장으로 자연스럽게 남김)를 제거하고, 이모지와 장식 기호를 제거하며, 연속 빈 줄을 한 줄바꿈으로 줄이고 앞뒤 공백을 자른다.
  - 길이: 결과가 480자를 넘으면 문장 경계(`다.`, `요.`, `.`, `?`, `!`, 줄바꿈)에서 480자 이하로 자른다. 경계가 없으면 477자 + `...`. 결과는 항상 500자 이하이고 비어 있지 않다(비면 안전 문구).
  - 유출 차단: 정리된 답에 (a) `secrets`의 8자 이상 값, (b) `aitop100_`로 시작하고 영숫자가 6자 이상 이어진 문자열, (c) `sk-ant-`로 시작하는 문자열, (d) `system_text`와 80자 이상 연속으로 겹치는 부분이 있으면 전체를 안전 문구 `"그 내용은 안내하는 항목이 아니에요. 문의하신 내용을 알려 주시면 도와드릴게요."`로 교체한다.
- R3: `app/media.py`.
  - `async load_image(client: httpx.AsyncClient, url: str) -> dict`: Anthropic 이미지 콘텐츠 블록(`{"type":"image","source":{"type":"base64","media_type":...,"data":...}}`)을 반환. 규칙: `https`만, 호스트가 `localhost`·IP 리터럴이 사설·루프백·링크로컬·예약 대역이면 거부(SSRF 방지), 요청 타임아웃 2초, 응답을 스트리밍으로 읽어 2MB(2,097,152바이트)를 넘으면 중단하고 거부, `Content-Type`이 `image/jpeg|png|webp|gif`가 아니면 거부, 리다이렉트는 따라가지 않는다. 실패는 `MediaError`(사유 코드 포함)이며 URL의 쿼리·토큰을 로그에 남기지 않는다.
  - 음성(`audio` 종류)은 인식하지 않는다. 음성 URL만 있고 텍스트가 비어 있으면 에이전트는 모델 호출 없이 `"음성은 지금 확인하기 어려워요. 내용을 글로 남겨 주시겠어요?"`로 답한다. 텍스트와 함께 오면 텍스트만 처리한다.
- R4: `app/agent.py`의 `Agent`(주입: `anthropic.AsyncAnthropic` 호환 클라이언트, `Settings`, `ActionClient`, 시스템 블록 목록, 도구 정의 목록, `httpx.AsyncClient`).
  - `async run_turn(session, user_text: str, media_urls: list[tuple[str,str]]) -> str`: 사용자 메시지 = 텍스트 블록 + (유효한) 이미지 블록들. 이미지 로드 실패는 해당 이미지만 건너뛰고 텍스트 끝에 `[이미지를 불러오지 못함]`을 덧붙여 모델이 알게 한다(침묵 금지).
  - 호출: `client.messages.create(model=settings.CLAUDE_MODEL, max_tokens=700, system=<시스템 블록>, tools=<도구 정의>, messages=<세션 기록+새 메시지>, output_config={"effort": "low"})`. `thinking`·`temperature`·`tool_choice`·어시스턴트 prefill은 보내지 않는다. 공식 SDK 사용법은 `.claude/skills/claude-api/python/claude-api/README.md`와 `tool-use.md`, `shared/model-migration.md`의 Claude Haiku 5.5 절(`.claude/skills/claude-api/shared/`)을 따르고 확인되지 않은 시그니처를 추측하지 않는다.
  - 루프: `stop_reason == "tool_use"`면 응답의 모든 `tool_use` 블록을 동시에(`asyncio.gather`) 실행하고 **모든 `tool_result`를 하나의 user 메시지**로 돌려준다(결과는 JSON 문자열, `ActionClient.call`의 dict를 그대로, 실패 결과는 `is_error`를 쓰지 않고 `ok:false` 필드로 전달). 최대 6회 반복, 초과하면 안전 문구 `"확인하는 데 시간이 걸리고 있어요. 담당자에게 넘겨 두었으니 잠시 후 다시 말씀해 주세요."`를 반환하고 로그에 사유를 남긴다. `end_turn`이면 텍스트 블록을 이어 붙인 것이 답이다. `refusal`이면 `"이 문의는 제가 바로 답하기 어려워요. 담당자에게 확인해 드릴게요."`. `max_tokens`로 잘렸으면 받은 텍스트를 정리해 쓰고 텍스트가 없으면 안전 문구. 텍스트 블록이 하나도 없으면 안전 문구와 경고 로그.
  - 마지막에 `clean_reply`를 적용한다. 세션에는 정리 전 모델 메시지(도구 블록 포함)를 턴 묶음으로 저장한다.
  - 시간 예산: `run_turn`은 호출자가 넘긴 `asyncio.timeout` 안에서 실행된다. 취소되면 세션에는 그 턴을 `(사용자 텍스트, 안전 안내 문구)`의 단순 쌍으로만 기록해 메시지 교대가 깨지지 않게 한다.
- R5: `app/handler.py`의 `TurnHandler(agent, sessions, settings, http_client)`가 t01의 핸들러 계약(`async __call__(SkillRequest) -> HandlerResult`)을 구현한다.
  - 사용자 락을 잡고 세션을 얻어 발화에서 TC ID를 추출·반영한다.
  - 요청에 `callback_url`이 있으면: `HandlerResult(text="", callback=<코루틴>)`를 돌려 서버가 즉시 `useCallback` 응답을 하게 한다. `callback`은 최대 50초 안에서 `run_turn`을 실행하고 `post_callback`으로 최종 답을 보낸다. 시간 초과나 오류는 안전 문구를 콜백으로 보낸다.
  - `callback_url`이 없으면: `TURN_BUDGET_SECONDS`(기본 4.2) 안에 `run_turn`을 실행해 `HandlerResult(text=답)`을 반환. 초과 시 `"답변을 준비하는 데 시간이 걸리고 있어요. 같은 내용을 한 번 더 보내 주세요."`를 반환한다.
  - 어떤 경우에도 예외 메시지·스택·키 값이 고객 응답에 들어가지 않는다(서버 로그에만).
- R6: 시스템 블록과 도구 정의는 프로세스 시작 시 한 번 만들어 재사용한다(요청마다 재생성·재정렬하지 않는다) — 프롬프트 캐시 적중을 위해 바이트가 변하지 않아야 한다.
- R7: 테스트는 가짜 Anthropic 클라이언트(`messages.create`가 미리 정한 응답을 순서대로 돌려주는 async 객체)와 `httpx.MockTransport`로 한다. 필수 케이스: 단순 텍스트 답, 도구 한 번 호출 후 답, 병렬 `tool_use` 두 개의 결과가 한 user 메시지로 돌아감, 6회 초과 반복, `refusal`, `max_tokens`, 텍스트 블록 없음, TC ID가 도구 호출 헤더로 전달됨과 누락 시 `test_case_id_missing`이 모델에 전달됨, TC ID 전환 시 기록 초기화, 세션 TTL·턴 상한과 tool 쌍 보존, 이미지 블록 구성과 로드 실패 표기, SSRF 거부(사설 IP·`http://`·리다이렉트·2MB 초과·잘못된 MIME), 음성만 있는 요청의 고정 답, 마크다운 제거·480자 절단·유출 차단 네 종류, 시간 초과 처리와 교대 보존, 콜백 경로 정상·실패, 핸들러에서 예외가 고객 문구에 새지 않음, 같은 사용자 동시 요청 직렬화.
- R8: (검토) 보안과 정직성: 이미지 로더의 SSRF·크기 방어가 우회 가능한 경로(DNS 이름이 사설 IP로 해석, URL 사용자정보 `user@host`, 대소문자·유니코드 호스트)에 대해서도 합리적인가, 비밀·시스템 프롬프트가 고객 응답·로그에 나가는 경로가 없는가, 오류가 침묵하지 않고 명시적 안전 문구와 로그로 처리되는가, 세션·락이 메모리 누수 없이 정리되는가.

## Scope and constraints

- 만들 파일: `app/session.py`, `app/guard.py`, `app/media.py`, `app/agent.py`, `app/handler.py`, `tests/test_session.py`, `tests/test_guard.py`, `tests/test_media.py`, `tests/test_agent.py`, `tests/test_handler.py`.
- 사용하는 기존 모듈: t01 `app/config.py`·`app/kakao.py`·`app/server.py`(핸들러 계약), t02 `app/actions.py`(`ActionClient`, `build_tools`), t03 `app/prompt.py`(`build_system_blocks`).
- DNS 해석을 쓰는 SSRF 방어는 `socket.getaddrinfo`를 주입 가능하게 해 테스트에서 가짜로 대체한다(실제 DNS 호출 금지).
- 메타 제약을 모두 따른다. 실제 Anthropic 호출·실제 액션 API 호출·실제 이미지 URL 접근은 테스트에서 하지 않는다.

## Out of scope

- `app/main.py` 조립, Render 설정, 공개 TC 재생(t05). 음성 인식. 영구 세션 저장.
- 프롬프트 내용 변경(t03 소관), 액션 도구 정의 변경(t02 소관).

## Prerequisite inputs

- t01: `Settings`(필드 `claude_api_key`, `bot_auth_token`, `CLAUDE_MODEL`, `TURN_BUDGET_SECONDS`, `SESSION_TTL_SECONDS`, `MAX_SESSION_TURNS` 등), `SkillRequest`(필드: `utterance`, `user_id`, `callback_url`, `media_urls` — 각 항목이 (종류, URL) 쌍), `HandlerResult(text, callback)`, `simple_text_response`, `post_callback`, `create_app`.
- t02: `ActionClient.call(tool_name, args, test_case_id) -> dict`, `build_tools(spec) -> list[ToolDef]`(`name`, `description`, `input_schema`).
- t03: `build_system_blocks() -> list[dict]`.
- 위 필드 이름이 다르면 실제 구현을 읽고 맞추되 계약 의미는 바꾸지 않는다.

<!-- devlyn:verification -->
## Verification

- `python -m pip install -r requirements.txt` 후 다음 pytest를 실행해 모두 통과해야 한다: `tests/test_session.py`(R1), `tests/test_guard.py`(R2), `tests/test_media.py`(R3), `tests/test_agent.py`(R4, R6, R7), `tests/test_handler.py`(R5, R7).
- 전체 `python -m pytest -q`가 통과한다(t01~t03 테스트 포함 회귀 확인).
- R4·R8의 SDK 사용 정확성과 보안은 검토 요구사항으로 확인한다.

## Deliverable and cleanup

- 위 파일을 남긴다. 캐시·임시 파일은 stage하지 않는다.
