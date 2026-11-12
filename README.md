# 미니 PJT: TEMPO (마라톤 훈련 조정 코치)

## 무엇을 푸나
정해진 훈련 계획표가 야근·수면 부족·놓친 훈련 같은 실제 컨디션을 반영하지 못해, 매주 "이번 주를 어떻게 바꿔 뛸지"를 러너 혼자 감으로 판단해야 하는 문제를 푼다. 질문 한 번에 내 기록 수치, 훈련 이론 근거와 출처, 요일별 조정안을 함께 답한다. 서비스 스펙은 [SERVICE.md](SERVICE.md)에 있다.

## 활용한 패턴 (Day 1~7)
- Day 1: LCEL chain + Pydantic 구조화 출력 — 답변에서 "제시된 주간 계획"을 `ExtractedPlan` 으로 추출해 주간 거리 한도를 코드로 검사한다 (`src/agent.py` 의 `output_guard`). 평가 심사 결과도 구조화 출력으로 받는다.
- Day 2: RAG — 직접 쓴 훈련 이론 문서 13종을 소제목 단위로 청킹해 Titan 임베딩과 Chroma에 색인한다. 벡터 검색과 하이브리드 검색(BM25 + 벡터)을 설정으로 바꿀 수 있다.
- Day 3: ReAct — 서브 에이전트 3종이 각자 도구를 스스로 골라 호출한다.
- Day 4: 도구 다중 — 한 질의에 기록 조회, 문서 검색, 페이스 계산, 계획 검사 도구가 결합된다 (총 9개).
- Day 4: MCP 서버 연동 — 러너 데이터(프로필, 계획표, 기록, 주간 집계, 컨디션, 지난 코치 피드백)를 fastmcp 서버로 노출하고 에이전트가 MCP 클라이언트로 호출한다.
- Day 5: 가드레일 — 입력 단계에서 의료 진단(G1), 프롬프트 인젝션(G3), 타인 정보(G4)를 규칙으로 차단하고, 출력 단계에서 주간 거리 한도(G5)를 코드로 검사한다. 첨부 이미지는 종류를 구조화 출력으로 판정해 러닝과 무관하면 G6으로 거절한다.
- Day 6: Multi-Agent Supervisor — 기록 담당, 이론 담당, 계획 담당으로 역할을 나누고 Supervisor가 위임과 종합을 맡는다.
- Day 7: Observability · Trace — LangSmith 트레이스, 질의별 로컬 JSONL 기록, API 응답의 `trace` 필드.
- Day 7: 평가 — 인-아웃 세트를 LLM-as-Judge와 코드 검사로 판정한다. RAGAS는 2차 평가에서 추가한다.
- HITL — 에이전트가 낸 주간 조정안은 바로 반영하지 않는다. LangGraph `interrupt` 로 승인 그래프를 멈추고, 사용자가 반영 범위(이 날만 / 이번 주 전체 / 다음 주까지)를 골라 승인하면 `Command(resume=...)` 로 이어서 반영한다. '다음 주까지'를 고르면 이번 주를 저장한 뒤 에이전트가 다음 주를 다시 짜고 한 번 더 승인을 받는다 (`src/approval.py`).
- 장기 메모리 — 사용자가 저장한 코치 피드백과 승인한 조정안을 파일에 남기고, 기록 담당이 `get_coach_feedback` 으로 다시 읽어 다음 질문에서 이전 조언과 이어지게 답한다.
- 멀티모달 입력 — 시계·앱 기록 화면이나 계획표 사진을 첨부하면 에이전트 모델이 보이는 수치를 글로 옮기고, 그 글이 입력 가드레일부터 같은 경로로 처리된다. 달리는 영상(30MB 이하)을 첨부하면 OpenCV 로 가운데 3초 구간에서 프레임 8장을 뽑아 한 번에 보여 주고 자세(상체, 팔치기, 착지 위치 등)를 관찰한다. 이론 담당이 자세 원칙 문서(`13_running_form.md`)로 근거를 단다 (`src/vision.py`).

적용하지 않은 패턴: 미들웨어, Plan-Execute.

## 아키텍처

