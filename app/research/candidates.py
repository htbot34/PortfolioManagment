"""Generate new-position candidates that match the investor's themes + risk profile."""
import json

from app.config import settings, risk_profile
from app.portfolio.store import Account
from app.research import prompts


def candidates(account: Account) -> dict:
    if not settings.anthropic_api_key:
        return {"error": "ANTHROPIC_API_KEY not set", "candidates": []}
    from anthropic import Anthropic
    client = Anthropic(api_key=settings.anthropic_api_key)
    held = [p.ticker for p in account.positions]
    risk = risk_profile()
    msg = client.messages.create(
        model=settings.claude_research_model,
        max_tokens=1200,
        system=prompts.SYSTEM_CANDIDATES,
        messages=[{
            "role": "user",
            "content": [{
                "type": "text",
                "text": (
                    f"RISK PROFILE:\n{json.dumps(risk, indent=2)}\n\n"
                    f"ALREADY HELD (do not propose duplicates): {held}\n\n"
                    "Output the JSON candidates list now."
                ),
            }],
        }],
    )
    raw = msg.content[0].text if msg.content else "{}"
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"raw": raw, "candidates": []}
