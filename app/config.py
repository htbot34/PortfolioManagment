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
    sec_user_agent: str = os.getenv("SEC_USER_AGENT", "PortfolioAdvisor research@example.com")

    portfolio_path: Path = ROOT / "portfolio.yaml"
    risk_profile_path: Path = ROOT / "risk_profile.yaml"
    cache_dir: Path = ROOT / ".cache"
    site_dir: Path = ROOT / "docs"


settings = Settings()
settings.cache_dir.mkdir(exist_ok=True)


@lru_cache
def risk_profile() -> dict:
    with open(settings.risk_profile_path) as f:
        return yaml.safe_load(f)
