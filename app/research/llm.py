"""GitHub Models client. Free LLM via OpenAI-compatible API.

Two model tiers:
  - ROUTINE: gpt-4o-mini, used for per-ticker JSON analyses (8+ calls per run).
  - SYNTHESIS: gpt-4o, used for the daily brief and other prose synthesis (~2 calls).

Both default to free-tier models. Set GITHUB_MODEL_ROUTINE / GITHUB_MODEL_SYNTHESIS
in env to override.
"""
import json
import os

import httpx

from app.config import settings

_ENDPOINT = "https://models.github.ai/inference/chat/completions"


def routine_model() -> str:
    return os.getenv("GITHUB_MODEL_ROUTINE", os.getenv("GITHUB_MODEL", "openai/gpt-4o-mini"))


def synthesis_model() -> str:
    return os.getenv("GITHUB_MODEL_SYNTHESIS", "openai/gpt-4o")


def available() -> bool:
    return bool(settings.github_token)


def chat_json(system: str, user: str, *, model: str | None = None,
              max_tokens: int = 1200, temperature: float = 0.3) -> dict | None:
    if not settings.github_token:
        return None
    model = model or routine_model()
    try:
        r = httpx.post(
            _ENDPOINT,
            headers={
                "Authorization": f"Bearer {settings.github_token}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "max_tokens": max_tokens,
                "temperature": temperature,
                "response_format": {"type": "json_object"},
            },
            timeout=90,
        )
        if r.status_code != 200:
            print(f"  LLM call failed: HTTP {r.status_code} - {r.text[:200]}")
            return None
        body = r.json()
        text = body["choices"][0]["message"]["content"]
        return json.loads(text)
    except Exception as e:
        print(f"  LLM call exception: {e}")
        return None