```
사용자 질문 (+ 선택: 첨부 이미지·영상 ── vision.py 가 보이는 수치나 자세를 글로 옮겨 질문 뒤에 붙임)
   │
   ▼
입력 가드레일 (guards.py) ── G1 의료 / G3 인젝션 / G4 타인 정보 / G6 무관한 이미지 ──▶ 고정 거절 문구
   │ 통과
   ▼
Supervisor (agent.py) ── 한 번에 한 담당에게 위임하고 결과를 종합
   ├─ data_agent    ── MCP 서버 (mcp_server.py) ── data/runner/*.json
   │                   get_runner_profile, get_planned_workouts, get_activities,
   │                   get_weekly_summary, get_condition,
   │                   get_coach_feedback (장기 메모리: 저장한 피드백·승인한 조정안)
   ├─ theory_agent  ── retrieve_training_docs ── Chroma ── data/docs/*.md (13종)
   └─ plan_agent    ── calc_pace, check_week_plan
   │
   ▼
출력 가드레일 ── 답변 속 주간 계획을 구조화 추출 ── G5 주간 거리 한도 검사 (코드)
   │
   ▼
승인 요청 (approval.py) ── 조정안이 있으면 interrupt 로 멈춰 사용자 승인 대기 ── POST /approve 로 재개
   │
   ▼
{answer, contexts, trace}   ◀── 콜백(tracer.py)이 도구 호출과 검색 문서를 수집
```

- 기준일은 2026-11-09 로 고정돼 있다 (가짜 데이터가 고정이므로 "오늘"도 고정).
- 모델: 에이전트 `claude-sonnet-4-6`, 계획 추출 `claude-haiku-4-5`, 심사 `claude-sonnet-4-5`, 임베딩 `titan-embed-text-v2` (모두 Amazon Bedrock).
- 모델·리전 선택: Bedrock 일일 토큰 한도는 모델·리전마다 따로 찬다. 화면의 채팅 위 선택 상자나 `/query` 의 `model`, `region` 으로 요청마다 바꿀 수 있다 (Claude Sonnet·Haiku, Amazon Nova, gpt-oss 13종, us-east-1·us-west-2). (모델, 리전)마다 그래프를 처음 쓸 때 한 번 조립해 재사용한다. 한도가 차면 `/query` 가 429 와 함께 다른 모델·리전을 고르라고 알려 준다. 고를 수 있는 목록은 `GET /models`.

## 실행 방법

Python 3.14 와 Amazon Bedrock 자격 증명이 필요하다.

```bash
python -m venv .venv
.venv\Scripts\activate            # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
```

프로젝트 루트에 `.env` 를 만든다.

```
AWS_ACCESS_KEY_ID=...
AWS_SECRET_ACCESS_KEY=...
AWS_DEFAULT_REGION=us-east-1
# 선택: LangSmith 트레이스
LANGSMITH_TRACING=true
LANGSMITH_API_KEY=...
LANGSMITH_PROJECT=runcoach
```

데이터와 색인을 만들고 서버를 띄운다.

```bash
python -m src.make_data                      # 가짜 러너 데이터 생성 (고정 난수)
python -m src.retriever --build              # 문서 색인 (72개 청크)
python -m uvicorn src.api:api --port 8000    # API 서버 + 웹 화면
```

- 주간 훈련 화면: http://localhost:8000 (훈련 카드를 누르면 그 훈련에 대해 묻고, 답변을 그 날짜의 코치 피드백으로 저장)
- 확인용 화면: http://localhost:8000/debug (예시 질문 18건, 근거 문서, 실행 과정)
- API:

```
POST /query
Content-Type: application/json
{"question": "지난주 일요일 장거리를 못 뛰었어. 이번 주 계획을 어떻게 조정하면 좋을까?"}

→ {"answer": "...", "contexts": [{"doc_id": "...", "text": "..."}], "trace": [{"step": "...", "input": "...", "output": "..."}], "meta": {...}}
```

