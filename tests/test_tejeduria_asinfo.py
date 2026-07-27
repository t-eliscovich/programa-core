"""Tests de la tab Producción Tejeduría Asinfo:
  - asinfo.service.produccion_tejeduria_mes (clasificación INTELA/tercerizado,
    agregación, fail-soft) mockeando metabase_client.
  - tejeduria_asinfo.service.resumen_mes (match contra compras K, pendientes)
    mockeando la producción Asinfo y las dos queries a scintela.compra.
Sin HTTP ni DB real.
"""
from __future__ import annotations

import contextlib
from unittest.mock import patch

import pytest

from modules._lib import metabase_client
from modules.asinfo import service as asvc
from modules.tejeduria_asinfo import queries as tq
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


def _run_resumen(compras, estampadas, tarifas=None):
    # today_ec fijo en julio 2026 → mes 7 es "el mes actual" (no pasado), así
    # el gate "meses viejos = todo cargado" no dispara y los tests son
    # deterministas (resumen_mes importa today_ec desde filters en runtime).
    import datetime as _dt
    with patch.object(tsvc.asinfo_service, "produccion_tejeduria_mes", return_value=_PROD), \
         patch.object(tsvc, "_compras_k_por_prov", return_value=compras), \
         patch.object(tsvc, "_ofts_estampadas", return_value=estampadas), \
         patch.object(tsvc._tarifas, "listar_tarifas", return_value=(tarifas or [])), \
         patch.object(tsvc, "falta_acumulada", return_value={}), \
         patch("filters.today_ec", return_value=_dt.date(2026, 7, 15)):
        return tsvc.resumen_mes(2026, 7)


def test_match_marca_falta_y_cargado():
    # AP ya tiene 1600 cargado (falta ~21); RY no tiene nada (falta 811).
    out = _run_resumen(
        compras={"AP": {"kg": 1600.0, "importe": 1656.0, "n": 1}},
        estampadas={},
    )
    tj = {t["cod"]: t for t in out["tejedores"]}
    assert tj["AP"]["compra_kg"] == 1600.0
    assert tj["AP"]["falta_kg"] == pytest.approx(21.25)
    assert tj["RY"]["compra_kg"] == 0.0
    assert tj["RY"]["falta_kg"] == pytest.approx(811.45)


def test_pendientes_solo_tercerizadas_sin_oft_estampado():
    # OFT-2 (AP) ya estampado (con $) → NO pendiente (estado 'compra'). OFT-4
    # (RY) sin estampar y con falta → pendiente. OFT-1 es INTELA (no se lista).
    out = _run_resumen(compras={}, estampadas={"OFT-2": 1656.0})
    nums = {of["numero"] for of in out["pendientes"]}
    assert nums == {"OFT-4"}
    assert all(not of["es_intela"] for of in out["pendientes"])
    # la OFT-2 estampada aparece en la lista por OF con su monto de compra
    porof = {of["numero"]: of for of in out["tercerizado_ofs"]}
    assert porof["OFT-2"]["estado"] == "compra"
    assert porof["OFT-2"]["compra_monto"] == pytest.approx(1656.0)
    assert porof["OFT-4"]["estado"] == "pendiente"


def test_resumen_diario_pivotea_por_tejedor():
    out = _run_resumen(compras={}, estampadas={})
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
         patch.object(tsvc._tarifas, "listar_tarifas", return_value=[]), \
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


# ---------------------------------------------------------------------------
# TARIFAS $/kg (mig 0133) + carga automática de pendientes — TMT 2026-07-26
# ---------------------------------------------------------------------------

_TARIFAS = [
    {"id_tarifa": 1, "cod_prov": "RY", "patron": None, "tarifa": 2.0125,
     "nota": "", "usuario_modifica": "", "fecha_modifica": None},
    {"id_tarifa": 2, "cod_prov": "AP", "patron": "HUF", "tarifa": 1.0350,
     "nota": "", "usuario_modifica": "", "fecha_modifica": None},
    {"id_tarifa": 3, "cod_prov": "AP", "patron": None, "tarifa": 0.5750,
     "nota": "", "usuario_modifica": "", "fecha_modifica": None},
]


