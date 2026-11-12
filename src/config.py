"""config.py - 경로, 모델, 기준일 등 프로젝트 전역 설정.

모든 경로를 이 파일 위치 기준의 절대 경로로 잡아, 어느 폴더에서 실행해도 같게 동작한다.
"""
import os
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import date, timedelta
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

# ---------- 경로 ----------
DATA_DIR = ROOT / "data"
DOCS_DIR = DATA_DIR / "docs"          # 훈련 이론 문서 (RAG 원본)
RUNNER_DIR = DATA_DIR / "runner"      # 가짜 러너 데이터 (make_data.py가 생성)
CHROMA_DIR = DATA_DIR / "chroma_db"   # 벡터 DB (retriever.py가 생성)
LOG_DIR = ROOT / "logs"               # 트레이스와 가드레일 기록


def state_dir() -> Path:
    """사용자가 남긴 상태(코치 피드백, 승인한 조정안)를 두는 폴더. 평소에는 data/.

    평가 스크립트는 RUNCOACH_STATE_DIR 를 빈 임시 폴더로 바꿔, 화면을 쓰며 쌓인 기억이
    평가 결과에 섞이지 않게 한다. 호출할 때마다 읽으므로 실행 중에 바꿔도 반영된다.
    """
    return Path(os.getenv("RUNCOACH_STATE_DIR") or DATA_DIR)

# ---------- 모델 ----------
REGION = os.getenv("RUNCOACH_REGION", "us-east-1")
MODEL_ID = os.getenv("RUNCOACH_MODEL", "us.anthropic.claude-sonnet-4-6")
FAST_MODEL_ID = os.getenv("RUNCOACH_FAST_MODEL", "us.anthropic.claude-haiku-4-5-20251001-v1:0")
EMBED_MODEL_ID = "amazon.titan-embed-text-v2:0"

# 고를 수 있는 모델과 리전. Bedrock 일일 토큰 한도는 모델·리전마다 따로 차므로,
# 한쪽이 막히면 다른 모델이나 다른 리전(또는 global 추론 프로필)으로 바꿔 쓴다.
# 모두 도구 호출과 구조화 출력이 되는 것을 확인했다 (2026-11-11). vision 은 이미지·영상 입력 가능 여부.
REGIONS = ["us-east-1", "us-west-2"]
MODEL_CHOICES = {
    "us.anthropic.claude-sonnet-4-6": {"label": "Claude Sonnet 4.6 (기본)", "vision": True},
    "global.anthropic.claude-sonnet-4-6": {"label": "Claude Sonnet 4.6 · global", "vision": True},
    "us.anthropic.claude-sonnet-4-5-20250929-v1:0": {"label": "Claude Sonnet 4.5", "vision": True},
    "global.anthropic.claude-sonnet-4-5-20250929-v1:0": {"label": "Claude Sonnet 4.5 · global", "vision": True},
    "us.anthropic.claude-haiku-4-5-20251001-v1:0": {"label": "Claude Haiku 4.5 (빠름)", "vision": True},
    "global.anthropic.claude-haiku-4-5-20251001-v1:0": {"label": "Claude Haiku 4.5 · global", "vision": True},
    "us.amazon.nova-pro-v1:0": {"label": "Amazon Nova Pro", "vision": True},
    "us.amazon.nova-lite-v1:0": {"label": "Amazon Nova Lite", "vision": True},
    "us.amazon.nova-2-lite-v1:0": {"label": "Amazon Nova 2 Lite", "vision": True},
    "global.amazon.nova-2-lite-v1:0": {"label": "Amazon Nova 2 Lite · global", "vision": True},
    "us.amazon.nova-micro-v1:0": {"label": "Amazon Nova Micro (글자만)", "vision": False},
    "openai.gpt-oss-120b-1:0": {"label": "gpt-oss 120B (글자만, us-east-1)", "vision": False,
                                "regions": ["us-east-1"]},
    "openai.gpt-oss-20b-1:0": {"label": "gpt-oss 20B (글자만, us-east-1)", "vision": False,
                               "regions": ["us-east-1"]},
}

# 요청 하나가 쓸 (모델, 리전). run_query 가 요청 동안만 설정하고, make_llm 이 읽는다.
_selected: ContextVar[tuple[str | None, str | None]] = ContextVar("llm_selected", default=(None, None))


def check_model(model: str | None, region: str | None) -> None:
    """고를 수 있는 모델·리전 조합인지 검사한다. 아니면 ValueError."""
    if model and model not in MODEL_CHOICES:
        raise ValueError(f"고를 수 없는 모델입니다: {model}")
    if region and region not in REGIONS:
        raise ValueError(f"고를 수 없는 리전입니다: {region}")
    allowed = MODEL_CHOICES.get(model or MODEL_ID, {}).get("regions")
    if allowed and (region or REGION) not in allowed:
        raise ValueError(f"{model} 은 {', '.join(allowed)} 에서만 쓸 수 있습니다.")