- 선택 항목 `image`: `data:image/png;base64,...` 형식의 첨부 이미지 (최대 3.5MB). 주면 판독 결과가 trace 의 `image_reader` 단계에 남는다.
- 선택 항목 `video`: `data:video/mp4;base64,...` 형식의 주행 영상 (최대 30MB). 프레임 관찰 결과가 trace 의 `video_reader` 단계에 남는다.
- 답변에 주간 조정안이 있으면 `meta.approval.thread_id` 가 온다. `POST /approve {"thread_id": "...", "approved": true, "scope": "day|week|through_next", "day": "2026-11-10"}` 로 승인하면 고른 범위만 반영되고, 거절하면 버린다. `through_next` 면 다음 주 조정 답변과 새 승인 요청이 `next` 로 함께 온다.

- 자체 평가: `python -m evaluation.run_inout --round 2 --repeat 3` (3회 실행, 케이스별 다수결)
  - 한도가 찼을 때: `--model global.anthropic.claude-sonnet-4-6 --judge-model global.anthropic.claude-sonnet-4-5-20250929-v1:0` 처럼 같은 모델의 global 프로필이나 `--region us-west-2` 로 바꾼다. 심사 모델은 1차와 같은 모델만 허용한다.
- 컨디션 대조 평가: `python -m evaluation.run_condition` (같은 기록에서 컨디션만 바꾼 2개 조건 x 질문 2개 x 3회)
- RAGAS: `python -m evaluation.run_ragas --round 2` (정답은 `evaluation/ragas_references.csv`, 한도가 찼으면 `--model` · `--region`)
- 한 건만 터미널에서: `python -m src.agent "요즘 나 어때?"`
- `./run.sh` 는 위 세 단계를 한 번에 실행한다.

## RAGAS 평가 결과
문서 검색이 쓰이는 5건(1, 2, 7, 8, 18번), 2차 평가 첫 회차 답변 기준 ([ragas_report.md](evaluation/ragas_report.md)).
- context_recall: 1.00 (목표 0.70 달성)
- context_precision: 0.44 (목표 0.70 미달. 문서 단위 확장이 관련 없는 소제목까지 붙인 대가. 1번은 채점 오류로 보이는 0.00, 빼면 0.55)
- faithfulness: 0.49 (목표 0.80 미달. 문서만 보는 1, 2번은 1.00. 7, 8, 18번은 답변의 본인 기록 수치가 MCP 조회 결과라 '문서 근거 없음'으로 셈)
- answer_relevancy: 0.29 (목표 0.80 미달. 수치·원칙·표를 함께 주는 긴 답변과 거절 답변(18번)에서 낮게 나오는 지표)

## 인-아웃 세트 통과율 (자체 평가)
- 1차 (첫 구현 직후, 2026-11-10): 10 / 18 통과 — [round1_report.md](evaluation/round1_report.md)
- 2차 (개선 후, 2026-11-12): **18 / 18 통과**, 가드레일 4/4 (모두 3회 중 3회) — 3회 실행 다수결 — [round2_report.md](evaluation/round2_report.md)
- 개선폭: +8건 (10 -> 18). 4, 7, 8번 개선 일부는 테스트 세트 확정 전에 기대 도구·모호한 기대 항목을 바로잡은 결과다. 2차 첫 실행은 16/18(가드레일 18번 1/3)이라 프롬프트만 고쳐 다시 돌렸다 ([round2_try1_report.md](evaluation/round2_try1_report.md))
- 평가는 코치 피드백·승인한 조정안을 비운 상태에서 돌린다. 화면을 쓰며 쌓인 기억이 결과에 섞이지 않게 하기 위해서다.

## 컨디션 대조 평가 (강도 조정 확인)
거리 한도(G5)를 지키는 것과 컨디션에 맞게 강도를 낮추는 것은 별개다. 그래서 훈련 기록·계획표는 그대로 두고 11/3~11/8 수면·안정시 심박·피로도만 바꾼 두 조건에서, 컨디션 단서가 없는 같은 질문을 3회씩 물어 화요일 인터벌에 대한 결정을 비교했다 ([condition_report.md](evaluation/condition_report.md)).

