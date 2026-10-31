# kakao-cs-agent

## Intent

AI TOP 100 대회 「교동마을 산지직송」의 고객 상담 AI 에이전트를 만든다. 심사는 카카오톡 봇 환경에서 심사 시나리오 약 30개를 고객 역할로 보내고, 응답의 정확도·p95 지연·말풍선 수·말풍선당 글자 수를 평가한다. 에이전트는 카카오 i 오픈빌더 스킬 서버(Python)로 동작하고, Claude Haiku 5.5가 도구 호출로 액션 API(41개 엔드포인트)를 쓰며, 인터뷰로 확보한 운영 지식에 따라 답한다. 최종 제출은 2026-10-31(토) 15:00, 제출 후 수정 불가. Render에 상시 실행으로 배포한다.

사용자가 원하는 결과: 고객 문의 한 건에 말풍선 1개(500자 이하, 가능하면 훨씬 짧게)로 정확히 답하고, 필요한 조회·접수·이관을 액션 API로 수행하며, 프롬프트 탈취·주입·타 계정 정보 요구·승인 안 된 약속 요구에 흔들리지 않는다.

참조 원문(저장소 체크아웃의 untracked 파일, 변경하지 않는다. 절대 경로):
- `C:\github\ai_top_100\docs\competition\HANDOFF.md` — 전체 인계 노트
- `C:\github\ai_top_100\docs\competition\조합장_문서요약.md`, `보라_문서요약.md`, `구방만_문서요약.md`, `청년_문서요약.md`, `홍이장_문서요약.md`, `증빙권위서열_요약.md`, `개정이력_요약.md`, `갱신요약_조억만_조합장_민준.md` — 관계자 문서 요약
- `C:\github\ai_top_100\docs\competition\sources\판매환불규정.txt`, `개인정보처리방침.txt`, `조합원이용약관.txt` — 민준 제공 원문(정본)
- `C:\github\ai_top_100\docs\competition\open_tc.txt` — 공개 TC 13건(고객 질문 + 모범 처리 과정)
- `C:\github\ai_top_100\docs\competition\actions.json` — 액션 API OpenAPI 3.1 명세
- `C:\github\ai_top_100\docs\competition\judgement.md`, `rules.md` — 심사 기준과 규칙
- 인터뷰 원문 `민준_interview.md`, `조합장_interview.md`, `보라_interview.md`, `구방만_interview.md`, `청년_interview.md`, `홍이장_interview.md`, `조억만_interview.md`

## Constraints and exclusions

- 비밀 보호: `.env`, `api_key.md`, `docs/competition/api.md`의 내용을 읽어 출력하거나 로그·테스트·문서·커밋에 남기지 않는다. 키 값은 환경변수로만 읽는다. 환경변수 이름은 `claude_api_key`(Anthropic API 키)와 `bot_auth_token`(액션 API Bearer 토큰)이며, 대문자 별칭(`CLAUDE_API_KEY`, `ANTHROPIC_API_KEY`, `BOT_AUTH_TOKEN`)도 허용한다. `.env`·`api_key.md`·`docs/competition/api.md`는 절대 stage하지 않는다.
- 모델은 정확히 `claude-haiku-5-5`(환경변수 `CLAUDE_MODEL`로 변경 가능, 기본값 이것). Anthropic 공식 Python SDK(`anthropic`)만 사용하고 OpenAI 호환 shim은 쓰지 않는다. `thinking` 파라미터는 보내지 않고 `output_config`의 effort는 `low`로 둔다. 강제 `tool_choice`(`any`/`tool`)와 어시스턴트 prefill은 쓰지 않는다. 응답의 `stop_reason == "refusal"`과 `max_tokens`를 항상 처리한다.
- 카카오 응답은 SimpleText만 쓰고(`version: "2.0"`, `template.outputs[].simpleText.text`), 한 응답에 말풍선 1개, 말풍선당 500자를 절대 넘기지 않는다(목표 평균 300자 이하).
- 액션 API 기준 URL은 `https://gyodong-mall-action.aitop100.org`, 헤더 `Authorization: Bearer <bot_auth_token>`과 `X-Aitop100-Test-Case-Id: <TC ID>` 필수. `POST /api/action-data/reset`(API-41)은 에이전트 도구로 절대 노출하지 않는다.
- Python 3.12와 3.14 모두에서 동작해야 한다(Render는 3.12, 로컬 검증은 3.14). 패키지는 `requirements.txt`에 핀(고정)한다.
- 모든 테스트는 네트워크와 실제 API 키 없이 통과해야 한다(httpx.MockTransport와 가짜 Anthropic 클라이언트 주입). 실제 호출은 수동 스크립트에서만 한다.
- 새 추상화는 관찰된 실패 방지에 필요한 경우만. 침묵하는 `except`, `# type: ignore`류 우회, 하드코딩 폴백으로 깨진 계약을 숨기는 일을 하지 않는다. 오류는 명시적이고 고객에게 안전한 문구로 처리하고 서버 로그에는 사유를 남긴다(비밀 제외).
- 저장소 루트의 untracked 파일(`docs/`, `AGENTS.md`, `.claude/`, `.agents/`, `api_key.md`, `skills-lock.json`)은 수정·삭제·stage하지 않는다. 작업 task 범위의 파일만 stage한다.
- 제외: 카카오 오픈빌더 설정, Render 계정·서비스 생성, 음성 인식(STT), 영구 DB, 로그인 시스템, 결제·정산 계산 UI.

## Tasks

