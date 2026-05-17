"""GitHub Models client. Free LLM via OpenAI-compatible API.

Records every call attempt into ATTEMPTS so failures are visible in the
build output / data.json. Fallback chain only uses OpenAI models (Llama
free tier has an 8k context cap that we exceed on the synthesis call).
"""
import json
import os

import httpx

from app.config import settings

_ENDPOINT = "https://models.github.ai/inference/chat/completions"
_FALLBACKS = ["openai/gpt-4o-mini"]

ATTEMPTS: list[dict] = []


def routine_model() -> str:
    return os.getenv("GITHUB_MODEL_ROUTINE", os.getenv("GITHUB_MODEL", "openai/gpt-4o-mini"))


def synthesis_model() -> str:
    return os.getenv("GITHUB_MODEL_SYNTHESIS", "openai/gpt-4o-mini")


def available() -> bool:
    return bool(settings.github_token)


def reset_attempts() -> None:
    ATTEMPTS.clear()


def _record(entry: dict) -> None:
    ATTEMPTS.append(entry)
    if len(ATTEMPTS) > 50:
        del ATTEMPTS[: len(ATTEMPTS) - 50]


def _call(model: str, system: str, user: str, max_tokens: int, temperature: float,
          tag: str) -> dict | None:
    entry: dict = {"model": model, "tag": tag, "prompt_chars": len(system) + len(user)}
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
        entry.update(kind="exception", msg=str(e)[:300])
        _record(entry)
        print(f"  LLM {entry}")
        return None
    entry["status"] = r.status_code
    if r.status_code != 200:
        entry.update(kind="http_error", body=r.text[:500])
        _record(entry)
        print(f"  LLM {entry}")
        return None
    try:
        body = r.json()
        choice = body["choices"][0]
        finish_reason = choice.get("finish_reason")
        text = choice["message"]["content"]
        out = json.loads(text)
        usage = body.get("usage", {})
        entry.update(kind="ok", finish_reason=finish_reason,
                     input_tokens=usage.get("prompt_tokens"),
                     output_tokens=usage.get("completion_tokens"),
                     total_tokens=usage.get("total_tokens"))
        _record(entry)
        return out
    except Exception as e:
        entry.update(kind="parse_error", msg=str(e)[:200], body=r.text[:500])
        _record(entry)
        print(f"  LLM {entry}")
        return None


def chat_json(system: str, user: str, *, model: str | None = None,
              max_tokens: int = 1200, temperature: float = 0.3,
              tag: str = "") -> dict | None:
    if not settings.github_token:
        return None
    primary = model or routine_model()
    tried: set[str] = set()
    for m in [primary, *_FALLBACKS]:
        if m in tried:
            continue
        tried.add(m)
        out = _call(m, system, user, max_tokens, temperature, tag)
        if out:
            out.setdefault("_model_used", m)
            return out
    return None