| 조건 | "내일 화요일 인터벌 그대로 해도 될까?" | "이번 주 계획 점검해줘" | 주간 거리 한도 |
|---|---|---|---|
| 피로 (수면 5시간대, 안정시 심박 57~59, 피로도 4) | 3/3 조정 (조깅으로 대체 2, 미룸 1) | 3/3 조정 (조깅으로 대체) | 모두 한도 안 |
| 회복 (수면 7.4~7.8시간, 안정시 심박 51~52, 피로도 2) | 3/3 유지 | 3/3 유지 | 모두 한도 안 |

- 4개 조합 모두 통과. 12회 모두 결정 근거로 컨디션 수치를 인용했다.
- 두 조건 모두 거리 한도를 지켰으므로, 거리 한도만으로는 두 조건을 가를 수 없다. 강도 조정은 컨디션 신호로만 일어났다.
- 처음 시험 실행에서는 회복 조건의 "계획 점검" 질문에서도 인터벌을 뺐다. 지난주 34km 대비 증가를 이유로 들었는데, 문서는 거리 판단 기준을 최근 4주 최댓값으로 정한다. "강도를 낮추는 근거는 컨디션 신호뿐, 놓친 훈련은 장거리 거리에만 반영"을 프롬프트에 넣은 뒤 위 결과가 나왔다 (NOTES.md 22).

## 확장 경로
- 영상 자세 분석 고도화: 지금은 짧은 구간 프레임 8장을 모델이 눈으로 관찰하는 수준이다. 관절 위치 추정(포즈 추정)으로 착지 위치·상체 기울기·케이던스를 수치로 재고, 같은 러너의 영상을 주기적으로 비교해 변화를 보여 준다. 부상 예측과 진단은 계속 하지 않는다 (G1).
- 승인 범위 확장: 지금은 이번 주와 다음 주(계획표가 있는 범위)까지 조정한다. 대회일까지 남은 전체 일정을 다시 짜는 것은 계획표 생성부터 필요하다.
- 실제 웨어러블 기록 서버로 교체: MCP 서버 파일만 바꾸면 에이전트 코드는 그대로다.
- 조정안을 캘린더·시계에 등록: 등록 전 사용자 승인 (지금의 승인 그래프를 그대로 쓴다).
- 넣지 않기로 한 것 — 유튜브 등 외부 영상 링크 분석: 다운로드 도구(`yt-dlp`)로 받아 같은 흐름에 넣을 수는 있지만 넣지 않았다. 유튜브 약관은 공식 기능 밖의 다운로드를 막고, 링크 영상은 대부분 타인의 모습이라 본인 정보만 다루는 정책(G4)과 맞지 않는다. 다운로드 도구가 자주 깨져 시연이 불안정하고, 긴 영상에서 달리는 구간을 따로 골라야 하며, 서버가 임의 링크를 내려받는 보안 부담도 생긴다. 본인 영상을 5~10초로 잘라 직접 올리는 방식을 쓴다.

## 테스트 세트 밖의 기능
승인(HITL), 장기 메모리, 이미지·영상 첨부는 Day 8 오후에 추가했다. 인-아웃 세트(18건)는 1차 평가와 비교할 수 있게 그대로 두었고, 이 세 기능은 API로 직접 확인했다 (NOTES.md 14~16).

## 트라이앤에러 회고
구현하며 막힌 22가지를 [NOTES.md](NOTES.md)에 그때그때 적었다. 배운 것을 묶으면 다섯 가지다.