def test_resolver_default_del_proveedor():
    assert tq.resolver(_TARIFAS, "RY", "M REYES KW22 C T-40") == pytest.approx(2.0125)
    assert tq.resolver(_TARIFAS, "AP", "A PONCE KW20 R/N") == pytest.approx(0.5750)


def test_resolver_patron_gana_sobre_default():
    # el caso real: Ponce cobra 1,035 los artículos con HUF y 0,575 el resto.
    assert tq.resolver(_TARIFAS, "AP", "A PONCE KW20 HUF40 R D/C LYCRA") == pytest.approx(1.0350)


def test_resolver_patron_mas_especifico_gana():
    tars = _TARIFAS + [
        {"id_tarifa": 9, "cod_prov": "AP", "patron": "HUF40 R/A", "tarifa": 1.5,
         "nota": "", "usuario_modifica": "", "fecha_modifica": None},
    ]
    # 'HUF40 R/A' (9 chars) le gana a 'HUF' (3) porque es más específico.
    assert tq.resolver(tars, "AP", "A PONCE KW22 HUF40 R/A") == pytest.approx(1.5)
    assert tq.resolver(tars, "AP", "A PONCE KW20 HUF40 R D/C") == pytest.approx(1.0350)


def test_resolver_sin_tarifa_devuelve_none():
    # proveedor sin fila → None. NUNCA inventamos un precio.
    assert tq.resolver(_TARIFAS, "UN", "R UNDA ALGO") is None
    assert tq.resolver(_TARIFAS, "", "x") is None
    assert tq.resolver([], "RY", "x") is None


def test_importe_sugerido_por_of():
    out = _run_resumen(compras={}, estampadas={}, tarifas=_TARIFAS)
    porof = {of["numero"]: of for of in out["tercerizado_ofs"]}
    # RY: 811,45 kg × 2,0125
    assert porof["OFT-4"]["tarifa"] == pytest.approx(2.0125)
    assert porof["OFT-4"]["importe_sugerido"] == pytest.approx(1633.04, abs=0.01)
    # AP sin HUF: 1.621,25 × 0,575
    assert porof["OFT-2"]["importe_sugerido"] == pytest.approx(932.22, abs=0.01)
    assert out["total_pendiente"] == pytest.approx(1633.04 + 932.22, abs=0.02)
    assert out["pendientes_sin_tarifa"] == 0


def test_sin_tarifa_no_sugiere_importe():
    out = _run_resumen(compras={}, estampadas={}, tarifas=[])
    porof = {of["numero"]: of for of in out["tercerizado_ofs"]}
    assert porof["OFT-4"]["tarifa"] is None
    assert porof["OFT-4"]["importe_sugerido"] is None
    assert out["total_pendiente"] == 0
    assert out["pendientes_sin_tarifa"] == 2


def _run_cargar(compras, estampadas, tarifas, falta_acum=None):
    """falta_acum=None → tope generoso (no bloquea), para probar el resto."""
    import datetime as _dt
    creadas = []

    def _fake_crear(**kw):
        creadas.append(kw)
        return {"id_compra": len(creadas), "numero": 900 + len(creadas)}

    if falta_acum is None:
        falta_acum = {"AP": 99999.0, "RY": 99999.0}
    with patch.object(tsvc.asinfo_service, "produccion_tejeduria_mes", return_value=_PROD), \
         patch.object(tsvc, "_compras_k_por_prov", return_value=compras), \
         patch.object(tsvc, "_ofts_estampadas", return_value=dict(estampadas)), \
         patch.object(tsvc._tarifas, "listar_tarifas", return_value=tarifas), \
         patch.object(tsvc, "falta_acumulada", return_value=dict(falta_acum)), \
         patch("filters.today_ec", return_value=_dt.date(2026, 7, 15)), \
         patch("modules.compras.queries.crear", side_effect=_fake_crear):
        res = tsvc.cargar_pendientes(2026, 7, usuario="tester", clave="TST")
    return res, creadas


