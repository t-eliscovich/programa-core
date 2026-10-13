"""Cartera documentada por vendedor (13/10/2026, pedido de Andrés).

Lo que protegen:
- la agrupación es la MISMA que Estados de cuenta › Vendedor (un vendedor
  inactivo cae en '(sin vendedor)'), así la foto y la pantalla dicen lo mismo;
- el % es cheques / (facturas + cheques);
- la foto diaria graba una fila por vendedor y es idempotente (ON CONFLICT);
- health/all llama a guardar_foto después de la foto del balance.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules.informes import cartera_documentada as cd  # noqa: E402


def _fila(cod, vend, saldo, cheques, activo=True, nombre=None):
    return {"codigo_cli": cod, "vend": vend, "vendedor_nombre": nombre or vend,
            "vendedor_activo": activo, "saldo": saldo, "cheques": cheques}


def test_agrupa_igual_que_la_pantalla_y_calcula_el_pct():
    filas = [
        _fila("A1", "EDG", 600, 400, nombre="Edgar Ramirez"),
        _fila("A2", "EDG", 400, 100, nombre="Edgar Ramirez"),
        _fila("B1", "PPR", 900, 100, nombre="Patricio Proano"),
        _fila("C1", "BED", 50, 50, activo=False),   # inactivo → sin vendedor
        _fila("C2", "", 100, 0),                     # vacío → sin vendedor
    ]
    g = cd.por_vendedor(filas)
    assert [x["vend"] for x in g] == ["EDG", "PPR", "~"]
    edg, ppr, sin = g
    assert (edg["clientes"], edg["saldo"], edg["cheques"], edg["pct"]) == (2, 1000, 500, 33.3)
    assert ppr["pct"] == 10.0
    assert (sin["vendedor"], sin["clientes"], sin["pct"]) == ("(sin vendedor)", 2, 25.0)


def test_pct_sin_cartera_es_none():
    assert cd.pct(0, 0) is None
    assert cd.pct(0, 10) == 100.0


def test_guardar_foto_graba_una_fila_por_vendedor_y_pisa(monkeypatch):
    llamadas = []
    monkeypatch.setattr(cd, "de_hoy", lambda: cd.por_vendedor([
        _fila("A1", "EDG", 600, 400), _fila("B1", "PPR", 900, 100)]))
    monkeypatch.setattr(cd.db, "execute", lambda sql, params=None, conn=None: llamadas.append((sql, params)) or 1)
    r = cd.guardar_foto("2026-10-13")
    assert r == {"fecha": "2026-10-13", "vendedores": 2, "pct_total": 25.0}
    assert len(llamadas) == 2
    assert all("ON CONFLICT (fecha, vend) DO UPDATE" in sql for sql, _ in llamadas)
    assert llamadas[0][1][:2] == ("2026-10-13", "EDG")


def test_la_foto_diaria_llama_a_la_cartera_documentada():
    fuente = (ROOT / "modules" / "admin_dbase" / "health_audit_view.py").read_text(encoding="utf-8")
    i_snap = fuente.index("snap = crear_snapshot_diario()")
    i_cdoc = fuente.index("_cdoc.guardar_foto(")
    assert i_cdoc > i_snap


def test_la_migracion_crea_la_tabla():
    sql = (ROOT / "migrations" / "0260_cartera_documentada_foto.sql").read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS scintela.cartera_documentada_foto" in sql
    assert "PRIMARY KEY (fecha, vend)" in sql
