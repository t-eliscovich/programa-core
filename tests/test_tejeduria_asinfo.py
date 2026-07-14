"""Tests de la tab Producción Tejeduría Asinfo:
  - asinfo.service.produccion_tejeduria_mes (clasificación INTELA/tercerizado,
    agregación, fail-soft) mockeando metabase_client.
  - tejeduria_asinfo.service.resumen_mes (match contra compras K, pendientes)
    mockeando la producción Asinfo y las dos queries a scintela.compra.
Sin HTTP ni DB real.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from modules._lib import metabase_client
from modules.asinfo import service as asvc
from modules.tejeduria_asinfo import service as tsvc


@pytest.fixture(autouse=True)
def _limpiar_cache():
    asvc.reset_prod_tejeduria_cache()
    yield
    asvc.reset_prod_tejeduria_cache()


# ---------------------------------------------------------------------------
# _clasificar_tejedor
# ---------------------------------------------------------------------------


def test_clasificar_intela_por_maquina():
    cod, label, es_intela = asvc._clasificar_tejedor("MQ16 ABY20 ABY14 F-96")
    assert (cod, es_intela) == ("KK", True)
    assert label == "INTELA"


def test_clasificar_tercerizado_por_nombre():
    assert asvc._clasificar_tejedor("A PONCE HY10 20 R/N 10%")[:1] == ("AP",)
    assert asvc._clasificar_tejedor("M REYES KW22 C T-40")[0] == "RY"


def test_clasificar_desconocido_sin_cod():
    cod, label, es_intela = asvc._clasificar_tejedor("GUALILAHUA algo")
    assert cod == ""          # no mapeado → cod vacío
    assert es_intela is False
    assert label  # etiqueta con las 2 primeras palabras


# ---------------------------------------------------------------------------
# produccion_tejeduria_mes
# ---------------------------------------------------------------------------

_ROWS = [
    {"numero": "OFT-1", "dia": "2026-07-14", "kg": 35421.37, "descripcion": "MQ16 ABY F-96"},
    {"numero": "OFT-2", "dia": "2026-07-08", "kg": 1621.25, "descripcion": "A PONCE KW20 R/N"},
    {"numero": "OFT-3", "dia": "2026-07-08", "kg": 1022.85, "descripcion": "A PONCE HY10 10%"},
    {"numero": "OFT-4", "dia": "2026-07-08", "kg": 811.45, "descripcion": "M REYES KW22 C T-40"},
]


def test_produccion_agrupa_y_clasifica():
    with patch.object(metabase_client, "fetch_dataset", return_value=_ROWS):
        out = asvc.produccion_tejeduria_mes(2026, 7)
    assert out["disponible"] is True
    assert out["total_kg"] == pytest.approx(35421.37 + 1621.25 + 1022.85 + 811.45, rel=1e-6)
    port = {t["cod"]: t for t in out["por_tejedor"]}
    assert port["KK"]["es_intela"] is True and port["KK"]["ofs"] == 1
    assert port["AP"]["kg"] == pytest.approx(1621.25 + 1022.85)
    assert port["AP"]["ofs"] == 2
    assert port["RY"]["kg"] == pytest.approx(811.45)


def test_produccion_sql_filtra_bodega_52_cerradas():
    with patch.object(metabase_client, "fetch_dataset", return_value=_ROWS) as m:
        asvc.produccion_tejeduria_mes(2026, 7)
    sql = m.call_args[0][1]
    assert "orden_fabricacion" in sql
    assert "id_bodega = 52" in sql
    assert "indicador_hoja = 1" in sql
    assert "estado_produccion = 5" in sql
    assert "'2026-07-01'" in sql and "'2026-08-01'" in sql


def test_produccion_fail_soft_si_asinfo_cae():
    with patch.object(metabase_client, "fetch_dataset", side_effect=RuntimeError("x")):
        out = asvc.produccion_tejeduria_mes(2026, 7)
    assert out["disponible"] is False
    assert out["ofs"] == [] and out["por_tejedor"] == []


def test_produccion_cachea():
    with patch.object(metabase_client, "fetch_dataset", return_value=_ROWS) as m:
        asvc.produccion_tejeduria_mes(2026, 7)
        asvc.produccion_tejeduria_mes(2026, 7)  # cache hit
    assert m.call_count == 1


# ---------------------------------------------------------------------------
# resumen_mes (match)
# ---------------------------------------------------------------------------

_PROD = {
    "disponible": True, "anio": 2026, "mes": 7, "total_kg": 4300.0,
    "ofs": [
        {"numero": "OFT-2", "dia": "2026-07-08", "kg": 1621.25,
         "descripcion": "A PONCE KW20", "cod": "AP", "label": "Ponce", "es_intela": False},
        {"numero": "OFT-4", "dia": "2026-07-08", "kg": 811.45,
         "descripcion": "M REYES KW22", "cod": "RY", "label": "Reyes", "es_intela": False},
        {"numero": "OFT-1", "dia": "2026-07-14", "kg": 1867.30,
         "descripcion": "MQ16 F-96", "cod": "KK", "label": "INTELA", "es_intela": True},
    ],
    "por_tejedor": [],
}


def _run_resumen(compras, estampadas):
    with patch.object(tsvc.asinfo_service, "produccion_tejeduria_mes", return_value=_PROD), \
         patch.object(tsvc, "_compras_k_por_prov", return_value=compras), \
         patch.object(tsvc, "_ofts_estampadas", return_value=estampadas):
        return tsvc.resumen_mes(2026, 7)


def test_match_marca_falta_y_cargado():
    # AP ya tiene 1600 cargado (falta ~21); RY no tiene nada (falta 811).
    out = _run_resumen(
        compras={"AP": {"kg": 1600.0, "importe": 1656.0, "n": 1}},
        estampadas=set(),
    )
    tj = {t["cod"]: t for t in out["tejedores"]}
    assert tj["AP"]["compra_kg"] == 1600.0
    assert tj["AP"]["falta_kg"] == pytest.approx(21.25)
    assert tj["RY"]["compra_kg"] == 0.0
    assert tj["RY"]["falta_kg"] == pytest.approx(811.45)


def test_pendientes_solo_tercerizadas_sin_oft_estampado():
    # OFT-2 (AP) ya estampado en una compra → NO pendiente. OFT-4 (RY) sí.
    out = _run_resumen(compras={}, estampadas={"OFT-2"})
    nums = {of["numero"] for of in out["pendientes"]}
    assert nums == {"OFT-4"}           # OFT-2 estampado, OFT-1 es INTELA
    assert all(not of["es_intela"] for of in out["pendientes"])


def test_resumen_diario_pivotea_por_tejedor():
    out = _run_resumen(compras={}, estampadas=set())
    dias = {d["dia"]: d for d in out["por_dia"]}
    assert dias["2026-07-08"]["kg"]["AP"] == pytest.approx(1621.25)
    assert dias["2026-07-08"]["kg"]["RY"] == pytest.approx(811.45)
    assert dias["2026-07-14"]["total"] == pytest.approx(1867.30)
    # orden: más nuevo arriba
    assert out["por_dia"][0]["dia"] == "2026-07-14"


def test_resumen_fail_soft_asinfo_no_disponible():
    prod_off = {"disponible": False, "anio": 2026, "mes": 7, "total_kg": 0.0,
                "ofs": [], "por_tejedor": []}
    with patch.object(tsvc.asinfo_service, "produccion_tejeduria_mes", return_value=prod_off), \
         patch.object(tsvc, "_compras_k_por_prov", return_value={}) as mc, \
         patch.object(tsvc, "_ofts_estampadas", return_value=set()) as me:
        out = tsvc.resumen_mes(2026, 7)
    assert out["disponible"] is False
    assert out["tejedores"] == [] and out["pendientes"] == []
    mc.assert_not_called()   # no consultamos compras si Asinfo no está
    me.assert_not_called()


# ---------------------------------------------------------------------------
# render de la ruta /produccion-tejeduria-asinfo
# ---------------------------------------------------------------------------


def test_tab_renderiza_200(app, fake_db):
    rid = fake_db.add_role("Tester", ["compras.ver"])
    uid = fake_db.add_user("test", b"$2b$12$fakehash", rid)
    c = app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = uid
    with patch.object(metabase_client, "fetch_dataset", return_value=_ROWS):
        r = c.get("/produccion-tejeduria-asinfo?anio=2026&mes=7")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "Tejeduría" in body
    assert "OFT-4" in body       # OF tercerizada pendiente (RY, sin OFT estampado)
    assert "OFT-1" not in body   # INTELA no se lista como OF individual
