---
id: t05
loop_id: kakao-cs-agent
title: Assembly, Render deploy and open-TC replay
kind: feature
review_requirements: [R6, R7]
---

## Context and goal

앞 task의 부품을 하나의 서비스로 조립하고(`app/main.py`), Render에 상시 실행으로 배포할 수 있게 설정하며, 공개 TC 13건을 재생·점검하는 수동 검증 스크립트와 심사 제출용 디브리프 초안을 만든다. 이 task가 조립 제품 전체의 통합 검증이다. 실제 키로 하는 호출은 오너가 수동으로 한다.

## Requirements

- R1: `app/main.py`가 `app = build_app()`을 노출한다. `build_app()`은 `Settings`를 읽고 `validate()`로 필수 키를 확인한 뒤(누락 시 변수 이름을 말하는 오류로 기동 실패 — 비밀 값은 출력하지 않음), `build_system_blocks()`·`build_tools()`를 한 번 만들어 `Agent`·`SessionStore`·`TurnHandler`를 조립하고 `create_app(handler)`를 반환한다. `httpx.AsyncClient`와 `AsyncAnthropic`은 앱 수명(lifespan) 동안 하나만 만들고 종료 시 닫는다. 키가 없는 환경에서도 `from app.main import build_app`은 임포트가 성공해야 하고(검증이 `build_app()` 호출 시점에만 일어남) `app.main:app`이 필요한 uvicorn 기동은 키가 있을 때만 시도한다. 이를 위해 모듈 수준 `app`은 지연 생성 방식(예: `def app(...)` ASGI 래퍼 또는 `uvicorn --factory app.main:build_app`)을 쓰고 선택한 방식을 `README.md`에 적는다.
- R2: `requirements.txt`가 정확한 버전으로 고정되어 있고 Python 3.12와 3.14 모두에서 설치 가능하며 `render.yaml` 검증용 `PyYAML`(두 버전 모두 휠 또는 순수 파이썬 설치가 되는 고정 버전)을 포함한다. `render.yaml`(Render Blueprint)에 웹 서비스 하나: `runtime: python`, `plan: starter`(상시 실행·절전 없음), `autoDeploy: true`, `healthCheckPath: /health`, `buildCommand: pip install -r requirements.txt`, `startCommand`: uvicorn으로 R1의 방식에 맞게 `--host 0.0.0.0 --port $PORT --workers 1`, 환경변수: `PYTHON_VERSION`(3.12.x), `claude_api_key`·`bot_auth_token`은 `sync: false`(값은 대시보드에서 입력), `CLAUDE_MODEL=claude-haiku-5-5`, `TURN_BUDGET_SECONDS=4.2`. 값에 비밀이 들어 있지 않다. 워커는 반드시 1개(메모리 세션).
- R3: `scripts/run_open_tc.py`: `knowledge/open_cases.md`(또는 `--file`)에서 공개 TC 13건(헤더 `# L-03` 등 `#`로 시작하는 케이스 ID, `case metadata`의 인입 일시·계정, `###### 고객 문의` 인용문, `###### 기대 대응`의 번호 목록)을 파싱한다. 옵션 `--dry-run`은 네트워크·키 없이 케이스별 ID, 고객 문의, 기대 대응에 나온 `API-NN` 번호 목록을 JSON으로 출력한다(13건 전부 비어 있지 않아야 함). 옵션 `--live`는 환경변수 키로 실제 `Agent`를 구성해 케이스마다 새 사용자 id와 TC ID를 넣은 첫 발화(`case metadata`의 `id: <ID>` + 인입 계정 + 고객 문의)로 `TurnHandler`를 호출하고, `ActionClient.calls`에서 실제 호출한 `API-NN` 목록, 최종 답(글자 수), 소요 시간을 출력하며 기대 `API-NN` 번호(중복 제거)를 모두 호출했는지·쓰기 API(API-22~40 중 접수류)를 확인 없이 호출하지 않았는지·답이 500자 이하인지를 PASS/FAIL로 요약한다. `--live`는 시작 전에 데이터 초기화를 할지 묻지 않고, 오너가 `--reset-first` 옵션을 줄 때만 `POST /api/action-data/reset`을 한 번 호출한다(이 스크립트가 reset을 호출하는 유일한 코드 경로이며 `app/` 안에는 없다). 실제 호출 비용·데이터 변경 경고를 `--help`와 README에 쓴다.
- R4: `scripts/smoke_skill.py URL`: 카카오 형식 페이로드(발화 "안녕하세요 배송 문의드려요", 임의 사용자 id)를 `URL`의 `/skill`로 POST하고 `/health`도 확인해 상태 코드·응답 형식(SimpleText 1개, 500자 이하)·소요 시간을 출력한다. 서버 쪽은 이 스크립트가 쓰는 것과 같은 계약만 만족하면 된다.
- R5: `README.md`: 로컬 실행(venv, `.env` 변수 이름만), 테스트, Render 배포 절차(저장소 연결 → Blueprint → 환경변수 두 개 입력 → 배포 후 `smoke_skill.py` 실행), 카카오 오픈빌더 연결 절차(스킬 URL = `https://<서비스>.onrender.com/skill`, 폴백 블록을 스킬로 연결, 콜백 사용 여부와 5초 한도의 의미, 평가 계정 접근·배포 확인 체크리스트), 시크릿 취급 규칙. 비밀 값은 쓰지 않는다.
- R6: (검토) 조립이 올바르다: 시스템 블록·도구 정의가 요청마다 재생성되지 않고, 클라이언트 수명이 맞고, 키 누락이 명시적으로 실패하며, `render.yaml`이 상시 실행·단일 워커·헬스체크를 만족하고, 스크립트가 reset을 안전하게(옵션이 있을 때만, 한 곳에서만) 다룬다.
- R7: (검토) `docs/debrief-draft.md`: 대회 제출 디브리프 가이드의 다섯 질문에 대한 한국어 초안(각 항목 4~8문장). 사실에 근거해야 한다. 근거: `C:\github\ai_top_100\docs\competition\HANDOFF.md`와 각 `*_문서요약.md`(인터뷰한 관계자 7명: 이민준·조합장 고길동·보라·구방만·김청년·홍이장·조억만, 확보한 정본 문서 목록, 연락처를 못 받은 박수복 어르신 등 미확보 항목), 설계(지식 프롬프트 + 액션 API 도구 호출, 증빙 서열·레인 판정·권한 한도 규칙, 방어 규칙), 검증(단위 테스트, 공개 TC 13건 재생, 한계). 다음 질문에 각각 답한다: 강점(어떤 상황을 잘 처리하도록 설계했고 이유), 정보 획득 방식, 검증 방식, 주요 설계·구현, 아쉬운 점과 한계(음성 인식 불가, 세션 메모리 유지, 5초 한도와 콜백 의존, 미확보 문서 — 작황·재해 기록, SKU 카탈로그, 가격 정책 시트, 인플루언서 계약서). 아직 하지 않은 일(실제 TC 통과 수치 등)을 한 것처럼 쓰지 않고, 수치가 필요한 곳은 `[오너 확인: …]` 자리표시자로 둔다.