def current_model() -> tuple[str, str]:
    """지금 요청이 쓰는 (메인 모델, 리전). 고르지 않았으면 기본값."""
    model, region = _selected.get()
    return model or MODEL_ID, region or REGION


@contextmanager
def use_model(model: str | None = None, region: str | None = None):
    """이 블록 안에서 만드는 모델(make_llm)이 고른 모델·리전을 쓰게 한다."""
    token = _selected.set((model, region))
    try:
        yield
    finally:
        _selected.reset(token)

# ---------- 검색 ----------
COLLECTION_NAME = "runcoach_docs"
# vector: 벡터 검색만 (1차 평가 기준선) / hybrid: BM25 + 벡터 앙상블 (2차부터 기본)
RETRIEVER_MODE = os.getenv("RUNCOACH_RETRIEVER", "hybrid")
RETRIEVER_K = int(os.getenv("RUNCOACH_RETRIEVER_K", "4"))
# 문서 단위 확장: 한 문서에서 조각이 이만큼 이상 잡히면 그 문서의 나머지 소제목도 함께 돌려준다.
# 0이면 끈다. 1차 평가 2번에서 '10_tapering#4(강도는 유지한다)'가 상위 6개에도 들지 못해 넣었다.
RETRIEVER_EXPAND_MIN_HITS = int(os.getenv("RUNCOACH_EXPAND_MIN_HITS", "2"))
RETRIEVER_MAX_CHUNKS = int(os.getenv("RUNCOACH_MAX_CHUNKS", "8"))

# ---------- 기준일 ----------
# 가짜 데이터가 고정돼 있으므로 "오늘"도 고정한다 (SERVICE.md 3번 데이터 시나리오).
TODAY = date.fromisoformat(os.getenv("RUNCOACH_TODAY", "2026-11-09"))
RUNNER_ID = "runner_01"

# ---------- 컨디션 시나리오 (조건 대조 평가용) ----------
# tired  : 생성된 데이터 그대로 (11/6~11/8 수면 5시간대, 안정시 심박 57~59, 피로도 4)
# rested : 훈련 기록·계획표는 그대로 두고 최근 컨디션만 회복된 값으로 바꾼다
# 같은 기록에서 컨디션만 바꿨을 때 고강도 훈련 조정이 달라지는지 보려고 둔다 (evaluation/run_condition.py).
CONDITION_SCENARIO = os.getenv("RUNCOACH_CONDITION", "tired")

# ---------- 가드레일 G5 ----------
# 제안 주간 거리는 최근 4주 실제 주간 거리 최댓값의 110%를 넘지 않는다.
WEEKLY_LIMIT_RATIO = 1.10
WEEKLY_LIMIT_LOOKBACK_WEEKS = 4

WEEKDAYS_KO = ["월", "화", "수", "목", "금", "토", "일"]


def week_start(d: date) -> date:
    """d가 속한 주의 월요일."""
    return d - timedelta(days=d.weekday())


def date_context() -> str:
    """프롬프트에 넣는 날짜 안내문. 모델이 '지난주', '이번 주'를 같은 기준으로 해석하게 한다."""
    this_mon = week_start(TODAY)
    last_mon = this_mon - timedelta(days=7)
    return (
        f"오늘은 {TODAY.isoformat()}({WEEKDAYS_KO[TODAY.weekday()]}요일)이다. "
        f"한 주는 월요일에 시작해 일요일에 끝난다. "
        f"이번 주는 {this_mon.isoformat()}~{(this_mon + timedelta(days=6)).isoformat()}, "
        f"지난주는 {last_mon.isoformat()}~{(last_mon + timedelta(days=6)).isoformat()}이다. "
        f"내일은 {(TODAY + timedelta(days=1)).isoformat()}이다."
    )


def make_llm(model: str | None = None, region: str | None = None, **kwargs):
    """Bedrock 채팅 모델을 만든다. temperature는 재현성을 위해 0으로 둔다.

    model·region 을 주지 않으면 지금 요청이 고른 값(use_model), 그것도 없으면 기본값을 쓴다.
    """
    from langchain_aws import ChatBedrockConverse

    kwargs.setdefault("temperature", 0)
    selected_model, selected_region = current_model()
    return ChatBedrockConverse(model=model or selected_model, region_name=region or selected_region, **kwargs)


def get_text(message) -> str:
    """ChatBedrockConverse는 content를 블록 리스트로 주기도 하므로 텍스트만 모아 반환한다."""
    content = getattr(message, "content", message)
    if isinstance(content, list):
        return "".join(
            block.get("text", "") for block in content if isinstance(block, dict)
        )
    return content or ""
