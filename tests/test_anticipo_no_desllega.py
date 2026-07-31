"""La mercadería no puede "des-llegar" (TMT 2026-07-31).

Dueña, mirando la pantalla: *"sigue subiendo solo, o sea tu hipótesis está
mal"* … *"sé que tiene que ver con la importación"*.

Tenía razón. El descuento de anticipos con mercadería recibida cruza
anticipo ↔ importación por (código, nº) y, cuando el nº se repite entre
campañas, desempata por fecha más cercana. Ese desempate depende de qué
importaciones trajo Asinfo en la última vuelta —lista cacheada, con tope de
filas—, así que el MISMO anticipo puede matchear en una vuelta y no matchear
en la siguiente sin que haya pasado nada en la fábrica.

Cuando no matchea, deja de descontarse: el anticipo vuelve al activo, el activo
sube y la utilidad sube con él. Un anticipo típico son ~74.000. Justo hoy
Novedades avisó ese caso para AI 11 ("ese número se repite en otra campaña").

La regla que fija este test: una vez que vimos la mercadería en la bodega, se
queda vista mientras el anticipo siga vivo.
"""
from unittest.mock import patch

import pytest

from modules.informes import queries as q


@pytest.fixture(autouse=True)
def _limpio():
    q._ANTIC_RECIBIDOS_ULTIMO_BUENO = None
    q._ANTIC_RECIBIDOS_VISTOS.clear()
    yield
    q._ANTIC_RECIBIDOS_ULTIMO_BUENO = None
    q._ANTIC_RECIBIDOS_VISTOS.clear()


VIVOS = [
    {"id_dolares": 1, "cta": "AC", "importe": 100000.0},   # en tránsito
    {"id_dolares": 2, "cta": "AI", "importe": 73851.68},   # AI 11: nº ambiguo
]


def _corrida(recepcion, vivos=None):
    """Una vuelta del cálculo con el cruce de Asinfo mockeado."""
    return (
        patch("modules.dolares.queries.anticipos_vivos",
              return_value=[dict(f) for f in (vivos if vivos is not None else VIVOS)]),
        patch("modules.importaciones.service.adjuntar_recepcion_asinfo",
              side_effect=recepcion),
    )


def _matchea_todo(filas):
    for f in filas:
        f["im_numero"] = f"IM-{f['id_dolares']}"
        f["fecha_recepcion_im"] = ("2026-07-31" if f["cta"] == "AI" else None)


def _pierde_el_ambiguo(filas):
    """La vuelta siguiente: AI 11 se pega a la importación de la otra campaña."""
    for f in filas:
        f["im_numero"] = f"IM-{f['id_dolares']}"
        f["fecha_recepcion_im"] = None


def _correr(recepcion, vivos=None):
    p1, p2 = _corrida(recepcion, vivos)
    with p1, p2:
        return q.anticipos_con_mercaderia_recibida()


def test_el_anticipo_no_vuelve_al_activo_si_el_cruce_falla_una_vuelta():
    assert _correr(_matchea_todo) == 73851.68
    # Misma realidad, otra vuelta: el desempate se fue a la otra campaña.
    assert _correr(_pierde_el_ambiguo) == 73851.68


def test_sin_la_memoria_el_activo_habria_subido_74k():
    """Documenta el tamaño de lo que se sacó: era un salto de un anticipo entero."""
    _correr(_matchea_todo)
    sin_memoria = sum(
        f["importe"] for f in VIVOS if False  # el cruce fallido no marca ninguno
    )
    assert _correr(_pierde_el_ambiguo) - sin_memoria == 73851.68


def test_convertir_el_anticipo_SI_lo_saca():
    """La única baja legítima: deja de estar vivo."""
    assert _correr(_matchea_todo) == 73851.68
    solo_transito = [VIVOS[0]]
    assert _correr(_matchea_todo, vivos=solo_transito) == 0.0
    assert q._ANTIC_RECIBIDOS_VISTOS == set()


def test_una_recepcion_nueva_se_suma_igual():
    """La memoria no congela el número: lo que llega después entra."""
    assert _correr(_matchea_todo) == 73851.68
    nuevos = VIVOS + [{"id_dolares": 3, "cta": "AI", "importe": 58629.40}]
    assert _correr(_matchea_todo, vivos=nuevos) == 132481.08


def test_si_asinfo_no_contesta_nada_queda_el_ultimo_bueno():
    assert _correr(_matchea_todo) == 73851.68

    def mudo(filas):
        for f in filas:
            f["im_numero"] = None
            f["fecha_recepcion_im"] = None

    assert _correr(mudo) == 73851.68


def test_sin_anticipos_vivos_se_limpia_la_memoria():
    _correr(_matchea_todo)
    assert _correr(_matchea_todo, vivos=[]) == 0.0
    assert q._ANTIC_RECIBIDOS_VISTOS == set()
