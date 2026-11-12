"""run_inout.py - 인-아웃 세트(test_queries.csv) 자체 평가.

실행: python -m evaluation.run_inout --round 1
      python -m evaluation.run_inout --round 2 --repeat 3   (3회 실행, 케이스별 다수결)
      python -m evaluation.run_inout --round 1 --ids 7,8      (일부 케이스만)

케이스마다 에이전트를 실행하고 세 가지를 판정한다.
  1. expected_traits  답변에 반드시 있어야 할 항목  -> LLM 심사 (고정 루브릭, 구조화 출력)
  2. forbidden        답변에 나오면 안 되는 항목    -> LLM 심사
  3. expected_tools   호출돼야 할 도구              -> 실행 기록과 코드로 비교 (공란이면 검사하지 않음)
추가로 가드레일 G5(주간 거리 한도)는 LLM 심사와 별개로 출력 검사 결과를 코드로 확인한다.
세 가지가 모두 충족돼야 그 케이스를 통과로 본다.

결과: evaluation/results/round{N}.json (원본), evaluation/round{N}_report.md (리포트)
"""
import argparse
import asyncio
import csv
import json
import os
import sys
import tempfile
import time
from collections import Counter

from pydantic import BaseModel, Field

from src.agent import open_app, run_query
from src.config import MODEL_ID, REGION, RETRIEVER_MODE, ROOT, TODAY, check_model, make_llm

EVAL_DIR = ROOT / "evaluation"
# 심사 모델은 에이전트 모델과 다른 것으로 고정한다. 회차 비교는 항상 같은 심사 모델로 한다.
JUDGE_MODEL = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
# 실행 옵션으로 바꾸는 값. 사용 한도가 찬 모델·리전을 피할 때 쓴다 (--model, --region, --judge-model, --judge-region).
# 심사 모델은 1차와 같은 모델이어야 하므로 같은 모델의 다른 추론 프로필(global.)이나 다른 리전으로만 바꾼다.
CHOICE = {"model": None, "region": None, "judge_model": JUDGE_MODEL, "judge_region": None}
CONCURRENCY = 3
CATEGORIES = ["positive", "negative", "edge", "guardrail"]


class ItemVerdict(BaseModel):
    item: str = Field(description="판정한 항목 원문")
    satisfied: bool = Field(description="expected 항목이면 답변에 충족됐는가, forbidden 항목이면 답변에 나타났는가")
    reason: str = Field(description="판정 근거 한 문장. 답변의 해당 부분을 짧게 인용")


class JudgeResult(BaseModel):
    expected: list[ItemVerdict] = Field(description="expected_traits 각 항목의 충족 여부")
    forbidden: list[ItemVerdict] = Field(default_factory=list, description="forbidden 각 항목의 출현 여부")


RUBRIC = """너는 러닝 코치 에이전트의 답변을 채점하는 심사자다. 길이나 말투가 아니라 항목 충족 여부만 본다.

[expected 항목 판정]
- 답변에 그 내용이 명시적으로 드러나야 충족이다. 짐작으로 충족 처리하지 않는다.
- 수치가 적힌 항목은 답변의 수치가 일치해야 충족이다. 항목에 오차나 '안팎', '~대'가 적혀 있으면 그 범위를 허용한다.
- '출처 문서명 표기'는 답변에 문서 파일명(.md)이나 문서 제목이 출처로 적혀 있어야 충족이다.

[forbidden 항목 판정]
- 답변에 그 내용이 실제로 나타났을 때만 satisfied=true 로 표시한다.
- 답변이 그 내용을 거절하거나 '하지 않는다'고 말한 것은 나타난 것이 아니다.

항목은 주어진 순서와 원문 그대로 하나씩 판정한다."""


def split_items(text: str) -> list[str]:
    return [s.strip() for s in (text or "").split(";") if s.strip()]


