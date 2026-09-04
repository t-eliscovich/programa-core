"""Mis pagos: lo que le recibimos, sin el detalle de adentro.

TMT 2026-08-26. En el estado de cuenta los pagos se ven sólo mientras siguen
EN CARTERA: una vez depositados desaparecen de la pestaña. Esta pantalla los
tiene todos, que es lo que contesta *"¿les llegó lo que les dejé?"*.

⭐ **Es un recibo, no la máquina de estados.** TMT: *"no mostremos tanto
detalle, sólo fecha y recibido"*. El recorrido del cheque —postergado,
depositado, endosado, devuelto— es trabajo nuestro, y contarlo abre preguntas
que él no hizo. Lo que le importa de la plata está en su estado de cuenta.

⭐ **No hay una consulta nueva.** Sale de `estado_cuenta_cliente`, la misma
función que la oficina: si el cliente viera otra cuenta, el que llama es él.
"""
from __future__ import annotations

import datetime as _dt
import re
import sys
from pathlib import Path

if not hasattr(_dt, "UTC"):          # el sandbox a veces corre python 3.10
    _dt.UTC = _dt.timezone.utc  # noqa: UP017

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules.cheques import estados  # noqa: E402

TPL = ROOT / "modules" / "portal" / "templates" / "portal"
PANTALLA = (TPL / "pagos.html").read_text(encoding="utf-8")
VISTAS = (ROOT / "modules" / "portal" / "views.py").read_text(encoding="utf-8")

HOY = _dt.date(2026, 8, 26)


def _cheque(**kw):
    """Un cheque como lo devuelve `estado_cuenta_cliente`."""
    base = {"id_cheque": 1, "no_cheque": "0012345", "fecha": HOY, "fechad": HOY,
            "fechaing": HOY, "fecha_recibido": HOY, "fecha_crea": HOY,
            "fechaout": None, "dia_ingreso": HOY, "fechad_original": None,
            "fecha_postergacion": None, "importe": 1000, "stat": "Z",
            "banco": "PICHINCHA", "nombre_banco": "PICHINCHA", "no_banco": 10,
            "por_cobrar": True}
    return {**base, **kw}


# ---------------------------------------------------------------------------
# Qué se le muestra
# ---------------------------------------------------------------------------


def test_el_anulado_no_se_le_muestra():
    """Para él ese cheque no existió: verlo listado es una pregunta que no
    tenía."""
    assert estados.se_le_muestra_al_cliente("X") is False


def test_todo_lo_demas_SI():
    """Un pago que le recibimos es un pago que le recibimos, sin importar por
    dónde ande adentro."""
    for letra in estados.ESTADOS:
        if letra == "X":
            continue
        assert estados.se_le_muestra_al_cliente(letra) is True, letra


def test_sin_estado_no_se_muestra():
    assert estados.se_le_muestra_al_cliente("") is False
    assert estados.se_le_muestra_al_cliente(None) is False


def test_la_pantalla_NO_cuenta_el_recorrido_del_cheque():
    """🚨 Lo que la dueña sacó. Si alguien vuelve a poner el estado, esto se
    pone rojo: son palabras nuestras, no del cliente."""
    sin_comentarios = re.sub(r"\{#.*?#\}", "", PANTALLA, flags=re.S)
    for palabra in ("Depositado", "Devuelto", "Endosado", "Postergado",
                    "cartera", "Daniela", "stat"):
        assert palabra not in sin_comentarios, palabra


# ---------------------------------------------------------------------------
# La pantalla
# ---------------------------------------------------------------------------


def _app_portal():
    import os
    from unittest.mock import patch

    from tests.test_routes_smoke import build_app
    with patch.dict(os.environ, {**os.environ, "MODO": "portal"}):
        return build_app()


def _con_pagos(monkeypatch, cheques):
    from modules.informes import queries as q
    monkeypatch.setattr(q, "estado_cuenta_cliente", lambda cod: {
        "cliente": {"codigo_cli": cod, "nombre": "ALMACENES TEXTILES"},
        "facturas": [], "cheques": cheques, "anticipos": [], "totales": {},
    })


def _pantalla(monkeypatch, cheques):
    app, deshacer = _app_portal()
    try:
        _con_pagos(monkeypatch, cheques)
        c = app.test_client()
        with c.session_transaction() as s:
            s["portal_cliente"] = "ATE"
        r = c.get("/mis-pagos")
        assert r.status_code == 200
        return r.get_data(as_text=True)
    finally:
        deshacer()


