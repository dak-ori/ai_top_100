# kakao-cs-agent

「교동마을 산지직송」 고객 상담 AI 에이전트. 카카오 i 오픈빌더 스킬 서버(FastAPI)가 고객 발화를 받으면 Claude Haiku 5.5가 지식 프롬프트와 액션 API 도구(40개)로 답을 만들어 SimpleText 말풍선 1개(500자 이하)로 돌려준다.

```
카카오톡 → 오픈빌더 폴백 블록 → POST /skill → TurnHandler → Agent(Claude + 액션 API 도구) → SimpleText
```

## 로컬 실행

Python 3.12 이상(3.12와 3.14에서 검증).

```sh
python -m venv .venv
.venv\Scripts\activate          # macOS/Linux: source .venv/bin/activate
python -m pip install -r requirements.txt
```

저장소 루트에 `.env` 파일을 만들고 **변수 이름**만 채운다(값은 본인 키. `.env`는 `.gitignore`에 들어 있어 커밋되지 않는다).

```
claude_api_key=
bot_auth_token=
```

- `claude_api_key`: Anthropic API 키. 별칭 `CLAUDE_API_KEY`, `ANTHROPIC_API_KEY`도 읽는다.
- `bot_auth_token`: 액션 API Bearer 토큰. 별칭 `BOT_AUTH_TOKEN`도 읽는다.
- 선택: `CLAUDE_MODEL`(기본 `claude-haiku-5-5`), `TURN_BUDGET_SECONDS`(기본 `4.2`), `SESSION_TTL_SECONDS`, `MAX_SESSION_TURNS`, `ACTION_API_BASE_URL`.
- 환경변수가 `.env`보다 우선한다. 키가 없으면 서버가 기동하지 않고 오류에 변수 **이름**만 나온다(값은 출력하지 않는다).

기동 방식은 **uvicorn `--factory`** 이다. `app/main.py`에는 모듈 수준 `app`이 없고 `build_app()`만 있다. `app = build_app()`처럼 임포트 시점에 앱을 만들면 키 없는 환경(테스트, 임포트)에서 실패하므로, 서버가 시작할 때 `build_app()`을 호출하게 한다. 시스템 블록과 도구 정의는 이때 한 번만 만들고, `httpx.AsyncClient`와 `AsyncAnthropic` 클라이언트도 앱 수명 동안 하나씩만 만들어 종료 때 닫는다.

```sh
python -m uvicorn --factory app.main:build_app --host 127.0.0.1 --port 8000 --workers 1
curl http://127.0.0.1:8000/health
```

**워커는 반드시 1개**다. 세션(대화 이력, 테스트 케이스 ID)이 프로세스 메모리에 있어 워커나 인스턴스가 둘 이상이면 같은 고객의 대화가 끊긴다.

## 테스트

```sh
python -m pytest -q
```

모든 테스트는 네트워크와 실제 키 없이 통과한다(`httpx.MockTransport`와 가짜 Anthropic 클라이언트 주입). 실제 호출은 아래 수동 스크립트에서만 한다.

## 공개 TC 재생 (`scripts/run_open_tc.py`)

```sh
python scripts/run_open_tc.py --dry-run                 # 키·네트워크 없이 케이스, 고객 문의, 기대 API 번호를 JSON으로 출력
python scripts/run_open_tc.py --live                    # 실제 호출. 오너가 키를 넣은 환경에서 수동 실행
python scripts/run_open_tc.py --live --only L-03 L-05   # 일부 케이스만
python scripts/run_open_tc.py --live --reset-first      # 시작 전에 액션 데이터를 한 번 초기화
```

> **`--live` 경고 — 비용과 데이터 변경**
> - 케이스마다 실제 Claude 호출이 일어난다(한 턴에 약 1센트, 키 소유자에게 청구).
> - 실제 액션 API를 호출하므로 환불·배송지 변경·에스컬레이션 같은 **접수가 해당 TC ID로 기록된다**. 이미 데이터를 바꾼 케이스를 다시 재생하면 모범 응대와 다른 응답이 나올 수 있다.
> - 시작 전에 초기화를 **묻지 않는다**. `--reset-first`를 줄 때만 `POST /api/action-data/reset`을 정확히 한 번 호출하고, 이것은 내 토큰의 **모든** TC 데이터를 처음 상태로 되돌린다. 이 스크립트가 reset을 호출하는 유일한 코드이며 `app/` 안에는 reset 호출이 없다(에이전트 도구로도 노출되지 않는다).

