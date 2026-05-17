"""GitHub Models client. Free LLM via OpenAI-compatible API.

Two model tiers:
  - ROUTINE: gpt-4o-mini, used for per-ticker JSON analyses
  - SYNTHESIS: gpt-4o-mini (default), used for the daily brief and
    candidate ranking. Configurable to a higher tier model via env var.

chat_json automatically retries with a fallback model if the primary
returns a non-200 or empty payload (rate limits / 5xx).
"""
import json
import os

import httpx

from app.config import settings

_ENDPOINT = "https://models.github.ai/inference/chat/completions"
_FALLBACKS = ["openai/gpt-4o-mini", "meta/Llama-3.3-70B-Instruct"]


def routine_model() -> str:
    return os.getenv("GITHUB_MODEL_ROUTINE", os.getenv("GITHUB_MODEL", "openai/gpt-4o-mini"))


def synthesis_model() -> str:
    return os.getenv("GITHUB_MODEL_SYNTHESIS", "openai/gpt-4o-mini")


def available() -> bool:
    return bool(settings.github_token)


LAST_ERROR: dict = {}


def _call(model: str, system: str, user: str, max_tokens: int, temperature: float) -> dict | None:
    global LAST_ERROR
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
            timeout=180,
        )
    except Exception as e:
        msg = f"exception (model={model}): {e}"
        print(f"  LLM {msg}")
        LAST_ERROR = {"model": model, "kind": "exception", "msg": str(e)[:300]}
        return None
    if r.status_code != 200:
        msg = f"failed model={model} HTTP {r.status_code} body={r.text[:300]}"
        print(f"  LLM {msg}")
        LAST_ERROR = {"model": model, "kind": "http", "status": r.status_code, "body": r.text[:600]}
        return None
    try:
        body = r.json()
        finish_reason = body["choices"][0].get("finish_reason")
        text = body["choices"][0]["message"]["content"]
        out = json.loads(text)
        LAST_ERROR = {"model": model, "kind": "ok", "finish_reason": finish_reason,
                      "input_tokens": body.get("usage", {}).get("prompt_tokens"),
                      "output_tokens": body.get("usage", {}).get("completion_tokens")}
        return out
    except Exception as e:
        msg = f"parse failed (model={model}): {e}; body[:300]={r.text[:300]}"
        print(f"  LLM {msg}")
        LAST_ERROR = {"model": model, "kind": "parse_error", "msg": str(e)[:200],
                      "body_excerpt": r.text[:600]}
        return None


def chat_json(system: str, user: str, *, model: str | None = None,
              max_tokens: int = 1200, temperature: float = 0.3) -> dict | None:
    if not settings.github_token:
        return None
    primary = model or routine_model()
    tried: set[str] = set()
    for m in [primary, *_FALLBACKS]:
        if m in tried:
            continue
        tried.add(m)
        out = _call(m, system, user, max_tokens, temperature)
        if out:
            out.setdefault("_model_used", m)
            return out
    return None
