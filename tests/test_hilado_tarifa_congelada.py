"""El $/kg del hilado no se recalcula cuando la fuente no contestó.

TMT 2026-08-12. La dueña abrió Resultados y vio la utilidad en **78.149**.
La traza del día:

    12/08 08:24   utilidad 142.485   $/kg 3,0443   compras 243.546 kg / 787.554 US$
    12/08 08:30   utilidad  78.149   $/kg 3,0201   compras       0 kg /       0 US$
                  └ cambió el $/kg de 3 etapas   −64.393

Asinfo dejó de contestar y las compras del mes se fueron a cero, porque la
lista de importaciones que las cruza vive allá. Sin compras el promedio
ponderado no se diluye y el $/kg vuelve al de apertura (3,0201): −0,0242 sobre
2.660.021 kg de stock = −64.393 de utilidad que nadie perdió.

Los dos guardias que teníamos miraban para otro lado:

· el de asimetría (31/07) busca "kg sin dólares" y "dólares sin kg"; acá
  vinieron LOS DOS en cero, que desde adentro es idéntico a un mes sin compras;
· `/admin/health/hilado-ukg` compara el $/kg mostrado contra el esperado CON
  esas mismas compras en cero — o sea, valida la cuenta y no los insumos.
  Dijo `ok: true` toda la mañana.

Regla de la dueña: *"no deberías bajarlo tanto, sólo a los 5 minutos
anteriores"* → el $/kg se queda donde estaba hasta que Asinfo vuelva.
"""
from unittest.mock import patch

import pytest

from modules.asinfo import service as sv
from modules.importaciones import service as isv

HI0, OPEN = 1_775_736.0, 3.0201
INV_INIC = {"hilo": HI0}
INV_ACT = {"hilo": 2_015_927.0, "en_proceso_tc": 45_880.0}
TARIFA_BUENA = 3.0443


@pytest.fixture(autouse=True)
def _sin_memoria():
    """Cada test arranca sin la tarifa del anterior."""
    sv._ULTIMA_TARIFA_HILADO = None
    yield
    sv._ULTIMA_TARIFA_HILADO = None


def _valuar(kg, us, dolares_propios=0.0, previa=0.0):
    rec = {"us": us, "kg": kg, "kg_con_costo": kg, "usd_kg": None}
    with patch.object(sv, "inventario_por_etapa_a_fecha", return_value=INV_INIC), \
         patch.object(sv, "inventario_por_etapa", return_value=INV_ACT), \
         patch.object(sv, "hilado_recibido_mes", return_value=kg), \
         patch.object(isv, "costo_hilado_recibido_mes", return_value=rec), \
         patch("modules.compras_locales.service.hilado_local_recibido_mes",
               return_value={"kg": 0.0, "us": 0.0}), \
         patch.object(sv, "dolares_hilo_del_mes", return_value=dolares_propios), \
         patch.object(sv, "_tarifa_hilado_previa", return_value=previa):
        return sv.mov_hilado_valuacion(2026, 8, OPEN)


# ── El caso del 12/08 ──────────────────────────────────────────────────────

def test_las_compras_en_cero_con_hilo_comprado_no_bajan_el_kg():
    out = _valuar(0.0, 0.0, dolares_propios=813_806.10, previa=TARIFA_BUENA)
    assert out["stock_act_ukg"] == pytest.approx(TARIFA_BUENA)
    assert out["tarifa_congelada"] is True
    assert "no contestó" in out["tarifa_motivo"]


def test_el_stock_no_se_revalua_mientras_asinfo_no_contesta():
    """Los 64.393: la diferencia entre sostener la tarifa y recalcularla."""
    sano = _valuar(243_546.0, 787_554.0)
    caido = _valuar(0.0, 0.0, dolares_propios=813_806.10, previa=sano["stock_act_ukg"])
    stock_total_kg = 2_660_021.0
    salto = (caido["stock_act_ukg"] - sano["stock_act_ukg"]) * stock_total_kg
    assert abs(salto) < 1.0, f"el stock se movió {salto:,.0f} sin que pasara nada"


def test_el_comportamiento_viejo_movia_64_mil():
    """Guard de regresión: documenta el tamaño de lo que se sacó."""
    sano = _valuar(243_546.0, 787_554.0)
    viejo = _valuar(0.0, 0.0, dolares_propios=0.0)  # sin testigo → como antes
    salto = (viejo["stock_act_ukg"] - sano["stock_act_ukg"]) * 2_660_021.0
    assert salto < -50_000, f"esperaba el escalón viejo, dio {salto:,.0f}"


# ── Lo que NO tiene que cambiar ────────────────────────────────────────────

def test_un_mes_sin_compras_de_verdad_sigue_usando_la_apertura():
    """Sin hilo comprado en el programa, cero compras es cero compras."""
    out = _valuar(0.0, 0.0, dolares_propios=0.0, previa=TARIFA_BUENA)
    assert out["stock_act_ukg"] == pytest.approx(OPEN)
    assert out["tarifa_congelada"] is False
    assert out["tarifa_motivo"] == ""


def test_con_compras_normales_diluye_como_siempre():
    out = _valuar(243_546.0, 787_554.0)
    esperado = (HI0 * OPEN + 787_554.0) / (HI0 + 243_546.0)
    assert out["avg_ukg"] == pytest.approx(esperado)
    assert out["tarifa_motivo"] == ""


def test_la_asimetria_tambien_sostiene_la_tarifa_en_vez_de_caer_a_la_apertura():
    """Mismo bug, la otra puerta: kilos sin dólares caían a la apertura."""
    out = _valuar(243_546.0, 0.0, previa=TARIFA_BUENA)
    assert out["asimetrico"] is True
    assert out["stock_act_ukg"] == pytest.approx(TARIFA_BUENA)
    assert out["tarifa_congelada"] is True


def test_sin_tarifa_anterior_cae_a_la_apertura_pero_lo_dice():
    """Proceso recién arrancado y sin fotos: no hay con qué sostenerla."""
    out = _valuar(0.0, 0.0, dolares_propios=813_806.10, previa=0.0)
    assert out["stock_act_ukg"] == pytest.approx(OPEN)
    assert out["tarifa_congelada"] is False
    assert out["tarifa_motivo"], "tiene que quedar el motivo para el cartel"


# ── La memoria ─────────────────────────────────────────────────────────────

def test_solo_se_recuerda_la_tarifa_calculada_con_las_compras_a_la_vista():
    _valuar(243_546.0, 787_554.0)
    buena = sv._ULTIMA_TARIFA_HILADO
    assert buena and buena["mm"] == 8
    _valuar(0.0, 0.0, dolares_propios=813_806.10, previa=buena["ukg"])
    assert buena == sv._ULTIMA_TARIFA_HILADO, "la foto enferma no puede ser el ancla"
