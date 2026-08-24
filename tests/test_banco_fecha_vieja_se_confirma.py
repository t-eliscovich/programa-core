"""Un movimiento de banco con fecha vieja se confirma, no se graba de una.

TMT 2026-08-07 (dueña, mirando el +$7.340 de la traza): *"debería ese
movimiento armarse con la fecha de hoy, no con el 05/08"*.

Los dos movimientos de *Comisiones e impuestos* los cargó Alex el **07/08 a las
12:00** con `fecha = 2026-08-05`. Mueven la utilidad de HOY pero quedan
archivados en la fila de anteayer, así que quien los busca por el día en que la
plata se movió no los encuentra — le pasó a ella en la pantalla de PICHINCHA.

El form YA proponía hoy. Lo que faltaba: que poner una fecha vieja sea un acto
deliberado y no un enter de más. **No se bloquea** —cargar el movimiento de
ayer es legítimo y pasa seguido—: se re-pregunta, igual que el prompt ACTIVA?,
y hasta que confirmen NO se graba nada.

⭐ TMT 2026-08-24 — la pregunta se MUDÓ. La nota de débito dejó de tener
pantalla propia: se emite en la misma que el cheque (dueña: *"cuando emitimos
nota de débito, tiene que ser igual que emitir cheque, misma pantalla. sin
numero de cheque"*), así que el freno vive ahora en `/bancos/emitir-cheque` y
vale para los DOS documentos — un cheque con fecha vieja archiva mal lo mismo
que una ND.
"""
from __future__ import annotations

import os
import sys
from datetime import timedelta

import pytest
from werkzeug.datastructures import MultiDict

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from modules.bancos import views as bancos_views  # noqa: E402
from tests.test_routes_smoke import ALL_PERMS, _make_fake_user, build_app  # noqa: E402

HOY = bancos_views.today_ec()
ANTEAYER = HOY - timedelta(days=3)


class _FakeQueries:
    """Stub de modules.bancos.queries — anota si se GRABÓ y con qué fecha."""

    DOCS_EMITIBLES = ("CH", "ND")

    class MovimientoRepetido(Exception):
        def __init__(self, id_transaccion=0, concepto="", fecha=None):
            self.id_transaccion = id_transaccion
            self.concepto = concepto
            self.fecha = fecha

    def __init__(self):
        self.creado = None

    def bancos_operativos(self):
        return [{"no_banco": 10, "nombre": "PICHINCHA"}]

    def posdat_abiertas_de(self, prov=None):
        return []

    def conceptos_frecuentes_egresos(self, limite=50):
        return []

    def proveedores_activos(self, limite=500):
        return []

    def emitir_cheque(self, **kw):
        self.creado = kw
        return {"id_transaccion": 1, "no_banco": kw["no_banco"],
                "no_cheque": kw.get("no_cheque") or "", "banco_nombre": "PICHINCHA",
                "importe": kw["importe"], "side_effect": "ninguno"}


@pytest.fixture
def cliente_y_queries():
    app, deshacer = build_app()
    app.config["WTF_CSRF_ENABLED"] = False

    @app.before_request
    def _login_falso():  # pragma: no cover - infraestructura del test
        from flask import g
        g.user = _make_fake_user()
        g.permisos = set(ALL_PERMS)

    fake = _FakeQueries()
    previo = bancos_views.queries
    bancos_views.queries = fake
    try:
        yield app.test_client(), fake
    finally:
        bancos_views.queries = previo
        deshacer()


def _post(cliente, campos):
    return cliente.post("/bancos/emitir-cheque", data=MultiDict(campos),
                        follow_redirects=False)


def _base(fecha, documento="ND"):
    return [("documento", documento), ("tipo", "otro"), ("no_banco", "10"),
            ("importe", "64,73"), ("fecha", fecha.isoformat()),
            ("concepto", "COMISIONES E IMPUESTOS")]


