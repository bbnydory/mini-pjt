"""make_data.py - 가짜 러너(runner_01) 데이터 4종을 만드는 스크립트.

실행: python -m src.make_data

만드는 파일 (data/runner/):
  profile.json            개인 기록, 심박존, 목표 대회
  planned_workouts.json   정해진 계획표 (과거 12주 + 이번 주 + 다음 주)
  activities.json         실제 수행한 훈련 기록 (과거 12주)
  condition.json          일별 컨디션 지표 (안정시 심박, 수면, 피로도)

난수 시드를 고정해 누가 실행해도 같은 파일이 나온다.
거리와 수행 여부는 난수가 아니라 아래 표에 직접 적어, SERVICE.md 3번의
"데이터 시나리오 (고정)"이 항상 성립하게 했다. 난수는 페이스와 심박의 잔떨림에만 쓴다.
"""
import json
import random
from datetime import date, timedelta

from src.config import RUNNER_DIR, RUNNER_ID, TODAY, WEEKDAYS_KO

SEED = 42
FIRST_MONDAY = date(2026, 8, 17)   # 1주차 월요일. 12주차는 2026-11-02 ~ 2026-11-08

# 요일 번호: 화=1, 수=2, 목=3, 토=5, 일=6 (월, 금은 휴식일)
TUE, WED, THU, SAT, SUN = 1, 2, 3, 5, 6

# 훈련 종류별 이름, 설명, 평균 페이스(초/km), 평균 심박, 최대 심박
WORKOUT_TYPES = {
    "easy":         ("조깅", "존2 심박으로 편하게", 350, 143, 151),
    "easy_strides": ("조깅+가속주", "존2 조깅 뒤 가속주 100m 6회", 348, 144, 166),
    "long":         ("장거리주", "마라톤 페이스보다 km당 30~60초 느리게", 345, 146, 156),
    "interval":     ("인터벌", "워밍업 3km, 1000m 5회(휴식 400m 조깅), 쿨다운 2km", 322, 156, 177),
    "tempo":        ("템포런", "워밍업 3km, 젖산역치 페이스 25분, 쿨다운 2km", 316, 159, 169),
    "cruise":       ("크루즈 인터벌", "워밍업 2km, 2km 3회(휴식 90초 조깅), 쿨다운 2km", 320, 157, 168),
    "hills":        ("언덕 반복", "워밍업 3km, 오르막 75초 8회, 쿨다운 2km", 340, 152, 172),
    "race":         ("하프마라톤 대회", "가을 하프마라톤 대회", 304, 167, 178),
}
HARD_TYPES = {"interval", "tempo", "cruise", "hills", "race"}

# 주차별 계획: (요일, 종류, 거리 km). 13주차가 "이번 주", 14주차가 "다음 주".
PLAN = {
    1:  [(TUE, "easy", 8.0), (WED, "easy", 6.0), (THU, "easy_strides", 8.0), (SAT, "easy", 4.0), (SUN, "long", 14.0)],
    2:  [(TUE, "easy", 8.0), (WED, "easy", 7.0), (THU, "hills", 8.0), (SAT, "easy", 5.0), (SUN, "long", 16.0)],
    3:  [(TUE, "easy", 8.0), (WED, "easy", 7.0), (THU, "tempo", 8.0), (SAT, "easy", 5.0), (SUN, "long", 18.0)],
    4:  [(TUE, "easy", 7.0), (WED, "easy", 6.0), (THU, "easy_strides", 7.0), (SAT, "easy", 4.0), (SUN, "long", 12.0)],
    5:  [(TUE, "interval", 9.0), (WED, "easy", 7.0), (THU, "tempo", 8.0), (SAT, "easy", 4.0), (SUN, "long", 18.0)],
    6:  [(TUE, "interval", 9.0), (WED, "easy", 8.0), (THU, "tempo", 9.0), (SAT, "easy", 4.0), (SUN, "long", 20.0)],
    7:  [(TUE, "interval", 10.0), (WED, "easy", 8.0), (THU, "tempo", 9.0), (SAT, "easy", 5.0), (SUN, "long", 20.0)],
    8:  [(TUE, "easy", 8.0), (WED, "easy", 7.0), (THU, "cruise", 8.0), (SAT, "easy", 5.0), (SUN, "long", 12.0)],
    9:  [(TUE, "easy_strides", 8.0), (WED, "easy", 7.0), (THU, "easy", 7.9), (SAT, "easy_strides", 4.0), (SUN, "race", 21.1)],
    10: [(TUE, "easy", 8.0), (WED, "easy", 10.0), (THU, "easy", 8.0), (SAT, "easy", 6.0), (SUN, "long", 20.0)],
    11: [(TUE, "interval", 10.0), (WED, "easy", 8.0), (THU, "tempo", 10.0), (SAT, "easy", 5.0), (SUN, "long", 22.0)],
    12: [(TUE, "interval", 10.0), (WED, "easy", 8.0), (THU, "tempo", 10.0), (SAT, "easy", 6.0), (SUN, "long", 24.0)],
    13: [(TUE, "interval", 10.0), (WED, "easy", 8.0), (THU, "tempo", 10.0), (SAT, "easy", 6.0), (SUN, "long", 26.0)],
    14: [(TUE, "easy_strides", 8.0), (WED, "easy", 8.0), (THU, "cruise", 8.0), (SAT, "easy", 4.0), (SUN, "long", 18.0)],
}
RECOVERY_WEEKS = {4, 8, 10, 14}

