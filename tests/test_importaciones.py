"""Tests del cruce Importaciones Asinfo ↔ compras del programa.

Cubre:
    - concepto_parser.parse_nota_importacion: extrae el código de la Nota
      (con todo el desorden real: paréntesis sin cerrar, doble espacio,
      basura al final, rangos, falso positivo 'INV 2026...').
    - modules.importaciones.service.importaciones_con_cruce: parsea + cruza
      contra scintela.compra (mock de asinfo + db).
    - vista /importaciones renderiza (landing + CSV).
Sin HTTP real ni DB real.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from concepto_parser import parse_nota_importacion
from modules.importaciones import service


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("nota,esperado", [
    ("ACMT/EXP/2026-27/8197 ( AC 36)", ("AC", 36, None)),
    ("INV 2026030405 ( MH  63)", ("MH", 63, None)),          # gana el último, no 'INV'
    ("ACMT/EXP/2026-27/8196 (AC 39)", ("AC", 39, None)),
    ("AYF02681 ( AI 16)", ("AI", 16, None)),
    ("ACMT/EXP/2026-27/8157 ( AC 22", ("AC", 22, None)),     # sin cerrar paréntesis
    ("ACMT/EXP/2026-27/8044 ( AC 25)\n", ("AC", 25, None)),  # basura final
    ("AYF2653 ( AI 15 )    ----1 ", ("AI", 15, None)),
    ("ALG ( MH 64-65 )", ("MH", 64, 65)),                    # rango
    ("ACMT/EXP/2026-27/8083 ( AC  94)", ("AC", 94, None)),   # doble espacio
])
def test_parse_nota_extrae_codigo(nota, esperado):
    r = parse_nota_importacion(nota)
    assert (r["prov"], r["numero"], r["numero_hasta"]) == esperado


@pytest.mark.parametrize("nota", ["", None, "   ", "sin codigo aca", "factura 999999 sola"])
def test_parse_nota_sin_codigo(nota):
    # 'factura 999999' no debe matchear: 'factura' tiene 7 letras, no 2-3.
    assert parse_nota_importacion(nota) == {} or parse_nota_importacion(nota).get("prov") is None


# ---------------------------------------------------------------------------
# importaciones_con_cruce
# ---------------------------------------------------------------------------


def _imp(nota, im="IM-1", prov_name="ARIESCOPE", total=1000.0, fecha_recepcion=None):
    return {
        "im_numero": im, "fecha": "2026-06-01",
        "fecha_recepcion": fecha_recepcion, "recibida": fecha_recepcion is not None,
        "bod": "BOD-000001" if fecha_recepcion else "",
        "total_asinfo": total, "proveedor": prov_name, "prov_cod_asinfo": "AC",
        "nota": nota,
    }


def test_cruce_matchea_compra_del_programa():
    importaciones = [_imp("ACMT/EXP/2026-27/8197 ( AC 36)", im="IM-588")]
    # _buscar_compras cruza por concepto-numérico: la fila trae ref_num (= concepto).
    compras = [{"id_compra": 501, "codigo_prov": "AC", "ref_num": 36, "numero": None,
                "importe": 51418.26, "tipo": "  ", "comprobante": "x", "concepto": "36", "fecha": "2026-05-01"}]
    with patch.object(service.asinfo_service, "importaciones_asinfo", return_value=importaciones), \
         patch.object(service.db, "fetch_all", return_value=compras):
        rows = service.importaciones_con_cruce()
    r = rows[0]
    assert r["codigo"] == "AC 36"
    assert r["compra"]["first_id"] == 501
    assert r["compra"]["importe_total"] == 51418.26
    assert r["compra"]["n"] == 1


def test_cruce_rango_suma_varias_compras():
    importaciones = [_imp("ALG ( MH 64-65 )", im="IM-700", prov_name="MORE HUMAN")]
    compras = [
        {"id_compra": 1, "codigo_prov": "MH", "ref_num": 64, "numero": None, "importe": 100.0, "tipo": "", "comprobante": "", "concepto": "64", "fecha": "2026-05-01"},
        {"id_compra": 2, "codigo_prov": "MH", "ref_num": 65, "numero": None, "importe": 200.0, "tipo": "", "comprobante": "", "concepto": "65", "fecha": "2026-05-02"},
    ]
    with patch.object(service.asinfo_service, "importaciones_asinfo", return_value=importaciones), \
         patch.object(service.db, "fetch_all", return_value=compras):
        rows = service.importaciones_con_cruce()
    assert rows[0]["compra"]["n"] == 2
    assert rows[0]["compra"]["importe_total"] == 300.0


def test_cruce_usa_anticipo_cuando_no_hay_compra():
    importaciones = [_imp("ACMT/EXP/2026-27/8197 ( AC 36)", im="IM-X")]
    # 1ª llamada a db = _buscar_compras (vacío), 2ª = _buscar_anticipos (match).
    with patch.object(service.asinfo_service, "importaciones_asinfo", return_value=importaciones), \
         patch.object(service.db, "fetch_all",
                      side_effect=[[], [{"cta": "AC", "ref_num": 36, "importe": 55871.19, "n": 3}]]):
        rows = service.importaciones_con_cruce()
    r = rows[0]
    assert r["compra"] is None
    assert r["fuente"] == "anticipo"
    assert r["importe_programa"] == 55871.19
    assert r["anticipo"]["n"] == 3


def test_cruce_compra_tiene_prioridad_sobre_anticipo():
    importaciones = [_imp("ACMT/EXP/2026-27/8197 ( AC 36)", im="IM-X")]
    with patch.object(service.asinfo_service, "importaciones_asinfo", return_value=importaciones), \
         patch.object(service.db, "fetch_all",
                      side_effect=[
                          [{"id_compra": 7, "codigo_prov": "AC", "ref_num": 36, "importe": 100.0, "concepto": "36"}],
                          [{"cta": "AC", "ref_num": 36, "importe": 999.0, "n": 2}],
                      ]):
        rows = service.importaciones_con_cruce()
    assert rows[0]["fuente"] == "compra"
    assert rows[0]["importe_programa"] == 100.0


def test_cruce_codigo_sin_compra_queda_sin_match():
    importaciones = [_imp("ACMT/EXP/2026-27/8200 ( AC 999)")]
    with patch.object(service.asinfo_service, "importaciones_asinfo", return_value=importaciones), \
         patch.object(service.db, "fetch_all", return_value=[]):
        rows = service.importaciones_con_cruce()
    assert rows[0]["codigo"] == "AC 999"
    assert rows[0]["compra"] is None


def test_cruce_sin_codigo_no_consulta_db():
    importaciones = [_imp("nota libre sin codigo")]
    with patch.object(service.asinfo_service, "importaciones_asinfo", return_value=importaciones), \
         patch.object(service.db, "fetch_all") as m:
        rows = service.importaciones_con_cruce()
    assert rows[0]["codigo"] is None
    m.assert_not_called()  # no hay refs → no se toca la DB


def test_cruce_db_caida_no_rompe():
    importaciones = [_imp("ACMT/EXP/2026-27/8197 ( AC 36)")]
    with patch.object(service.asinfo_service, "importaciones_asinfo", return_value=importaciones), \
         patch.object(service.db, "fetch_all", side_effect=RuntimeError("db offline")):
        rows = service.importaciones_con_cruce()
    # se muestra igual, sin cruce
    assert rows[0]["codigo"] == "AC 36"
    assert rows[0]["compra"] is None


# ---------------------------------------------------------------------------
# Vista /importaciones
# ---------------------------------------------------------------------------


def _login_informes(app, fake_db):
    rid = fake_db.add_role("Tester", ["stock.ver"])
    uid = fake_db.add_user("test", b"$2b$12$fakehash", rid)
    c = app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = uid
    return c


def _row(**kw):
    base = {
        "im_numero": "IM-588", "fecha": "2026-06-01",
        "fecha_recepcion": "2026-06-03", "recibida": True, "bod": "BOD-000002270",
        "total_asinfo": 51418.26, "proveedor": "ARIESCOPE", "prov_cod_asinfo": "AC",
        "nota": "ACMT/EXP/2026-27/8197 ( AC 36)", "codigo": "AC 36", "prov": "AC",
        "numero": 36, "numero_hasta": None,
        "compra": {"ids": [501], "first_id": 501, "importe_total": 51418.26, "n": 1, "tipo": ""},
        "anticipo": None, "fuente": "compra", "importe_programa": 51418.26,
    }
    base.update(kw)
    return base


def test_vista_importaciones_renderiza(app, fake_db):
    c = _login_informes(app, fake_db)
    with patch.object(service, "importaciones_con_cruce", return_value=[_row()]):
        r = c.get("/importaciones")
    assert r.status_code == 200
    assert b"IM-588" in r.data
    assert b"AC 36" in r.data


def test_vista_importaciones_csv(app, fake_db):
    c = _login_informes(app, fake_db)
    with patch.object(service, "importaciones_con_cruce", return_value=[_row()]):
        r = c.get("/importaciones?export=csv")
    assert r.status_code == 200
    assert "text/csv" in r.headers["Content-Type"]
    assert b"AC 36" in r.data
