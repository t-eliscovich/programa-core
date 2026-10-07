"""Tamara 2026-10-07: el aviso "la bodega de tela cruda tiene 272 kg de más en
el saldo" no llevaba a ningún lado. Ahora lleva a /stock/asinfo-de-mas, que
lista los lotes."""
from __future__ import annotations

import inspect

from modules.asinfo import salidas_sin_saldo as sss


def test_sql_de_una_bodega():
    sql = sss._sql_lotes_de_mas(52)
    assert "id_bodega = 52" in sql and "saldo_producto_lote" in sql
    assert "de_mas" in sql and "ultima_salida" in sql
    assert "id_bodega = 51" not in sql


def test_lotes_de_mas_suma_y_descuenta_la_base(monkeypatch):
    from modules._lib import metabase_client
    filas = [{"codigo": "A", "producto": "x", "lote": "L1", "saldo": 10, "movimientos": 0,
              "de_mas": 10, "ultima_salida": "SM-1", "fecha_salida": "2026-10-06 10:00"},
             {"codigo": "B", "producto": "y", "lote": "L2", "saldo": "35.5", "movimientos": 0,
              "de_mas": "35.5", "ultima_salida": None, "fecha_salida": None},
             {"codigo": "C", "de_mas": "no-numero"}]
    monkeypatch.setattr(metabase_client, "fetch_dataset_estado", lambda *a, **k: (filas, True))
    r = sss.lotes_de_mas(53)
    assert r["ok"] and len(r["lotes"]) == 2
    assert r["total"] == 45.5 and r["base"] == sss.CUADRE_BASE_KG[53]
    assert r["nuevo"] == round(45.5 - sss.CUADRE_BASE_KG[53], 2)
    assert r["lotes"][1]["ultima_salida"] == ""


def test_lotes_de_mas_sin_asinfo(monkeypatch):
    from modules._lib import metabase_client
    monkeypatch.setattr(metabase_client, "fetch_dataset_estado", lambda *a, **k: ([], False))
    assert sss.lotes_de_mas(52)["ok"] is False

    def boom(*a, **k):
        raise RuntimeError("caído")
    monkeypatch.setattr(metabase_client, "fetch_dataset_estado", boom)
    assert sss.lotes_de_mas(52)["ok"] is False


def test_el_aviso_lleva_el_link():
    src = inspect.getsource(sss.health)
    assert 'url=f"/stock/asinfo-de-mas?bodega={b[\'id\']}"' in src


def test_los_avisos_viejos_ganan_el_link_al_leerse():
    from modules.avisos import queries
    src = inspect.getsource(queries.listar)
    assert "stock-de-mas:%%" in src and "/stock/asinfo-de-mas?bodega=" in src


def test_la_pantalla(app, monkeypatch):
    from flask import g

    @app.before_request
    def _login():
        g.user = {"id_usuario": 0, "username": "t", "id_rol": 0,
                  "nombre_rol": "Accionista", "activo": True, "vend": None}
        g.permisos = {"*"}

    monkeypatch.setattr(sss, "lotes_de_mas", lambda b: {
        "ok": True, "total": 272.0, "base": 0.0, "nuevo": 272.0,
        "lotes": [{"codigo": "JER", "producto": "JERSEY", "lote": "L-9", "saldo": 272.0,
                   "movimientos": 0.0, "de_mas": 272.0, "ultima_salida": "SM-77",
                   "fecha_salida": "2026-10-06 15:42"}]})
    c = app.test_client()
    r = c.get("/stock/asinfo-de-mas?bodega=52")
    html = r.get_data(as_text=True)
    assert r.status_code == 200
    assert "Kilos de más en el saldo de Asinfo" in html and "L-9" in html and "SM-77" in html
    assert c.get("/stock/asinfo-de-mas?bodega=99").status_code == 404
    assert c.get("/stock/asinfo-de-mas?bodega=x").status_code == 200
