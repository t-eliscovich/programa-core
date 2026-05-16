"""SECRET_KEY loading semantics — fail fast in prod, warn in dev."""
import os

import pytest


def test_prod_without_secret_raises(monkeypatch):
    monkeypatch.delenv("SECRET_KEY", raising=False)
    monkeypatch.delenv("FLASK_ENV", raising=False)
    monkeypatch.setenv("ENV", "production")
    # Re-import to pick up env
    import importlib

    import app as app_mod
    importlib.reload(app_mod)
    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        app_mod._load_secret_key()


def test_prod_with_short_secret_raises(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "short")
    monkeypatch.delenv("FLASK_ENV", raising=False)
    monkeypatch.setenv("ENV", "production")
    import importlib

    import app as app_mod
    importlib.reload(app_mod)
    with pytest.raises(RuntimeError, match="corta"):
        app_mod._load_secret_key()


def test_dev_without_secret_falls_back(monkeypatch):
    monkeypatch.delenv("SECRET_KEY", raising=False)
    monkeypatch.delenv("FLASK_ENV", raising=False)
    monkeypatch.setenv("ENV", "development")
    import importlib

    import app as app_mod
    importlib.reload(app_mod)
    key = app_mod._load_secret_key()
    assert key == "dev-only-replace-me"


def test_valid_secret_accepted(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "x" * 64)
    monkeypatch.delenv("FLASK_ENV", raising=False)
    monkeypatch.setenv("ENV", "production")
    import importlib

    import app as app_mod
    importlib.reload(app_mod)
    key = app_mod._load_secret_key()
    assert key == "x" * 64
