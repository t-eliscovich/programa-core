"""SECRET_KEY loading — PERSISTIDA en disco para estabilidad de sesión.

TMT 2026-06-29: cambiamos el contrato. Antes prod fallaba (RuntimeError) sin
SECRET_KEY. Ahora la clave se persiste en un archivo y se prefiere SIEMPRE, así
sobrevive reinicios/deploys (si el entorno la regeneraba en cada arranque, las
cookies de login se invalidaban y los usuarios quedaban deslogueados). Cubre el
nuevo contrato: archivo -> env (y se persiste) -> prod genera una estable.
"""
import importlib
import os


def _reload():
    import app as app_mod
    importlib.reload(app_mod)
    return app_mod


def _iso(monkeypatch, tmp_path):
    monkeypatch.setenv("SECRET_KEY_FILE", str(tmp_path / ".secret_key"))


def test_prod_without_secret_genera_estable(monkeypatch, tmp_path):
    _iso(monkeypatch, tmp_path)
    monkeypatch.delenv("SECRET_KEY", raising=False)
    monkeypatch.delenv("FLASK_ENV", raising=False)
    monkeypatch.setenv("ENV", "production")
    app_mod = _reload()
    k1 = app_mod._load_secret_key()
    assert len(k1) >= 32
    k2 = app_mod._load_secret_key()
    assert k1 == k2


def test_persistida_gana_aunque_cambie_el_env(monkeypatch, tmp_path):
    _iso(monkeypatch, tmp_path)
    monkeypatch.delenv("FLASK_ENV", raising=False)
    monkeypatch.setenv("ENV", "production")
    monkeypatch.setenv("SECRET_KEY", "A" * 64)
    app_mod = _reload()
    assert app_mod._load_secret_key() == "A" * 64
    monkeypatch.setenv("SECRET_KEY", "B" * 64)
    assert app_mod._load_secret_key() == "A" * 64


def test_dev_without_secret_falls_back(monkeypatch, tmp_path):
    _iso(monkeypatch, tmp_path)
    monkeypatch.delenv("SECRET_KEY", raising=False)
    monkeypatch.delenv("FLASK_ENV", raising=False)
    monkeypatch.setenv("ENV", "development")
    app_mod = _reload()
    assert app_mod._load_secret_key() == "dev-only-replace-me"


def test_valid_secret_accepted_y_persistido(monkeypatch, tmp_path):
    _iso(monkeypatch, tmp_path)
    monkeypatch.setenv("SECRET_KEY", "x" * 64)
    monkeypatch.delenv("FLASK_ENV", raising=False)
    monkeypatch.setenv("ENV", "production")
    app_mod = _reload()
    assert app_mod._load_secret_key() == "x" * 64
    assert os.path.exists(os.environ["SECRET_KEY_FILE"])


def test_prod_short_env_key_se_ignora(monkeypatch, tmp_path):
    _iso(monkeypatch, tmp_path)
    monkeypatch.setenv("SECRET_KEY", "short")
    monkeypatch.delenv("FLASK_ENV", raising=False)
    monkeypatch.setenv("ENV", "production")
    app_mod = _reload()
    k = app_mod._load_secret_key()
    assert k != "short"
    assert len(k) >= 32
