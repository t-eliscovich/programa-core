"""Los kilos no entran al promedio hasta que llega su plata (TMT 2026-07-31).

Dueña: *"el stock ahora subió 20k, por favor buscá mejor si a la mañana estaba
distinto"*. Sí estaba distinto, y el registro de Novedades del 31/07 lo muestra:

    08:58  Asinfo recibe IM-0000565 → 24.494 kg entran a la bodega
    09:06  la conversión crea la compra → US$ 58.629          (8 min después)
    09:09  Asinfo recibe IM-0000563 → 22.993 kg
    09:36  la compra                                          (27 min después)
    09:16  Asinfo recibe AI 11 → 22.565 kg
    10:34  la compra → US$ 73.852                             (76 min después)

Durante cada hueco esos kilos entraban al promedio ponderado del hilado SIN
dólares —gratis— y el promedio revalúa TODO el stock de hilo. Con ~1,85 M kg en
bodega y ~2,18 M kg de base, la aritmética es brutal:

    · cada US$ de compra que entra   → el stock se mueve ~0,85 US$
    · cada kg que entra sin su US$   → el stock se mueve ~2,55 US$ para ABAJO

O sea escalones de ±60.000 en la utilidad durante la mañana, y el rebote cuando
la compra por fin se carga. Ahora el denominador del promedio son sólo los kg
cuyo importe ya está cargado: la recepción sola no mueve nada y la carga de la
compra mueve apenas lo que esa importación se aparta del promedio.
"""
from unittest.mock import patch

import pytest

from modules.asinfo import service as sv
from modules.importaciones import service as isv


# ── El dato: kg_con_costo ──────────────────────────────────────────────────

def _cruce(filas):
    return patch.object(isv, "importaciones_con_cruce", return_value=filas)


BASE = {"recibida": True, "fecha_recepcion": "2026-07-31", "fecha": "2026-07-15"}


def test_los_kg_sin_compra_cargada_quedan_fuera_del_costo():
    filas = [
        {**BASE, "prov": "AC", "numero": 32, "kg": 24494.0, "importe_programa": 58629.40},
        {**BASE, "prov": "AC", "numero": 17, "kg": 22993.0, "importe_programa": None},
    ]
    with _cruce(filas):
        out = isv.costo_hilado_recibido_mes(2026, 7)
    assert out["kg"] == 47487.0            # los kilos físicos siguen siendo todos
    assert out["kg_con_costo"] == 24494.0  # pero al promedio entran sólo éstos
    assert out["us"] == 58629.40
    assert out["usd_kg"] == round(58629.40 / 24494.0, 4)


def test_las_partidas_1_y_2_comparten_el_costo_y_cuentan_los_dos_kg():
    """AI 11 viene partida en dos: el importe está en una sola de las filas."""
    filas = [
        {**BASE, "prov": "AI", "numero": 11, "kg": 11282.39, "importe_programa": 73851.68},
        {**BASE, "prov": "AI", "numero": 11, "kg": 11282.39, "importe_programa": None},
    ]
    with _cruce(filas):
        out = isv.costo_hilado_recibido_mes(2026, 7)
    assert out["us"] == 73851.68             # una sola vez
    assert out["kg_con_costo"] == 22564.78   # los DOS medios kilos
    assert out["kg"] == 22564.78


def test_sin_ninguna_compra_cargada_no_hay_costo_ni_kg():
    filas = [{**BASE, "prov": "AC", "numero": 32, "kg": 24494.0,
              "importe_programa": None}]
    with _cruce(filas):
        out = isv.costo_hilado_recibido_mes(2026, 7)
    assert out["us"] == 0.0 and out["kg_con_costo"] == 0.0
    assert out["kg"] == 24494.0


# ── El efecto: la recepción sola ya no mueve la tarifa ─────────────────────

HI0, OPEN = 1_775_736.0, 2.99
INV_INIC = {"hilo": HI0}
INV_ACT = {"hilo": 1_847_840.0, "en_proceso_tc": 51_959.0}


def _valuacion(kg_con_costo, us, kg_total=None):
    rec = {"us": us, "kg": (kg_total if kg_total is not None else kg_con_costo),
           "kg_con_costo": kg_con_costo, "usd_kg": None}
    return (
        patch.object(sv, "inventario_por_etapa_a_fecha", return_value=INV_INIC),
        patch.object(sv, "inventario_por_etapa", return_value=INV_ACT),
        patch.object(sv, "hilado_recibido_mes",
                     return_value=(kg_total if kg_total is not None else kg_con_costo)),
        patch.object(isv, "costo_hilado_recibido_mes", return_value=rec),
        patch("modules.compras_locales.service.hilado_local_recibido_mes",
              return_value={"kg": 0.0, "us": 0.0}),
    )


def _stock_us(kg_con_costo, us, kg_total=None):
    ps = _valuacion(kg_con_costo, us, kg_total)
    with ps[0], ps[1], ps[2], ps[3], ps[4]:
        return sv.mov_hilado_valuacion(2026, 7, OPEN)


def test_la_recepcion_SOLA_ya_no_mueve_el_stock():
    """El escalón de las 09:16: 22.565 kg entran y su plata todavía no."""
    antes = _stock_us(380_000.0, 1_150_000.0, kg_total=380_000.0)
    # llega la recepción: +22.565 kg físicos, mismos dólares
    despues = _stock_us(380_000.0, 1_150_000.0, kg_total=402_565.0)
    assert despues["stock_act_us"] == antes["stock_act_us"]
    assert despues["kg_sin_costo"] == pytest.approx(22_565.0)


def test_cargar_la_compra_mueve_un_pasito_no_un_escalon():
    """Antes: +62.000 de un saque. Ahora: sólo el desvío contra el promedio."""
    antes = _stock_us(380_000.0, 1_150_000.0, kg_total=402_565.0)
    # 10:34 — entra la compra de AI 11: sus kg Y sus dólares, juntos.
    despues = _stock_us(402_565.0, 1_150_000.0 + 73_851.68, kg_total=402_565.0)
    salto = despues["stock_act_us"] - antes["stock_act_us"]
    assert abs(salto) < 15_000, f"salto de {salto:,.0f} — sigue habiendo escalón"


def test_el_viejo_comportamiento_era_un_escalon_de_decenas_de_miles():
    """Guard de regresión: documenta el tamaño de lo que se sacó.

    Simula el cálculo VIEJO (denominador = kg físicos) para el mismo evento.
    """
    def viejo(kg_fisicos, us):
        avg = (HI0 * OPEN + us) / (HI0 + kg_fisicos)
        return INV_ACT["hilo"] * avg + INV_ACT["en_proceso_tc"] * OPEN

    antes = viejo(402_565.0, 1_150_000.0)                 # kg ya adentro, sin plata
    despues = viejo(402_565.0, 1_150_000.0 + 73_851.68)   # llega la plata
    assert despues - antes > 50_000


def test_si_no_hay_nada_cargado_no_diluye():
    """Kilos sí, plata no: la tarifa se queda en la de apertura."""
    out = _stock_us(0.0, 0.0, kg_total=404_657.0)
    assert out["avg_ukg"] == OPEN
    assert out["kg_sin_costo"] == pytest.approx(404_657.0)


def test_todo_cargado_se_comporta_igual_que_antes():
    """El camino feliz no cambia: mismo promedio ponderado de siempre."""
    out = _stock_us(404_657.0, 1_224_937.0)
    esperado = (HI0 * OPEN + 1_224_937.0) / (HI0 + 404_657.0)
    assert out["avg_ukg"] == pytest.approx(esperado)
    assert out["kg_sin_costo"] == 0.0