def test_cargar_pendientes_crea_con_kg_por_tarifa():
    res, creadas = _run_cargar({}, {}, _TARIFAS)
    assert res["creadas"] == 2 and res["salteadas"] == 0
    por_prov = {c["codigo_prov"]: c for c in creadas}
    assert por_prov["RY"]["tipo"] == "K"
    assert por_prov["RY"]["kg"] == pytest.approx(811.45)
    assert por_prov["RY"]["importe"] == pytest.approx(1633.04, abs=0.01)
    # estampa el OFT en el concepto (match fino de acá en adelante)
    assert por_prov["RY"]["concepto"].startswith("OFT-4")
    # fecha = día de la OF, no hoy
    assert por_prov["RY"]["fecha"].isoformat() == "2026-07-08"
    # marcador que import_dbf preserva en el sync del dBase
    assert por_prov["RY"]["usuario"] == tsvc.MARCADOR_CARGA == "asinfo-tejeduria"


def test_cargar_pendientes_saltea_sin_tarifa():
    # sólo RY tiene tarifa → AP se saltea, no se inventa el precio.
    solo_ry = [t for t in _TARIFAS if t["cod_prov"] == "RY"]
    res, creadas = _run_cargar({}, {}, solo_ry)
    assert res["creadas"] == 1 and res["salteadas"] == 1
    assert [c["codigo_prov"] for c in creadas] == ["RY"]
    assert any("sin tarifa" in d["motivo"] for d in res["detalle"] if not d["ok"])


def test_cargar_pendientes_topea_por_lo_que_falta():
    # EL caso real del 26/07: a RY sólo le faltan 100 kg acumulados pero la OF
    # trae 811 → NO se carga (sin el tope se creaba una compra duplicada de una
    # OF ya pagada).
    res, creadas = _run_cargar(
        {}, {}, _TARIFAS, falta_acum={"RY": 100.0, "AP": 99999.0})
    cods = [c["codigo_prov"] for c in creadas]
    assert "RY" not in cods
    assert "AP" in cods
    assert any("excede lo que falta de RY" in d["motivo"]
               for d in res["detalle"] if not d["ok"])


def test_tope_es_acumulado_no_del_mes():
    # El mes dice que RY está cubierto (compró 711 de los 811 producidos en el
    # mes) pero el ACUMULADO dice que faltan 2.000 kg → se carga igual. Ese es
    # todo el punto de que el tope sea por proveedor y no por mes: el maquilero
    # factura con desfase y a caballo de dos meses.
    res, creadas = _run_cargar(
        {"RY": {"kg": 711.45, "importe": 1400.0, "n": 1}}, {}, _TARIFAS,
        falta_acum={"RY": 2000.0, "AP": 2000.0})
    assert "RY" in [c["codigo_prov"] for c in creadas]


def test_sin_asinfo_no_carga_nada():
    # falta_acumulada devuelve {} cuando Asinfo no responde → mejor no cargar
    # que cargar de más.
    res, creadas = _run_cargar({}, {}, _TARIFAS, falta_acum={})
    assert creadas == [] and res["creadas"] == 0
    assert res["salteadas"] == 2


def test_rango_ventana_3_meses():
    assert tsvc._rango_ventana(2026, 7) == ("2026-05-01", "2026-08-01")
    assert tsvc._rango_ventana(2026, 1) == ("2025-11-01", "2026-02-01")
    assert tsvc._rango_ventana(2026, 12) == ("2026-10-01", "2027-01-01")
    assert tsvc._rango_ventana(2026, 7, meses=1) == ("2026-07-01", "2026-08-01")


def test_falta_acumulada_resta_lo_cargado_y_saltea_intela():
    prod = {"disponible": True, "por_tejedor": [
        {"cod": "RY", "label": "Reyes", "es_intela": False, "kg": 5000.0, "ofs": 6},
        {"cod": "AP", "label": "Ponce", "es_intela": False, "kg": 9000.0, "ofs": 9},
        {"cod": "KK", "label": "INTELA", "es_intela": True, "kg": 600000.0, "ofs": 200},
    ]}
    with patch.object(tsvc.asinfo_service, "produccion_tejeduria_rango", return_value=prod), \
         patch.object(tsvc, "_compras_k_por_prov_rango",
                      return_value={"RY": 2740.97, "AP": 5159.45, "KK": 1.0}):
        out = tsvc.falta_acumulada(2026, 7)
    assert out == {"RY": pytest.approx(2259.03), "AP": pytest.approx(3840.55)}
    assert "KK" not in out          # INTELA es autoprod, no factura


