"""El descuento de anticipos usa el MISMO corte que la conversión (TMT 2026-07-31).

Dueña, mirando el KPI que le ofrecí: *"pero esos 17 no deberían estar
directamente. ¿por qué me ofrecés esto?"*. Tenía razón — no faltaba un rótulo,
sobraba un descuento.

El descuento existe para tapar UNA ventana: entre que Asinfo marca la recepción
(entran los kilos al stock) y que la conversión crea la compra (sale el anticipo
del activo). Ahí, y sólo ahí, la mercadería está contada dos veces.

La conversión automática tiene `fecha_corte` —se sella el día que se prende— y
no toca ninguna recepción anterior. El descuento no lo tenía, así que se comía
anticipos viejos: mercadería recibida hace meses, cuyos kilos ya se consumieron
y no están en ningún stock. Los restaba del activo contra nada.

El 31/07 eran 17.900, y eran justo la diferencia entre lo que decía /dolares
(2.156.425) y lo que decía el balance (2.138.525).
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


CORTE = "2026-07-29"

VIVOS = [
    # Llegó HOY: la conversión todavía no corrió → sí hay doble conteo.
    {"id_dolares": 1, "cta": "AC", "importe": 58629.40, "rec": "2026-07-31"},
    # Llegó en MAYO: esos kilos ya se tejieron. La conversión no la mira y el
    # descuento tampoco tiene por qué.
    {"id_dolares": 2, "cta": "AI", "importe": 17900.00, "rec": "2026-05-14"},
    # En tránsito: sigue siendo un activo puro.
    {"id_dolares": 3, "cta": "MH", "importe": 100000.0, "rec": None},
]


def _correr(vivos=None, corte=CORTE):
    def recepcion(filas):
        for f in filas:
            f["im_numero"] = f"IM-{f['id_dolares']}"
            f["fecha_recepcion_im"] = f.get("rec")

    with patch("modules.dolares.queries.anticipos_vivos",
               return_value=[dict(f) for f in (vivos if vivos is not None else VIVOS)]), \
         patch("modules.importaciones.service.adjuntar_recepcion_asinfo",
               side_effect=recepcion), \
         patch("modules.importaciones.autobap.config",
               return_value={"fecha_corte": corte}):
        return q.anticipos_con_mercaderia_recibida()


def test_solo_se_descuenta_lo_recibido_desde_el_corte():
    assert _correr() == 58629.40          # y NO 58629.40 + 17900


def test_el_anticipo_viejo_ya_no_se_come_17900():
    """El caso exacto del 31/07: /dolares y el balance tienen que cerrar."""
    total_vivo = 2_156_425.0
    with patch.object(q.db, "fetch_one", return_value={"total": total_vivo}):
        def recepcion(filas):
            for f in filas:
                f["im_numero"] = f"IM-{f['id_dolares']}"
                f["fecha_recepcion_im"] = f.get("rec")

        # Sólo el viejo: nada llegó dentro de la ventana.
        with patch("modules.dolares.queries.anticipos_vivos",
                   return_value=[dict(VIVOS[1]), dict(VIVOS[2])]), \
             patch("modules.importaciones.service.adjuntar_recepcion_asinfo",
                   side_effect=recepcion), \
             patch("modules.importaciones.autobap.config",
                   return_value={"fecha_corte": CORTE}):
            assert q.anticipos() == total_vivo   # el balance = la pantalla


def test_lo_de_hoy_si_se_sigue_descontando():
    """No romper lo que el descuento vino a arreglar."""
    assert _correr(vivos=[VIVOS[0]]) == 58629.40


def test_sin_corte_configurado_se_comporta_como_antes():
    assert _correr(corte=None) == 58629.40 + 17900.00


def test_el_de_transito_nunca_entra():
    assert _correr(vivos=[VIVOS[2]], corte=None) == 0.0


def test_la_memoria_de_ya_llego_respeta_el_corte():
    """Un viejo no puede colarse por la puerta de atrás de la memoria."""
    _correr()
    assert q._ANTIC_RECIBIDOS_VISTOS == {1}


def test_si_no_se_puede_leer_la_config_no_rompe():
    def recepcion(filas):
        for f in filas:
            f["im_numero"] = f"IM-{f['id_dolares']}"
            f["fecha_recepcion_im"] = f.get("rec")

    with patch("modules.dolares.queries.anticipos_vivos",
               return_value=[dict(f) for f in VIVOS]), \
         patch("modules.importaciones.service.adjuntar_recepcion_asinfo",
               side_effect=recepcion), \
         patch("modules.importaciones.autobap.config",
               side_effect=RuntimeError("sin tabla")):
        assert q.anticipos_con_mercaderia_recibida() == 58629.40 + 17900.00
