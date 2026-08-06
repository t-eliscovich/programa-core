"""El DESCUENTO se carga desde la ficha del cliente (TMT 2026-08-06).

La campanita del sync dice "cargarle cupo y descuento", pero el descuento
nunca tuvo pantalla (venía del dBase). Estos tests fijan: el campo existe en
la ficha, va gateado por `cupos.editar` igual que el cupo, y en queries la
semántica es la de cupo (None = no tocar, VACIAR = NULL).
"""
from __future__ import annotations

import db
from modules.clientes import queries as q
from modules.clientes.views import _parse_descuento


def test_parse_descuento():
    assert _parse_descuento("7") == 7.0
    assert _parse_descuento("7,5") == 7.5
    assert _parse_descuento(" ") is None
    assert _parse_descuento("basura") is None


def _captura_execute(monkeypatch):
    llamadas = []
    monkeypatch.setattr(db, "execute", lambda sql, params=None, conn=None: llamadas.append((sql, params)) or 1)
    return llamadas


def test_editar_descuento_entra_al_update(monkeypatch):
    llamadas = _captura_execute(monkeypatch)
    q.editar("VOL", descuento=7.0, usuario="test")
    sql, params = llamadas[0]
    assert "descuento = %s" in sql
    assert 7.0 in params


def test_editar_descuento_vaciar_pone_null(monkeypatch):
    llamadas = _captura_execute(monkeypatch)
    q.editar("VOL", descuento=q.VACIAR, usuario="test")
    sql, params = llamadas[0]
    assert "descuento = %s" in sql
    assert params[0] is None  # primer valor del SET


def test_editar_descuento_none_no_toca(monkeypatch):
    llamadas = _captura_execute(monkeypatch)
    q.editar("VOL", nombre="X", usuario="test")
    sql, _ = llamadas[0]
    assert "descuento" not in sql


def test_crear_lleva_descuento(monkeypatch):
    monkeypatch.setattr(db, "fetch_one", lambda *a, **k: None)
    capturado = {}

    def fake_ret(sql, params=None, conn=None):
        capturado["sql"], capturado["params"] = sql, params
        return {"id_cliente": 1, "codigo_cli": "ZZZ"}

    monkeypatch.setattr(db, "execute_returning", fake_ret)
    q.crear(codigo_cli="ZZZ", nombre="PRUEBA", descuento=7.0)
    assert "descuento" in capturado["sql"]
    assert 7.0 in capturado["params"]


def test_form_tiene_campo_descuento_gateado():
    from pathlib import Path
    html = Path("modules/clientes/templates/clientes/form.html").read_text()
    assert 'name="descuento"' in html
    # mismo gate que el cupo
    idx = html.index('name="descuento"')
    bloque = html[idx - 200: idx + 400]
    assert "cupos.editar" in bloque
