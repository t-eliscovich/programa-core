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
"""
from __future__ import annotations

import os
import sys
from datetime import date, timedelta

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

    class ActivaRequerida(Exception):
        def __init__(self, cta=""):
            self.cta = cta

    def __init__(self):
        self.creado = None

    def lista_bancos(self):
        return [{"no_banco": 10, "nombre": "PICHINCHA"}]

    def proveedores_activos(self, limite=500):
        return []

    def proveedores_op_saldos(self, limite=500):
        return []

    def crear_movimiento_simple(self, **kw):
        self.creado = kw
        return {"id_transaccion": 1, "no_banco": kw["no_banco"],
                "importe": kw["importe"], "saldo_nuevo": 0.0}


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
    return cliente.post("/bancos/nuevo-movimiento", data=MultiDict(campos),
                        follow_redirects=False)


def _base(fecha):
    return [("documento", "ND"), ("no_banco", "10"), ("importe", "64,73"),
            ("fecha", fecha.isoformat()), ("concepto", "COMISIONES E IMPUESTOS")]


def test_con_fecha_vieja_no_graba_nada_y_pregunta(cliente_y_queries):
    """⭐ LA REGRESIÓN: entre el enter y el asiento hay una pregunta."""
    cliente, fake = cliente_y_queries
    r = _post(cliente, _base(ANTEAYER))

    assert fake.creado is None, (
        "grabó el movimiento con fecha vieja sin preguntar — es exactamente "
        "lo que dejó los $7.404,88 archivados en el día equivocado"
    )
    assert r.status_code == 200, "tiene que re-renderizar el form, no redirigir"
    html = r.get_data(as_text=True)
    assert "FECHA VIEJA" in html
    assert ANTEAYER.strftime("%d/%m/%Y") in html, "la pregunta dice QUÉ fecha"


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

    El `<input type="date" name="fecha">` está ARRIBA en el form, así que un
    botón llamado `fecha` no pisaría nada y el bug se vería como "aprieto y no
    hace nada". Misma trampa que desactivaba usuarios el 05/08.
    """
    from pathlib import Path

    tpl = (Path(__file__).resolve().parent.parent / "modules" / "bancos"
           / "templates" / "bancos" / "nuevo_movimiento.html").read_text(
        encoding="utf-8")
    bloque = tpl.split("{% if pregunta_fecha %}")[1].split("{% endif %}")[0]
    assert 'name="usar_hoy"' in bloque
    assert 'name="fecha"' not in bloque


def test_una_fecha_futura_tampoco_dispara_la_pregunta(cliente_y_queries):
    """La pregunta es por lo VIEJO. Un posdatado no es el caso de este guard."""
    cliente, fake = cliente_y_queries
    manana = HOY + timedelta(days=1)
    _post(cliente, _base(manana))

    assert fake.creado is not None
    assert fake.creado["fecha"] == manana


def test_hoy_es_hoy_en_ecuador_no_en_utc():
    """El contenedor está en UTC y Ecuador en UTC−5.

    Con `date.today()` acá, entre las 19:00 EC y la medianoche el guard vería
    "mañana" y saltaría la pregunta con TODO lo que se cargue esa tarde.
    """
    import inspect

    src = inspect.getsource(bancos_views.nuevo_movimiento)
    assert "fecha < today_ec()" in " ".join(src.split())
    assert "date.today()" not in src
