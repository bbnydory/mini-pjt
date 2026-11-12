"""feedback.py - 날짜별 코치 피드백 저장소.

웹 화면에서 훈련 카드를 눌러 에이전트에게 물은 뒤, 그 답변을 해당 날짜의 코치 피드백으로
저장하고 다시 읽는다. data/feedback.json 한 파일에 날짜별 목록으로 쌓는다.

  {"2026-11-08": [{"saved_at": "...", "question": "...", "answer": "...", "summary": "..."}]}

실행 중에 만들어지는 파일이라 저장소에는 올리지 않는다 (.gitignore).
"""
import json
import re
import threading
import time
from datetime import date

from src.config import state_dir

def _path():
    """피드백 파일 위치 (평가 때는 빈 임시 폴더)."""
    return state_dir() / "feedback.json"
_lock = threading.Lock()


def load_all() -> dict[str, list[dict]]:
    """저장된 피드백 전체를 날짜별 목록으로 돌려준다. 파일이 없으면 빈 dict."""
    if not _path().exists():
        return {}
    try:
        with open(_path(), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def summarize(answer: str, limit: int = 90) -> str:
    """카드에 보일 한 줄 요약. 답변의 첫 문장에서 마크다운 기호를 걷어 낸다."""
    for line in answer.splitlines():
        text = re.sub(r"[#*`>|]", "", line).strip(" -")
        if len(text) >= 8:
            first = re.split(r"(?<=[.다요])\s", text, maxsplit=1)[0]
            return first[:limit] + ("…" if len(first) > limit else "")
    return answer.strip()[:limit]


def save(day: str, question: str, answer: str) -> dict:
    """한 날짜에 피드백을 추가하고 저장한 항목을 돌려준다. 날짜 형식이 틀리면 ValueError."""
    date.fromisoformat(day)
    entry = {
        "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "question": question.strip(),
        "answer": answer.strip(),
        "summary": summarize(answer),
    }
    with _lock:
        data = load_all()
        data.setdefault(day, []).append(entry)
        _path().parent.mkdir(parents=True, exist_ok=True)
        with open(_path(), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    return entry


def delete_last(day: str) -> bool:
    """한 날짜의 가장 최근 피드백을 지운다. 지울 것이 없으면 False."""
    with _lock:
        data = load_all()
        if not data.get(day):
            return False
        data[day].pop()
        if not data[day]:
            del data[day]
        with open(_path(), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    return True