def test_falta_acumulada_fail_soft_sin_asinfo():
    with patch.object(tsvc.asinfo_service, "produccion_tejeduria_rango",
                      return_value={"disponible": False, "por_tejedor": []}):
        assert tsvc.falta_acumulada(2026, 7) == {}


def test_cargar_pendientes_saltea_oft_ya_estampado():
    res, creadas = _run_cargar({}, {"OFT-4": 1633.04}, _TARIFAS)
    assert [c["codigo_prov"] for c in creadas] == ["AP"]
    assert res["creadas"] == 1


def test_cargar_pendientes_una_of_que_falla_no_corta_el_lote():
    import datetime as _dt
    llamadas = []

    def _crear_falla_primera(**kw):
        llamadas.append(kw)
        if len(llamadas) == 1:
            raise ValueError("periodo cerrado")
        return {"id_compra": 2, "numero": 902}

    with patch.object(tsvc.asinfo_service, "produccion_tejeduria_mes", return_value=_PROD), \
         patch.object(tsvc, "_compras_k_por_prov", return_value={}), \
         patch.object(tsvc, "_ofts_estampadas", return_value={}), \
         patch.object(tsvc._tarifas, "listar_tarifas", return_value=_TARIFAS), \
         patch.object(tsvc, "falta_acumulada",
                      return_value={"AP": 99999.0, "RY": 99999.0}), \
         patch("filters.today_ec", return_value=_dt.date(2026, 7, 15)), \
         patch("modules.compras.queries.crear", side_effect=_crear_falla_primera):
        res = tsvc.cargar_pendientes(2026, 7, usuario="t", clave="T")
    assert res["creadas"] == 1 and res["salteadas"] == 1
    assert any("periodo cerrado" in d["motivo"] for d in res["detalle"] if not d["ok"])


def test_listar_tarifas_fail_soft_sin_tabla():
    # migración 0133 sin aplicar → la pantalla no rompe.
    with patch("db.fetch_all", side_effect=RuntimeError("no existe la relación")):
        assert tq.listar_tarifas() == []


def test_dry_run_no_escribe_y_devuelve_el_mismo_plan():
    """El preview usa la MISMA función con dry_run=True: no puede mentir."""
    import datetime as _dt
    with patch.object(tsvc.asinfo_service, "produccion_tejeduria_mes", return_value=_PROD), \
         patch.object(tsvc, "_compras_k_por_prov", return_value={}), \
         patch.object(tsvc, "_ofts_estampadas", return_value={}), \
         patch.object(tsvc._tarifas, "listar_tarifas", return_value=_TARIFAS), \
         patch.object(tsvc, "falta_acumulada",
                      return_value={"AP": 99999.0, "RY": 99999.0}), \
         patch.object(tsvc, "_importes_k_existentes", return_value={}), \
         patch("filters.today_ec", return_value=_dt.date(2026, 7, 15)), \
         patch("modules.compras.queries.crear") as m_crear:
        plan = tsvc.cargar_pendientes(2026, 7, usuario="t", dry_run=True)
    m_crear.assert_not_called()                    # NO escribió nada
    assert plan["dry_run"] is True
    assert plan["creadas"] == 2
    assert plan["importe"] == pytest.approx(1633.04 + 932.22, abs=0.02)
    # el detalle trae todo lo que la pantalla necesita mostrar
    ok = [d for d in plan["detalle"] if d["ok"]]
    assert {d["oft"] for d in ok} == {"OFT-2", "OFT-4"}
    assert all(d["tarifa"] and d["kg"] and d["descripcion"] is not None for d in ok)


