"""run_condition.py - 컨디션 대조 평가: 같은 훈련 기록에서 컨디션만 바꾸면 고강도 훈련이 조정되는가.

실행: python -m evaluation.run_condition                       2개 조건 x 질문 2개 x 3회
      python -m evaluation.run_condition --repeat 1 --no-report 시험 실행
      python -m evaluation.run_condition --model us.anthropic.claude-sonnet-4-6 --region us-west-2

왜 따로 두나:
  주간 거리 한도(G5)는 코드가 검사하지만, 한도 안의 계획이라도 피로한 날 인터벌을 그대로 두면
  이 서비스의 핵심 가치(컨디션 반영)를 지키지 못한다. 거리 한도와 강도 조정은 별개라서,
  훈련 기록·계획표는 그대로 두고 최근 수면·안정시 심박·피로도만 바꾼 두 조건을 비교한다.

  tired  : 생성 데이터 그대로. 11/6~11/8 수면 5.0~5.4시간, 안정시 심박 57~59 (평소 52), 피로도 4
  rested : 같은 기간을 수면 7.4~7.8시간, 안정시 심박 51~52, 피로도 2 로 바꾼 것 (data_access.RESTED_CONDITION)

질문에는 컨디션 단서(잠, 피로 등)를 넣지 않는다. 에이전트가 기록을 조회해 스스로 판단해야 한다.
판정 대상은 화요일(11/10) 인터벌이며, 심사 모델이 답변에서 화요일 결정을 다음 중 하나로 분류한다.
  keep      인터벌을 계획대로 유지
  reduced   인터벌은 하되 회차·거리·페이스를 낮춤
  replaced  쉬운 조깅이나 휴식으로 바꿈
  moved     다른 날로 미룸
  unclear   답변으로 판단할 수 없음
조정됨 = reduced, replaced, moved.

통과 기준 (조건·질문별 3회 다수결):
  tired  에서는 3회 중 2회 이상 조정됨
  rested 에서는 3회 중 2회 이상 keep
  그리고 두 조건 모두 제시한 주간 계획이 거리 한도(G5) 안 -> 거리 한도만으로는 두 조건을 가를 수 없음을 보인다.
인-아웃 세트(test_queries.csv)와 별개의 평가이며 그 통과율에 섞지 않는다.
"""
import argparse
import asyncio
import json
import os
import sys
import tempfile
import time
from typing import Literal

from pydantic import BaseModel, Field

from src.config import MODEL_ID, REGION, ROOT, check_model, make_llm

EVAL_DIR = ROOT / "evaluation"
JUDGE_MODEL = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
CONCURRENCY = 3
SCENARIOS = {
    "tired": "피로 (생성 데이터 그대로)",
    "rested": "회복 (수면·심박·피로도만 바꿈)",
}
QUESTIONS = {
    "q1": "내일 화요일 인터벌 그대로 해도 될까?",
    "q2": "이번 주 훈련 계획 점검해줘. 바꿀 게 있으면 요일별로 알려줘.",
}
ADJUSTED = {"reduced", "replaced", "moved"}


class TuesdayDecision(BaseModel):
    """답변이 화요일(11/10) 인터벌에 대해 내린 결정."""
    decision: Literal["keep", "reduced", "replaced", "moved", "unclear"] = Field(
        description="keep: 인터벌을 계획대로 유지 / reduced: 인터벌은 하되 회차·거리·페이스를 낮춤 / "
                    "replaced: 쉬운 조깅이나 휴식으로 바꿈 / moved: 다른 날로 미룸 / unclear: 판단 불가. "
                    "여러 선택지를 나열했다면 답변이 권장한 것을 고른다"
    )
    cites_condition: bool = Field(description="결정 근거로 수면·안정시 심박·피로도 같은 컨디션 수치를 들었는가")
    reason: str = Field(description="판정 근거가 된 답변 속 문장 요약 (한 문장)")


