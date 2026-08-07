"""La APERTURA del $/kg del hilado se lee GRABADA — nunca se recalcula.

Dueña, 2026-08-07: *"el inicial no se puede recalcular, está claro eso?"*.

El bug: `views.py` tomaba `stock_act_ukg` (una tarifa de CIERRE, que ya tiene
las compras del mes adentro) y se la pasaba a `mov_hilado_valuacion` como si
fuera la apertura. Esa función vuelve a diluirle las compras del mes encima, así
que los mismos dólares se contaban DOS veces:

    apertura real (cierre de julio)                       3,0201
      ↓ diluye los $504.819 de compras de agosto          3,0391   ← correcto
      ↓ y se las vuelve a diluir                          3,0567
      ↓ y otra vez                                        3,0717   ← lo que se mostraba

Sobre 2.596.340 kg —el $/kg del hilado arrastra tejido (+0,5) y terminado
(+2,2)— eso son ~$85.278 de stock y de utilidad que nadie ganó.
"""
from __future__ import annotations

from unittest.mock import patch

from modules.informes import queries
from modules.informes.views import _build_mov_asinfo

# Números REALES de agosto 2026, medidos en producción el 07/08.
APERTURA_JULIO = 3.0200921    # iniciales.um de julio — el valor GRABADO
CIERRE_CON_COMPRAS = 3.0557   # stock_act_ukg: ya tiene las compras del mes
HI0 = 1868693.0               # hilo en bodega al 01/08 (Asinfo)
COMPRAS_KG = 154400.0
COMPRAS_US = 504819.71


def _data(stock_act_ukg=CIERRE_CON_COMPRAS, stock_inic_ukg=APERTURA_JULIO):
    return {"header": {
        "hilado": {"stock_act_ukg": stock_act_ukg, "stock_inic_ukg": stock_inic_ukg},
        "tejido": {}, "terminado": {}, "colorantes": {},
    }}


def _inv(hilo=HI0):
    return {"disponible": True, "hilo": hilo, "tela_cruda": 50000.0,
            "terminada": 333315.0, "en_proceso_tc": 0.0, "en_proceso_pt": 0.0}


def _entorno(apertura):
    """Todo Asinfo mockeado; lo único que varía es la apertura."""
    return [
        patch.object(queries, "apertura_ukg_hilado", return_value=apertura),
        patch("modules.asinfo.service.hilado_recibido_mes", return_value=COMPRAS_KG),
        patch("modules.asinfo.service.fabricacion_flujo_mes",
              return_value={"issued": 0.0, "fab": 0.0}),
        patch("modules.asinfo.service.ventas_facturado_kg", return_value=0.0),
        patch("modules.importaciones.service.promedio_hilado_usd_kg", return_value=2.5),
        patch("modules.importaciones.service.costo_hilado_recibido_mes",
              return_value={"us": COMPRAS_US, "kg": COMPRAS_KG,
                            "kg_con_costo": COMPRAS_KG, "usd_kg": COMPRAS_US / COMPRAS_KG}),
    ]


def _correr(apertura, data=None):
    ctx = _entorno(apertura)
    for c in ctx:
        c.start()
    try:
        return _build_mov_asinfo(data or _data(), _inv(), _inv(), anio=2026, mes=8)
    finally:
        for c in reversed(ctx):
            c.stop()


# ── El invariante ─────────────────────────────────────────────────────────

def test_la_apertura_es_la_grabada_y_no_el_cierre():
    """Aunque `stock_act_ukg` traiga 3,0557, la apertura tiene que ser 3,0201."""
    hl = _correr(APERTURA_JULIO)["hilado"]
    assert round(hl["stock_inic_ukg"], 4) == round(APERTURA_JULIO, 4)
    assert round(hl["stock_inic_ukg"], 4) != round(CIERRE_CON_COMPRAS, 4)


def test_la_apertura_no_se_mueve_cuando_entra_una_compra():
    """El corazón del asunto: la apertura del mes es la misma con o sin compras.

    Si alguien vuelve a alimentarla con una tarifa que contiene las compras, este
    test se pone rojo.
    """
    sin_compras = patch("modules.importaciones.service.costo_hilado_recibido_mes",
                        return_value={"us": 0.0, "kg": 0.0, "kg_con_costo": 0.0,
                                      "usd_kg": 0.0})
    con = _correr(APERTURA_JULIO)["hilado"]["stock_inic_ukg"]
    with sin_compras:
        sin = _correr(APERTURA_JULIO)["hilado"]["stock_inic_ukg"]
    assert round(con, 6) == round(sin, 6) == round(APERTURA_JULIO, 6)


def test_el_fallback_nunca_es_una_tarifa_de_cierre():
    """Sin mes anterior grabado cae a `stock_inic_ukg`, jamás a `stock_act_ukg`."""
    hl = _correr(0.0)["hilado"]
    assert round(hl["stock_inic_ukg"], 4) == round(APERTURA_JULIO, 4)


def test_la_doble_dilucion_daria_85k_de_mas():
    """Aritmética del bug, con los números reales de agosto 2026.

    Deja registrado cuánto costaba, para que si alguien 'simplifica' esto se vea
    el tamaño de lo que se está tocando.
    """
    una = (HI0 * APERTURA_JULIO + COMPRAS_US) / (HI0 + COMPRAS_KG)
    dos = (HI0 * una + COMPRAS_US) / (HI0 + COMPRAS_KG)
    assert round(una, 4) == 3.0391
    assert round(dos, 4) == 3.0567
    kg_totales = 1978859.0 + 299043.0 + 318438.0   # hilado + tejido + terminado
    assert round((dos - una) * kg_totales, 0) == 45659.0


def test_apertura_ukg_hilado_lee_el_mes_anterior():
    with patch.object(queries, "tarifa_iniciales_mes_anterior",
                      return_value=APERTURA_JULIO) as t:
        assert queries.apertura_ukg_hilado(8, 2026) == APERTURA_JULIO
    t.assert_called_once_with(8, 2026, "um")
