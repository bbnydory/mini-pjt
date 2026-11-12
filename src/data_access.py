"""data_access.py - 가짜 러너 데이터(JSON)를 읽고 집계하는 함수 모음.

MCP 서버(mcp_server.py)와 가드레일 G5 검사(tools.py)가 같은 계산을 쓰도록 한곳에 모았다.
"""
import json
from datetime import date, timedelta
from functools import lru_cache

from src.config import (
    CONDITION_SCENARIO,
    RUNNER_DIR,
    TODAY,
    WEEKLY_LIMIT_LOOKBACK_WEEKS,
    WEEKLY_LIMIT_RATIO,
    week_start,
)


@lru_cache(maxsize=None)
def _load(name: str):
    path = RUNNER_DIR / name
    if not path.exists():
        raise FileNotFoundError(
            f"{path} 가 없습니다. 먼저 `python -m src.make_data` 를 실행하세요."
        )
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def profile() -> dict:
    return _load("profile.json")


def data_range() -> tuple[str, str]:
    """기록이 있는 기간 (시작일, 종료일). 첫 주 월요일부터 어제까지다."""
    rows = _load("condition.json")
    return rows[0]["date"], (TODAY - timedelta(days=1)).isoformat()


def _between(rows: list[dict], start: str, end: str) -> list[dict]:
    return [r for r in rows if start <= r["date"] <= end]


def parse_range(start_date: str, end_date: str) -> tuple[str, str]:
    """날짜 형식을 검증하고 (시작, 종료) 순서를 맞춘다. 형식이 틀리면 ValueError."""
    s, e = date.fromisoformat(start_date), date.fromisoformat(end_date)
    if s > e:
        s, e = e, s
    return s.isoformat(), e.isoformat()


def planned_workouts(start_date: str, end_date: str) -> list[dict]:
    return _between(_load("planned_workouts.json"), start_date, end_date)


def activities(start_date: str, end_date: str) -> list[dict]:
    return _between(_load("activities.json"), start_date, end_date)


# rested 시나리오에서 덮어쓸 최근 컨디션 (11/3~11/8). 훈련 기록과 계획표는 건드리지 않는다.
RESTED_CONDITION = {
    "2026-11-03": (52, 7.6, 2), "2026-11-04": (51, 7.4, 2), "2026-11-05": (52, 7.8, 2),
    "2026-11-06": (52, 7.5, 2), "2026-11-07": (51, 7.7, 2), "2026-11-08": (52, 7.4, 2),
}


def condition(start_date: str, end_date: str) -> list[dict]:
    """기간의 일별 컨디션. 시나리오가 rested 면 최근 며칠의 심박·수면·피로도만 회복된 값으로 바꿔 돌려준다."""
    rows = _between(_load("condition.json"), start_date, end_date)
    if CONDITION_SCENARIO != "rested":
        return rows
    out = []
    for r in rows:
        if r["date"] in RESTED_CONDITION:
            hr, sleep, fatigue = RESTED_CONDITION[r["date"]]
            r = {**r, "resting_hr": hr, "sleep_hours": sleep, "fatigue_1to5": fatigue}
        out.append(r)
    return out


def _week_summary(monday: date) -> dict:
    start, end = monday.isoformat(), (monday + timedelta(days=6)).isoformat()
    plan = planned_workouts(start, end)
    acts = activities(start, end)
    hard_km = sum(a["distance_km"] for a in acts if a["type"] not in ("easy", "easy_strides", "long"))
    return {
        "week_start": start,
        "week_end": end,
        "planned_sessions": len(plan),
        "completed_sessions": len(acts),
        "planned_km": round(sum(p["planned_distance_km"] for p in plan), 1),
        "actual_km": round(sum(a["distance_km"] for a in acts), 1),
        "hard_session_km": round(hard_km, 1),
        "missed": [
            {
                "date": p["date"],
                "weekday": p["weekday"],
                "name": p["name"],
                "planned_distance_km": p["planned_distance_km"],
                "reason": p.get("missed_reason", ""),
            }
            for p in plan
            if p["status"] == "missed"
        ],
    }


def weekly_summary(weeks: int = 4) -> list[dict]:
    """오늘 이전에 끝난 최근 N주의 주간 집계 (오래된 주부터)."""
    weeks = max(1, min(int(weeks), 12))
    last_monday = week_start(TODAY) - timedelta(days=7)
    mondays = [last_monday - timedelta(days=7 * i) for i in range(weeks)]
    return [_week_summary(m) for m in reversed(mondays)]


def weekly_limit() -> dict:
    """가드레일 G5: 제안 가능한 주간 거리 상한.

    최근 4주 동안 실제로 달린 주간 거리 중 최댓값의 110%.
    """
    recent = weekly_summary(WEEKLY_LIMIT_LOOKBACK_WEEKS)
    max_km = max(w["actual_km"] for w in recent)
    return {
        "limit_km": round(max_km * WEEKLY_LIMIT_RATIO, 1),
        "basis_max_km": max_km,
        "lookback_weeks": WEEKLY_LIMIT_LOOKBACK_WEEKS,
        "ratio": WEEKLY_LIMIT_RATIO,
        "rule": (
            f"제안하는 주간 거리 합계는 최근 {WEEKLY_LIMIT_LOOKBACK_WEEKS}주 실제 주간 거리 "
            f"최댓값({max_km}km)의 110%를 넘지 않는다."
        ),
    }


def week_view(monday: date) -> dict:
    """웹 화면의 주간 보기용 데이터. 한 주(월~일)의 요일별 계획, 수행 기록, 컨디션을 묶는다."""
    start, end = monday.isoformat(), (monday + timedelta(days=6)).isoformat()
    plan = {p["date"]: p for p in planned_workouts(start, end)}
    acts = {a["date"]: a for a in activities(start, end)}
    cond = {c["date"]: c for c in condition(start, end)}
    all_plan = _load("planned_workouts.json")
    first_monday = week_start(date.fromisoformat(all_plan[0]["date"]))
    last_monday = week_start(date.fromisoformat(all_plan[-1]["date"]))

    days = []
    for i in range(7):
        d = monday + timedelta(days=i)
        key = d.isoformat()
        days.append({
            "date": key,
            "weekday": "월화수목금토일"[i],
            "is_today": d == TODAY,
            "is_past": d < TODAY,
            "status": plan[key]["status"] if key in plan else "rest",
            "planned": plan.get(key),
            "activity": acts.get(key),
            "condition": cond.get(key),
        })
    return {
        "week_start": start,
        "week_end": end,
        "today": TODAY.isoformat(),
        "is_current_week": monday == week_start(TODAY),
        "has_prev": monday > first_monday,
        "has_next": monday < last_monday,
        "stats": {
            "planned_sessions": len(plan),
            "completed_sessions": len(acts),
            "missed_sessions": sum(1 for p in plan.values() if p["status"] == "missed"),
            "planned_km": round(sum(p["planned_distance_km"] for p in plan.values()), 1),
            "actual_km": round(sum(a["distance_km"] for a in acts.values()), 1),
        },
        "weekly_limit_km": weekly_limit()["limit_km"],
        "days": days,
    }