- 케이스마다 새 사용자 id로 첫 발화(`id: <케이스 ID>` + 인입 계정 + 첨부 URL + 고객 문의)만 `TurnHandler`에 보낸다.
- 케이스별로 실제 호출한 `API-NN`, 답 글자 수, 소요 시간을 출력하고 세 가지를 PASS/FAIL로 판정한다: ① 기대 대응에 나온 `API-NN`을 모두 호출했는가 ② 쓰기 API(API-22~40 중 접수류, 에스컬레이션 35 제외)를 확인 없이 호출하지 않았는가 ③ 답이 500자 이하인가.
- 첫 발화 한 번만 보내므로, 고객 확인을 받은 뒤에 접수하는 케이스(예: L-03의 API-24)는 에이전트가 올바르게 되물으면 ①이 FAIL로 나온다. 줄별 사유(`missing`, `called`)를 읽고 판단하고, 여러 턴이 필요한 케이스는 카카오톡에서 이어서 확인한다.
- 공개 TC 파일(`knowledge/open_cases.md`)에는 케이스가 15건 있고, 그중 L-20(주입 방어)과 Y-24(판매자 정보)는 기대 대응에 `API-NN`이 없다. 이 둘은 ①이 비어 있는 채로 통과한다.

## Render 배포

설정은 `render.yaml`(Blueprint)에 있다: 웹 서비스 1개, `plan: starter`(상시 실행, 절전 없음), `healthCheckPath: /health`, `startCommand`는 `uvicorn --factory app.main:build_app --host 0.0.0.0 --port $PORT --workers 1`, Python 3.12.

1. Render 대시보드에서 이 GitHub 저장소를 연결한다.
2. **New → Blueprint**에서 저장소를 고르면 `render.yaml`을 읽어 서비스를 만든다.
3. 환경변수 두 개를 대시보드에 입력한다: `claude_api_key`, `bot_auth_token`(`sync: false`라 값이 저장소에 없다).
4. 배포가 끝나면 `/health`가 200이 될 때까지 기다린 뒤 스모크 스크립트를 실행한다.

```sh
python scripts/smoke_skill.py https://<서비스>.onrender.com
```

`/health` 상태 코드와, `/skill`에 카카오 형식 페이로드("안녕하세요 배송 문의드려요")를 보냈을 때의 상태 코드, 응답 형식(SimpleText 1개, 500자 이하), 소요 시간을 출력한다. 하나라도 어긋나거나 5초를 넘으면 종료 코드 1이다. 이 호출은 실제 Claude 턴 1회(약 1센트)를 쓴다.

`autoDeploy: true`이므로 연결한 브랜치에 푸시하면 다시 배포된다. 심사 기간(제출 후)에는 수정·재배포하지 않는다.

## 카카오 오픈빌더 연결

1. 스킬 → 새 스킬을 만들고 **스킬 URL**을 `https://<서비스>.onrender.com/skill`로 지정한다.
2. 시나리오의 **폴백 블록**에 이 스킬을 연결하고 응답 방식을 스킬 데이터로 한다. 모든 발화가 폴백으로 들어와 서버가 답한다.
3. **콜백 사용 여부**를 정한다. 스킬 서버의 응답 한도는 **5초**다. 5초를 넘기면 카카오가 오류를 낸다.
   - 콜백을 켜면 요청에 `callbackUrl`이 오고, 서버는 즉시 `useCallback` 응답(대기 문구 1개)을 보낸 뒤 최종 답을 콜백으로 보낸다(최대 약 50초). 대기 문구가 말풍선 1건으로 세어질 수 있어 심사 지표에는 불리하다.
   - 콜백을 끄면 서버가 `TURN_BUDGET_SECONDS`(4.2초) 안에 단일 응답을 목표로 하고, 넘기면 짧은 안내문("같은 내용을 한 번 더 보내 주세요")을 돌려준다. 말풍선 수를 최소로 하려면 콜백을 끄고 이 방식을 쓴다.
4. 봇을 배포하고, 평가 계정이 챗봇에 접근할 수 있게 한다.

체크리스트:

- [ ] 스킬 URL이 `https://<서비스>.onrender.com/skill`이고 오타가 없다.
- [ ] 폴백 블록이 스킬에 연결되어 있고 다른 블록이 발화를 가로채지 않는다.
- [ ] 콜백 설정이 의도한 대로다(위 3번).
- [ ] 평가 계정으로 봇에 말을 걸어 답이 온다.
- [ ] 봇 배포가 끝났고 Render 서비스가 Live이며 `smoke_skill.py`가 통과했다.
- [ ] `claude_api_key`, `bot_auth_token`이 Render에 입력되어 있다.
- [ ] 제출 이후에는 서비스와 봇을 수정하지 않는다.

## 시크릿 취급 규칙

- 키 값은 환경변수(Render 대시보드, 로컬 `.env`)로만 읽는다. 코드, 테스트, 문서, 로그, 커밋에 쓰지 않는다.
- `.env`, `api_key.md`, `docs/competition/api.md`는 읽어서 출력하거나 stage하지 않는다.
- 설정 오류 메시지는 변수 이름만 말한다. 로그에는 키와 토큰을 남기지 않는다.
- reset(API-41)은 에이전트 도구가 아니다. 데이터 초기화는 오너가 `run_open_tc.py --live --reset-first`로만 한다.

## 통합 중 수정

- 발견한 결함 없음. t01~t04 코드는 수정하지 않았다.
