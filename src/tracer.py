"""tracer.py - 실행 과정을 모으는 콜백 핸들러와 JSONL 기록.

Supervisor 구조에서는 서브 에이전트 안의 도구 호출이 최상위 messages에 올라오지 않는다.
그래서 콜백으로 모든 도구 호출을 잡아 API 응답의 `trace`와 `contexts`를 만든다
(7일차 local_tracer.py, run_eval.py의 ToolRecorder와 같은 방식).

LangSmith 트레이스는 .env 의 LANGSMITH_TRACING=true 로 켜며 코드 변경 없이 함께 기록된다.
"""
import json
import time

from langchain_core.callbacks import BaseCallbackHandler

from src.config import LOG_DIR

MAX_OUTPUT_CHARS = 1200


def _to_text(value) -> str:
    """도구 출력(문자열, ToolMessage, 블록 리스트 등)을 문자열로 바꾼다."""
    content = getattr(value, "content", value)
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                parts.append(str(block.get("text", "")))
            else:
                parts.append(str(getattr(block, "text", block)))
        return "".join(parts)
    return str(content)


class RunRecorder(BaseCallbackHandler):
    """질의 한 건의 실행 과정을 기록한다.

    steps     [{step, agent, input, output, latency_s}]  -> API 응답의 trace
    contexts  [{doc_id, source, text}]                    -> API 응답의 contexts
    tools     호출된 도구 이름 목록 (평가에서 expected_tools와 비교)
    agents    거쳐 간 서브 에이전트 이름 목록
    """

    def __init__(self):
        self.steps: list[dict] = []
        self.contexts: list[dict] = []
        self.tools: list[str] = []
        self.agents: list[str] = []
        self._open: dict = {}          # run_id -> (도구 이름, 입력, 시작 시각, 담당)

    def add_step(self, step: str, input_text: str, output_text: str, agent: str = "pipeline"):
        """콜백이 아닌 파이프라인 단계(가드레일 등)를 직접 기록한다."""
        self.steps.append({
            "step": step, "agent": agent,
            "input": str(input_text)[:MAX_OUTPUT_CHARS],
            "output": str(output_text)[:MAX_OUTPUT_CHARS],
        })

    # ---- 도구 ----
    @staticmethod
    def _agent_of(metadata: dict | None) -> str:
        """이 도구를 부른 담당 이름. LangGraph가 콜백에 넘기는 실행 경로에서 읽는다.

        경로는 'data_agent:<id>|tools:<id>' 형태이고 맨 앞 조각이 최상위 노드(담당)다.
        위임 순서로 추측하면 Supervisor가 여러 담당을 한꺼번에 불렀을 때 담당이 뒤섞였다.
        """
        ns = (metadata or {}).get("langgraph_checkpoint_ns", "")
        top = ns.split("|")[0].split(":")[0]
        return top or (metadata or {}).get("langgraph_node", "") or "supervisor"

    def on_tool_start(self, serialized, input_str, *, run_id, metadata=None, **kwargs):
        name = (serialized or {}).get("name", "") or kwargs.get("name", "")
        self._open[run_id] = (name, str(input_str), time.time(), self._agent_of(metadata))

    def on_tool_end(self, output, *, run_id, **kwargs):
        name, input_str, started, agent = self._open.pop(run_id, ("", "", time.time(), ""))
        elapsed = round(time.time() - started, 2)

        # langgraph-supervisor는 담당 이동을 transfer_to_<agent> 도구로 표현한다
        if name.startswith("transfer_to_"):
            target = name[len("transfer_to_"):]
            if target not in self.agents:
                self.agents.append(target)
            self.steps.append({"step": "route", "agent": "supervisor",
                               "input": "", "output": f"{target} 에게 위임"})
            return
        if name.startswith("transfer_back_to_"):
            return

        text = _to_text(output)
        self.tools.append(name)
        self.steps.append({
            "step": name, "agent": agent,
            "input": input_str[:MAX_OUTPUT_CHARS],
            "output": text[:MAX_OUTPUT_CHARS],
            "latency_s": elapsed,
        })
        if name == "retrieve_training_docs":
            self._collect_contexts(text)

    def on_tool_error(self, error, *, run_id, **kwargs):
        name, input_str, _, agent = self._open.pop(run_id, ("", "", 0, ""))
        self.tools.append(name)
        self.steps.append({"step": name, "agent": agent,
                           "input": input_str[:300], "output": f"[오류] {error}"})

    def _collect_contexts(self, text: str):
        try:
            hits = json.loads(text)
        except (TypeError, ValueError):
            return
        seen = {c["doc_id"] for c in self.contexts}
        for hit in hits if isinstance(hits, list) else []:
            if isinstance(hit, dict) and hit.get("doc_id") not in seen:
                seen.add(hit.get("doc_id"))
                self.contexts.append({
                    "doc_id": hit.get("doc_id", ""),
                    "source": hit.get("source", ""),
                    "text": hit.get("text", ""),
                })


def write_trace_log(record: dict) -> None:
    """질의 한 건의 요약을 logs/trace.jsonl 에 한 줄로 남긴다."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    record = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), **record}
    with open(LOG_DIR / "trace.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