def test_dry_run_y_ejecucion_dan_el_mismo_conjunto():
    plan, _ = _run_cargar({}, {}, _TARIFAS)          # ejecución real (mockeada)
    import datetime as _dt
    with patch.object(tsvc.asinfo_service, "produccion_tejeduria_mes", return_value=_PROD), \
         patch.object(tsvc, "_compras_k_por_prov", return_value={}), \
         patch.object(tsvc, "_ofts_estampadas", return_value={}), \
         patch.object(tsvc._tarifas, "listar_tarifas", return_value=_TARIFAS), \
         patch.object(tsvc, "falta_acumulada",
                      return_value={"AP": 99999.0, "RY": 99999.0}), \
         patch.object(tsvc, "_importes_k_existentes", return_value={}), \
         patch("filters.today_ec", return_value=_dt.date(2026, 7, 15)):
        prev = tsvc.cargar_pendientes(2026, 7, usuario="t", dry_run=True)
    assert prev["creadas"] == plan["creadas"]
    assert prev["importe"] == pytest.approx(plan["importe"])
    assert ({d["oft"] for d in prev["detalle"] if d["ok"]}
            == {d["oft"] for d in plan["detalle"] if d["ok"]})


def test_avisa_duplicado_por_importe_pero_no_bloquea():
    # Ya existe una compra de RY por 1.633,04 → avisa, pero la carga igual
    # (dos OFs del mismo peso dan el mismo importe; bloquear tiraría legítimas).
    import datetime as _dt
    with patch.object(tsvc.asinfo_service, "produccion_tejeduria_mes", return_value=_PROD), \
         patch.object(tsvc, "_compras_k_por_prov", return_value={}), \
         patch.object(tsvc, "_ofts_estampadas", return_value={}), \
         patch.object(tsvc._tarifas, "listar_tarifas", return_value=_TARIFAS), \
         patch.object(tsvc, "falta_acumulada",
                      return_value={"AP": 99999.0, "RY": 99999.0}), \
         patch.object(tsvc, "_importes_k_existentes",
                      return_value={"RY": [1633.04]}), \
         patch("filters.today_ec", return_value=_dt.date(2026, 7, 15)):
        plan = tsvc.cargar_pendientes(2026, 7, usuario="t", dry_run=True)
    ry = next(d for d in plan["detalle"] if d["oft"] == "OFT-4")
    ap = next(d for d in plan["detalle"] if d["oft"] == "OFT-2")
    assert ry["ok"] is True and ry["dup_warn"] is True
    assert ap["dup_warn"] is False
    assert plan["avisos_dup"] == 1


def test_preview_renderiza_200(app, fake_db):
    rid = fake_db.add_role("Tester", ["compras.ver", "compras.crear"])
    uid = fake_db.add_user("test2", b"$2b$12$fakehash", rid)
    c = app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = uid
    with patch.object(metabase_client, "fetch_dataset", return_value=_ROWS), \
         patch.object(tsvc, "falta_acumulada", return_value={"AP": 9999.0, "RY": 9999.0}), \
         patch.object(tsvc, "_importes_k_existentes", return_value={}):
        r = c.get("/produccion-tejeduria-asinfo/cargar-pendientes?anio=2026&mes=7")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "PREVISUALIZACIÓN" in body or "previsualización" in body.lower()


def test_auto_carga_al_abrir_la_pantalla(app, fake_db):
    """Dueña 2026-07-26: 'que cada ingreso genere un pasivo'. Abrir la tab en el
    MES EN CURSO crea sola la compra+pasivo de las OFs pendientes."""
    import datetime as _dt
    rid = fake_db.add_role("Tester3", ["compras.ver", "compras.crear"])
    uid = fake_db.add_user("test3", b"$2b$12$fakehash", rid)
    c = app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = uid
    hoy = _dt.date(2026, 7, 15)
    with patch.object(metabase_client, "fetch_dataset", return_value=_ROWS), \
         patch.object(tsvc._tarifas, "listar_tarifas", return_value=_TARIFAS), \
         patch.object(tsvc, "falta_acumulada", return_value={"AP": 9999.0, "RY": 9999.0}), \
         patch.object(tsvc, "_importes_k_existentes", return_value={}), \
         patch("filters.today_ec", return_value=hoy), \
         patch("modules.tejeduria_asinfo.views.today_ec", return_value=hoy), \
         patch("modules.compras.queries.crear",
               return_value={"id_compra": 1, "numero": 900}) as m_crear:
        r = c.get("/produccion-tejeduria-asinfo?anio=2026&mes=7")
    assert r.status_code == 200
    assert m_crear.called, "no auto-cargó al abrir la pantalla"


