"""approval.py - 주간 조정안 승인 (Human-in-the-Loop)과 승인된 조정안 저장소.

에이전트가 주간 계획 조정안을 내면 바로 반영하지 않고 사용자의 승인을 기다린다.
LangGraph 의 interrupt 로 승인 그래프를 멈춰 두고, 사용자가 승인·거절하면
Command(resume=...) 로 멈춘 지점부터 이어서 실행한다.

  request(...)  조정안으로 승인 그래프를 시작한다 -> interrupt 에서 멈추고 thread_id 를 돌려준다
  resolve(...)  사용자의 결정으로 그래프를 재개한다 -> 승인이면 저장, 거절이면 버린다

승인된 조정안은 data/adjusted_plans.json 에 주(월요일 날짜) 단위로 쌓는다. 주간 화면이 이 값을
보여 주고, 기록 담당의 get_coach_feedback 도 함께 돌려줘 다음 질문에서 기억한다.
실행 중에 만들어지는 파일이라 저장소에는 올리지 않는다 (.gitignore).
"""
import json
import threading
import time
import uuid
from datetime import date, timedelta
from typing import TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from src import data_access as da
from src.config import TODAY, state_dir, week_start


def _path():
    """승인한 조정안 파일 위치 (평가 때는 빈 임시 폴더)."""
    return state_dir() / "adjusted_plans.json"


WEEKDAYS = "월화수목금토일"
_lock = threading.Lock()


