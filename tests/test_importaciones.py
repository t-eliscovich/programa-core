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
    # _buscar_anticipos ahora devuelve filas CRUDAS (con fecha); n = cuántas.
    anticipos = [
        {"cta": "AC", "ref_num": 36, "importe": 30000.0, "fecha": "2026-05-01"},
        {"cta": "AC", "ref_num": 36, "importe": 20000.0, "fecha": "2026-05-10"},
        {"cta": "AC", "ref_num": 36, "importe": 5871.19, "fecha": "2026-05-20"},
    ]
    with patch.object(service.asinfo_service, "importaciones_asinfo", return_value=importaciones), \
         patch.object(service.db, "fetch_all",
                      side_effect=[[], anticipos, [], []]):
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
                          [], [],
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


def test_cruce_atribuye_anticipos_por_anio_no_los_mezcla():
    """El nº de concepto se reusa entre años: "AC 31" existe en 2024 y 2026.
    Cada importación debe recibir SOLO los anticipos de su año (fecha cercana),
    no la suma de todos. Regresión del bug 119.329 vs 58.629 (2026-07-10)."""
    from modules.importaciones import pago
    importaciones = [
        {"im_numero": "IM-2026", "fecha": "2026-04-16", "fecha_recepcion": "2026-07-09",
         "recibida": True, "bod": "BOD-1", "total_asinfo": 0.0, "proveedor": "ARIESCOPE",
         "prov_cod_asinfo": "AC", "nota": "ACMT/EXP/2026-27/78004 ( AC 31)"},
        {"im_numero": "IM-2024", "fecha": "2024-04-26", "fecha_recepcion": "2024-07-12",
         "recibida": True, "bod": "BOD-2", "total_asinfo": 0.0, "proveedor": "ARIESCOPE",
         "prov_cod_asinfo": "AC", "nota": "ACMT/EXP/2024-27/12345 ( AC 31)"},
    ]
    anticipos = [
        {"cta": "AC", "ref_num": 31, "importe": 58345.30, "fecha": "2026-06-02"},
        {"cta": "AC", "ref_num": 31, "importe": 284.10, "fecha": "2026-04-21"},
        {"cta": "AC", "ref_num": 31, "importe": 60000.00, "fecha": "2024-05-10"},
    ]
    with patch.object(service.asinfo_service, "importaciones_asinfo", return_value=importaciones), \
         patch.object(service.db, "fetch_all", side_effect=[[], anticipos]), \
         patch.object(pago, "estados_por_im", return_value={}), \
         patch.object(pago, "movimientos_por_im", return_value={}):
        rows = service.importaciones_con_cruce()
    by_im = {r["im_numero"]: r for r in rows}
    assert round(by_im["IM-2026"]["importe_programa"], 2) == 58629.40  # solo 2026
    assert round(by_im["IM-2024"]["importe_programa"], 2) == 60000.00  # solo 2024


def test_cruce_ventana_descarta_anticipo_de_otro_anio_ausente():
    """Si la importación vieja (2024) NO vino en el set y aparecen anticipos de
    2024 para el mismo código, NO se pegan a la de 2026: caen fuera de la ventana
    (>300 días) → se descartan en vez de inflar el costo."""
    from modules.importaciones import pago
    importaciones = [
        {"im_numero": "IM-2026", "fecha": "2026-04-16", "fecha_recepcion": "2026-07-09",
         "recibida": True, "bod": "BOD-1", "total_asinfo": 0.0, "proveedor": "ARIESCOPE",
         "prov_cod_asinfo": "AC", "nota": "ACMT/EXP/2026-27/78004 ( AC 31)"},
    ]
    anticipos = [
        {"cta": "AC", "ref_num": 31, "importe": 58345.30, "fecha": "2026-06-02"},
        {"cta": "AC", "ref_num": 31, "importe": 284.10, "fecha": "2026-04-21"},
        {"cta": "AC", "ref_num": 31, "importe": 60000.00, "fecha": "2024-05-10"},  # IM vieja ausente
    ]
    with patch.object(service.asinfo_service, "importaciones_asinfo", return_value=importaciones), \
         patch.object(service.db, "fetch_all", side_effect=[[], anticipos]), \
         patch.object(pago, "estados_por_im", return_value={}), \
         patch.object(pago, "movimientos_por_im", return_value={}):
        rows = service.importaciones_con_cruce()
    assert round(rows[0]["importe_programa"], 2) == 58629.40  # solo los 2 de 2026
    assert rows[0]["anticipo"]["n"] == 2


def test_costo_hilado_recibido_mes_dedup_y_filtra_por_mes():
    """Costo-a-la-fecha del hilado recibido en el mes: cuenta el $ UNA vez por
    (prov, nº, año) — las partidas ---1/---2 comparten costo — y solo las
    recibidas en el mes pedido."""
    rows = [
        # AC 31 recibida julio 2026 (importación partida en 2 filas)
        {"recibida": True, "fecha_recepcion": "2026-07-09", "kg": 24494.0,
         "importe_programa": 58629.40, "prov": "AC", "numero": 31, "fecha": "2026-04-16"},
        {"recibida": True, "fecha_recepcion": "2026-07-09", "kg": 1000.0,
         "importe_programa": 58629.40, "prov": "AC", "numero": 31, "fecha": "2026-04-16"},
        # recibida en junio → fuera del mes
        {"recibida": True, "fecha_recepcion": "2026-06-30", "kg": 9999.0,
         "importe_programa": 12345.0, "prov": "AI", "numero": 20, "fecha": "2026-06-01"},
        # en tránsito → fuera
        {"recibida": False, "fecha_recepcion": None, "kg": 5000.0,
         "importe_programa": 9000.0, "prov": "AI", "numero": 21, "fecha": "2026-06-01"},
    ]
    with patch.object(service, "importaciones_con_cruce", return_value=rows):
        out = service.costo_hilado_recibido_mes(2026, 7)
    assert out["us"] == 58629.40                 # $ una sola vez (dedup)
    assert out["kg"] == 25494.0                  # kg de ambas filas de julio
    assert out["usd_kg"] == round(58629.40 / 25494.0, 4)