def test_auto_carga_NO_dispara_en_meses_pasados(app, fake_db):
    """Navegar a un mes viejo no puede crear compras retroactivas."""
    import datetime as _dt
    rid = fake_db.add_role("Tester4", ["compras.ver", "compras.crear"])
    uid = fake_db.add_user("test4", b"$2b$12$fakehash", rid)
    c = app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = uid
    with patch.object(metabase_client, "fetch_dataset", return_value=_ROWS), \
         patch.object(tsvc._tarifas, "listar_tarifas", return_value=_TARIFAS), \
         patch.object(tsvc, "falta_acumulada", return_value={"AP": 9999.0, "RY": 9999.0}), \
         patch.object(tsvc, "_importes_k_existentes", return_value={}), \
         patch("filters.today_ec", return_value=_dt.date(2026, 9, 3)), \
         patch("modules.tejeduria_asinfo.views.today_ec", return_value=_dt.date(2026, 9, 3)), \
         patch("modules.compras.queries.crear") as m_crear:
        r = c.get("/produccion-tejeduria-asinfo?anio=2026&mes=7")
    assert r.status_code == 200
    assert not m_crear.called, "no debe auto-cargar meses pasados"


def test_auto_carga_requiere_permiso_de_crear(app, fake_db):
    """Quien sólo mira (compras.ver) no crea pasivos por abrir la pantalla."""
    import datetime as _dt
    rid = fake_db.add_role("SoloVer", ["compras.ver"])
    uid = fake_db.add_user("solover", b"$2b$12$fakehash", rid)
    c = app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = uid
    hoy = _dt.date(2026, 7, 15)
    with patch.object(metabase_client, "fetch_dataset", return_value=_ROWS), \
         patch.object(tsvc._tarifas, "listar_tarifas", return_value=_TARIFAS), \
         patch.object(tsvc, "falta_acumulada", return_value={"AP": 9999.0, "RY": 9999.0}), \
         patch.object(tsvc, "_importes_k_existentes", return_value={}), \
         patch("filters.today_ec", return_value=hoy), \
         patch("modules.tejeduria_asinfo.views.today_ec", return_value=hoy), \
         patch("modules.compras.queries.crear") as m_crear:
        r = c.get("/produccion-tejeduria-asinfo?anio=2026&mes=7")
    assert r.status_code == 200
    assert not m_crear.called


@contextlib.contextmanager
def _asinfo_estable():
    """Asinfo + tarifas fijas, con hoy dentro de julio 2026 — para testear el
    render de la pantalla sin que dependa del reloj ni de la red."""
    import datetime as _dt
    with contextlib.ExitStack() as st:
        st.enter_context(patch.object(
            metabase_client, "fetch_dataset", return_value=_ROWS))
        st.enter_context(patch.object(
            tsvc._tarifas, "listar_tarifas", return_value=_TARIFAS))
        st.enter_context(patch.object(
            tsvc, "falta_acumulada", return_value={"AP": 9999.0, "RY": 9999.0}))
        st.enter_context(patch.object(
            tsvc, "_importes_k_existentes", return_value={}))
        st.enter_context(patch(
            "filters.today_ec", return_value=_dt.date(2026, 7, 15)))
        yield


def _cliente_solo_ver(app, fake_db, nombre):
    """Cliente logueado que sólo puede MIRAR — así el GET de la tab no dispara
    la auto-carga y podemos testear el render del filtro en aislamiento."""
    rid = fake_db.add_role(f"Ver-{nombre}", ["compras.ver"])
    uid = fake_db.add_user(nombre, b"$2b$12$fakehash", rid)
    c = app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = uid
    return c