# ---------------------------------------------------------------------------
# 승인된 조정안 저장소
# ---------------------------------------------------------------------------
def load_all() -> dict[str, dict]:
    """승인된 조정안 전체를 {주 시작일: 조정안} 으로 돌려준다. 파일이 없으면 빈 dict."""
    if not _path().exists():
        return {}
    try:
        with open(_path(), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(data: dict) -> None:
    _path().parent.mkdir(parents=True, exist_ok=True)
    with open(_path(), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


SCOPES = {"day": "이 날만", "week": "이번 주 전체", "through_next": "다음 주까지 이어서"}


def to_dated_weeks(days: list[dict], base: str | None = None) -> dict[str, list[dict]]:
    """요일로 적힌 계획을 실제 날짜로 바꿔 주별로 묶는다. 1주차는 base 주(기본: 기준일이 속한 주)다."""
    base = date.fromisoformat(base) if base else week_start(TODAY)
    weeks: dict[str, list[dict]] = {}
    for d in days:
        wd = str(d.get("weekday", "")).strip()[:1]
        if wd not in WEEKDAYS:
            continue
        monday = base + timedelta(days=7 * (int(d.get("week") or 1) - 1))
        day = monday + timedelta(days=WEEKDAYS.index(wd))
        weeks.setdefault(monday.isoformat(), []).append({
            "date": day.isoformat(), "weekday": wd,
            "workout": d.get("workout", ""), "distance_km": float(d.get("distance_km") or 0),
        })
    for rows in weeks.values():
        rows.sort(key=lambda r: r["date"])
    return weeks


def select_scope(weeks: dict[str, list[dict]], scope: str, day: str = "") -> dict[str, list[dict]]:
    """승인 범위에 맞는 날만 고른다.

    day           그 날짜 하나만 (훈련 카드를 눌러 물었을 때)
    week          첫 주(조정 대상 주)만
    through_next  답변에 나온 주 전부 (다음 주는 저장 뒤 따로 다시 조정한다)
    """
    if scope == "day":
        picked = {m: [r for r in rows if r["date"] == day] for m, rows in weeks.items()}
        return {m: rows for m, rows in picked.items() if rows}
    if scope == "week":
        first = min(weeks) if weeks else None
        return {first: weeks[first]} if first else {}
    return weeks


def _week_total(monday: str, overrides: list[dict]) -> float:
    """원래 계획에 조정한 날을 덮어쓴 주간 합계."""
    sunday = (date.fromisoformat(monday) + timedelta(days=6)).isoformat()
    km = {p["date"]: p["planned_distance_km"] for p in da.planned_workouts(monday, sunday)}
    km.update({r["date"]: r["distance_km"] for r in overrides})
    return round(sum(km.values()), 1)


def save_plan(weeks: dict[str, list[dict]], question: str, comment: str = "") -> list[str]:
    """승인된 날들을 주 단위로 저장하고 저장한 주 시작일 목록을 돌려준다.

    이미 조정한 주라면 같은 날짜만 덮어쓰고 나머지 날은 그대로 둔다 (그날만 바꾸는 경우).
    """
    with _lock:
        data = load_all()
        for monday, rows in weeks.items():
            merged = {r["date"]: r for r in data.get(monday, {}).get("days", [])}
            merged.update({r["date"]: r for r in rows})
            days = sorted(merged.values(), key=lambda r: r["date"])
            data[monday] = {
                "approved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "question": question.strip(),
                "comment": comment.strip(),
                "total_km": _week_total(monday, days),
                "days": days,
            }
        _write(data)
    return sorted(weeks)


def already_approved(days: list[dict], base: str | None = None) -> bool:
    """같은 조정안이 이미 승인돼 있으면 True. 승인한 계획을 다시 묻는 답변에서 승인 요청을 또 만들지 않는다."""
    weeks = to_dated_weeks(days, base)
    saved = load_all()
    key = lambda rows: [(r["date"], r["distance_km"]) for r in rows if r["distance_km"] > 0]
    # 답변이 승인한 계획의 일부(예: 화요일만)를 다시 말한 경우도 같은 계획으로 본다
    return bool(weeks) and all(m in saved and set(key(rows)) <= set(key(saved[m]["days"])) for m, rows in weeks.items())


def load_week(monday: str) -> dict | None:
    """한 주의 승인된 조정안. 없으면 None."""
    return load_all().get(monday)


def delete_week(monday: str) -> bool:
    """한 주의 승인된 조정안을 지운다 (원래 계획으로 되돌린다). 지울 것이 없으면 False."""
    with _lock:
        data = load_all()
        if monday not in data:
            return False
        del data[monday]
        _write(data)
    return True


# ---------------------------------------------------------------------------
# 승인 그래프 (LangGraph interrupt)
# ---------------------------------------------------------------------------
class ApprovalState(TypedDict, total=False):
    question: str
    plan: list[dict]        # [{week, weekday, workout, distance_km}]
    base: str               # 1주차의 월요일 (다음 주를 다시 조정할 때는 다음 주 월요일)
    totals: dict
    decision: dict          # {"approved": bool, "comment": str, "scope": "day|week|through_next", "day": "YYYY-MM-DD"}
    saved_weeks: list[str]
    next_week: str          # through_next 일 때 이어서 조정할 주의 월요일 (없으면 "")
    result: str


def _ask_human(state: ApprovalState) -> dict:
    """사람의 결정을 기다린다. 여기서 그래프가 멈추고, 재개될 때 사용자의 결정이 돌아온다."""
    decision = interrupt({"plan": state["plan"], "totals": state["totals"]})
    return {"decision": decision if isinstance(decision, dict) else {"approved": bool(decision)}}


def _route(state: ApprovalState) -> str:
    return "apply" if state["decision"].get("approved") else "discard"


def _apply(state: ApprovalState) -> dict:
    """승인된 조정안을 고른 범위만큼 저장한다."""
    dec = state["decision"]
    scope = dec.get("scope") if dec.get("scope") in SCOPES else "week"
    weeks = select_scope(to_dated_weeks(state["plan"], state.get("base")), scope, dec.get("day", ""))
    if not weeks:
        return {"saved_weeks": [], "next_week": "",
                "result": "승인했지만 조정안에 고른 날짜가 없어 반영하지 않았습니다."}
    saved = save_plan(weeks, state["question"], dec.get("comment", ""))
    n_days = sum(len(rows) for rows in weeks.values())
    next_week = ""
    if scope == "through_next":
        following = (date.fromisoformat(max(saved)) + timedelta(days=7)).isoformat()
        sunday = (date.fromisoformat(following) + timedelta(days=6)).isoformat()
        if da.planned_workouts(following, sunday):     # 다음 주 계획표가 있을 때만 이어서 조정한다
            next_week = following
    label = SCOPES[scope]
    return {"saved_weeks": saved, "next_week": next_week,
            "result": f"승인 ({label}): {', '.join(saved)} 주의 {n_days}일을 계획에 반영했습니다."
                      + (" 다음 주 계획을 이어서 조정합니다." if next_week else "")}


def _discard(state: ApprovalState) -> dict:
    """거절된 조정안은 저장하지 않는다."""
    comment = state["decision"].get("comment", "").strip()
    return {"result": "거절: 계획에 반영하지 않았습니다." + (f" (의견: {comment})" if comment else "")}


def _build():
    g = StateGraph(ApprovalState)
    g.add_node("ask_human", _ask_human)
    g.add_node("apply", _apply)
    g.add_node("discard", _discard)
    g.add_edge(START, "ask_human")
    g.add_conditional_edges("ask_human", _route, {"apply": "apply", "discard": "discard"})
    g.add_edge("apply", END)
    g.add_edge("discard", END)
    # 멈춘 상태를 thread_id 별로 보관한다. 서버를 다시 띄우면 대기 중이던 승인 요청은 사라진다.
    return g.compile(checkpointer=InMemorySaver())


_graph = _build()


def request(question: str, days: list[dict], totals: dict, base: str | None = None) -> dict:
    """조정안 승인 요청을 만든다. 그래프는 interrupt 에서 멈추고 결정을 기다린다."""
    thread_id = uuid.uuid4().hex
    config = {"configurable": {"thread_id": thread_id}}
    base = base or week_start(TODAY).isoformat()
    _graph.invoke({"question": question, "plan": days, "base": base, "totals": totals}, config)
    weeks = to_dated_weeks(days, base)
    return {"thread_id": thread_id, "status": "pending", "weeks": weeks, "base": base,
            "totals_km": {m: _week_total(m, rows) for m, rows in weeks.items()}}


def resolve(thread_id: str, approved: bool, comment: str = "", scope: str = "week", day: str = "") -> dict:
    """사용자의 결정(승인 여부, 범위)으로 멈춘 그래프를 재개한다. 대기 중인 요청이 없으면 KeyError."""
    config = {"configurable": {"thread_id": thread_id}}
    snapshot = _graph.get_state(config)
    if not snapshot.next:
        raise KeyError(thread_id)
    out = _graph.invoke(Command(resume={"approved": approved, "comment": comment, "scope": scope, "day": day}), config)
    return {"thread_id": thread_id, "status": "approved" if approved else "rejected", "result": out["result"],
            "saved_weeks": out.get("saved_weeks", []), "next_week": out.get("next_week", "")}


def next_week_question(next_monday: str) -> str:
    """'다음 주까지'를 승인했을 때 다음 주를 다시 조정하도록 에이전트에게 보낼 질문."""
    nxt = date.fromisoformat(next_monday)
    this_monday = (nxt - timedelta(days=7)).isoformat()
    plan = load_week(this_monday) or {"days": [], "total_km": 0}
    rows = ", ".join(f"{r['weekday']} {r['workout']} {r['distance_km']:g}km" for r in plan["days"])
    return (
        f"이번 주({this_monday} 주) 계획을 이렇게 조정하기로 승인했어: {rows} (주간 {plan['total_km']}km). "
        f"이 조정을 반영해서 다음 주({next_monday}~{(nxt + timedelta(days=6)).isoformat()}) 계획도 조정해줘. "
        "다음 주 원래 계획표와 비교해 거리, 특히 장거리가 갑자기 늘지 않게 이어 주고, 다음 주 요일별 계획을 표로 보여줘."
    )


if __name__ == "__main__":
    # 간단한 확인: 승인 요청 -> 멈춤 -> 거절로 재개
    req = request("테스트", [{"week": 1, "weekday": "화", "workout": "조깅", "distance_km": 8}], {1: 8})
    print("대기:", req)
    print("재개:", resolve(req["thread_id"], approved=False, comment="테스트라 버림"))
