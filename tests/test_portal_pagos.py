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
    # "Devuelto" volvió el 09/09 (dueña: "mostrar cheques protestados"), pero
    # como pestaña propia y rótulo, no como recorrido.
    for palabra in ("Depositado", "Endosado", "Postergado",
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
        "facturas": [], "cheques": cheques, "anticipos": [],
        "totales": q.totales_estado_cuenta_en_cero(),
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


def test_el_mes_del_grupo_es_el_de_la_fecha_que_se_muestra(monkeypatch):
    """🐞 09/09/2026, con AJT: un bloque "SIN FECHA" entre septiembre y julio
    con cuatro cheques que decían "recibido 27/07/2026". El grupo iba por
    `dia_ingreso` pelado y la fila por `dia_ingreso or fecha_recibido or
    fecha`: dos reglas para la misma fecha. Ahora es UNA (`dia_recibido`)."""
    from datetime import date
    html = _pantalla(monkeypatch, [
        _cheque(id_cheque=1, no_cheque="0001111", dia_ingreso=date(2026, 9, 1)),
        _cheque(id_cheque=2, no_cheque="0002222", dia_ingreso=None,
                fecha_recibido=date(2026, 7, 27), fecha=date(2026, 7, 27)),
    ])
    assert "Sin fecha" not in html and "SIN FECHA" not in html
    assert "Julio 2026" in html
    assert html.index("Septiembre 2026") < html.index("0001111") < html.index("Julio 2026") < html.index("0002222")


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
    assert "Todavía no registramos pagos suyos" in html


def test_el_devuelto_se_ve_como_un_pago_mas(monkeypatch):
    """Se lo recibimos igual: se ve. Desde el 04/09 (dueña) va ROTULADO
    "devuelto", porque sin el rótulo el cliente creía que ya había pagado."""
    html = _pantalla(monkeypatch, [_cheque(stat="1", no_cheque="0077777")])
    assert "0077777" in html
    assert "devuelto" in html


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
    # Desde el 04/09/2026 el link vive en el armazón (barra + menú) y en el
    # "Ver todos" del inicio.
    armazon = (TPL / "_app.html").read_text(encoding="utf-8")
    assert '"/mis-pagos"' in re.sub(r"\{#.*?#\}", "", armazon, flags=re.S)
    # Desde el 09/09/2026 el inicio es la grilla de la oficina: el link va
    # sólo por el armazón.


def test_el_cheque_devuelto_se_ve_pero_rotulado(monkeypatch):
    """Dueña 04/09: un devuelto que se ve como un pago más hace creer al
    cliente que ya pagó. Se ve, con "devuelto" y sin la fecha de depósito."""
    html = _pantalla(monkeypatch, [_cheque(id_cheque=1, no_cheque="0004444", stat="1")])
    assert "devuelto" in html and "el banco lo devolvió" in html
    assert "Para depositar" not in html


def test_cada_cheque_dice_que_facturas_pago(monkeypatch):
    """Dueña 09/09/2026: "mostrar los cheques que facturas pagaron". Sale de
    `chequesxfact` por `informes.queries.aplicaciones_cliente` (el portal
    no lee facturas ni cheques por su cuenta)."""
    from modules.informes import queries as iq
    monkeypatch.setattr(iq, "aplicaciones_cliente", lambda cod: [
        {"id_cheque": 1, "no_cheque": "0001840", "id_fact": 9, "numf": 183341,
         "numf_completo": "001-099-000183341", "aplicado": 735.25},
        {"id_cheque": 1, "no_cheque": "0001840", "id_fact": 8, "numf": 183198,
         "numf_completo": "001-099-000183198", "aplicado": 580.74},
    ])
    html = _pantalla(monkeypatch, [_cheque(id_cheque=1, no_cheque="0001840"),
                                   _cheque(id_cheque=2, no_cheque="0001841")])
    # En LISTA, una factura por renglón (dueña 09/09: "está todo en uno").
    assert "<tr><td>183341</td><td class=\"n\">735,25</td></tr>" in html
    assert "<tr><td>183198</td><td class=\"n\">580,74</td></tr>" in html
    # El otro cheque no pagó nada todavía: sin la lista.
    assert html.count('class="pago-facturas"') == 1


def test_el_cruce_no_tumba_la_lista_si_la_consulta_falla(monkeypatch):
    from modules.informes import queries as iq
    monkeypatch.setattr(iq, "aplicaciones_cliente", lambda cod: (_ for _ in ()).throw(RuntimeError("sin base")))
    html = _pantalla(monkeypatch, [_cheque(no_cheque="0001840")])
    assert "0001840" in html and 'class="pago-facturas"' not in html


def test_la_consulta_del_cruce_vive_en_informes_y_lee_chequesxfact():
    import inspect

    from modules.informes import queries as iq
    assert "scintela.chequesxfact" in inspect.getsource(iq.aplicaciones_cliente)


def test_los_devueltos_tienen_su_pestana_y_su_chip_en_el_inicio(monkeypatch):
    """Dueña 09/09/2026: "mostrar en algún lugar cheques protestados"."""
    from datetime import date
    cheques = [_cheque(id_cheque=1, no_cheque="0004444", stat="1", dia_ingreso=date(2026, 7, 27)),
               _cheque(id_cheque=2, no_cheque="0005555", stat="Z", dia_ingreso=date(2026, 9, 1))]
    html = _pantalla(monkeypatch, cheques)
    assert "Devueltos (1)" in html and "0005555" in html
    app, deshacer = _app_portal()
    try:
        _con_pagos(monkeypatch, cheques)
        c = app.test_client()
        with c.session_transaction() as s:
            s["portal_cliente"] = "ATE"
        solo = c.get("/mis-pagos?ver=devueltos").get_data(as_text=True)
        assert "0004444" in solo and "0005555" not in solo
        inicio = c.get("/estado-de-cuenta").get_data(as_text=True)
        assert "1 cheque devuelto" in inicio and 'href="/mis-pagos?ver=devueltos"' in inicio
    finally:
        deshacer()
