import os
from functools import lru_cache
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


class Settings:
    anthropic_api_key: str | None = os.getenv("ANTHROPIC_API_KEY") or None
    finnhub_api_key: str | None = os.getenv("FINNHUB_API_KEY") or None
    claude_research_model: str = os.getenv("CLAUDE_RESEARCH_MODEL", "claude-opus-4-7")
    claude_summary_model: str = os.getenv("CLAUDE_SUMMARY_MODEL", "claude-haiku-4-5-20251001")
    sec_user_agent: str = os.getenv("SEC_USER_AGENT", "PortfolioAdvisor research@example.com")

    portfolio_path: Path = ROOT / "portfolio.yaml"
    risk_profile_path: Path = ROOT / "risk_profile.yaml"
    cache_dir: Path = ROOT / ".cache"
    db_path: Path = ROOT / "portfolio.db"


settings = Settings()
settings.cache_dir.mkdir(exist_ok=True)


@lru_cache
def risk_profile() -> dict:
    with open(settings.risk_profile_path) as f:
        return yaml.safe_load(f)