def test_costo_hilado_recibido_mes_sin_datos_es_cero():
    with patch.object(service, "importaciones_con_cruce", return_value=[]):
        out = service.costo_hilado_recibido_mes(2026, 7)
    assert out == {"us": 0.0, "kg": 0.0, "usd_kg": None}


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


# ---------------------------------------------------------------------------
# No duplicar el anticipo (dueña 2026-07-10: "no puede estar duplicada")
# ---------------------------------------------------------------------------


def _row_anticipo(**kw):
    """Fila de importación pagada por ANTICIPO (no compra), con desglose."""
    base = _row(
        im_numero="IM-562", codigo="AC 15", prov="AC", numero=15,
        nota="ACMT/EXP/2026-27/8068 ( AC 15)",
        compra=None, fuente="anticipo", importe_programa=86771.30,
        anticipo={
            "importe_total": 86771.30, "n": 4,
            "items": [
                {"fecha": "2026-07-08", "importe": 18000.0},
                {"fecha": "2026-07-08", "importe": 15000.0},
                {"fecha": "2026-06-02", "importe": 53509.80},
                {"fecha": "2026-05-01", "importe": 261.50},
            ],
        },
        # el anticipo de 15.000 cargado por esta pantalla YA está en /dolares
        # (pago.py le crea la fila viva) → está dentro de los 86.771,30.
        anticipo_aplicado=15000.0,
        movimientos=[{"fecha": "2026-07-08", "tipo": "anticipo",
                      "monto": 15000.0, "nota": "cae", "id_transaccion": 1}],
    )
    base.update(kw)
    return base


def test_vista_no_duplica_anticipo_mirror_de_movimiento(app, fake_db):
    """El anticipo cargado por importaciones crea su fila viva en /dolares → ya
    está en el cruce; NO se vuelve a sumar como movimiento. 101.771 era
    86.771 (/dolares) + 15.000 (mov). El total correcto es 86.771,30."""
    c = _login_informes(app, fake_db)
    with patch.object(service, "importaciones_con_cruce", return_value=[_row_anticipo()]):
        r = c.get("/importaciones")
    html = r.get_data(as_text=True)
    assert r.status_code == 200
    assert "86.771,30" in html          # el total real (cruce de /dolares)
    assert "101.771,30" not in html     # ya NO duplica el 15.000


def test_vista_pago_parcial_si_suma(app, fake_db):
    """Un movimiento 'pago' (parcial contra stock) NO va a /dolares → sí se suma
    encima del cruce. 80.000 (/dolares) + 5.000 (pago) = 85.000."""
    row = _row_anticipo(
        anticipo={"importe_total": 80000.0, "n": 1,
                  "items": [{"fecha": "2026-07-08", "importe": 80000.0}]},
        importe_programa=80000.0, anticipo_aplicado=5000.0,
        movimientos=[{"fecha": "2026-07-09", "tipo": "pago",
                      "monto": 5000.0, "nota": "parcial", "id_transaccion": 2}],
    )
    c = _login_informes(app, fake_db)
    with patch.object(service, "importaciones_con_cruce", return_value=[row]):
        r = c.get("/importaciones")
    html = r.get_data(as_text=True)
    assert "85.000,00" in html          # total = 80.000 (/dolares) + 5.000 (pago)


def test_buscar_anticipos_sql_filtra_solo_vivos():
    """_buscar_anticipos solo cuenta anticipos VIVOS (st vacío): al convertir a
    compra pasan a st='B' y deben salir del cruce, igual que salen de /dolares."""
    cap = {}
    with patch.object(service.db, "fetch_all",
                      side_effect=lambda sql, params: cap.update(sql=sql) or []):
        service._buscar_anticipos({("AC", 15)})
    assert "coalesce(st" in cap["sql"].lower()


def test_cruce_anticipo_incluye_items_desglose():
    """El cruce adjunta el desglose (items) de los anticipos de /dolares para
    que el "Ver" muestre las N partidas (antes solo los movimientos de la pantalla)."""
    from modules.importaciones import pago
    importaciones = [_imp("ACMT/EXP/2026-27/8197 ( AC 36)", im="IM-X")]
    anticipos = [
        {"cta": "AC", "ref_num": 36, "importe": 30000.0, "fecha": "2026-05-01"},
        {"cta": "AC", "ref_num": 36, "importe": 20000.0, "fecha": "2026-05-10"},
    ]
    with patch.object(service.asinfo_service, "importaciones_asinfo", return_value=importaciones), \
         patch.object(service.db, "fetch_all", side_effect=[[], anticipos]), \
         patch.object(pago, "estados_por_im", return_value={}), \
         patch.object(pago, "movimientos_por_im", return_value={}):
        rows = service.importaciones_con_cruce()
    items = rows[0]["anticipo"]["items"]
    assert len(items) == 2
    assert items[0]["fecha"] == "2026-05-10"          # más nuevo arriba
    assert round(sum(i["importe"] for i in items), 2) == 50000.0
