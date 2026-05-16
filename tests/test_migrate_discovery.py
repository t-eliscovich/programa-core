"""Tests for scripts/migrate.py — the pieces that don't need a live DB."""
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _load_migrate_module():
    """Import scripts/migrate.py without running main()."""
    path = ROOT / "scripts" / "migrate.py"
    spec = importlib.util.spec_from_file_location("migrate_test_module", path)
    mod = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(ROOT))
    spec.loader.exec_module(mod)
    return mod


def test_version_regex_matches_valid_filenames():
    mod = _load_migrate_module()
    assert mod.VERSION_RE.match("0001_init.sql")
    assert mod.VERSION_RE.match("0042_seed_roles.py")
    assert mod.VERSION_RE.match("9999_lots_of_words_here.sql")


def test_version_regex_rejects_bad_filenames():
    mod = _load_migrate_module()
    assert not mod.VERSION_RE.match("init.sql")          # no version
    assert not mod.VERSION_RE.match("1_init.sql")        # too short
    assert not mod.VERSION_RE.match("00001_init.sql")    # too long
    assert not mod.VERSION_RE.match("0001_init.txt")     # wrong ext
    assert not mod.VERSION_RE.match("0001 init.sql")     # space in name


def test_discover_returns_sorted_list():
    mod = _load_migrate_module()
    found = mod.discover()
    versions = [v for v, _, _ in found]
    assert versions == sorted(versions)
    # We know these exist right now.
    assert "0001" in versions
    assert "0002" in versions
    assert "0003" in versions


def test_discover_filters_non_migration_files(tmp_path, monkeypatch):
    mod = _load_migrate_module()
    fake_dir = tmp_path / "migrations"
    fake_dir.mkdir()
    (fake_dir / "0001_one.sql").write_text("-- x")
    (fake_dir / "README.md").write_text("not a migration")
    (fake_dir / "0002_two.py").write_text("def run(conn): pass")
    (fake_dir / ".DS_Store").write_text("")
    monkeypatch.setattr(mod, "MIGRATIONS_DIR", fake_dir)

    versions = [v for v, _, _ in mod.discover()]
    assert versions == ["0001", "0002"]
