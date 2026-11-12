"""mcp_server.py - 러너 훈련 데이터 MCP 서버 (fastmcp, stdio).

노출하는 도구 6개:
  - get_runner_profile()                       개인 기록, 심박존, 목표 대회
  - get_planned_workouts(start_date, end_date) 정해진 계획표
  - get_activities(start_date, end_date)       실제 수행한 훈련 기록
  - get_weekly_summary(weeks)                  주간 집계와 주간 거리 한도
  - get_condition(start_date, end_date)        안정시 심박, 수면, 피로도
  - get_coach_feedback(start_date, end_date)   저장된 지난 코치 피드백과 승인한 조정안 (장기 메모리)

단독으로 쓰는 파일이 아니라 에이전트(agent.py)가 자식 프로세스로 띄워 사용한다.
이 서버는 runner_01 한 명의 데이터만 제공한다. 다른 사람을 지정하는 인자가 없으므로
타인의 기록은 구조적으로 조회할 수 없다 (가드레일 G4).

실제 서비스에서는 이 파일만 웨어러블 기록 서버로 바꾸면 되고 에이전트 코드는 그대로 둔다.
"""
import json
import sys
from pathlib import Path

# 자식 프로세스로 실행될 때도 `src` 패키지를 찾을 수 있게 프로젝트 루트를 경로에 넣는다
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastmcp import FastMCP  # noqa: E402

from src import data_access as da  # noqa: E402
from src import approval, feedback  # noqa: E402
from src.config import RUNNER_ID  # noqa: E402

mcp = FastMCP("runcoach-runner-data")


def _dump(payload) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _range_or_error(start_date: str, end_date: str):
    try:
        return da.parse_range(start_date, end_date), None
    except ValueError:
        return None, "날짜 형식이 잘못되었습니다. YYYY-MM-DD 형식으로 입력하세요. 예: 2026-11-02"


@mcp.tool()
def get_runner_profile() -> str:
    """러너 본인의 프로필을 조회한다.

    개인 최고 기록(풀, 하프, 10K), 심박존(최대 심박, 젖산역치 심박, 존별 범위),
    평소 안정시 심박, 목표 대회와 목표 기록이 필요할 때 사용한다.
    훈련 기록이나 계획표에는 사용하지 않는다.
    """
    return _dump(da.profile())


@mcp.tool()
def get_planned_workouts(start_date: str, end_date: str) -> str:
    """정해진 훈련 계획표를 기간으로 조회한다.

    각 훈련의 날짜, 요일, 종류, 계획 거리, 상태(completed, missed, upcoming)를 돌려준다.
    계획과 실제를 비교하거나 앞으로 예정된 훈련을 확인할 때 사용한다.
    실제로 달린 페이스와 심박은 get_activities로 조회한다.

    Args:
        start_date: 시작일 (YYYY-MM-DD)
        end_date: 종료일 (YYYY-MM-DD)
    """
    rng, err = _range_or_error(start_date, end_date)
    if err:
        return err
    rows = da.planned_workouts(*rng)
    if not rows:
        return f"{rng[0]}~{rng[1]} 기간에는 계획된 훈련이 없습니다."
    return _dump({
        "runner_id": RUNNER_ID,
        "count": len(rows),
        "total_planned_km": round(sum(r["planned_distance_km"] for r in rows), 1),
        "workouts": rows,
    })


@mcp.tool()
def get_activities(start_date: str, end_date: str) -> str:
    """실제로 수행한 훈련 기록을 기간으로 조회한다.

    각 훈련의 날짜, 종류, 거리, 시간, 평균 페이스, 평균 심박, 최대 심박을 돌려준다.
    주간 합계만 필요하면 get_weekly_summary가 더 알맞다.

    Args:
        start_date: 시작일 (YYYY-MM-DD)
        end_date: 종료일 (YYYY-MM-DD)
    """
    rng, err = _range_or_error(start_date, end_date)
    if err:
        return err
    rows = da.activities(*rng)
    first, last = da.data_range()
    if not rows:
        return (
            f"{rng[0]}~{rng[1]} 기간에는 훈련 기록이 없습니다. "
            f"조회 가능한 기간은 {first}~{last}입니다."
        )
    return _dump({
        "runner_id": RUNNER_ID,
        "count": len(rows),
        "total_distance_km": round(sum(r["distance_km"] for r in rows), 1),
        "available_range": f"{first}~{last}",
        "activities": rows,
    })


@mcp.tool()
def get_weekly_summary(weeks: int = 4) -> str:
    """오늘 이전에 끝난 최근 N주의 주간 집계를 조회한다.

    주마다 계획 횟수와 수행 횟수, 계획 거리와 실제 거리, 놓친 훈련 목록을 돌려준다.
    주간 거리 추이, 계획 대비 수행률, 놓친 훈련을 확인할 때 사용한다.
    제안 가능한 주간 거리 상한(weekly_distance_limit)도 함께 돌려준다.

    Args:
        weeks: 조회할 주 수 (1~12, 기본 4)
    """
    return _dump({
        "runner_id": RUNNER_ID,
        "weeks": da.weekly_summary(weeks),
        "weekly_distance_limit": da.weekly_limit(),
    })


