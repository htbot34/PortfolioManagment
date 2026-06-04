import os
from functools import lru_cache
from pathlib import Path

import yaml

try:
    from dotenv import load_dotenv
    ROOT = Path(__file__).resolve().parent.parent
    load_dotenv(ROOT / ".env")
except ImportError:
    ROOT = Path(__file__).resolve().parent.parent


# SEC EDGAR rejects placeholder / generic User-Agents per its fair-access
# policy. This is the bundled default - it is intentionally NOT a usable
# contact; CI / .env must override SEC_USER_AGENT with a real monitored
# "Name email@domain". Without one, every CIK lookup 403s and the insider
# signal mutes (now loudly - see app/data/filings.py).
SEC_UA_PLACEHOLDER = "PortfolioAdvisor (set SEC_USER_AGENT=\"YourName your@email.com\")"


class Settings:
    github_token: str | None = os.getenv("GITHUB_TOKEN") or None
    github_model: str = os.getenv("GITHUB_MODEL", "openai/gpt-4o-mini")
    sec_user_agent: str = os.getenv("SEC_USER_AGENT", SEC_UA_PLACEHOLDER)
    # CIK ticker->code index is fetched from sec.gov/files/company_tickers.json
    # and persisted at the repo root so it survives across builds. The map is
    # near-static (new tickers appear rarely), so the TTL is LONG by design:
    # a long TTL means we rarely re-hit SEC, which is exactly when a transient
    # 403/429 would otherwise force a fall-back to stale. Resolution survives
    # any index outage once the map has ever been cached.
    cik_cache_ttl_days: int = int(os.getenv("CIK_CACHE_TTL_DAYS", "30"))

    portfolio_path: Path = ROOT / "portfolio.yaml"
    risk_profile_path: Path = ROOT / "risk_profile.yaml"
    cache_dir: Path = ROOT / ".cache"
    site_dir: Path = ROOT


settings = Settings()
settings.cache_dir.mkdir(exist_ok=True)


def sec_user_agent_is_placeholder(ua: str | None = None) -> bool:
    """True when SEC_USER_AGENT is not a real, deliverable contact.

    SEC requires a monitored ``Name email@domain`` and its CDN tends to 403
    non-deliverable shapes. We flag: empty, the bundled placeholder, anything
    without an ``@``, the ``example.com`` sample, and GitHub ``noreply``
    addresses (the workflow's historical default, which 403s).
    """
    ua = (ua if ua is not None else settings.sec_user_agent) or ""
    ua = ua.strip()
    if not ua or ua == SEC_UA_PLACEHOLDER:
        return True
    low = ua.lower()
    return ("@" not in ua) or ("example.com" in low) or ("users.noreply.github.com" in low)


@lru_cache
def risk_profile() -> dict:
    with open(settings.risk_profile_path) as f:
        return yaml.safe_load(f)
