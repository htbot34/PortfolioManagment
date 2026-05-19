"""Stable recommendation IDs.

The ID is the first 8 hex chars of ``sha1(date|ticker|action)`` so the same
recommendation produced on the same day reliably gets the same ID across
runs - which is what the override loop (Phase 3) will key on.
"""
from __future__ import annotations

import hashlib


def make_rec_id(date_str: str, ticker: str, action: str) -> str:
    raw = f"{date_str}|{(ticker or '').upper()}|{(action or '').lower()}"
    return hashlib.sha1(raw.encode()).hexdigest()[:8]
