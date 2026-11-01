"""오픈라우터(OpenRouter) 텍스트 생성 모델 호출.

- 모델 3개(1차, 2차, 3차)를 .env / Streamlit Secrets 의 MODEL_1, MODEL_2, MODEL_3 에서 읽는다.
- 모델 하나를 부른 때부터 응답을 받을 때까지 120초가 넘으면 끊고 다음 모델로 넘어간다(전체 대기 시간 제한).
  requests 의 timeout 은 '응답이 시작될 때까지'만 재기 때문에, 별도 스레드에서 호출하고 시계로 직접 잰다.
- openrouter/free 라우터는 쓰지 않는다(공고 4.3절, 쓰면 감점).
"""
from __future__ import annotations

import sys
import threading
import time

import requests

import db

URL = "https://openrouter.ai/api/v1/chat/completions"
LIMIT_SECONDS = 120

# 기본값은 6장 노트가 2026-10-31에 확인한 무료 텍스트 모델입니다. 무료 모델은 예고 없이 목록에서 빠지므로(빠지면 404),
# openrouter.ai/models 에서 지금도 (free) 인지 확인하고 .env 와 Streamlit Secrets 의 MODEL_1, MODEL_2, MODEL_3 로 바꿔 넣으세요.
# 404 나 429 가 나도 앱은 다음 모델로 넘어가고, 셋 다 실패하면 앱이 직접 배정합니다.
DEFAULT_MODELS = [
    "nvidia/nemotron-3.5-super-vl:free",
    "nvidia/nemotron-3-ultra-550b-a55b:free",
    "cohere/north-mini-code:free",
]


def get_models():
    models = []
    for i, default in enumerate(DEFAULT_MODELS, start=1):
        name = (db.get_secret(f"MODEL_{i}", default) or "").strip()
        if not name or name.lower().startswith("openrouter/free"):
            print(f"[경고] MODEL_{i} 가 비어 있거나 openrouter/free 라서 건너뜀", file=sys.stderr)
            continue
        models.append(name)
    return models


def _worker(model, prompt, api_key, out):
    try:
        r = requests.post(
            URL,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 4000,               # 생각하는 데 다 써서 빈 응답이 나오지 않게 넉넉히
                "temperature": 0,
                "reasoning": {"effort": "low"},   # 추론 모델이 오래 생각하지 않게 함
            },
            timeout=(10, LIMIT_SECONDS + 10),     # 끊긴 스레드가 언젠가 끝나게 하는 안전장치
        )
        if r.status_code != 200:
            out["error"] = f"HTTP {r.status_code}"
            return
        data = r.json()
        if isinstance(data, dict) and data.get("error"):
            out["error"] = "오류 응답"
            return
        content = data["choices"][0]["message"]["content"]
        if not isinstance(content, str) or not content.strip():
            out["error"] = "빈 응답"
            return
        out["text"] = content
    except Exception as e:  # 키가 들어 있을 수 있으므로 오류 내용은 버리고 종류만 남긴다
        out["error"] = f"요청 오류({type(e).__name__})"


def ask_model(model, prompt, api_key, limit=LIMIT_SECONDS, on_tick=None):
    """모델 하나에 묻는다. (답 글, None) 또는 (None, 실패 이유)를 돌려준다. limit 초가 넘으면 끊는다."""
    if not api_key:
        return None, "OPENROUTER_API_KEY 가 설정되지 않음"
    out = {}
    th = threading.Thread(target=_worker, args=(model, prompt, api_key, out), daemon=True)
    start = time.monotonic()
    th.start()
    while True:
        th.join(1.0)
        elapsed = time.monotonic() - start
        if not th.is_alive():
            break
        if elapsed >= limit:
            return None, f"{limit}초 안에 응답이 없어 끊음"
        if on_tick:
            on_tick(int(elapsed))
    if "text" in out:
        return out["text"], None
    return None, out.get("error", "알 수 없는 오류")