JUDGE_PROMPT = (
    "너는 러닝 코치 답변의 평가자다. 아래 답변이 2026-11-10(화) 인터벌 훈련(원래 계획: 1000m 5회, 총 10km)을 "
    "어떻게 하라고 권했는지 분류한다. 답변에 쓰인 내용만 근거로 삼는다.\n\n질문: {question}\n\n답변:\n{answer}"
)


async def run_one(app, judge, scenario: str, qid: str, k: int, sem, choice: dict) -> dict:
    from src.agent import run_query

    async with sem:
        started = time.time()
        try:
            out = await run_query(app, QUESTIONS[qid], **choice)
            error = ""
        except Exception as e:
            out, error = {"answer": "", "meta": {}}, f"{type(e).__name__}: {e}"
        meta = out["meta"]
        verdict = None
        if out["answer"]:
            for wait in (0, 20, 60):
                try:
                    await asyncio.sleep(wait)
                    verdict = await judge.ainvoke(JUDGE_PROMPT.format(question=QUESTIONS[qid], answer=out["answer"]))
                    break
                except Exception as e:   # 심사 호출 제한은 잠시 기다렸다 다시 시도한다
                    error = f"심사 오류: {type(e).__name__}: {e}"
        og = meta.get("output_guard") or {}
        row = {
            "scenario": scenario, "qid": qid, "run": k + 1,
            "decision": verdict.decision if verdict else "unclear",
            "cites_condition": bool(verdict and verdict.cites_condition),
            "reason": verdict.reason if verdict else error,
            "g5_status": og.get("status", "-"), "weekly_totals_km": og.get("weekly_totals_km", {}),
            "tools": meta.get("tools", []), "latency_s": round(time.time() - started, 1),
            "answer": out["answer"], "error": error if not verdict else "",
        }
        print(f"[{scenario:6s} {qid} #{k + 1}] {row['decision']:8s} 컨디션인용={row['cites_condition']!s:5s} "
              f"G5={row['g5_status']:9s} {row['latency_s']:5.1f}s | {row['reason'][:70]}", flush=True)
        return row


def summarize(rows: list[dict], repeat: int) -> list[dict]:
    """조건·질문별로 결정을 모으고 통과 여부를 판정한다."""
    need = repeat // 2 + 1
    out = []
    for scenario in SCENARIOS:
        for qid in QUESTIONS:
            rs = [r for r in rows if r["scenario"] == scenario and r["qid"] == qid]
            adjusted = sum(r["decision"] in ADJUSTED for r in rs)
            kept = sum(r["decision"] == "keep" for r in rs)
            passed = adjusted >= need if scenario == "tired" else kept >= need
            g5_ok = all(r["g5_status"] in ("ok", "rewritten", "no_plan", "-") for r in rs)
            out.append({"scenario": scenario, "qid": qid, "decisions": [r["decision"] for r in rs],
                        "adjusted": adjusted, "kept": kept, "passed": passed, "g5_ok": g5_ok,
                        "cites_condition": sum(r["cites_condition"] for r in rs)})
    return out