def test_filtro_solo_pendientes_recorta_la_tabla(app, fake_db):
    """Dueña 2026-07-26: '¿se me va a hacer gigante la lista?'. El chip «sólo
    pendientes» muestra únicamente las OFs que todavía necesitan una mano."""
    c = _cliente_solo_ver(app, fake_db, "solofiltro")
    with _asinfo_estable():
        todas = c.get("/produccion-tejeduria-asinfo?anio=2026&mes=7")
        pend = c.get("/produccion-tejeduria-asinfo?anio=2026&mes=7&solo=pend")
    assert todas.status_code == 200 and pend.status_code == 200
    b_todas = todas.get_data(as_text=True)
    b_pend = pend.get_data(as_text=True)
    # El chip existe en las dos vistas.
    assert "Sólo pendientes" in b_todas and "Todas" in b_pend
    # Filtrado nunca muestra MÁS filas que sin filtrar.
    assert b_pend.count("<tr>") <= b_todas.count("<tr>")
    # Y las OFs ya cargadas (estado != pendiente) desaparecen: el marcador
    # «cargado» de la última columna no puede estar en la vista filtrada.
    assert ">cargado<" not in b_pend


def test_filtro_default_es_todas(app, fake_db):
    """Sin ?solo= la tabla viene completa — es la que cuadra PRODUCIDO vs
    CARGADO, así que no se recorta por default."""
    c = _cliente_solo_ver(app, fake_db, "solodefault")
    with _asinfo_estable():
        r = c.get("/produccion-tejeduria-asinfo?anio=2026&mes=7")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    # El chip "Todas" queda marcado (fondo oscuro) cuando no hay filtro.
    assert "Todas (" in body
    assert "solo=pend" in body, "falta el link al filtro"


def test_secciones_plegables_con_flecha(app, fake_db):
    """Dueña 2026-07-26: 'la lista está muy larga, por lo menos poné una flecha
    para abajo'. Las dos tablas largas son <details> nativos: la de OFs abre
    sola si hay pendientes, el diario arranca SIEMPRE plegado."""
    c = _cliente_solo_ver(app, fake_db, "soloflecha")
    with _asinfo_estable():
        r = c.get("/produccion-tejeduria-asinfo?anio=2026&mes=7")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "<summary>Tercerizado por OF" in body
    assert "<summary>Diario · ingreso a bodega" in body
    # El diario nunca viene abierto (es una fila por día).
    diario = body[body.index("<summary>Diario") - 200: body.index("<summary>Diario")]
    assert "<details>" in diario and "<details open>" not in diario


def test_of_plegada_cuando_no_hay_pendientes(app, fake_db):
    """Si no hay nada para cargar, la tabla de OFs arranca cerrada — el número
    del título ya dice todo."""
    c = _cliente_solo_ver(app, fake_db, "solocerrada")
    # Sin pendientes: estampamos todos los OFT de la muestra.
    with _asinfo_estable(), patch.object(
        tsvc, "_ofts_estampadas",
        return_value={"OFT-1": 1, "OFT-2": 1, "OFT-3": 1, "OFT-4": 1},
    ):
        r = c.get("/produccion-tejeduria-asinfo?anio=2026&mes=7")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    i = body.index("<summary>Tercerizado por OF")
    assert "<details open>" not in body[i - 200:i], "no debería abrirse sin pendientes"


def test_tarifas_plegadas_con_resumen_en_la_cabecera(app, fake_db):
    """Dueña 2026-07-26: 'podés hacer lo mismo con las tarifas?'. El panel se
    pliega, pero el resumen de la cabecera lleva las tarifas vigentes — no hay
    que abrirlo para saber a cuánto se valúa."""
    c = _cliente_solo_ver(app, fake_db, "solotarifas")
    with _asinfo_estable():
        r = c.get("/produccion-tejeduria-asinfo?anio=2026&mes=7")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    i = body.index("<summary>Tarifas de tejeduría")
    assert "<details open>" not in body[i - 200:i], "con tarifas cargadas va plegado"
    # Las tarifas vigentes se leen sin abrir.
    cabecera = body[i:i + 700]
    for t in _TARIFAS:
        assert t["cod_prov"] in cabecera


def test_tarifas_abiertas_si_no_hay_ninguna(app, fake_db):
    """Sin tarifas cargadas el panel arranca ABIERTO — ahí sí hay que hacer algo."""
    c = _cliente_solo_ver(app, fake_db, "solosintarifa")
    with _asinfo_estable(), patch.object(
        tsvc._tarifas, "listar_tarifas", return_value=[],
    ):
        r = c.get("/produccion-tejeduria-asinfo?anio=2026&mes=7")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    i = body.index("<summary>Tarifas de tejeduría")
    assert "<details open>" in body[i - 200:i]
