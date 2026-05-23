from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = REPO_ROOT / ".github" / "ISSUE_TEMPLATE"
LABELS_FILE = REPO_ROOT / ".github" / "labels.yml"


def _template_labels():
    labels = set()
    for path in sorted(TEMPLATES_DIR.glob("*.yml")):
        with path.open() as fh:
            data = yaml.safe_load(fh) or {}
        declared = data.get("labels") or []
        if isinstance(declared, str):
            declared = [declared]
        for name in declared:
            labels.add((name, path.name))
    return labels


def _defined_labels():
    with LABELS_FILE.open() as fh:
        data = yaml.safe_load(fh) or []
    return {entry["name"] for entry in data}


def test_every_template_label_is_defined_in_labels_yml():
    defined = _defined_labels()
    missing = [(name, src) for name, src in _template_labels() if name not in defined]
    assert not missing, (
        "Issue templates reference labels that are not defined in "
        ".github/labels.yml. Add them so sync_labels.yml will provision them, "
        "otherwise GitHub will silently drop them at issue-creation time and "
        "process_trade.yml will skip those issues. Missing: " + repr(missing)
    )


def test_labels_yml_entries_have_required_fields():
    with LABELS_FILE.open() as fh:
        data = yaml.safe_load(fh) or []
    assert data, ".github/labels.yml is empty"
    for entry in data:
        assert "name" in entry, f"label entry missing name: {entry!r}"
        assert "color" in entry, f"label {entry['name']!r} missing color"
        color = entry["color"].lstrip("#")
        assert len(color) == 6 and all(c in "0123456789abcdefABCDEF" for c in color), (
            f"label {entry['name']!r} has invalid color {entry['color']!r}"
        )