def test_sin_sesion_manda_a_la_puerta():
    app, deshacer = _app_portal()
    try:
        r = app.test_client().get("/mis-pagos")
        assert r.status_code == 302
        assert r.headers["Location"].endswith("/ingresar")
    finally:
        deshacer()


def test_el_cheque_YA_DEPOSITADO_tambien_se_ve(monkeypatch):
    """🚨 El motivo de esta pantalla. En el estado de cuenta desaparece —la
    pestaña filtra por `por_cobrar`— y es justo el que él pregunta. Se ve, sin
    contarle el recorrido: se ve que se lo recibimos."""
    html = _pantalla(monkeypatch, [
        _cheque(stat="B", por_cobrar=False, no_cheque="0088888",
                fechaout=_dt.date(2026, 8, 20)),
    ])
    assert "0088888" in html


def test_el_anulado_NO_aparece_en_la_pantalla(monkeypatch):
    html = _pantalla(monkeypatch, [_cheque(stat="X", no_cheque="0099999")])
    assert "0099999" not in html


def test_lo_ultimo_va_primero(monkeypatch):
    """`estado_cuenta_cliente` los da del más viejo al más nuevo, porque el
    papel se lee así. Acá vino a mirar el de la semana pasada."""
    html = _pantalla(monkeypatch, [
        _cheque(id_cheque=1, no_cheque="0001111"),
        _cheque(id_cheque=2, no_cheque="0002222"),
    ])
    assert html.index("0002222") < html.index("0001111")


def test_el_orden_es_por_la_fecha_que_se_muestra(monkeypatch):
    """🐞 04/09/2026, con AJT: la columna "Recibido" salía 01/09, 01/09,
    21/07, 01/09… porque se daba vuelta el orden de la consulta (fecha del
    cheque) y en pantalla se muestra OTRA fecha (la de ingreso)."""
    from datetime import date
    html = _pantalla(monkeypatch, [
        _cheque(id_cheque=1, no_cheque="0001111", dia_ingreso=date(2026, 9, 1)),
        _cheque(id_cheque=2, no_cheque="0002222", dia_ingreso=date(2026, 7, 21)),
        _cheque(id_cheque=3, no_cheque="0003333", dia_ingreso=date(2026, 9, 1)),
    ])
    assert html.index("0003333") < html.index("0001111") < html.index("0002222")


def test_el_deposito_no_se_llama_cheque(monkeypatch):
    """En la tabla de cheques viven las tres cosas. Decirle 'Cheque' a una
    transferencia hace que el cliente jure que él no dejó ningún cheque."""
    html = _pantalla(monkeypatch, [_cheque(no_banco=90, nombre_banco="DEP.PICH.")])
    assert "Depósito" in html


def test_el_efectivo_tampoco(monkeypatch):
    html = _pantalla(monkeypatch, [_cheque(no_banco=99, stat="C", por_cobrar=False)])
    assert "Efectivo" in html


def test_sin_pagos_lo_dice_sin_asustar(monkeypatch):
    html = _pantalla(monkeypatch, [])
    assert "Todavía no tenemos ningún pago suyo cargado" in html


def test_el_devuelto_se_ve_como_un_pago_mas(monkeypatch):
    """Se lo recibimos igual. Que el banco lo haya devuelto se ve donde se
    discute la plata —su estado de cuenta—, no acá."""
    html = _pantalla(monkeypatch, [_cheque(stat="1", no_cheque="0077777")])
    assert "0077777" in html
    assert "devolvió" not in html


# ---------------------------------------------------------------------------
# Las reglas de siempre
# ---------------------------------------------------------------------------


def test_los_numeros_salen_de_la_funcion_de_la_oficina():
    assert "_cargar_estado_cuenta" in VISTAS


def test_el_portal_sigue_sin_escribir_consultas_de_plata():
    """El mismo candado que ya cuidaba el estado de cuenta, ahora con una
    pantalla más colgando de él."""
    for tabla in ("scintela.factura", "scintela.cheque", "SUM("):
        assert tabla not in VISTAS


def test_se_llega_desde_el_estado_de_cuenta():
    """Una pantalla sin link es una pantalla que no existe."""
    # Desde el 04/09/2026 el link vive en el menú de abajo, que el estado
    # de cuenta incluye.
    ec = (TPL / "estado_cuenta.html").read_text(encoding="utf-8")
    assert '{% include "portal/_menu.html" %}' in ec
    menu = (TPL / "_menu.html").read_text(encoding="utf-8")
    assert '"/mis-pagos"' in re.sub(r"\{#.*?#\}", "", menu, flags=re.S)
