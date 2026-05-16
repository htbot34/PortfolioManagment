"""GitHub Models client. Free LLM via OpenAI-compatible API.

In GitHub Actions, set `permissions: { models: read }` on the workflow and pass
the workflow's GITHUB_TOKEN through to this process. Locally, create a PAT with
`models:read` scope and export it as GITHUB_TOKEN.

Returns None on any failure (no key, rate limit, network) so the caller can
gracefully fall back to rule-based output.
"""
import json

import httpx

from app.config import settings

_ENDPOINT = "https://models.github.ai/inference/chat/completions"


def available() -> bool:
    return bool(settings.github_token)


def chat_json(system: str, user: str, max_tokens: int = 900) -> dict | None:
    if not settings.github_token:
        return None
    try:
        r = httpx.post(
            _ENDPOINT,
            headers={
                "Authorization": f"Bearer {settings.github_token}",
                "Content-Type": "application/json",
            },
            json={
                "model": settings.github_model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "max_tokens": max_tokens,
                "temperature": 0.3,
                "response_format": {"type": "json_object"},
            },
            timeout=60,
        )
        if r.status_code != 200:
            return None
        body = r.json()
        text = body["choices"][0]["message"]["content"]
        return json.loads(text)
    except Exception:
        return None
