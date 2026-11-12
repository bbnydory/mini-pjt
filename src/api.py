"""api.py - TEMPO API 서버와 확인용 웹 화면.

실행: python -m uvicorn src.api:api --port 8000
화면: http://localhost:8000

  POST /query      {"question": "...", "image": 선택} -> {"answer", "contexts", "trace", "meta"}
                   image 는 data:image/...;base64,... 형식의 첨부 이미지 (기록 화면 캡처 등)
                   video 는 data:video/mp4;base64,... 형식의 주행 영상 (최대 30MB, 자세 관찰)
                   조정안이 나오면 meta.approval 에 승인 대기 중인 thread_id 가 담긴다
  POST /approve    {"thread_id", "approved", "comment", "scope", "day"} 조정안 승인·거절 (HITL 재개)
                   scope: day(그날만) / week(이번 주 전체) / through_next(다음 주까지 이어서 조정)
  DELETE /adjusted/{week_start}   승인한 조정안을 지우고 원래 계획으로 되돌림
  GET  /           주간 훈련 화면 (훈련 카드를 눌러 에이전트에게 묻고 피드백을 저장)
  GET  /debug      질문을 보내고 답변·근거 문서·실행 과정을 확인하는 화면
  GET  /week       주간 화면 데이터 (?start=YYYY-MM-DD, 생략하면 기준일이 속한 주). 승인한 조정안 포함
  GET  /feedback   저장된 코치 피드백 전체
  POST /feedback   {"date", "question", "answer"} 를 그 날짜의 코치 피드백으로 저장
  DELETE /feedback/{date}   그 날짜의 가장 최근 피드백 삭제
  GET  /examples   화면의 예시 질문 (evaluation/test_queries.csv)
  GET  /models     고를 수 있는 모델·리전 (/query 의 model, region 으로 넘긴다. 한도가 차면 429)
  GET  /health     서버 상태

화면은 /query 를 그대로 호출하므로, 화면에서 본 동작이 API로 확인하는 동작과 같다.
"""
import csv
import time
import traceback
from contextlib import asynccontextmanager
from datetime import date

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from src import approval, feedback, vision
from src import data_access as da
from src.agent import open_app, run_query
from src.config import (LOG_DIR, MODEL_CHOICES, MODEL_ID, REGION, REGIONS, RETRIEVER_MODE, ROOT, RUNNER_ID,
                        TODAY, check_model, week_start)

APP_PAGE = ROOT / "src" / "web" / "app.html"      # 주간 훈련 화면
DEBUG_PAGE = ROOT / "src" / "web" / "index.html"  # 확인용 화면
TEST_QUERIES = ROOT / "evaluation" / "test_queries.csv"

state = {"app": None}


@asynccontextmanager
async def lifespan(_: FastAPI):
    # 서버가 떠 있는 동안 MCP 서버 연결과 그래프를 한 번만 만들어 재사용한다
    async with open_app() as graph:
        state["app"] = graph
        yield
    state["app"] = None


api = FastAPI(title="TEMPO", lifespan=lifespan)


class Query(BaseModel):
    question: str = Field(min_length=1, max_length=1000)
    image: str | None = Field(default=None, max_length=5_000_000)
    video: str | None = Field(default=None, max_length=41_000_000)   # 30MB 영상의 base64 길이
    model: str | None = Field(default=None, max_length=80)     # 생략하면 기본 모델 (GET /models)
    region: str | None = Field(default=None, max_length=20)    # 생략하면 기본 리전


def _check_choice(model: str | None, region: str | None, needs_vision: bool) -> None:
    """고른 모델·리전이 쓸 수 있는 조합인지, 이미지·영상을 읽을 수 있는 모델인지 검사한다."""
    try:
        check_model(model, region)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    if needs_vision and not MODEL_CHOICES[model or MODEL_ID]["vision"]:
        raise HTTPException(status_code=400, detail=f"{model} 은 이미지·영상을 읽지 못합니다. 다른 모델을 고르세요.")


def _is_throttled(e: Exception) -> bool:
    text = f"{type(e).__name__} {e}"
    return "ThrottlingException" in text or "Too many tokens" in text or "Too many requests" in text


