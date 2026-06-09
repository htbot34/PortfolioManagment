"""Per-page render isolation: one broken template must not freeze the site.

Regression for the daily "Refresh site" outage: a single template exception
in main()'s render block aborted the whole refresh before data.json was
written, so the public site froze. ``build_site.render_and_publish`` must
swallow per-page failures, still publish every healthy page plus data.json
and .nojekyll, and surface the failures under diagnostics.render_errors.
"""
import json

from app import build_site


class _FakeTemplate:
    def __init__(self, name: str, fail: bool):
        self._name = name
        self._fail = fail

    def render(self, **ctx) -> str:
        if self._fail:
            raise RuntimeError("boom: forced render failure")
        return f"<html>{self._name}</html>"


class _FakeEnv:
    """Stands in for the Jinja Environment; one template always raises."""

    def __init__(self, failing: str):
        self._failing = failing

    def get_template(self, name: str) -> _FakeTemplate:
        return _FakeTemplate(name, fail=(name == self._failing))


def test_one_failing_page_still_publishes_everything_else(tmp_path, capsys):
    pages = [
        ("index.html", "index.html", {}),
        ("positions.html", "positions.html", {}),
        ("ticker.html", "ticker/NVDA.html", {}),
    ]
    data_dump = {"diagnostics": {"prices": "ok (test)"}, "brief": {}}

    errors = build_site.render_and_publish(
        _FakeEnv(failing="positions.html"), tmp_path, pages, data_dump)

    # No exception propagated and the failing page is named.
    assert errors == [("positions.html", "RuntimeError('boom: forced render failure')")]
    # Healthy pages (including the nested per-ticker page) still rendered.
    assert (tmp_path / "index.html").read_text() == "<html>index.html</html>"
    assert (tmp_path / "ticker" / "NVDA.html").exists()
    assert not (tmp_path / "positions.html").exists()
    # data.json + .nojekyll ALWAYS publish, with the failure in diagnostics.
    assert (tmp_path / ".nojekyll").exists()
    dump = json.loads((tmp_path / "data.json").read_text())
    assert dump["diagnostics"]["render_errors"] == [
        {"page": "positions.html",
         "error": "RuntimeError('boom: forced render failure')"},
    ]
    assert dump["diagnostics"]["prices"] == "ok (test)"  # existing diag kept
    captured = capsys.readouterr()
    assert "RENDER FAILURES (site still published): positions.html" in captured.out
    assert "boom: forced render failure" in captured.err  # traceback printed


def test_clean_run_publishes_with_empty_render_errors(tmp_path, capsys):
    pages = [("index.html", "index.html", {})]
    data_dump = {"diagnostics": {}}

    errors = build_site.render_and_publish(
        _FakeEnv(failing="<none>"), tmp_path, pages, data_dump)

    assert errors == []
    assert (tmp_path / "index.html").exists()
    dump = json.loads((tmp_path / "data.json").read_text())
    assert dump["diagnostics"]["render_errors"] == []
    assert "RENDER FAILURES" not in capsys.readouterr().out
