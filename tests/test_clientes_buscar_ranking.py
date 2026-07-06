"""Ranking del buscador de /clientes (queries.buscar).

Queja dueña 2026-07-06: buscar "edu" en /clientes devolvía un listado de
gente con EDUARDO en el NOMBRE (orden por código ASC) y el cliente con
CÓDIGO "EDU" (su 3er mayor deudor) no aparecía en la primera página.

El fix ranquea como la búsqueda global (Ctrl/Cmd+K → informes.buscar_clientes):
    0. código EXACTO
    1. código que EMPIEZA con el término
    2. resto (nombre/RUC contienen)
y dentro de cada grupo por saldo pendiente DESC, luego nombre.
Sin término de búsqueda se mantiene el orden por código ASC (pedido dueña
2026-05-20).

Como el ranking vive en SQL (tiene que pasar ANTES del LIMIT/OFFSET del
paginado), el test EJECUTA la query real contra SQLite en memoria
(ATTACH ':memory:' AS scintela emula el schema), traduciendo los
placeholders %(name)s de psycopg a :name de sqlite3.
"""
from __future__ import annotations

import os
import re
import sqlite3
import sys

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import db as _db  # noqa: E402
from modules.clientes import queries  # noqa: E402


def _mk_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("ATTACH DATABASE ':memory:' AS scintela")
    conn.execute(
        """
        CREATE TABLE scintela.cliente (
            id_cliente INTEGER PRIMARY KEY,
            codigo_cli TEXT, nombre TEXT, telefono TEXT, ruc TEXT,
            stop TEXT, cupo INTEGER, pago TEXT, vend TEXT, fecha_cupo TEXT,
            direccion1 TEXT, direccion2 TEXT,
            provincia TEXT, canton TEXT, parroquia TEXT,
            activo INTEGER
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE scintela.factura (
            codigo_cli TEXT, saldo REAL, stat TEXT, usuario_crea TEXT
        )
        """
    )
    return conn


def _seed(conn: sqlite3.Connection) -> None:
    # (codigo, nombre) — AAJ/CED tienen EDUARDO en el NOMBRE (el caso real);
    # EDX empieza con "ED"+... (prefijo); EDU es el código exacto; ZZZ no matchea.
    clientes = [
        (1, "AAJ", "EDUARDO AGUIRRE"),
        (2, "CED", "CARLOS EDUARDO DIAZ"),
        (3, "EDU", "EDUARDO BARRIGA"),
        (4, "EDUC", "EDUCOMERCIO SA"),
        (5, "EDUX", "OTRO PREFIJO"),
        (6, "ZZZ", "SIN MATCH"),
    ]
    for id_, cod, nom in clientes:
        conn.execute(
            "INSERT INTO scintela.cliente (id_cliente, codigo_cli, nombre, activo)"
            " VALUES (?, ?, ?, 1)",
            (id_, cod, nom),
        )
    # Saldos pendientes: EDU deudor grande; EDUX > EDUC (para ver saldo DESC
    # dentro del grupo prefijo); CED > AAJ (idem grupo nombre).
    facturas = [
        ("EDU", 50000.0, "Z"),
        ("EDUC", 100.0, None),
        ("EDUX", 900.0, "A"),
        ("AAJ", 10.0, "Z"),
        ("CED", 700.0, "Z"),
        # Ruido que NO debe contar como saldo pendiente:
        ("EDUC", 99999.0, "T"),   # stat T = cerrada
        ("AAJ", 0.0, "Z"),        # saldo 0
    ]
    conn.executemany(
        "INSERT INTO scintela.factura (codigo_cli, saldo, stat, usuario_crea) VALUES (?, ?, ?, '')",
        facturas,
    )


def _sqlite_fetch_all(conn: sqlite3.Connection):
    """fetch_all estilo db.py pero contra sqlite: %(name)s → :name."""

    def fake_fetch_all(sql: str, params=None, **kw):
        sql = re.sub(r"%\((\w+)\)s", r":\1", sql)
        rows = conn.execute(sql, params or {}).fetchall()
        return [dict(r) for r in rows]

    return fake_fetch_all


@pytest.fixture()
def buscar_sqlite(monkeypatch):
    conn = _mk_conn()
    _seed(conn)
    monkeypatch.setattr(_db, "fetch_all", _sqlite_fetch_all(conn))
    yield
    conn.close()


def test_codigo_exacto_primero(buscar_sqlite):
    """'edu' (minúscula, case-insensitive): EDU arriba de todo."""
    filas = queries.buscar("edu", incluir_inactivos=True)
    codigos = [f["codigo_cli"] for f in filas]
    assert codigos[0] == "EDU", codigos
    # Grupo 1: códigos que EMPIEZAN con el término, saldo DESC (EDUX 900 > EDUC 100
    # — la factura stat=T de 99999 de EDUC NO cuenta).
    assert codigos[1:3] == ["EDUX", "EDUC"], codigos
    # Grupo 2: matches por NOMBRE, saldo DESC (CED 700 > AAJ 10).
    assert codigos[3:] == ["CED", "AAJ"], codigos
    assert "ZZZ" not in codigos


def test_term_con_espacios_y_mayusculas(buscar_sqlite):
    """El término se trimea y es case-insensitive: '  EDU  ' == 'edu'."""
    filas = queries.buscar("  EDU  ", incluir_inactivos=True)
    assert [f["codigo_cli"] for f in filas][0] == "EDU"


def test_sin_termino_mantiene_orden_por_codigo(buscar_sqlite):
    """Sin q, el listado sigue ordenado por código ASC (pedido dueña 2026-05-20)."""
    filas = queries.buscar("", incluir_inactivos=True)
    codigos = [f["codigo_cli"] for f in filas]
    assert codigos == sorted(codigos), codigos
    assert len(codigos) == 6


def test_saldo_pendiente_calculado(buscar_sqlite):
    """El saldo que ordena es el pendiente (>0, stat abierto), no el total."""
    filas = queries.buscar("edu", incluir_inactivos=True)
    por_cod = {f["codigo_cli"]: f for f in filas}
    assert float(por_cod["EDU"]["saldo_total"]) == 50000.0
    assert float(por_cod["EDUC"]["saldo_total"]) == 100.0  # sin la T de 99999
    assert int(por_cod["AAJ"]["n_abiertas"]) == 1          # la de saldo 0 no cuenta


def test_ranking_pasa_antes_del_limit(buscar_sqlite):
    """Con paginado chico, EDU tiene que estar en la PRIMERA página."""
    filas = queries.buscar("edu", incluir_inactivos=True, limite=2, offset=0)
    assert [f["codigo_cli"] for f in filas] == ["EDU", "EDUX"]
