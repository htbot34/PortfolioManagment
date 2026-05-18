"""Centralized logger.

Configure once at import; every module calls ``get_logger(__name__)``.
Replaces silent ``except Exception: pass`` patterns with one-line warnings
to stderr (visible in workflow logs and in the build output).
"""
import logging
import os
import sys

_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

_root = logging.getLogger("portfolio_advisor")
if not _root.handlers:
    _handler = logging.StreamHandler(sys.stderr)
    _handler.setFormatter(logging.Formatter(_FORMAT))
    _root.addHandler(_handler)
    _root.setLevel(getattr(logging, _LEVEL, logging.INFO))
    _root.propagate = False


def get_logger(name: str) -> logging.Logger:
    """Return a logger namespaced under portfolio_advisor.<name>."""
    short = name.replace("app.", "").replace("portfolio_advisor.", "")
    return _root.getChild(short)