@mcp.tool()
def get_condition(start_date: str, end_date: str) -> str:
    """일별 컨디션 지표를 기간으로 조회한다.

    날짜별 안정시 심박, 수면 시간, 주관적 피로도(1~5)와 기간 평균을 돌려준다.
    평소 안정시 심박(baseline)과 비교해 피로 누적 여부를 판단할 때 사용한다.
    수면 6시간 미만 일수, 안정시 심박이 평소보다 5회 이상 높은 일수와 연속 일수는
    signals에 계산돼 있으니 직접 세지 말고 그 값을 그대로 쓴다.

    Args:
        start_date: 시작일 (YYYY-MM-DD)
        end_date: 종료일 (YYYY-MM-DD)
    """
    rng, err = _range_or_error(start_date, end_date)
    if err:
        return err
    rows = da.condition(*rng)
    first, last = da.data_range()
    if not rows:
        return (
            f"{rng[0]}~{rng[1]} 기간에는 컨디션 기록이 없습니다. "
            f"조회 가능한 기간은 {first}~{last}입니다."
        )
    n = len(rows)
    baseline = da.profile()["heart_rate"]["baseline_resting_hr"]

    def streak(flag) -> int:
        """기간의 마지막 날부터 거슬러 올라가며 조건이 이어진 일수."""
        count = 0
        for r in reversed(rows):
            if not flag(r):
                break
            count += 1
        return count

    short_sleep = lambda r: r["sleep_hours"] < 6          # noqa: E731
    high_rhr = lambda r: r["resting_hr"] - baseline >= 5  # noqa: E731
    return _dump({
        "runner_id": RUNNER_ID,
        "baseline_resting_hr": baseline,
        # 날짜를 세는 일은 모델에게 맡기지 않고 여기서 계산해 넘긴다
        "signals": {
            "days_sleep_under_6h": sum(short_sleep(r) for r in rows),
            "consecutive_days_sleep_under_6h_until_last_day": streak(short_sleep),
            "days_resting_hr_5_or_more_above_baseline": sum(high_rhr(r) for r in rows),
            "consecutive_days_resting_hr_high_until_last_day": streak(high_rhr),
            "last_day": rows[-1]["date"],
        },
        "average": {
            "resting_hr": round(sum(r["resting_hr"] for r in rows) / n, 1),
            "sleep_hours": round(sum(r["sleep_hours"] for r in rows) / n, 1),
            "fatigue_1to5": round(sum(r["fatigue_1to5"] for r in rows) / n, 1),
        },
        "days": rows,
    })



@mcp.tool()
def get_coach_feedback(start_date: str, end_date: str) -> str:
    """사용자가 저장해 둔 지난 코치 피드백(이전 질문과 답변)과 승인한 주간 조정안을 기간으로 조회한다.

    대화가 끝나도 남는 장기 메모리다. 웹 화면에서 사용자가 답변을 그 날짜의 코치 피드백으로
    저장한 것만 들어 있다. "지난번에 뭐라고 했지?", "전에 줄이라고 한 거" 처럼 이전 조언을
    가리키는 질문, 그리고 계획 조정 질문에서 이전 조언과 어긋나지 않게 답하려 할 때 사용한다.
    날짜는 YYYY-MM-DD 형식이다. 저장된 피드백이 없으면 없다고 돌려준다.
    """
    rng, err = _range_or_error(start_date, end_date)
    if err:
        return err
    saved = feedback.load_all()
    items = [
        {"date": day, "saved_at": e["saved_at"], "question": e["question"],
         "summary": e["summary"], "answer": e["answer"][:800]}
        for day in sorted(saved) if rng[0] <= day <= rng[1]
        for e in saved[day]
    ]
    plans = [
        {"week_start": monday, **plan} for monday, plan in sorted(approval.load_all().items())
        if any(d["date"] >= rng[0] for d in plan["days"])   # 앞으로 따를 조정안은 기간 뒤여도 돌려준다
    ]
    if not items and not plans:
        return f"{rng[0]}~{rng[1]} 기간에 저장된 코치 피드백과 승인한 조정안이 없습니다."
    return _dump({"start_date": rng[0], "end_date": rng[1], "feedback": items,
                  "approved_plans": plans,
                  "note": "approved_plans 는 사용자가 승인해 원래 계획표 대신 따르기로 한 조정안이다"})


if __name__ == "__main__":
    try:
        mcp.run(show_banner=False)   # stdio 모드로 실행 (시작 배너는 로그를 어지럽혀 끈다)
    except TypeError:
        mcp.run()
