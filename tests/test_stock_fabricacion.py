"""Tests de las DOS tabs de Stock — Fabricación TC / PT (TMT 2026-06-10).

Réplica del Excel "Saldos Inventarios Proceso Produccion Nube": resumen
Inventario en Proceso (OSM / Ing. Fab. / Saldo), Órdenes de Fabricación
(planif / fab / por producir — PT agrupado por tejido) y detalle por OFT,
con los totales de todo el stock arriba y secciones (por producto, por lote,
importaciones) adentro.
"""
from __future__ import annotations

import os
import sys
from unittest.mock import patch

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from modules.asinfo import service  # noqa: E402


def _login_stock(app, fake_db):
    rid = fake_db.add_role("Tester", ["stock.ver"])
    uid = fake_db.add_user("test", b"$2b$12$fakehash", rid)
    c = app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = uid
    return c


_DATA_TC = {
    "resumen": {"issued": 144647.75, "fab": 96587.13, "saldo": 48060.62,
                "planif": 206328.15, "por_producir": 109741.02, "n_ofts": 64},
    "por_tejido": [{"tejido": "(s/categoría)", "planif": 206328.15, "fab": 96587.13,
                    "por_producir": 109741.02, "n_ofts": 64}],
    "ofts": [{"oft": "OFT-000035309", "producto": "TELA CRUDA JERSEY", "prod_codigo": "TCJ-01",
              "tejido": "", "planif": 4950.72, "fab": 3496.58, "issued": 4950.72,
              "saldo": 1454.14, "por_producir": 1454.14}],
}

_DATA_PT = {
    "resumen": {"issued": 83645.28, "fab": 42014.79, "saldo": 41630.49,
                "planif": 131905.14, "por_producir": 88987.70, "n_ofts": 200},
    "por_tejido": [
        {"tejido": "Fleece", "planif": 55443.5, "fab": 15373.24, "por_producir": 40070.26, "n_ofts": 80},
        {"tejido": "Jersey", "planif": 28787.5, "fab": 12175.9, "por_producir": 16611.6, "n_ofts": 60},
    ],
    "ofts": [{"oft": "OFT-000033227.1", "producto": "FLEECE NEG", "prod_codigo": "FLN-02",
              "tejido": "Fleece", "planif": 135.0, "fab": 128.45, "issued": 135.0,
              "saldo": 6.55, "por_producir": 6.55}],
}

_TOTALES = [
    {"id_bodega": 51, "bodega": "Bodega Hilo", "lotes": 90, "total_kg": 50000.0},
    {"id_bodega": 52, "bodega": "Bodega Tela Cruda", "lotes": 120, "total_kg": 255795.25},
    {"id_bodega": 53, "bodega": "Bodega PT", "lotes": 300, "total_kg": 310568.0},
]


def _render(app, fake_db, proceso, data, **extra_patches):
    c = _login_stock(app, fake_db)
    from modules.stock import queries as stock_queries

    with patch.object(service, "fabricacion_proceso", return_value=data), \
         patch.object(service, "stock_asinfo_lote_totales", return_value=_TOTALES), \
         patch.object(stock_queries, "resumen_stock",
                      return_value={"total": {"kg": 600000.0, "ukg": 0, "us": 1234567.0}}):
        return c.get(f"/stock/fabricacion-{proceso}", **extra_patches)


def test_fabricacion_tc_renderiza_estructura_excel(app, fake_db):
    r = _render(app, fake_db, "tc", _DATA_TC)
    assert r.status_code == 200
    # Bloques del Excel
    assert b"Inventario en proceso" in r.data
    assert b"Despachado (OSM)" in r.data
    assert "Ing. fabricación".encode() in r.data
    assert "Órdenes de fabricación".encode() in r.data
    assert b"HILO" in r.data
    # Totales de todo el stock arriba
    assert b">Hilo<" in r.data and b">PT<" in r.data
    # Detalle OFT
    assert b"OFT-000035309" in r.data


def test_fabricacion_pt_agrupa_por_tejido(app, fake_db):
    r = _render(app, fake_db, "pt", _DATA_PT)
    assert r.status_code == 200
    assert b"Fleece" in r.data and b"Jersey" in r.data
    assert b"Total general" in r.data
    assert b"TELA CRUDA" in r.data  # material del proceso


def test_fabricacion_csv_export(app, fake_db):
    c = _login_stock(app, fake_db)
    from modules.stock import queries as stock_queries

    with patch.object(service, "fabricacion_proceso", return_value=_DATA_PT), \
         patch.object(service, "stock_asinfo_lote_totales", return_value=[]), \
         patch.object(stock_queries, "resumen_stock", return_value={}):
        r = c.get("/stock/fabricacion-pt?export=csv")
    assert r.status_code == 200
    assert b"OFT-000033227.1" in r.data


def test_fabricacion_fail_soft_sin_asinfo(app, fake_db):
    """Asinfo caído → la página renderiza 200 con el error visible, no 500."""
    c = _login_stock(app, fake_db)
    from modules.stock import queries as stock_queries

    with patch.object(service, "fabricacion_proceso", side_effect=RuntimeError("metabase down")), \
         patch.object(service, "stock_asinfo_lote_totales", side_effect=RuntimeError("x")), \
         patch.object(stock_queries, "resumen_stock", side_effect=RuntimeError("x")):
        r = c.get("/stock/fabricacion-tc")
    assert r.status_code == 200
    assert b"metabase down" in r.data


def test_fabricacion_bodegas_en_orden_de_proceso(app, fake_db):
    """Los KPIs van Hilo → Tela Cruda → PT, no por kg desc."""
    r = _render(app, fake_db, "tc", _DATA_TC)
    body = r.data.decode("utf-8", "ignore")
    i_h, i_tc, i_pt = body.find(">Hilo<"), body.find(">Tela Cruda<"), body.find(">PT<")
    assert -1 < i_h < i_tc < i_pt


def test_fabricacion_service_totales_incluyen_negativos(monkeypatch):
    """A diferencia de stock_en_proceso, acá entran TODAS las OFTs abiertas
    (también saldo<0) para que los totales cierren contra el Excel."""
    from modules.asinfo import service as svc

    rows = [
        {"oft": "OFT-1", "producto": "A", "prod_codigo": "A1", "tejido": "Jersey",
         "planif": 100.0, "fab": 40.0, "issued": 90.0},
        {"oft": "OFT-2", "producto": "B", "prod_codigo": "B1", "tejido": "Fleece",
         "planif": 50.0, "fab": 60.0, "issued": 55.0},  # saldo -5 (yield)
    ]
    monkeypatch.setattr(svc.metabase_client, "fetch_dataset", lambda *a, **k: rows)
    svc._FABRICACION_CACHE.clear()
    out = svc.fabricacion_proceso(53)
    assert out["resumen"]["n_ofts"] == 2
    assert abs(out["resumen"]["issued"] - 145.0) < 0.01
    assert abs(out["resumen"]["fab"] - 100.0) < 0.01
    assert abs(out["resumen"]["saldo"] - 45.0) < 0.01          # 50 + (-5)
    assert abs(out["resumen"]["por_producir"] - 50.0) < 0.01   # 60 + (-10)
    tej = {t["tejido"]: t for t in out["por_tejido"]}
    assert abs(tej["Fleece"]["por_producir"] - (-10.0)) < 0.01
