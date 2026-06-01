"""Smoke checks for static assets referenced by Jinja templates."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATIC_ROOT = ROOT / "static"
TEMPLATE_ROOTS = (ROOT / "templates", ROOT / "modules")

STATIC_URL_FOR_RE = re.compile(
    r"url_for\(\s*['\"]static['\"]\s*,\s*filename\s*=\s*['\"]([^'\"]+)['\"]"
)
STATIC_PATH_RE = re.compile(r"""["']/static/([^"'?#]+)""")

CRITICAL_STATIC_ASSETS = {
    "app.js",
    "intela-logo.png",
    "sortable-tables.js",
    "tailwind.css",
    "vendor/chart.umd.min.js",
}


def _template_files() -> list[Path]:
    files: list[Path] = []
    for root in TEMPLATE_ROOTS:
        files.extend(path for path in root.rglob("*.html") if path.is_file())
    return sorted(files)


def _referenced_static_assets() -> dict[str, set[str]]:
    references: dict[str, set[str]] = {}
    for path in _template_files():
        text = path.read_text(encoding="utf-8")
        rel_path = str(path.relative_to(ROOT))
        for match in STATIC_URL_FOR_RE.finditer(text):
            references.setdefault(match.group(1).split("?", 1)[0], set()).add(rel_path)
        for match in STATIC_PATH_RE.finditer(text):
            references.setdefault(match.group(1).split("?", 1)[0], set()).add(rel_path)
    return references


def test_critical_static_assets_exist():
    missing = sorted(asset for asset in CRITICAL_STATIC_ASSETS if not (STATIC_ROOT / asset).is_file())
    assert not missing


def test_template_static_asset_references_exist():
    references = _referenced_static_assets()
    missing = {
        asset: sorted(source_paths)
        for asset, source_paths in references.items()
        if not (STATIC_ROOT / asset).is_file()
    }
    assert not missing
