"""tools.py - 도메인 도구.

  retrieve_training_docs  훈련 이론 문서 검색 (RAG)              -> 이론 담당
  calc_pace               목표 기록과 km당 페이스 환산            -> 계획 담당
  check_week_plan         주간 계획의 거리 합계를 한도와 비교     -> 계획 담당
  runner_mcp_tools        러너 데이터 MCP 서버의 도구 5개 로드    -> 기록 담당
"""
import json
import os
import sys
from contextlib import asynccontextmanager

from langchain_core.tools import tool
from pydantic import BaseModel, Field

from src import data_access as da
from src import retriever
from src.config import ROOT


# ---------- RAG ----------
@tool
def retrieve_training_docs(query: str) -> str:
    """마라톤 훈련 이론 문서에서 관련 내용을 검색한다.

    심박존, 존2 조깅, 장거리주, 마라톤 페이스주, 젖산역치 훈련, 인터벌, 가속주와 업힐,
    주기화와 주간 구성(놓친 훈련 처리), 주간 거리 증가와 과훈련 신호(컨디션에 따른 조정),
    테이퍼링, 대회 페이스 전략과 보급, 대회 후 회복과 부상 예방에 대한 근거가 필요할 때 쓴다.
    결과는 doc_id, source(문서 파일명), text 목록이다. 답변에는 source를 출처로 표기한다.

    Args:
        query: 찾고 싶은 내용을 담은 검색 문장
    """
    hits = retriever.search(query)
    if not hits:
        return "관련 문서를 찾지 못했습니다."
    return json.dumps(hits, ensure_ascii=False, indent=2)


# ---------- 페이스 계산 ----------
def _to_seconds(text: str) -> int:
    """'3:30:00', '1:45:29', '4:59' 같은 문자열을 초로 바꾼다."""
    parts = [int(p) for p in text.strip().split(":")]
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    raise ValueError(text)


def _clock(sec: float) -> str:
    sec = int(round(sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


@tool
def calc_pace(target_time: str = "", pace_per_km: str = "", distance_km: float = 42.195,
              offset_sec_per_km: float = 0) -> str:
    """목표 기록과 km당 페이스를 서로 환산한다. 암산하지 말고 반드시 이 도구로 계산한다.

    target_time만 주면 km당 페이스를, pace_per_km만 주면 예상 완주 기록을 돌려준다.
    offset_sec_per_km 를 주면 계산한 km당 페이스에 그만큼 더한다 (음수면 빠르게).
    예: 인터벌 페이스 = 10km 기록 페이스보다 10~15초 빠르게 -> target_time='0:48:40',
    distance_km=10, offset_sec_per_km=-10 과 -15 로 두 번 계산해 범위를 만든다.

    Args:
        target_time: 목표 완주 기록. 'h:mm:ss' 형식 (예: '3:30:00')
        pace_per_km: km당 페이스. 'm:ss' 형식 (예: '4:59')
        distance_km: 거리 km. 풀코스 42.195(기본), 하프 21.0975, 10K 10
        offset_sec_per_km: km당 페이스 보정 초. 음수면 빠르게, 양수면 느리게 (기본 0)
    """
    try:
        if target_time:
            total = _to_seconds(target_time)
            per_km = total / distance_km
        elif pace_per_km:
            per_km = _to_seconds(pace_per_km)
            total = per_km * distance_km
        else:
            return "target_time 또는 pace_per_km 중 하나를 입력하세요."
    except (ValueError, ZeroDivisionError):
        return "시간 형식이 잘못되었습니다. 기록은 'h:mm:ss', 페이스는 'm:ss' 형식으로 입력하세요."
    if offset_sec_per_km:
        per_km = max(1.0, per_km + offset_sec_per_km)
        total = per_km * distance_km

    splits = {
        f"{km}km": _clock(per_km * km)
        for km in (5, 10, 21.0975, 30, 42.195)
        if km <= distance_km + 1e-6
    }
    return json.dumps({
        "distance_km": distance_km,
        "finish_time": _clock(total),
        "pace_per_km": _clock(per_km),
        "pace_seconds_per_km": round(per_km, 1),
        "split_times": splits,
    }, ensure_ascii=False, indent=2)


# ---------- 가드레일 G5: 주간 거리 한도 ----------
class PlanSession(BaseModel):
    weekday: str = Field(description="요일 (월~일)")
    workout: str = Field(description="훈련 종류 (예: 조깅, 장거리주, 인터벌, 템포런, 휴식)")
    distance_km: float = Field(ge=0, description="거리 km. 휴식일은 0")


class WeekPlanInput(BaseModel):
    sessions: list[PlanSession] = Field(description="한 주(월~일)의 훈련 목록")


def evaluate_week_total(total_km: float) -> dict:
    """주간 거리 합계가 한도 안인지 판정한다. 출력 가드레일도 같은 함수를 쓴다."""
    limit = da.weekly_limit()
    total_km = round(total_km, 1)
    return {
        "total_km": total_km,
        "limit_km": limit["limit_km"],
        "ok": total_km <= limit["limit_km"] + 1e-9,
        "rule": limit["rule"],
    }


@tool(args_schema=WeekPlanInput)
def check_week_plan(sessions: list[PlanSession]) -> str:
    """주간 계획의 거리 합계가 주간 거리 한도 안인지 검사한다.

    주간 계획이나 조정안을 사용자에게 제시하기 전에 반드시 이 도구로 검사한다.
    ok가 false이면 거리를 줄여 다시 검사하고, ok가 true인 계획만 제시한다.
    """
    total = sum(s.distance_km for s in sessions)
    result = evaluate_week_total(total)
    result["message"] = (
        "한도 이내입니다. 이 계획을 제시해도 됩니다."
        if result["ok"]
        else f"한도를 {round(result['total_km'] - result['limit_km'], 1)}km 초과했습니다. "
             "거리를 줄여 다시 검사하세요."
    )
    return json.dumps(result, ensure_ascii=False, indent=2)


# ---------- MCP ----------
@asynccontextmanager
async def runner_mcp_tools():
    """러너 데이터 MCP 서버를 자식 프로세스로 띄우고 도구 목록을 내준다.

    세션을 열어 둔 채로 도구를 쓰므로 서버 프로세스는 한 번만 뜬다. 세션 없이 도구를 만들면
    도구를 호출할 때마다 서버가 새로 떠서 호출당 약 3초가 더 걸렸다.
    """
    from langchain_mcp_adapters.client import MultiServerMCPClient
    from langchain_mcp_adapters.tools import load_mcp_tools

    client = MultiServerMCPClient({
        "runner": {
            "command": sys.executable,
            "args": [str(ROOT / "src" / "mcp_server.py")],
            "transport": "stdio",
            # 자식 프로세스는 기본 환경 변수(PATH 등)만 물려받는다. 기준일·컨디션 시나리오 같은
            # 프로젝트 설정(RUNCOACH_*)은 직접 넘겨야 서버도 같은 조건으로 데이터를 낸다.
            "env": {k: v for k, v in os.environ.items() if k.startswith("RUNCOACH_")},
        }
    })
    async with client.session("runner") as session:
        yield await load_mcp_tools(session)
