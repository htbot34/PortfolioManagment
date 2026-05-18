"""Loose JSON parsing for LLM outputs.

The model occasionally wraps JSON in ```json fences, prepends explanatory
text, or appends a trailing newline. This helper strips fences and finds
the outermost ``{ ... }`` braces before json.loads.
"""
import json
import re

_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)


def parse_json_loose(text: str) -> dict | None:
    """Parse a JSON object from text that might be fenced or noisy.

    Returns the parsed dict, or None if no valid JSON object can be extracted.
    """
    if not text:
        return None
    cleaned = _FENCE_RE.sub("", text).strip()
    # Fast path: already valid JSON
    try:
        out = json.loads(cleaned)
        return out if isinstance(out, dict) else None
    except json.JSONDecodeError:
        pass
    # Fallback: locate outermost braces
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        out = json.loads(cleaned[start : end + 1])
        return out if isinstance(out, dict) else None
    except json.JSONDecodeError:
        return None