## Scope and constraints

- 만들 파일: `app/main.py`, `render.yaml`, `scripts/run_open_tc.py`, `scripts/smoke_skill.py`, `README.md`, `docs/debrief-draft.md`, `tests/test_main.py`, `tests/test_scripts.py`. `requirements.txt` 보강만 허용.
- `docs/debrief-draft.md`는 `docs/` 아래에 있지만 `docs/competition/`(untracked 원자료)은 건드리지 않는다. `docs/specs/`(이 패키지)도 건드리지 않는다.
- 메타 제약을 모두 따른다. 이 task의 자동 검증은 키·네트워크 없이 통과해야 한다(`--dry-run`과 가짜 주입).

## Out of scope

- 실제 Render 배포 실행, 카카오 오픈빌더 설정, 실제 키로 `--live` 실행(오너가 수행).
- 지식·프롬프트·에이전트 로직 변경(앞 task 소관). 통합 중 앞 task의 결함을 발견하면 최소 수정으로 고치고 어떤 결함이었는지 `README.md` 끝 `## 통합 중 수정` 절에 한 줄씩 남긴다.

## Prerequisite inputs

- t01~t04의 모든 모듈과 테스트, `knowledge/open_cases.md`(공개 TC 13건).
- 공개 TC 형식: `# L-03` 헤더, `case metadata`(`- id: L-03`, `- 상담 인입 일시`, `- 인입창구`, `- 인입 계정: 회원 로그인 · 서지호 · 019-****-5231`), `###### 고객 문의`의 `>` 인용문, `###### 기대 대응`의 번호 목록(각 항목에 `API-NN` 표기).

<!-- devlyn:verification -->
## Verification

- `python -m pip install -r requirements.txt`, 전체 `python -m pytest -q`(앞 task 회귀 포함) 통과.
- `python -m pytest -q tests/test_main.py` — 키 없이 임포트 성공, 키 없이 `build_app()`이 변수 이름을 말하는 오류(값 없음), 가짜 키 환경에서 `build_app()`이 성공하고 `GET /health` 200, `POST /skill`이 가짜 Anthropic으로 SimpleText 1개를 반환(R1, R2 일부).
- `python scripts/run_open_tc.py --dry-run`이 케이스 13개를 출력하고 종료 코드 0, 각 케이스에 `API-` 번호가 하나 이상(R3).
- `python -m pytest -q tests/test_scripts.py` — 파서가 13건을 정확히 읽고, `--live` 요약 로직(PASS/FAIL 판정, reset 옵션 처리)이 가짜 주입으로 맞고, `smoke_skill.py`가 가짜 서버 응답을 검증(R3, R4).
- `python -c`로 `render.yaml`을 YAML로 읽어 R2의 항목(상시 실행 plan, healthCheckPath, workers 1, 비밀 키 `sync: false`, 값에 비밀 패턴 없음)을 확인하고, `README.md`에 R5의 절이 있음을 확인한다.
- R6·R7은 검토 요구사항으로 확인한다.

## Deliverable and cleanup

- 위 파일을 남긴다. 가상환경·캐시·임시 출력은 stage하지 않는다.