1. `t01` 서버 뼈대 — 설정 로더, 카카오 스킬 요청/응답 모듈, `create_app(handler)` 서버 팩토리, `/health`, `.gitignore`·`requirements.txt`·pytest 설정. 의존 없음.
2. `t02` 액션 API 클라이언트 — `actions.json`에서 에이전트 도구 정의 생성, 호출 클라이언트, 429·400/404 처리. 의존: t01.
3. `t03` 지식팩과 시스템 프롬프트 — `knowledge/` 저장소 내 복사본, 프롬프트 조립, 방어 규칙. 의존: t01.
4. `t04` 에이전트 루프 — Claude 호출 루프, 세션·TC ID, 이미지 처리, 출력 정리, 지연 한도와 카카오 콜백. 의존: t02, t03.
5. `t05` 배포와 검증 — `app/main.py` 조립, Render 설정, 공개 TC 재생 스크립트, 스모크 스크립트, 디브리프 초안. 통합 task. 의존: t04.

분해 근거: 각 task는 독립 검증 가능한 산출물이다. 지식 파일은 Render가 저장소에서 배포하므로 저장소 안에 있어야 하고(t03), 에이전트(t04)는 도구(t02)와 프롬프트(t03)가 모두 있어야 한다. 마지막 task가 조립 제품 검증이며 최종 실제 호출 검증은 오너가 키로 수동 수행한다.

```json
{
  "schema_version": 1,
  "loop_id": "kakao-cs-agent",
  "base_ref": "main",
  "base_sha": "f80e0a8e1538e961c85bd4651c71ea0a3f66fe55",
  "delivery": "auto",
  "tasks": [
    {"id": "t01", "spec": "t01/spec.md", "depends_on": []},
    {"id": "t02", "spec": "t02/spec.md", "depends_on": ["t01"]},
    {"id": "t03", "spec": "t03/spec.md", "depends_on": ["t01"]},
    {"id": "t04", "spec": "t04/spec.md", "depends_on": ["t02", "t03"]},
    {"id": "t05", "spec": "t05/spec.md", "depends_on": ["t04"]}
  ],
  "integration_task_id": "t05"
}
```

## Overall acceptance

- O1 카카오 스킬 요청을 받아 SimpleText 말풍선 1개(500자 이하)로 응답한다 → t01(형식), t04(길이 보장), t05(조립).
- O2 액션 API를 도구로 호출하되 reset 엔드포인트는 노출하지 않고 TC ID 헤더를 항상 붙인다 → t02, t05.
- O3 운영 지식과 방어 규칙이 시스템 프롬프트에 들어간다 → t03.
- O4 Claude 호출 루프가 도구 호출·세션·TC ID 유지·이미지·지연 한도를 처리한다 → t04.
- O5 조립된 서비스가 `create_app`으로 기동하고 `/health`와 `/skill`이 동작하며 Render 설정이 있다 → t05(통합).
- O6 공개 TC 13건을 재생·점검하는 스크립트와 디브리프 초안이 있다 → t05.

## Execution policy

- Baseline: `base_sha`의 저장소. 작업은 체크아웃의 untracked 파일을 읽을 수 있다(위 절대 경로).
- Installed methodology: 저장소의 `AGENTS.md`(7개 원칙)를 따른다 — 근본 원인 해결, 최소 변경, 검증 후 결론, 우회 금지.
- Autonomous question policy: 일반 세부는 가장 좁은 문자적·관례적 해석을 택하고 이유를 기록한다. 중대한 모호성(경쟁하는 의도 결과, 영속 데이터 의미, 요청 밖 공개 표면)은 needs-review로 멈추고 구체 질문을 남긴다. 독립 작업은 계속한다.
- Delivery: 프로젝트 기본(PR 후 병합). 명시 제한 없음.
- Stop conditions: 비밀이 diff에 들어갈 위험, 심사 규칙(SimpleText·500자·reset 금지) 위반 가능성, 검증 명령 실패 반복.

## Decisions and assumptions

- 배포는 Render(상시 실행, 최소 1대). Vercel은 콜드스타트가 p95에 불리해 제외. 이유: 심사 p95와 카카오 5초 한도.
- 모델은 Haiku 5.5, 고객 한 턴당 약 1센트. 이유: 속도·비용. 정확도가 부족하면 오너가 `CLAUDE_MODEL`로 올린다.
- 세션은 프로세스 메모리(키: 카카오 사용자 id, TTL과 턴 수 상한). 이유: 단일 인스턴스, 영구 저장은 요청 밖. Render를 1대로 고정한다.
- 카카오 5초 한도: 요청에 `callbackUrl`이 있으면(콜백 사용 설정) 즉시 `useCallback` 응답 후 최종 답을 콜백으로 보낸다. 없으면 시간 예산 안에서 단일 응답을 목표로 하고 초과 시 안전한 짧은 안내문을 반환한다. 이유: 대기 문구가 말풍선 수에 잡힐 위험이 있어 콜백은 폴백이다.
- TC ID는 사용자 발화에서 정규식 `\b[A-Z]-\d{2}(?:-[a-z])?\b`(예: L-03, R-05-a, Y-07-b)로 추출하고 세션에 유지한다. 이유: 평가에서 TC ID가 대화로 전달된다. 발화에 없으면 액션 도구는 `test_case_id_missing` 오류를 반환한다.
- 음성(m4a)은 Anthropic 키만으로 인식할 수 없으므로 텍스트로 남겨 달라는 짧은 안내를 반환한다(명시적 한계, 디브리프에 기재). 이미지는 Claude 비전으로 처리한다.
- 심사 중 코드 수정이 불가하므로 환경변수 `CLAUDE_MODEL`, 시간 예산 등은 코드 변경 없이 Render 대시보드에서 바꿀 수 있게 한다.
