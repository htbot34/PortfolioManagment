"""Local test bootstrap.

The only purpose right now is to stub out optional native deps for environments
where they can't be built (e.g. ``sgmllib3k`` -> ``feedparser`` on some
sandboxes). CI installs the real packages from ``requirements.txt`` so this
stub is harmless there: real modules win the import race.
"""
from __future__ import annotations

import sys
from types import ModuleType
from unittest.mock import MagicMock


def _stub_if_missing(name: str) -> None:
    try:
        __import__(name)
    except Exception:
        sys.modules[name] = MagicMock(spec=ModuleType(name))


for _name in ("feedparser", "httpx", "yfinance"):
    _stub_if_missing(_name)