def write_report(rows, summary, repeat, elapsed, choice) -> str:
    """조건 대조 평가 리포트를 마크다운으로 쓴다."""
    lines = [
        "# 컨디션 대조 평가 결과",
        "",
        f"- 실행 시각: {time.strftime('%Y-%m-%d %H:%M')} · 소요 {round(elapsed / 60, 1)}분",
        f"- 에이전트 모델: `{choice.get('model') or MODEL_ID}` ({choice.get('region') or REGION}) · 심사 모델: `{JUDGE_MODEL}`",
        f"- 방법: 훈련 기록·계획표는 그대로 두고 11/3~11/8 컨디션만 바꾼 두 조건에서 같은 질문을 {repeat}회씩 묻고, "
        "화요일(11/10) 인터벌에 대한 결정을 심사 모델이 분류했다. 질문에는 컨디션 단서를 넣지 않았다.",
        "- 통과 기준: 피로 조건은 과반이 조정(reduced·replaced·moved), 회복 조건은 과반이 유지(keep). "
        "두 조건 모두 주간 거리 한도(G5) 안.",
        "- 기억 상태: 코치 피드백과 승인한 조정안을 비운 상태에서 실행",
        "",
        "| 조건 | 컨디션 (11/6~11/8) |",
        "|---|---|",
        "| tired (피로) | 수면 5.4·5.0·5.2시간, 안정시 심박 57·58·59 (평소 52), 피로도 4 |",
        "| rested (회복) | 수면 7.5·7.7·7.4시간, 안정시 심박 52·51·52, 피로도 2 |",
        "",
        "## 결과",
        "",
        "| 조건 | 질문 | 화요일 인터벌 결정 | 조정 | 유지 | 컨디션 수치 인용 | G5 | 판정 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for s in summary:
        lines.append(f"| {SCENARIOS[s['scenario']]} | {QUESTIONS[s['qid']]} | {', '.join(s['decisions'])} | "
                     f"{s['adjusted']}/{repeat} | {s['kept']}/{repeat} | {s['cites_condition']}/{repeat} | "
                     f"{'한도 안' if s['g5_ok'] else '초과'} | {'통과' if s['passed'] else '실패'} |")
    passed = sum(s["passed"] for s in summary)
    lines += ["", f"통과 {passed}/{len(summary)}", "", "## 회차별 판정 근거", ""]
    for r in rows:
        lines.append(f"- [{r['scenario']} {r['qid']} #{r['run']}] **{r['decision']}** — {r['reason']}"
                     f" (G5 {r['g5_status']}, 주간 합계 {r['weekly_totals_km'] or '-'})")
    path = EVAL_DIR / "condition_report.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path)


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repeat", type=int, default=3, help="조건·질문마다 몇 번 물을지")
    parser.add_argument("--model", default=None, help="에이전트 모델 (사용 한도가 찼을 때 바꾼다)")
    parser.add_argument("--region", default=None, help="에이전트 리전")
    parser.add_argument("--judge-region", default=None, help="심사 리전")
    parser.add_argument("--no-report", action="store_true", help="리포트를 쓰지 않는다 (시험 실행)")
    args = parser.parse_args()
    check_model(args.model, args.region)
    choice = {k: v for k, v in {"model": args.model, "region": args.region}.items() if v}
    # 화면을 쓰며 쌓인 기억(코치 피드백, 승인한 조정안)이 결과에 섞이지 않게 빈 임시 폴더를 쓴다
    os.environ["RUNCOACH_STATE_DIR"] = tempfile.mkdtemp(prefix="tempo-eval-state-")

    judge = make_llm(JUDGE_MODEL, region=args.judge_region).with_structured_output(TuesdayDecision)
    sem = asyncio.Semaphore(CONCURRENCY)
    started, rows = time.time(), []
    for scenario in SCENARIOS:
        # 조건은 MCP 서버(자식 프로세스)가 뜰 때 정해지므로 조건마다 앱을 새로 연다
        os.environ["RUNCOACH_CONDITION"] = scenario
        from src.agent import open_app

        async with open_app() as app:
            jobs = [run_one(app, judge, scenario, qid, k, sem, choice) for qid in QUESTIONS for k in range(args.repeat)]
            rows += await asyncio.gather(*jobs)
    os.environ.pop("RUNCOACH_CONDITION", None)

    elapsed = time.time() - started
    summary = summarize(rows, args.repeat)
    print()
    for s in summary:
        print(f"{'통과' if s['passed'] else '실패'}  {s['scenario']:6s} {s['qid']}  결정={s['decisions']}  G5={'ok' if s['g5_ok'] else '초과'}")
    if args.no_report:
        return
    (EVAL_DIR / "results").mkdir(exist_ok=True)
    with open(EVAL_DIR / "results" / "condition.json", "w", encoding="utf-8") as f:
        json.dump({"summary": summary, "rows": rows}, f, ensure_ascii=False, indent=2)
    print("리포트:", write_report(rows, summary, args.repeat, elapsed, choice))


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