1. **에이전트끼리의 협업은 프롬프트 한 줄에 크게 흔들린다.** Supervisor가 두 담당을 동시에 불러 같은 조회를 세 번 반복했고(153초), "한 번에 한 담당" 규칙 하나로 47초가 됐다. 또 서브 에이전트는 Supervisor의 지시가 아니라 사용자 질문 원문을 보기 때문에 "담당 밖이면 거절"이라고 쓰자 조회 자체를 거절했다. 각 담당에게 "질문 전체가 아니라 자기 몫만"을 적어야 했다. (NOTES 1, 8)
2. **세고 더하고 비교하는 일은 모델이 아니라 코드가 한다.** 모델은 수면 부족 3일을 "4일 연속"으로 셌고, 주간 합계 검사도 믿을 수 없었다. 일수는 MCP 도구가 계산해 넘기고(`signals`), 주간 거리 한도는 계획 담당의 도구 검사와 출력 단계의 구조화 추출 후 코드 검사로 두 번 지킨다. 인터벌 페이스 범위도 계산 도구에 보정 초를 넣어 계산하게 했다. (NOTES 5, 6, 21)
3. **검색이 안 잡히면 개수보다 단위를 바꾼다.** 테이퍼링 질문의 핵심 조각("강도는 유지한다")은 검색 개수를 늘려도, 하이브리드로 바꿔도 안 잡혔다. 질문과 단어도 의미도 겹치지 않았기 때문이다. 한 문서가 여러 번 잡히면 그 문서의 나머지 소제목을 붙이는 문서 단위 확장으로 풀었다. (NOTES 9)
4. **평가는 측정 대상만 바뀌게 설계해야 한다.** 기대 도구를 구현 전에 추측해 적어 정상 동작이 실패로 판정됐고, 화면에서 테스트하며 쌓인 기억(피드백·승인 조정안)이 평가 답변에 섞였다. 평가는 기억을 비운 상태에서 돌리고, 강사 피드백에 따라 같은 기록에서 컨디션만 바꾼 대조 평가를 붙였다. 그 결과 거리 한도는 두 조건 모두 지키므로 한도만으로는 핵심 가치(컨디션 반영)를 확인할 수 없다는 점, 그리고 회복 상태에서도 "지난주 대비 급증"을 이유로 인터벌을 빼던 잘못된 판단을 찾았다. (NOTES 8, 22)
5. **물은 것에 먼저 답해야 쓸 만하다.** 인터벌 카드에서 페이스를 물었는데 컨디션 신호를 보고 "오늘 쉬라"고만 답했다. 답을 먼저, 컨디션 우려는 "다만"으로 덧붙이게 하고 화면이 보내는 카드 정보에 '내일' 같은 날짜 관계를 붙였다. (NOTES 21)

운영 면에서는 Bedrock 일일 토큰 한도에 여러 번 막혔다. 심사 모델을 에이전트와 다른 모델로 둔 덕에 한쪽이 막혀도 다른 쪽은 돌았고, 결국 요청마다 모델·리전을 고를 수 있게 했다. (NOTES 12, 20)

다시 한다면: 도구 설계를 먼저 끝내고 테스트 세트를 쓰고, 사용자 상태(기억) 저장 위치를 처음부터 설정으로 분리하고, 컨디션 대조 같은 "한 변수만 바꾼 평가"를 1차 평가부터 넣겠다.

## 핵심 코드 위치
- `src/agent.py` — 메인 에이전트 그래프 (`build_graph`), 질의 처리 파이프라인 (`run_query`), 출력 가드레일 (`output_guard`)
- `src/tools.py` — 도메인 도구 (`retrieve_training_docs`, `calc_pace`, `check_week_plan`, MCP 도구 로드)
- `src/retriever.py` — RAG 파이프라인 (청킹, 색인, 검색)
- `src/mcp_server.py` — 러너 데이터 MCP 서버 (지난 코치 피드백 조회 = 장기 메모리 포함)
- `src/guards.py` — 입력 가드레일
- `src/tracer.py` — 트레이스 수집
- `src/api.py` — `/query` API와 화면용 API (`/week`, `/feedback`, `/approve`, `/models`)
- `src/config.py` — 설정, 고를 수 있는 모델 목록(`MODEL_CHOICES`)과 요청별 모델 선택(`use_model`)
- `src/web/app.html` — 주간 훈련 화면, `src/web/index.html` — 확인용 화면 (`/debug`)
- `src/feedback.py` — 날짜별 코치 피드백 저장
- `src/approval.py` — 조정안 승인 그래프 (HITL, `interrupt` / `Command(resume)`)와 승인된 조정안 저장
- `src/vision.py` — 첨부 이미지 판독, 영상 프레임 추출과 자세 관찰 (멀티모달 입력)
- `evaluation/run_inout.py` — 인-아웃 세트 자체 평가 (3회 실행, 다수결)
- `evaluation/run_ragas.py` — RAGAS 4개 지표 측정
