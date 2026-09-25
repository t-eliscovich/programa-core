"""TMT 2026-09-25: con DB_POOL_MIN=1 el pool cerraba casi toda conexión
devuelta y la siguiente consulta pagaba ~1 s de abrir otra contra el RDS."""
import db


def test_el_env_viejo_no_baja_el_piso(monkeypatch):
    monkeypatch.setenv("DB_POOL_MIN", "1")
    assert db._minconn() == db.POOL_MIN_PISO >= 6


def test_se_puede_pedir_mas(monkeypatch):
    monkeypatch.setenv("DB_POOL_MIN", "8")
    assert db._minconn() == 8


def test_valor_roto_cae_al_piso(monkeypatch):
    monkeypatch.setenv("DB_POOL_MIN", "abc")
    assert db._minconn() == db.POOL_MIN_PISO
