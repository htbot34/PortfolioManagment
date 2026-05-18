from app.research.jsonparse import parse_json_loose


def test_plain_json():
    assert parse_json_loose('{"a": 1}') == {"a": 1}


def test_fenced_json():
    text = "```json\n{\"a\": 1, \"b\": [1,2]}\n```"
    assert parse_json_loose(text) == {"a": 1, "b": [1, 2]}


def test_fenced_no_lang():
    assert parse_json_loose("```\n{\"x\":true}\n```") == {"x": True}


def test_noisy_prefix_suffix():
    text = "Here's your JSON:\n```json\n{\"action\":\"buy\"}\n```\nLet me know."
    assert parse_json_loose(text) == {"action": "buy"}


def test_outermost_braces_when_garbage_in_front():
    text = "garble garble {\"k\": \"v\"} trailing"
    assert parse_json_loose(text) == {"k": "v"}


def test_returns_none_on_invalid():
    assert parse_json_loose("not json at all") is None
    assert parse_json_loose("") is None
    assert parse_json_loose(None) is None  # type: ignore[arg-type]


def test_rejects_top_level_list():
    assert parse_json_loose("[1,2,3]") is None
