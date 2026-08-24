"""Después de emitir un cheque se vuelve al FORM, no a los movimientos del banco.

TMT 2026-08-17 (dueña): *"cuando emitís el cheque, también que vuelva a
pantalla de emitir"*.

Antes el POST redirigía a `/bancos/<no_banco>`. Está bien para CONTROLAR un
cheque suelto —lo ves en su contexto— y mal para el trabajo real, que es pagar
una TANDA de posdatados: había que volver al wizard a mano por cada cheque.

Lo que se arrastra en el redirect y lo que NO importa tanto como el redirect
mismo:

  · `no_banco` — para no volver a elegirlo. Es casi siempre Pichincha, pero si
    la tanda salió de otro banco, elegirlo de nuevo por cheque es el mismo
    trámite que vinimos a sacar.
  · `fecha` — el form la resetea a HOY si no se la pasan. Una tanda con fecha
    de otro día perdía la fecha a partir del segundo cheque.
  · `id_posdat` NO se arrastra: esa obligación se acaba de pagar. Volver con
    ella pre-cargada y pre-seleccionada es una invitación a pagarla dos veces.
  · el N° de cheque tampoco: el form lo recalcula como MAX(numreferencia)+1
    del banco, y el que se acaba de emitir ya está insertado con
    documento='CH', así que el siguiente sale solo.
"""
from __future__ import annotations

import os
import sys
from urllib.parse import parse_qs, urlparse

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


@pytest.fixture
def logueado(app):
    """Login falso: registrar un before_request DESPUÉS de create_app().

    Pisar `auth.load_logged_in_user` no sirve — `app.py` ya lo importó cuando
    corre la suite entera (pasa solo, falla en conjunto).
    """
    from flask import g

    @app.before_request
    def _login():
        g.user = {"id_usuario": 1, "username": "tamara", "id_rol": 1,
                  "nombre_rol": "Accionista", "activo": True, "vend": None}
        g.permisos = {"*"}

    return app


@pytest.fixture
def emitir_fake(monkeypatch):
    """Reemplaza la emisión real y registra con qué la llamaron."""
    from modules.bancos import queries as bq

    llamadas = {}

    def _fake(**kw):
        llamadas.update(kw)
        return {
            "id_transaccion": 9001,
            "no_banco": kw["no_banco"],
            "no_cheque": kw.get("no_cheque") or "",
            "banco_nombre": "Pichincha",
            "tipo": kw["tipo"],
            "importe": float(kw["importe"]),
            "side_effect": "ninguno",
            "id_mov_doble": None,
        }

    monkeypatch.setattr(bq, "emitir_cheque", _fake)
    return llamadas


def _emitir(client, **extra):
    # `confirmar_fecha` va desde el 2026-08-24: la pantalla pregunta cuando la
    # fecha es anterior a hoy (freno que vino de la nota de débito) y este test
    # es sobre el REDIRECT, no sobre esa pregunta. La fecha del caso es fija
    # porque el test mira que el redirect la arrastre.
    datos = {"tipo": "otro", "no_banco": "1", "importe": "1.500,00",
             "fecha": "2026-08-14", "no_cheque": "100501", "confirmar_fecha": "1",
             "beneficiario": "HILTEXPOY", "concepto": "PAGO FACTURA"}
    datos.update(extra)
    return client.post("/bancos/emitir-cheque", data=datos)


def test_vuelve_al_form_de_emitir_y_no_a_los_movimientos(logueado, emitir_fake):
    r = _emitir(logueado.test_client())
    assert r.status_code == 302
    destino = urlparse(r.headers["Location"])
    assert destino.path == "/bancos/emitir-cheque", (
        f"tenía que volver al form; fue a {r.headers['Location']}")


def test_arrastra_el_banco_y_la_fecha(logueado, emitir_fake):
    r = _emitir(logueado.test_client())
    args = parse_qs(urlparse(r.headers["Location"]).query)
    assert args.get("no_banco") == ["1"]
    assert args.get("fecha") == ["2026-08-14"], (
        "sin la fecha, el 2º cheque de una tanda vieja vuelve a HOY")


def test_no_arrastra_la_posdat_recien_pagada(logueado, emitir_fake):
    """Volver con la posdat pre-cargada invita a pagarla dos veces."""
    r = _emitir(logueado.test_client(), tipo="proveedor", id_posdat="4321")
    args = parse_qs(urlparse(r.headers["Location"]).query)
    assert "id_posdat" not in args, f"volvió con la posdat puesta: {args}"


def test_el_cartel_dice_que_cheque_salio(logueado, emitir_fake):
    """Al volver al form ya no se ve el cheque en la lista del banco.

    Si el cartel no dice el número, la dueña queda sin confirmación de CUÁL
    salió — que es justo lo que se pierde al no caer en /bancos/<n>.
    """
    c = logueado.test_client()
    _emitir(c)
    with c.session_transaction() as sesion:
        mensajes = " ".join(m for _, m in sesion.get("_flashes", []))
    assert "100501" in mensajes, f"el cartel no nombra el cheque: {mensajes!r}"
    assert "Pichincha" in mensajes


def test_la_fecha_del_querystring_prellena_el_form(logueado, emitir_fake):
    """La otra mitad: que el form LEA el ?fecha= que le manda el redirect."""
    r = logueado.test_client().get("/bancos/emitir-cheque?fecha=2026-08-14")
    assert r.status_code == 200
    assert 'value="2026-08-14"' in r.get_data(as_text=True)


def test_sin_fecha_en_la_url_el_form_usa_hoy(logueado, emitir_fake):
    from filters import today_ec

    r = logueado.test_client().get("/bancos/emitir-cheque")
    assert r.status_code == 200
    assert f'value="{today_ec().isoformat()}"' in r.get_data(as_text=True)


def test_si_la_emision_falla_no_redirige(logueado, monkeypatch):
    """Un ValueError tiene que dejarte en el form CON el error, no rebotar."""
    from modules.bancos import queries as bq

    def _explota(**kw):
        raise ValueError("Banco no_banco=99 no existe.")

    monkeypatch.setattr(bq, "emitir_cheque", _explota)
    r = _emitir(logueado.test_client(), no_banco="99")
    assert r.status_code == 200, "un error de validación no puede redirigir"
