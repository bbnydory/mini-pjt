#!/usr/bin/env bash
# TEMPO 실행 스크립트: 데이터와 색인이 없으면 만들고 API 서버(웹 화면 포함)를 띄운다.
# 사용: ./run.sh          -> http://localhost:8000
#       PORT=8765 ./run.sh
set -e
cd "$(dirname "$0")"

PY=python
[ -x .venv/bin/python ] && PY=.venv/bin/python
[ -x .venv/Scripts/python.exe ] && PY=.venv/Scripts/python.exe

[ -f data/runner/profile.json ] || "$PY" -m src.make_data
[ -d data/chroma_db ] || "$PY" -m src.retriever --build

exec "$PY" -m uvicorn src.api:api --port "${PORT:-8000}"
