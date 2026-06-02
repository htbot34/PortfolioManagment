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


class Settings:
    github_token: str | None = os.getenv("GITHUB_TOKEN") or None
    github_model: str = os.getenv("GITHUB_MODEL", "openai/gpt-4o-mini")
    # SEC EDGAR rejects placeholder / generic User-Agents. The workflow
    # env (refresh.yml) injects a real identifying `name email` string
    # per SEC's fair-access policy; the local default falls back to one
    # that's clearly identifiable. Without a real UA, every CIK lookup
    # 403s and the insider signal silently mutes to score 0.
    sec_user_agent: str = os.getenv(
        "SEC_USER_AGENT",
        "PortfolioAdvisor (set SEC_USER_AGENT=\"YourName your@email.com\")",
    )
    # CIK ticker->code index is fetched from sec.gov/files/company_tickers.json
    # and persisted at the repo root so it survives across builds. Refresh
    # at most once per TTL to avoid hammering SEC on every refresh run.
    cik_cache_ttl_days: int = int(os.getenv("CIK_CACHE_TTL_DAYS", "7"))

    portfolio_path: Path = ROOT / "portfolio.yaml"
    risk_profile_path: Path = ROOT / "risk_profile.yaml"
    cache_dir: Path = ROOT / ".cache"
    site_dir: Path = ROOT


settings = Settings()
settings.cache_dir.mkdir(exist_ok=True)


@lru_cache
def risk_profile() -> dict:
    with open(settings.risk_profile_path) as f:
        return yaml.safe_load(f)
