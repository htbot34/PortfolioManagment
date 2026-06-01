"""Sidecar writer that surfaces newly-added recommendations to the workflow.

The build pipeline persists every rec the brief produces via
``rec_history.record_pending`` (idempotent on rec_id). When that call returns
new entries, we mirror the full rec dicts -- not just the trimmed history
entries -- to ``.new_recs.json`` so the refresh workflow can open a GitHub
issue per rec. The file is gitignored and overwritten every run.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable


SIDECAR_PATH = Path(__file__).resolve().parent.parent / ".new_recs.json"


def _recs_from_brief(brief: dict) -> list[dict]:
    out: list[dict] = []
    pa = (brief or {}).get("primary_action")
    if pa:
        out.append({**pa, "_slot": "primary"})
    for sa in (brief or {}).get("secondary_actions") or []:
        if sa:
            out.append({**sa, "_slot": "secondary"})
    return out


def write_sidecar(
    brief: dict,
    added: Iterable[dict],
    path: Path | None = None,
) -> list[dict]:
    """Write the full rec dicts for newly-added rec_ids to ``.new_recs.json``.

    Always overwrites the file. Writes an empty list when nothing is new so a
    stale file from a previous run can't trigger a spurious notification.
    Returns the list that was written.
    """
    p = path or SIDECAR_PATH
    new_ids = {e.get("rec_id") for e in added if e.get("rec_id")}
    payload = [r for r in _recs_from_brief(brief) if r.get("rec_id") in new_ids]
    p.write_text(json.dumps(payload, indent=2))
    return payload
