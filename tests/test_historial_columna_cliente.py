"""El historial dice DE QUÉ CLIENTE es cada cheque y cada factura.

TMT 2026-08-18 (dueña, sobre `/historial?pagina=3`): *"nunca veo los clientes
en cheques y facturas, ponelos en algún lado"*.

Estaba sólo como código de 3 letras metido adentro del concepto ("Factura
182101 HOM", "Cheque 4839 de IIA") — y las filas de aplicación a factura, que
son 6.066, tienen el concepto VACÍO a propósito (repetía las dos columnas de
al lado), así que ahí no estaba en ningún lado.

Lo que se muestra es el CÓDIGO, no el nombre: *"nono codigo del cliente nada
mas no nombre entero"*. El nombre queda en el tooltip.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from unittest.mock import patch

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import db  # noqa: E402
from modules.historial import queries  # noqa: E402

_LISTA = Path("modules/historial/templates/historial/lista.html")

_CLIENTES = [{"cod": "IIA", "nombre": "INDUSTRIAS IIA S.A."},
             {"cod": "HOM", "nombre": "HOMERO TEXTIL CIA. LTDA."}]


def _login(app, fake_db):
    rid = fake_db.add_role("Ver", ["historial.ver", "informes.ver"])
    uid = fake_db.add_user("u", b"$2b$12$fake", rid)
    c = app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = uid
    return c


def _mov(**kw):
    base = {"id_mov": 1, "tipo": "cheque_creado", "fecha": None,
            "origen_table": "cheque", "origen_id": 4839,
            "destino_table": "cheque", "destino_id": 4839,
            "importe": 1200.0, "concepto": "Cheque 4839 de IIA", "usuario": "alex",
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


def test_el_cheque_muestra_el_codigo_del_cliente(app, fake_db):
    html = _pantalla(app, fake_db, _mov(), {
        "FROM scintela.cheque c": [{"id_cheque": 4839, "no_cheque": "4839",
                                    "no_banco": 10, "codigo_cli": "IIA",
                                    "banco_nombre": "PICHINCHA"}],
        "FROM scintela.cliente": _CLIENTES,
    })
    assert ">IIA</a>" in html
    assert "/clientes/IIA/cuenta" in html


def test_el_nombre_entero_no_se_pinta_pero_queda_en_el_tooltip(app, fake_db):
    """🚨 *"nono codigo del cliente nada mas no nombre entero"*: el nombre en
    la celda se comía el ancho de Concepto."""
    html = _pantalla(app, fake_db, _mov(), {
        "FROM scintela.cheque c": [{"id_cheque": 4839, "no_cheque": "4839",
                                    "no_banco": 10, "codigo_cli": "IIA",
                                    "banco_nombre": "PICHINCHA"}],
        "FROM scintela.cliente": _CLIENTES,
    })
    assert 'title="INDUSTRIAS IIA S.A."' in html
    assert ">INDUSTRIAS IIA S.A.<" not in html


def test_el_cobro_aplicado_a_factura_tambien_aunque_no_tenga_concepto(app, fake_db):
    """🚨 Ésta es la fila del pedido: el concepto viene vacío a propósito, así
    que sin la columna el cliente no aparecía por ningún lado."""
    html = _pantalla(
        app, fake_db,
        _mov(tipo="cheque_aplicado_a_factura", concepto="",
             destino_table="factura", destino_id=177986),
        {"FROM scintela.cheque c": [{"id_cheque": 4839, "no_cheque": "0",
                                     "no_banco": 90, "codigo_cli": "IIA",
                                     "banco_nombre": "DEP.PICH."}],
         "FROM scintela.factura WHERE": [{"id_factura": 177986, "numf": "177986",
                                          "codigo_cli": "IIA"}],
         "FROM scintela.cliente": _CLIENTES},
    )
    assert ">IIA</a>" in html


def test_la_factura_emitida_muestra_su_cliente(app, fake_db):
    html = _pantalla(
        app, fake_db,
        _mov(tipo="factura_emitida", origen_table="factura", origen_id=182101,
             destino_table="factura", destino_id=182101,
             concepto="Factura 182101 HOM"),
        {"FROM scintela.factura WHERE": [{"id_factura": 182101, "numf": "182101",
                                          "codigo_cli": "HOM"}],
         "FROM scintela.cliente": _CLIENTES},
    )
    assert ">HOM</a>" in html
    assert "/clientes/HOM/cuenta" in html


def test_un_cliente_sin_ficha_en_pc_sigue_mostrando_su_codigo(app, fake_db):
    """Los códigos del dBase que nunca se dieron de alta acá: el código es
    justamente lo que se muestra, así que no cambia nada."""
    html = _pantalla(app, fake_db, _mov(), {
        "FROM scintela.cheque c": [{"id_cheque": 4839, "no_cheque": "4839",
                                    "no_banco": 10, "codigo_cli": "ZZZ",
                                    "banco_nombre": "PICHINCHA"}],
        "FROM scintela.cliente": [],
    })
    assert ">ZZZ</a>" in html
    assert 'title="ZZZ"' in html


def test_un_movimiento_sin_cliente_no_inventa_uno(app, fake_db):
    """Un gasto o un anticipo en dólares no tienen cliente: raya, sin link."""
    html = _pantalla(
        app, fake_db,
        _mov(tipo="caja_s_to_gasto", origen_table="caja", origen_id=896,
             destino_table="xgast", destino_id=553, concepto="SU ADM"),
        {"FROM scintela.xgast": [{"id_xgast": 553, "num": 7}]},
    )
    assert "/clientes/" not in html


# ── la grilla no se desalinea ────────────────────────────────────────────────
def test_la_tabla_tiene_nueve_columnas():
    html = _LISTA.read_text()
    cabecera = html.split("<thead")[1].split("</thead>")[0]
    assert cabecera.count("<th ") == 9
    cols = re.findall(r"<col(?!group)\b", html.split("</colgroup>")[0])
    assert len(cols) == 9   # 8 con ancho fijo + Concepto, que se lleva el resto


def test_los_colspan_acompanan_a_la_columna_nueva():
    """🚨 El card violeta del batch y las filas de detalle abarcan la fila
    ENTERA: un colspan viejo deja la tarjeta corta y la grilla torcida."""
    html = _LISTA.read_text()
    assert 'colspan="8"' not in html
    assert html.count('colspan="9"') == 3
