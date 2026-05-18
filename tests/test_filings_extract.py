"""Tests for the BeautifulSoup-based EDGAR HTML extractor."""
from app.data import filings


_SAMPLE_HTML = """
<html><head>
  <title>Quarterly Report</title>
  <style>body{font-family:sans-serif}</style>
  <script>var x = 1;</script>
</head>
<body>
  <ix:header>iXBRL plumbing should not appear in output</ix:header>
  <p>Revenue grew 14% year over year.</p>
  <ul>
    <li>Segment A: $1.2B</li>
    <li>Segment B: $0.8B</li>
  </ul>
  <table>
    <tr><th>Metric</th><th>Q3</th><th>Q4</th></tr>
    <tr><td>Revenue</td><td>1200</td><td>1400</td></tr>
    <tr><td>Operating income</td><td>200</td><td>260</td></tr>
  </table>
  <p>Forward-looking commentary about FY guidance.</p>
</body></html>
"""


def test_extract_text_drops_scripts_styles_and_ixbrl():
    out = filings._extract_text(_SAMPLE_HTML, max_chars=10_000)
    assert "var x" not in out
    assert "iXBRL plumbing" not in out
    assert "font-family" not in out


def test_extract_text_preserves_prose_and_table_rows():
    out = filings._extract_text(_SAMPLE_HTML, max_chars=10_000)
    assert "Revenue grew 14%" in out
    assert "Segment A: $1.2B" in out
    # Table cells should be present and on their own lines.
    lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
    assert "Revenue" in lines
    assert "1200" in lines
    assert "Operating income" in lines


def test_extract_text_respects_max_chars():
    out = filings._extract_text(_SAMPLE_HTML, max_chars=50)
    assert len(out) <= 50
