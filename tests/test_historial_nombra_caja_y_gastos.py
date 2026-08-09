"""El historial nombra lo que no tiene número: caja, gastos, compras, USD.

TMT 2026-08-09, mirando `/historial?pagina=3`: *"Compra N° 10144, caja y
gastos siguen mal"* → *"quiero que se vea concepto de gasto, a qué V1, V2, etc
fue aplicado"*, *"caja tampoco sé el concepto exacto"* y, sobre el concepto
"22758         7", *"no puede ser ese número lo importante del movimiento"*.

Las tres causas eran distintas:

1. El `num` del gasto NO es un correlativo: es la CATEGORÍA V1..V9 del legacy
   (la que arma GTEJ/GTIN/GGF en el balance). "Gasto #553" era el id interno y
   "Gasto #7" se leía como "el gasto número 7" cuando quería decir V7.
2. Caja no tiene número NI lo necesita: fecha, concepto e importe ya están en
   la fila. El id interno era ruido.
3. El concepto "22758         7" es el N° de factura del proveedor con el DÍA
   del mes pegado — ancho fijo del dBase (ALTAS.PRG L649), que el puente de
   formulas_app sigue escribiendo. El día ya está en la columna Fecha.
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import db  # noqa: E402
from modules.compras import queries as cq  # noqa: E402
from modules.gastos import queries as gq  # noqa: E402
from modules.historial import queries  # noqa: E402


# ── 1) el gasto se nombra por su rubro ───────────────────────────────────────
def test_el_num_del_gasto_es_la_categoria():
    assert gq.nombre_categoria(7) == "V7 · Personal admin"
    assert gq.nombre_categoria(4) == "V4 · Personal tintorería"


def test_una_categoria_que_no_existe_no_inventa_nombre():
    assert gq.nombre_categoria(0) == ""
    assert gq.nombre_categoria(None) == ""
    assert gq.nombre_categoria("x") == ""


# ── 2) la referencia de la compra, sin el día ────────────────────────────────
def test_el_dia_pegado_se_va():
    assert cq.referencia("22758         7", date(2026, 8, 7)) == "22758"
    assert cq.referencia("F26-27/125    9", date(2026, 8, 9)) == "F26-27/125"


def test_si_el_numero_final_no_es_el_dia_se_queda():
    """🚨 El corte es a ciegas si no se chequea: "101  3" con fecha del 9 NO es
    el formato del dBase, y ese 3 quiere decir otra cosa."""
    assert cq.referencia("101           3", date(2026, 8, 9)) == "101 3"


def test_un_concepto_normal_no_se_toca():
    assert cq.referencia("AC 36/8/57", None) == "AC 36/8/57"
    assert cq.referencia("", None) == ""
    assert cq.referencia(None, None) == ""


# ── 3) las etiquetas de los tipos ────────────────────────────────────────────
def test_los_tipos_tecnicos_tienen_castellano():
    assert queries.label("caja_s_to_gasto") == "Caja → Gasto"
    assert queries.label("nota_debito") == "Nota de débito"
    assert queries.label("dolares_anticipo") == "Anticipo en dólares"
    # El fallback sigue existiendo para un tipo nuevo que nadie etiquetó.
    assert queries.label("un_tipo_flamante") == "Un Tipo Flamante"


def test_caja_no_lleva_el_id_interno():
    url, etq = queries.link_origen({"origen_table": "caja", "origen_id": 896})
    assert etq == "Caja"
    assert url == "/caja?id=896"    # el link sigue cayendo en LA fila


def test_el_pago_de_importacion_ya_no_sale_como_nombre_de_tabla():
    _u, etq = queries.link_origen(
        {"origen_table": "importacion_pago_mov", "origen_id": 2})
    assert etq == "Pago de importación"


# ── 4) la pantalla entera ────────────────────────────────────────────────────
def _login(app, fake_db):
    rid = fake_db.add_role("Ver", ["historial.ver", "informes.ver"])
    uid = fake_db.add_user("u", b"$2b$12$fake", rid)
    c = app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = uid
    return c


def _mov(**kw):
    base = {"id_mov": 1, "tipo": "caja_s_to_gasto", "fecha": None,
            "origen_table": "caja", "origen_id": 896,
            "destino_table": "xgast", "destino_id": 553,
            "importe": 400.0, "concepto": "SU ADM QUINTEROS", "usuario": "alex",
            "estado": "activo", "metadata": None, "batch_id": None}
    base.update(kw)
    return base


def _pantalla(app, fake_db, mov, filas_por_tabla):
    """`filas_por_tabla`: substring del SQL → filas que devuelve el batch."""
    def _fake(sql, params=None, *a, **k):
        for aguja, filas in filas_por_tabla.items():
            if aguja in (sql or ""):
                return filas
        return []

    c = _login(app, fake_db)
    with patch.object(queries, "listar", return_value=[mov]), \
         patch.object(queries, "conteos", return_value={"total_activos": 0}), \
         patch.object(queries, "resumen_batches", return_value={}), \
         patch.object(queries, "op_cuenta_por_retiro", return_value={}), \
         patch.object(db, "fetch_all", side_effect=_fake):
        r = c.get("/historial")
    assert r.status_code == 200, r.data[:400]
    return r.data.decode()


def test_la_fila_de_caja_a_gasto_se_lee_entera(app, fake_db):
    html = _pantalla(app, fake_db, _mov(),
                     {"FROM scintela.xgast": [{"id_xgast": 553, "num": 7}]})
    assert "Caja → Gasto" in html          # el tipo, en castellano
    assert "Gasto V7 · Personal admin" in html
    assert "Caja #896" not in html
    assert "Gasto #553" not in html


def test_una_compra_sin_numero_se_nombra_por_su_proveedor(app, fake_db):
    html = _pantalla(
        app, fake_db,
        _mov(tipo="compra_a_posdat", origen_table="compra", origen_id=350,
             destino_table="posdat", destino_id=926, concepto="lo que sea"),
        {"FROM scintela.compra c": [{"id_compra": 350, "numero": "", "fecha": None,
                                     "tipo": "", "concepto": "AC 36/8/57",
                                     "prov": "OP", "proveedor": "OVER PRICE"}]})
    assert "Compra OP · Over Price · AC 36/8/57" in html
    assert "Compra #350" not in html


def test_el_concepto_de_la_compra_dice_que_se_compro(app, fake_db):
    html = _pantalla(
        app, fake_db,
        _mov(tipo="compra_a_posdat", origen_table="compra", origen_id=496,
             destino_table="posdat", destino_id=926, concepto="22758         7"),
        {"FROM scintela.compra c": [{"id_compra": 496, "numero": "10144",
                                     "fecha": date(2026, 8, 7), "tipo": "Q",
                                     "concepto": "22758         7",
                                     "prov": "SY",
                                     "proveedor": "SEYQUIN CIA.LTDA."}]})
    # 🚨 TMT 2026-08-09: *"¿el 10144 es importante?"*. La lista de /compras ni
    # siquiera muestra el N° —es un correlativo interno, sólo vive en la ficha
    # a la que lleva el link—: lo que la reconoce es el proveedor.
    assert "Compra SY · Seyquin Cia.Ltda." in html
    assert "10144" not in html
    assert "Químicos · fact. 22758" in html
    assert "22758         7" not in html


def test_la_pantalla_de_compras_muestra_la_misma_referencia_que_el_historial():
    """🚨 TMT 2026-08-09 señaló la celda Concepto de /compras: "22758 7". Las
    dos pantallas leen la MISMA columna, así que tienen que limpiarla con la
    misma función — si no, una dice "22758" y la otra "22758 7"."""
    from pathlib import Path
    vista = Path("modules/compras/views.py").read_text(encoding="utf-8")
    tpl = Path("modules/compras/templates/compras/lista.html").read_text(
        encoding="utf-8")
    assert "queries.referencia(" in vista
    assert "concepto_ref" in tpl
