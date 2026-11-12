"""run_ragas.py - RAGAS 4개 지표 측정 (문서 검색 케이스 5건).

실행: python -m evaluation.run_ragas --round 2          2차 평가 첫 회차의 답변과 검색 문서로 측정
      python -m evaluation.run_ragas --live             에이전트를 지금 실행해 측정
      python -m evaluation.run_ragas --live --ids 2     일부 케이스만 (리포트를 쓰지 않는다)

대상은 evaluation/ragas_references.csv 에 정답이 있는 케이스다 (1, 2, 7, 8, 18번).
11번은 정답이 "자료에 없음"이라 재현율을 정의할 수 없어 제외했고, 나머지 12건은 문서 검색이 없다.

지표와 필요한 입력:
  context_recall     정답의 내용이 검색된 문서에 들어 있는가        (질문, 검색 문서, 정답)
  context_precision  검색된 문서 중 정답과 관련된 것이 위에 있는가  (질문, 검색 문서, 정답)
  faithfulness       답변이 검색된 문서에 근거하는가                (질문, 검색 문서, 답변)
  answer_relevancy   답변이 질문에 맞는가                            (질문, 답변)

정답은 어시스턴트가 문서 원문에서 초안을 쓰고 사용자가 원문과 대조해 승인한다.
review_status 가 "승인"으로 시작하지 않는 행이 있으면 --allow-draft 없이는 실행하지 않는다.
RAGAS 수치는 보고용이며 인-아웃 통과 판정에는 쓰지 않는다.
"""
import argparse
import asyncio
import csv
import json
import os
import sys
import tempfile
import time
import types

from src.config import EMBED_MODEL_ID, REGION, ROOT, make_llm

EVAL_DIR = ROOT / "evaluation"
REFERENCES = EVAL_DIR / "ragas_references.csv"
# 채점 모델은 인-아웃 심사와 같은 모델로 고정한다
RAGAS_MODEL = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
TARGETS = {"context_recall": 0.70, "context_precision": 0.70, "faithfulness": 0.80, "answer_relevancy": 0.80}


def _import_ragas():
    """RAGAS를 불러온다.

    ragas 0.4.3 은 예전 langchain_community 경로(chat_models.vertexai, llms.VertexAI)를 불러오는데
    지금 쓰는 langchain_community 에는 없다. 쓰지 않는 경로라 빈 모듈로 채운 뒤 불러온다.
    """
    name = "langchain_community.chat_models.vertexai"
    if name not in sys.modules:
        stub = types.ModuleType(name)
        stub.ChatVertexAI = type("ChatVertexAI", (), {})
        sys.modules[name] = stub
    import langchain_community.llms as community_llms

    if not hasattr(community_llms, "VertexAI"):
        community_llms.VertexAI = type("VertexAI", (), {})

    from ragas import EvaluationDataset, SingleTurnSample, evaluate
    from ragas.embeddings import LangchainEmbeddingsWrapper
    from ragas.llms import LangchainLLMWrapper
    from ragas.metrics import (
        Faithfulness,
        LLMContextPrecisionWithReference,
        LLMContextRecall,
        ResponseRelevancy,
    )

    metrics = {
        "context_recall": LLMContextRecall(),
        "context_precision": LLMContextPrecisionWithReference(),
        "faithfulness": Faithfulness(),
        "answer_relevancy": ResponseRelevancy(),
    }
    return EvaluationDataset, SingleTurnSample, evaluate, LangchainLLMWrapper, LangchainEmbeddingsWrapper, metrics


