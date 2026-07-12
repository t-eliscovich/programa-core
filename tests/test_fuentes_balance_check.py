"""Tests del check read-only de fuentes del balance (fuentes_balance_check).

Pedido dueña 2026-07-12: ver de dónde sale cada línea del balance para
decidir la migración a Asinfo + Fórmulas. Estos tests fijan el CONTRATO
de la función: estructura de cada fila, que sea puramente lectura (no
toca la DB para escribir) y que el resumen cuente bien.

Se stubbean los componentes (totf/totc/…) con valores conocidos, así el
test no necesita una base — mide estructura, no aritmética real.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ["ENV"] = "development"

from modules.informes import queries  # noqa: E402


@pytest.fixture
def stub_componentes(monkeypatch):
    """Reemplaza los componentes del balance por valores fijos."""
    monkeypatch.setattr(queries, "totf", lambda: 4916202.77)
    monkeypatch.setattr(queries, "totc", lambda: 123456.0)
    monkeypatch.setattr(queries, "saldo_bancos", lambda: [{"saldo": 1000.0, "no_banco": 1},
                                                          {"saldo": 2000.0, "no_banco": 2}])
    monkeypatch.setattr(queries, "salcaj", lambda: 500.0)
    monkeypatch.setattr(queries, "posdat_totales", lambda: {"pos1": 10.0, "pos2": 20.0, "totp": 99999.0})
    monkeypatch.setattr(queries, "activos_totales", lambda: {"umaq": 111.0, "uact": 222.0})
    monkeypatch.setattr(queries, "anticipos", lambda: 333.0)
    monkeypatch.setattr(queries, "uret_mes_corriente", lambda: 44.0)
    monkeypatch.setattr(queries, "retiros_total_anual", lambda: 55.0)
    monkeypatch.setattr(queries, "ventas_anio_en_curso", lambda: 66.0)
    monkeypatch.setattr(queries, "historia_ultimo_snapshot", lambda: {"uqui": 279591.0})
    monkeypatch.setattr(queries, "historia_ultimo_mes", lambda: {"patrimonio": 20115887.0})


def test_estructura_y_valores(stub_componentes):
    out = queries.fuentes_balance_check()
    assert set(out) == {"filas", "resumen"}
    filas = out["filas"]
    assert len(filas) >= 12

    # Cada fila tiene el contrato completo.
    campos = {"seccion", "linea", "valor", "tabla", "carga", "carga_label", "nivel", "nota"}
    for f in filas:
        assert campos <= set(f), f"faltan campos en {f['linea']}"
        assert isinstance(f["valor"], float)
        assert f["carga"] in {"dbase", "formulas", "asinfo", "programa", "cierre", "derivado"}

    por_linea = {f["linea"]: f for f in filas}
    # Valores computados de los componentes stubbeados.
    assert por_linea["Facturas (cartera)"]["valor"] == pytest.approx(4916202.77)
    assert por_linea["Caja"]["valor"] == pytest.approx(500.0)
    # Bancos = suma de saldos (3000) + pos1 + pos2 (30) = 3030.
    assert por_linea["Bancos"]["valor"] == pytest.approx(3030.0)
    assert por_linea["Pasivos"]["valor"] == pytest.approx(99999.0)
    assert por_linea["Maquinaria / Equipo"]["valor"] == pytest.approx(111.0)
    assert por_linea["Patrimonio último cierre"]["valor"] == pytest.approx(20115887.0)


def test_resumen_cuenta_bien(stub_componentes):
    out = queries.fuentes_balance_check()
    r = out["resumen"]
    assert r["total"] == len(out["filas"])
    # La mayoría de las líneas hoy dependen del dBase.
    assert r["dbase"] >= 1
    # El patrimonio queda marcado como 'cierre'.
    assert r["cierre"] >= 1


def test_una_linea_que_falla_no_rompe_el_check(stub_componentes, monkeypatch):
    """Si un componente tira excepción, el check no se cae: usa el default."""
    def _boom():
        raise RuntimeError("db caída")

    monkeypatch.setattr(queries, "salcaj", _boom)
    out = queries.fuentes_balance_check()
    por_linea = {f["linea"]: f for f in out["filas"]}
    assert por_linea["Caja"]["valor"] == 0.0  # cayó a default, no explotó
    assert por_linea["Facturas (cartera)"]["valor"] == pytest.approx(4916202.77)


def test_es_solo_lectura(stub_componentes, monkeypatch):
    """El check NO debe escribir en la DB: si algún componente llamara a
    db.execute/tx, lo detectamos. (Los componentes stubbeados no tocan db;
    este test protege contra que fuentes_balance_check agregue escrituras.)"""
    import db as _db

    def _fail_write(*a, **k):
        raise AssertionError("fuentes_balance_check NO debe escribir en la DB")

    monkeypatch.setattr(_db, "execute", _fail_write, raising=False)
    monkeypatch.setattr(_db, "tx", _fail_write, raising=False)
    queries.fuentes_balance_check()  # no debe levantar
