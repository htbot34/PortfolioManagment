"""New-position candidates. LLM-only feature (no rule-based fallback yet)."""
import json

from app.config import risk_profile
from app.portfolio.store import Account
from app.research import llm, prompts


def candidates(account: Account) -> dict:
    if not llm.available():
        return {"error": "Set GITHUB_TOKEN (or wait for the scheduled GitHub Action) to enable candidate generation.",
                "candidates": []}
    held = [p.ticker for p in account.positions]
    risk = risk_profile()
    user = (
        f"RISK PROFILE:\n{json.dumps(risk, indent=2)}\n\n"
        f"ALREADY HELD (do not propose duplicates): {held}\n\n"
        "Output the JSON candidates now."
    )
    refined = llm.chat_json(prompts.SYSTEM_CANDIDATES, user, max_tokens=900)
    if refined and "candidates" in refined:
        return refined
    return {"candidates": []}