def test_con_fecha_vieja_no_graba_nada_y_pregunta(cliente_y_queries):
    """⭐ LA REGRESIÓN: entre el enter y el asiento hay una pregunta."""
    cliente, fake = cliente_y_queries
    r = _post(cliente, _base(ANTEAYER))

    assert fake.creado is None, (
        "grabó el movimiento con fecha vieja sin preguntar — es exactamente "
        "lo que dejó los $7.404,88 archivados en el día equivocado"
    )
    assert r.status_code == 200, "tiene que preguntar, no redirigir"
    html = r.get_data(as_text=True)
    assert "FECHA VIEJA" in html
    assert ANTEAYER.strftime("%d/%m/%Y") in html, "la pregunta dice QUÉ fecha"


def test_el_cheque_con_fecha_vieja_pregunta_igual(cliente_y_queries):
    """⭐ TMT 2026-08-24: el freno se mudó y ahora TAMBIÉN cubre el cheque."""
    cliente, fake = cliente_y_queries
    r = _post(cliente, _base(ANTEAYER, documento="CH"))

    assert fake.creado is None, "un cheque con fecha vieja archiva mal igual"
    assert "FECHA VIEJA" in r.get_data(as_text=True)


def test_confirmada_la_fecha_vieja_se_graba_tal_cual(cliente_y_queries):
    """Cargar el movimiento de ayer es legítimo: confirmando, entra."""
    cliente, fake = cliente_y_queries
    _post(cliente, _base(ANTEAYER) + [("confirmar_fecha", "1")])

    assert fake.creado is not None, "confirmada, tiene que grabar"
    assert fake.creado["fecha"] == ANTEAYER


def test_con_la_fecha_de_hoy_no_pregunta_nada(cliente_y_queries):
    """El camino normal no se paga con un click de más."""
    cliente, fake = cliente_y_queries
    _post(cliente, _base(HOY))

    assert fake.creado is not None
    assert fake.creado["fecha"] == HOY


def test_el_boton_de_hoy_pisa_la_fecha_vieja(cliente_y_queries):
    """El otro botón de la pregunta: "No, ponele la de hoy"."""
    cliente, fake = cliente_y_queries
    _post(cliente, _base(ANTEAYER) + [("usar_hoy", "1")])

    assert fake.creado is not None
    assert fake.creado["fecha"] == HOY, (
        "el botón tiene que PISAR la fecha vieja del input"
    )


def test_el_boton_no_puede_llamarse_como_el_input_de_fecha():
    """🚨 Dos inputs con el mismo `name` ⇒ `form.get()` devuelve el PRIMERO.

    La fecha tipeada viaja en un hidden dentro del MISMO form de la pregunta,
    así que un botón llamado `fecha` no pisaría nada y el bug se vería como
    "aprieto y no hace nada". Misma trampa que desactivaba usuarios el 05/08.
    """
    from pathlib import Path

    tpl = (Path(__file__).resolve().parent.parent / "modules" / "bancos"
           / "templates" / "bancos" / "emitir_confirmar.html").read_text(
        encoding="utf-8")
    bloque = tpl.split("{% if pregunta_fecha %}")[1].split("{% endif %}")[0]
    assert 'name="usar_hoy"' in bloque
    assert 'name="fecha"' not in bloque


def test_el_hidden_de_la_pregunta_lleva_la_fecha_ya_pisada(cliente_y_queries):
    """🪤 Si apretás "ponele la de hoy" y DESPUÉS salta otra pregunta, el
    hidden tiene que llevar la fecha NUEVA, no la vieja que se descartó."""
    cliente, fake = cliente_y_queries

    def _repetido(**kw):
        raise fake.MovimientoRepetido(id_transaccion=99, concepto="ALGO")

    fake.emitir_cheque = _repetido
    r = _post(cliente, _base(ANTEAYER) + [("usar_hoy", "1")])
    html = r.get_data(as_text=True)

    assert f'name="fecha" value="{HOY.isoformat()}"' in html
    assert ANTEAYER.isoformat() not in html, (
        "la fecha vieja seguía colgada del form: el próximo submit la grababa"
    )


def test_una_fecha_futura_tampoco_dispara_la_pregunta(cliente_y_queries):
    """La pregunta es por lo VIEJO. Un posdatado no es el caso de este guard."""
    cliente, fake = cliente_y_queries
    manana = HOY + timedelta(days=1)
    _post(cliente, _base(manana))

    assert fake.creado is not None
    assert fake.creado["fecha"] == manana