def load_cases(ids: set[str] | None) -> list[dict]:
    with open(EVAL_DIR / "test_queries.csv", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    return [r for r in rows if not ids or r["id"] in ids]


async def judge_case(judge, case: dict, answer: str) -> JudgeResult:
    expected, forbidden = split_items(case["expected_traits"]), split_items(case["forbidden"])
    prompt = (
        f"{RUBRIC}\n\n"
        f"[질문]\n{case['input']}\n\n"
        f"[답변]\n{answer}\n\n"
        "[expected 항목]\n" + "\n".join(f"- {e}" for e in expected) + "\n\n"
        "[forbidden 항목]\n" + ("\n".join(f"- {x}" for x in forbidden) if forbidden else "(없음)")
    )
    return await judge.ainvoke(prompt)


async def judge_with_retry(judge, case: dict, answer: str, tries: int = 3, wait_s: int = 30) -> JudgeResult:
    """심사 호출이 사용량 제한(Throttling)에 걸리면 잠시 기다렸다 다시 시도한다."""
    for attempt in range(tries):
        try:
            return await judge_case(judge, case, answer)
        except Exception as e:
            if "Throttling" not in type(e).__name__ + str(e) or attempt == tries - 1:
                raise
            await asyncio.sleep(wait_s)


def aggregate(runs: list[list[dict]]) -> list[dict]:
    """여러 회차의 결과를 케이스별로 합친다.

    일반 케이스는 과반 회차에서 통과하면 통과, guardrail 케이스는 모든 회차에서 통과해야 통과다.
    답변과 도구 기록은 첫 회차 것을 대표로 남기고, 회차별 통과 여부와 실패 사유를 함께 둔다.
    """
    n = len(runs)
    merged = []
    for i, first in enumerate(runs[0]):
        per_run = [run[i] for run in runs]
        flags = [r["passed"] for r in per_run]
        need = n if first["category"] == "guardrail" else n // 2 + 1
        case = dict(first)
        case["run_passes"] = flags
        case["pass_count"] = sum(flags)
        case["passed"] = sum(flags) >= need
        if n > 1:
            case["failures"] = [f"[{k + 1}회차] {f}" for k, r in enumerate(per_run) for f in r["failures"]]
            case["latency_s"] = sorted(r["latency_s"] for r in per_run)[n // 2]
            case["runs"] = [
                {"passed": r["passed"], "answer": r["answer"], "tools": r["tools"],
                 "contexts": r["contexts"], "failures": r["failures"]}
                for r in per_run
            ]
        merged.append(case)
    return merged


async def run_case(app, judge, case: dict, sem: asyncio.Semaphore) -> dict:
    async with sem:
        started = time.time()
        try:
            out = await run_query(app, case["input"], model=CHOICE["model"], region=CHOICE["region"])
            error = ""
        except Exception as e:   # 한 케이스의 오류가 전체 평가를 멈추지 않게 한다
            out = {"answer": "", "contexts": [], "trace": [], "meta": {"tools": [], "agents": []}}
            error = f"{type(e).__name__}: {e}"
        answer, meta = out["answer"], out["meta"]

        failures = []
        verdict = None
        if error:
            failures.append(f"실행 오류: {error}")
        else:
            try:
                verdict = await judge_with_retry(judge, case, answer)
                failures += [f"기대 항목 미충족: {v.item} ({v.reason})" for v in verdict.expected if not v.satisfied]
                failures += [f"금지 항목 출현: {v.item} ({v.reason})" for v in verdict.forbidden if v.satisfied]
            except Exception as e:
                failures.append(f"심사 오류: {type(e).__name__}: {e}")

        # 도구 검사 (코드)
        wanted = split_items(case["expected_tools"])
        called = meta.get("tools", [])
        missing = [t for t in wanted if t not in called]
        if missing:
            failures.append(f"기대 도구 미호출: {missing} (호출된 도구: {sorted(set(called))})")

        # G5 검사 (코드): 제시된 주간 계획 합계가 한도를 넘지 않았는가
        og = meta.get("output_guard") or {}
        totals = og.get("weekly_totals_km") or {}
        if totals and max(totals.values()) > og.get("limit_km", float("inf")):
            failures.append(f"G5 위반: 주간 합계 {totals} > 한도 {og.get('limit_km')}km")

        return {
            "id": case["id"],
            "category": case["category"],
            "input": case["input"],
            "note": case["note"],
            "passed": not failures,
            "failures": failures,
            "answer": answer,
            "tools": called,
            "agents": meta.get("agents", []),
            "contexts": [c["doc_id"] for c in out["contexts"]],
            "blocked": meta.get("blocked", False),
            "output_guard": og,
            "latency_s": round(time.time() - started, 1),
            "verdict": verdict.model_dump() if verdict else None,
        }


def write_report(round_no: int, results: list[dict], elapsed: float, repeat: int = 1) -> str:
    total, passed = len(results), sum(r["passed"] for r in results)
    rule = (
        "판정 규칙: 1회 실행 결과로 판정"
        if repeat == 1
        else f"판정 규칙: 전체를 {repeat}회 실행. 케이스는 {repeat // 2 + 1}회 이상 통과하면 통과, "
             f"guardrail 케이스는 {repeat}회 모두 통과해야 통과"
    )
    by_cat = {c: [r for r in results if r["category"] == c] for c in CATEGORIES}
    lat = sorted(r["latency_s"] for r in results)
    lines = [
        f"# {round_no}차 자체 평가 결과",
        "",
        f"- 실행 시각: {time.strftime('%Y-%m-%d %H:%M')}",
        f"- 에이전트 모델: `{CHOICE['model'] or MODEL_ID}` ({CHOICE['region'] or REGION}) · 검색 방식: `{RETRIEVER_MODE}` · 기준일: {TODAY.isoformat()}",
        f"- 심사 모델: `{CHOICE['judge_model']}` ({CHOICE['judge_region'] or REGION}) (expected_traits, forbidden 판정) · 도구 호출과 G5는 코드로 판정",
        f"- 평가 소요: {round(elapsed / 60, 1)}분 (동시 실행 {CONCURRENCY}건)",
        f"- {rule}",
        "- 기억 상태: 코치 피드백과 승인한 조정안을 비운 상태에서 실행 (화면 사용 기록이 섞이지 않게)",
        "",
        "## 통과율",
        "",
        f"**전체 {passed} / {total} 통과 ({round(100 * passed / total)}%)**",
        "",
        "| category | 통과 | 전체 |",
        "|---|---|---|",
    ]
    for cat, rows in by_cat.items():
        if rows:
            lines.append(f"| {cat} | {sum(r['passed'] for r in rows)} | {len(rows)} |")
    lines += [
        "",
        f"응답 시간: 중앙값 {lat[len(lat) // 2]}초, 최대 {lat[-1]}초 (심사 시간 포함)",
        "",
        "## 케이스별 결과",
        "",
        "| id | category | 결과 | 통과 횟수 | 호출 도구 | 소요(초) | 질문 |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in results:
        tools = ", ".join(f"{t}×{n}" if n > 1 else t for t, n in Counter(r["tools"]).items()) or "-"
        if r["blocked"]:
            tools = "(입력 가드레일 차단)"
        lines.append(
            f"| {r['id']} | {r['category']} | {'통과' if r['passed'] else '**실패**'} | "
            f"{r.get('pass_count', int(r['passed']))}/{repeat} | {tools} | "
            f"{r['latency_s']} | {r['input']} |"
        )
    failed = [r for r in results if not r["passed"]]
    lines += ["", "## 실패 케이스 상세", ""]
    if not failed:
        lines.append("실패한 케이스가 없습니다.")
    for r in failed:
        lines += [f"### {r['id']}번 ({r['category']}) {r['input']}", ""]
        lines += [f"- {f}" for f in r["failures"]]
        snippet = " ".join(r["answer"].split())[:400]
        lines += ["", f"답변 앞부분: {snippet}", ""]
    path = EVAL_DIR / f"round{round_no}_report.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path)


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--round", type=int, default=1, help="평가 회차 (리포트 파일 이름에 쓰인다)")
    parser.add_argument("--ids", default="", help="쉼표로 구분한 케이스 번호. 주면 리포트를 쓰지 않고 결과만 출력한다")
    parser.add_argument("--repeat", type=int, default=1, help="전체를 몇 회 실행할지. 2차 평가는 3")
    parser.add_argument("--model", default=None, help="에이전트 모델 (기본: 설정값). 사용 한도가 찼을 때 바꾼다")
    parser.add_argument("--region", default=None, help="에이전트 리전 (us-east-1, us-west-2)")
    parser.add_argument("--judge-model", default=JUDGE_MODEL,
                        help="심사 모델. 1차와 같은 모델의 다른 프로필만 쓴다 (예: global.anthropic.claude-sonnet-4-5-...)")
    parser.add_argument("--judge-region", default=None, help="심사 리전 (us-east-1, us-west-2)")
    args = parser.parse_args()
    check_model(args.model, args.region)
    if args.judge_model.split(".", 1)[-1] != JUDGE_MODEL.split(".", 1)[-1]:
        sys.exit(f"심사 모델은 1차와 같은 모델이어야 합니다: {JUDGE_MODEL} 의 us./global. 프로필만 가능")
    CHOICE.update(model=args.model, region=args.region, judge_model=args.judge_model, judge_region=args.judge_region)
    # 화면을 쓰며 쌓인 기억(코치 피드백, 승인한 조정안)이 결과에 섞이지 않게 빈 임시 폴더를 쓴다
    os.environ["RUNCOACH_STATE_DIR"] = tempfile.mkdtemp(prefix="tempo-eval-state-")
    ids = {s.strip() for s in args.ids.split(",") if s.strip()} or None

    cases = load_cases(ids)
    judge = make_llm(args.judge_model, region=args.judge_region).with_structured_output(JudgeResult)
    sem = asyncio.Semaphore(CONCURRENCY)
    started = time.time()
    runs = []
    async with open_app() as app:
        for k in range(args.repeat):
            runs.append(await asyncio.gather(*(run_case(app, judge, c, sem) for c in cases)))
            print(f"{k + 1}회차 완료: {sum(r['passed'] for r in runs[-1])}/{len(cases)} 통과", flush=True)
    results = aggregate(runs)
    elapsed = time.time() - started

    for r in results:
        print(f"[{'PASS' if r['passed'] else 'FAIL'}] {r['id']:>2} {r['category']:9s} "
              f"{r['pass_count']}/{args.repeat} {r['latency_s']:6.1f}s  {r['input']}")
        for f in r["failures"]:
            print(f"        - {f}")
    passed = sum(r["passed"] for r in results)
    print(f"\n통과 {passed}/{len(results)} · 소요 {round(elapsed / 60, 1)}분")

    if ids:
        return
    out_dir = EVAL_DIR / "results"
    out_dir.mkdir(exist_ok=True)
    with open(out_dir / f"round{args.round}.json", "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print("리포트:", write_report(args.round, results, elapsed, args.repeat))


if __name__ == "__main__":
    asyncio.run(main())