def load_references(ids: set[str] | None) -> list[dict]:
    """정답 파일을 읽는다."""
    with open(REFERENCES, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    return [r for r in rows if not ids or r["id"] in ids]


def from_round(round_no: int, refs: list[dict]) -> dict[str, dict]:
    """저장된 평가 결과(첫 회차)에서 답변과 검색 문서를 가져온다."""
    from src.retriever import load_chunks

    texts = {c.metadata["doc_id"]: c.page_content for c in load_chunks()}
    with open(EVAL_DIR / "results" / f"round{round_no}.json", encoding="utf-8") as f:
        results = {r["id"]: r for r in json.load(f)}
    out = {}
    for ref in refs:
        case = results[ref["id"]]
        first = (case.get("runs") or [case])[0]
        out[ref["id"]] = {
            "response": first["answer"],
            "context_ids": first["contexts"],
            "contexts": [texts[i] for i in first["contexts"] if i in texts],
        }
    return out


async def from_live(refs: list[dict]) -> dict[str, dict]:
    """에이전트를 지금 실행해 답변과 검색 문서를 얻는다."""
    from src.agent import open_app, run_query

    out = {}
    async with open_app() as app:
        for ref in refs:
            res = await run_query(app, ref["question"])
            out[ref["id"]] = {
                "response": res["answer"],
                "context_ids": [c["doc_id"] for c in res["contexts"]],
                "contexts": [c["text"] for c in res["contexts"]],
            }
    return out


def write_report(rows: list[dict], source: str, model: str, drafts: int) -> str:
    """케이스별 점수와 평균, 목표치 비교를 마크다운으로 쓴다."""
    names = list(TARGETS)
    mean = {
        n: round(sum(r[n] for r in rows if r[n] is not None) / max(1, sum(r[n] is not None for r in rows)), 3)
        for n in names
    }
    lines = [
        "# RAGAS 평가 결과",
        "",
        f"- 실행 시각: {time.strftime('%Y-%m-%d %H:%M')}",
        f"- 답변과 검색 문서의 출처: {source}",
        f"- 채점 모델: `{model}` · 임베딩: `{EMBED_MODEL_ID}`",
        f"- 대상: 문서 검색 케이스 {len(rows)}건. 표본이 작아 평균은 참고용이며 케이스별 점수를 함께 본다.",
        "- 정답: `evaluation/ragas_references.csv`"
        + (f" (승인 전 초안 {drafts}건 포함)" if drafts else " (review_status 가 모두 승인)"),
        "",
        "## 평균과 목표치",
        "",
        "| 지표 | 평균 | 목표 | 판정 |",
        "|---|---|---|---|",
    ]
    for n in names:
        lines.append(f"| {n} | {mean[n]:.2f} | {TARGETS[n]:.2f} | {'달성' if mean[n] >= TARGETS[n] else '미달'} |")
    lines += ["", "## 케이스별 점수", "", "| id | 질문 | " + " | ".join(names) + " | 검색된 문서 |", "|---|---|" + "---|" * (len(names) + 1)]
    for r in rows:
        scores = " | ".join("-" if r[n] is None else f"{r[n]:.2f}" for n in names)
        lines.append(f"| {r['id']} | {r['question']} | {scores} | {', '.join(r['context_ids'])} |")
    path = EVAL_DIR / "ragas_report.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--round", type=int, default=0, help="저장된 평가 결과 회차 (evaluation/results/roundN.json)")
    parser.add_argument("--live", action="store_true", help="에이전트를 지금 실행해 측정한다")
    parser.add_argument("--ids", default="", help="쉼표로 구분한 케이스 번호. 주면 리포트를 쓰지 않는다")
    parser.add_argument("--allow-draft", action="store_true", help="승인 전 정답으로도 실행한다 (시험용)")
    parser.add_argument("--model", default=RAGAS_MODEL, help="채점 모델 (기본값 외에는 시험용)")
    parser.add_argument("--region", default=None, help="채점 리전 (us-east-1, us-west-2). 사용 한도가 찼을 때 바꾼다")
    args = parser.parse_args()
    ids = {s.strip() for s in args.ids.split(",") if s.strip()} or None

    refs = load_references(ids)
    drafts = sum(not r["review_status"].startswith("승인") for r in refs)
    if drafts and not args.allow_draft:
        sys.exit(f"승인되지 않은 정답이 {drafts}건 있습니다. 원문과 대조해 review_status 를 '승인'으로 바꾸거나 "
                 "--allow-draft 로 시험 실행하세요.")
    if not args.live and not args.round:
        sys.exit("--round N 또는 --live 중 하나를 지정하세요.")

    if args.live:
        # 화면을 쓰며 쌓인 기억(코치 피드백, 승인한 조정안)이 결과에 섞이지 않게 빈 임시 폴더를 쓴다
        os.environ["RUNCOACH_STATE_DIR"] = tempfile.mkdtemp(prefix="tempo-eval-state-")
    data = asyncio.run(from_live(refs)) if args.live else from_round(args.round, refs)
    source = "에이전트 실시간 실행" if args.live else f"{args.round}차 평가 첫 회차 (evaluation/results/round{args.round}.json)"

    EvaluationDataset, SingleTurnSample, evaluate, LLMWrapper, EmbWrapper, metrics = _import_ragas()
    from langchain_aws import BedrockEmbeddings

    samples = [
        SingleTurnSample(
            user_input=r["question"],
            response=data[r["id"]]["response"],
            retrieved_contexts=data[r["id"]]["contexts"],
            reference=r["reference"],
        )
        for r in refs
    ]
    result = evaluate(
        dataset=EvaluationDataset(samples=samples),
        metrics=list(metrics.values()),
        llm=LLMWrapper(make_llm(args.model, region=args.region)),
        embeddings=EmbWrapper(BedrockEmbeddings(model_id=EMBED_MODEL_ID, region_name=args.region or REGION)),
        show_progress=False,
    )

    rows = []
    for ref, scores in zip(refs, result.scores):
        row = {"id": ref["id"], "question": ref["question"], "context_ids": data[ref["id"]]["context_ids"]}
        for name, metric in metrics.items():
            value = scores.get(metric.name)
            row[name] = None if value is None or value != value else round(float(value), 3)   # NaN 은 None
        rows.append(row)
        print(row["id"], {n: row[n] for n in TARGETS}, "| 검색", row["context_ids"])

    if ids:
        return
    (EVAL_DIR / "results").mkdir(exist_ok=True)
    with open(EVAL_DIR / "results" / "ragas.json", "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)
    print("리포트:", write_report(rows, source, args.model, drafts))


if __name__ == "__main__":
    main()