@api.post("/query")
async def query(q: Query):
    if state["app"] is None:
        raise HTTPException(status_code=503, detail="에이전트가 아직 준비되지 않았습니다.")
    _check_choice(q.model, q.region, bool(q.image or q.video))
    try:
        if q.image:
            vision.check_image(q.image)
        if q.video:
            vision.check_video(q.video)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    try:
        result = await run_query(state["app"], q.question.strip(), q.image, video=q.video,
                                 model=q.model, region=q.region)
    except Exception as e:
        if _is_throttled(e):
            # 일일 토큰 한도나 분당 요청 한도에 걸렸다. 다른 모델이나 리전을 고르면 된다.
            raise HTTPException(status_code=429, detail=(
                f"{q.model or MODEL_ID} ({q.region or REGION}) 의 Bedrock 사용 한도가 찼습니다. "
                "다른 모델이나 리전(global 포함)을 골라 다시 보내 주세요.")) from e
        # 원인을 나중에 확인할 수 있게 전체 오류 내용을 파일로 남긴다
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOG_DIR / "errors.log", "a", encoding="utf-8") as f:
            f.write(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] {q.question}\n{traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {e}") from e
    return JSONResponse(content=result, media_type="application/json; charset=utf-8")


@api.get("/examples")
def examples():
    with open(TEST_QUERIES, encoding="utf-8") as f:
        rows = [
            {"id": r["id"], "category": r["category"], "input": r["input"], "note": r["note"]}
            for r in csv.DictReader(f)
        ]
    return JSONResponse(content=rows, media_type="application/json; charset=utf-8")


@api.get("/models")
def models():
    """고를 수 있는 모델과 리전. 일일 토큰 한도가 찬 모델을 피해 다른 모델·리전을 고를 때 쓴다."""
    return _json({
        "default": {"model": MODEL_ID, "region": REGION},
        "regions": REGIONS,
        "models": [{"id": k, **v} for k, v in MODEL_CHOICES.items()],
    })


@api.get("/health")
def health():
    return {
        "status": "ok" if state["app"] is not None else "starting",
        "runner_id": RUNNER_ID,
        "today": TODAY.isoformat(),
        "model": MODEL_ID,
        "retriever": RETRIEVER_MODE,
    }


class FeedbackIn(BaseModel):
    date: str = Field(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
    question: str = Field(min_length=1, max_length=1000)
    answer: str = Field(min_length=1, max_length=20000)


def _json(content):
    return JSONResponse(content=content, media_type="application/json; charset=utf-8")


@api.get("/week")
def week(start: str = ""):
    """한 주의 계획, 수행 기록, 컨디션과 그 주에 저장된 코치 피드백을 돌려준다."""
    try:
        monday = week_start(date.fromisoformat(start)) if start else week_start(TODAY)
    except ValueError as e:
        raise HTTPException(status_code=400, detail="start 는 YYYY-MM-DD 형식이어야 합니다.") from e
    view = da.week_view(monday)
    saved = feedback.load_all()
    for day in view["days"]:
        day["feedback"] = saved.get(day["date"], [])
    view["profile"] = da.profile()
    view["adjusted"] = approval.load_week(view["week_start"])
    return _json(view)


class ApprovalIn(BaseModel):
    thread_id: str = Field(min_length=8, max_length=64)
    approved: bool
    comment: str = Field(default="", max_length=500)
    scope: str = Field(default="week", pattern=r"^(day|week|through_next)$")
    model: str | None = Field(default=None, max_length=80)     # 다음 주를 다시 짤 때 쓸 모델
    region: str | None = Field(default=None, max_length=20)
    day: str = Field(default="", pattern=r"^([0-9]{4}-[0-9]{2}-[0-9]{2})?$")


@api.post("/approve")
async def approve(body: ApprovalIn):
    """승인 대기 중인 조정안에 사용자의 결정(승인 여부, 범위)을 전해 멈춘 승인 그래프를 재개한다.

    범위가 '다음 주까지'면 이번 주를 저장한 뒤 에이전트에게 다음 주 조정을 이어서 맡기고,
    그 답변(새 승인 요청 포함)을 next 로 함께 돌려준다.
    """
    try:
        out = approval.resolve(body.thread_id, body.approved, body.comment, body.scope, body.day)
    except KeyError as e:
        raise HTTPException(status_code=404, detail="대기 중인 승인 요청이 없습니다 (이미 처리했거나 서버가 다시 시작됨).") from e
    if out["next_week"] and state["app"] is not None:
        question = approval.next_week_question(out["next_week"])
        _check_choice(body.model, body.region, False)
        try:
            out["next"] = await run_query(state["app"], question, plan_week=out["next_week"],
                                          model=body.model, region=body.region)
        except Exception as e:
            if not _is_throttled(e):
                raise
            out["result"] += " (다음 주 조정은 모델 사용 한도로 실패했습니다. 다른 모델을 골라 채팅으로 요청해 주세요.)"
            return _json(out)
        out["next"]["question"] = question
    return _json(out)


@api.delete("/adjusted/{monday}")
def adjusted_delete(monday: str):
    return _json({"deleted": approval.delete_week(monday)})


@api.get("/feedback")
def feedback_list():
    return _json(feedback.load_all())


@api.post("/feedback")
def feedback_save(body: FeedbackIn):
    try:
        return _json(feedback.save(body.date, body.question, body.answer))
    except ValueError as e:
        raise HTTPException(status_code=400, detail="날짜가 올바르지 않습니다.") from e


@api.delete("/feedback/{day}")
def feedback_delete(day: str):
    return _json({"deleted": feedback.delete_last(day)})


@api.get("/", response_class=HTMLResponse)
def index():
    return HTMLResponse(APP_PAGE.read_text(encoding="utf-8"))


@api.get("/debug", response_class=HTMLResponse)
def debug():
    return HTMLResponse(DEBUG_PAGE.read_text(encoding="utf-8"))