# 계획했지만 수행하지 못한 훈련: (주차, 요일) -> 사유
MISSED = {
    (5, SAT): "업무 일정으로 미수행",
    (12, SUN): "업무 일정으로 미수행",
}

# 특정 훈련의 값을 직접 지정 (난수 대신)
OVERRIDES = {
    (9, SUN): {"duration_sec": 6410, "avg_hr": 167, "max_hr": 178},   # 하프 1:46:50
    (12, SAT): {"avg_hr": 149, "max_hr": 158},   # 수면 부족으로 조깅 심박이 존2를 넘음
}

BASELINE_RESTING_HR = 52
# 최근 며칠의 컨디션 악화 (날짜 -> 안정시 심박, 수면 시간, 피로도 1~5)
CONDITION_OVERRIDES = {
    date(2026, 11, 5): (54, 6.1, 3),
    date(2026, 11, 6): (57, 5.4, 4),
    date(2026, 11, 7): (58, 5.0, 4),
    date(2026, 11, 8): (59, 5.2, 4),
}


def fmt_duration(sec: int) -> str:
    h, rem = divmod(int(sec), 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}"


def fmt_pace(sec_per_km: float) -> str:
    total = int(round(sec_per_km))
    return f"{total // 60}:{total % 60:02d}"


def day_of(week: int, dow: int) -> date:
    return FIRST_MONDAY + timedelta(days=(week - 1) * 7 + dow)


def build_profile() -> dict:
    return {
        "runner_id": RUNNER_ID,
        "description": "주 5회 훈련하는 직장인 마라톤 러너 (교육용 가짜 데이터)",
        "training_days": ["화", "수", "목", "토", "일"],
        "rest_days": ["월", "금"],
        "personal_records": {
            "marathon": {"time": "3:52:10", "date": "2026-03-15"},
            "half_marathon": {"time": "1:46:50", "date": "2026-10-18"},
            "10k": {"time": "48:40", "date": "2026-05-10"},
        },
        "heart_rate": {
            "max_hr": 180,
            "lactate_threshold_hr": 165,
            "baseline_resting_hr": BASELINE_RESTING_HR,
            "zones": {
                "zone1": "140 미만",
                "zone2": "140~148",
                "zone3": "149~156",
                "zone4": "157~164",
                "zone5": "165 이상",
            },
        },
        "goal": {
            "race": "풀코스 마라톤",
            "race_date": "2027-03-14",
            "target_time": "3:30:00",
            "target_pace_per_km": "4:59",
        },
    }


def build_plan_and_activities(rng: random.Random):
    planned, activities = [], []
    for week, sessions in PLAN.items():
        for dow, wtype, km in sessions:
            d = day_of(week, dow)
            name, detail, pace, avg_hr, max_hr = WORKOUT_TYPES[wtype]
            if d >= TODAY:
                status = "upcoming"
            elif (week, dow) in MISSED:
                status = "missed"
            else:
                status = "completed"
            item = {
                "date": d.isoformat(),
                "weekday": WEEKDAYS_KO[d.weekday()],
                "week_start": day_of(week, 0).isoformat(),
                "type": wtype,
                "name": name,
                "detail": detail,
                "planned_distance_km": km,
                "is_hard": wtype in HARD_TYPES,
                "recovery_week": week in RECOVERY_WEEKS,
                "status": status,
            }
            if status == "missed":
                item["missed_reason"] = MISSED[(week, dow)]
            planned.append(item)

            if status != "completed":
                continue
            over = OVERRIDES.get((week, dow), {})
            sec_per_km = pace + rng.uniform(-6, 6)
            duration = over.get("duration_sec", int(round(km * sec_per_km)))
            activities.append({
                "date": d.isoformat(),
                "weekday": WEEKDAYS_KO[d.weekday()],
                "type": wtype,
                "name": name,
                "distance_km": km,
                "duration": fmt_duration(duration),
                "duration_sec": duration,
                "avg_pace_per_km": fmt_pace(duration / km),
                "avg_hr": over.get("avg_hr", avg_hr + rng.randint(-2, 2)),
                "max_hr": over.get("max_hr", min(180, max_hr + rng.randint(-1, 1))),
                "is_race": wtype == "race",
            })
    return planned, activities


def build_condition(rng: random.Random) -> list[dict]:
    rows = []
    d = FIRST_MONDAY
    last = TODAY - timedelta(days=1)
    while d <= last:
        if d in CONDITION_OVERRIDES:
            rhr, sleep, fatigue = CONDITION_OVERRIDES[d]
        else:
            rhr = BASELINE_RESTING_HR + rng.choice([-1, 0, 0, 0, 1])
            sleep = round(rng.uniform(6.6, 7.8), 1)
            fatigue = rng.choice([2, 2, 2, 3])
        rows.append({
            "date": d.isoformat(),
            "weekday": WEEKDAYS_KO[d.weekday()],
            "resting_hr": rhr,
            "sleep_hours": sleep,
            "fatigue_1to5": fatigue,
        })
        d += timedelta(days=1)
    return rows


def main():
    rng = random.Random(SEED)
    RUNNER_DIR.mkdir(parents=True, exist_ok=True)
    planned, activities = build_plan_and_activities(rng)
    files = {
        "profile.json": build_profile(),
        "planned_workouts.json": planned,
        "activities.json": activities,
        "condition.json": build_condition(rng),
    }
    for name, payload in files.items():
        with open(RUNNER_DIR / name, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        size = len(payload) if isinstance(payload, list) else 1
        print(f"{name}: {size}건 저장")


if __name__ == "__main__":
    main()
