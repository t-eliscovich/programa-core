"""Nada externo puede mover la utilidad sin que se sepa (TMT 2026-07-31).

Después de los saltos que reportó Alex (576 → 545 → 609 → 688 en diez minutos)
se revisaron TODAS las piezas del balance que dependen de una fuente externa.
La regla que fijan estos tests es una sola:

    si una fuente externa falla, el número NO cambia — se usa el último bueno
    y se avisa. Sólo se cae a otra base cuando no hay ningún valor bueno.

Un número quieto y de hace tres minutos es mucho mejor que uno fresco y
equivocado, porque el equivocado no se distingue del bueno.
"""
from unittest.mock import patch

import pytest

from modules.asinfo import service as svc


@pytest.fixture(autouse=True)
def _limpio():
    svc._OK_POR_CACHE.clear()
    svc._INVENTARIO_ASOF_CACHE.clear()
    yield
    svc._OK_POR_CACHE.clear()
    svc._INVENTARIO_ASOF_CACHE.clear()


# ── La tarifa $/kg del hilado: kilos sin dólares ────────────────────────────
# Los KILOS comprados salen de Asinfo y los DÓLARES de nuestra base, cada uno
# con su propio `except → 0`. Si llegan los kilos y no los dólares, el promedio
# ponderado calcula como si ese hilo hubiese entrado GRATIS — y esa tarifa
# revalúa los ~800.000 kg de stock.

def _valuacion(*, compras_kg, compras_us, hi0=300000.0, hi1=300000.0, open_ukg=2.95):
    inv = {"disponible": True, "hilo": hi1, "en_proceso_tc": 0.0,
           "tela_cruda": 0.0, "terminada": 0.0, "en_proceso_pt": 0.0}
    inv0 = {"disponible": True, "hilo": hi0}
    with patch.object(svc, "inventario_por_etapa_a_fecha", return_value=inv0), \
         patch.object(svc, "inventario_por_etapa", return_value=inv), \
         patch.object(svc, "hilado_recibido_mes", return_value=compras_kg), \
         patch("modules.importaciones.service.costo_hilado_recibido_mes",
               # TMT 2026-07-31: el denominador del promedio pasó a ser
               # `kg_con_costo` — los kilos cuyo importe YA está cargado. Estos
               # escenarios son "llegaron los kilos y los dólares no", así que
               # esos kilos no tienen costo asociado todavía.
               return_value={"us": compras_us, "kg": compras_kg,
                             "kg_con_costo": (compras_kg if compras_us else 0.0)}), \
         patch("modules.compras_locales.service.hilado_local_recibido_mes",
               return_value={"kg": 0.0, "us": 0.0}):
        return svc.mov_hilado_valuacion(2026, 7, open_ukg)


def test_kilos_sin_dolares_no_desploma_la_tarifa():
    """100.000 kg 'gratis' sobre 300.000 de apertura bajaban la tarifa 25 %.

    TMT 2026-07-31: ya no hace falta el guard de asimetría para este caso — los
    kilos sin importe cargado directamente NO entran al denominador, así que no
    hay nada que diluir. Lo que se exige sigue siendo lo mismo: la tarifa no se
    mueve. Y ahora además queda dicho cuántos kilos están esperando su plata.
    """
    r = _valuacion(compras_kg=100000.0, compras_us=0.0)
    assert r["avg_ukg"] == 2.95              # la de apertura, sin diluir
    assert round(r["stock_act_ukg"], 4) == 2.95
    assert r["kg_sin_costo"] == 100000.0


def test_dolares_sin_kilos_tampoco_la_dispara():
    r = _valuacion(compras_kg=0.0, compras_us=250000.0)
    assert r["asimetrico"] is True
    assert r["avg_ukg"] == 2.95


def test_con_los_dos_lados_la_tarifa_se_diluye_normal():
    """El camino feliz NO cambia: compras reales sí mueven el promedio."""
    r = _valuacion(compras_kg=100000.0, compras_us=310000.0)
    assert r["asimetrico"] is False
    esperado = (300000.0 * 2.95 + 310000.0) / (300000.0 + 100000.0)
    assert round(r["avg_ukg"], 6) == round(esperado, 6)


def test_sin_compras_del_mes_queda_la_apertura():
    r = _valuacion(compras_kg=0.0, compras_us=0.0)
    assert r["asimetrico"] is False
    assert r["avg_ukg"] == 2.95


# ── El inventario de APERTURA (hi0) tampoco se cachea vacío 5 minutos ───────

def test_el_inventario_de_apertura_distingue_vacio_de_no_contestaron():
    from modules._lib import metabase_client

    with patch.object(svc, "disponible", return_value=True), \
         patch.object(metabase_client, "fetch_dataset_estado",
                      return_value=([], False)):
        out = svc.inventario_por_etapa_a_fecha("2026-07-01")
    assert out["disponible"] is False
    # Guardado como FRACASO → vence a los 30 s, no a los 5 minutos.
    import time as _t
    ts, _v = svc._INVENTARIO_ASOF_CACHE["2026-07-01"]
    assert svc._INVENTARIO_ASOF_TTL_SECS - (_t.time() - ts) <= svc._FALLO_TTL_SECS + 2


def test_el_inventario_de_apertura_bueno_se_cachea_normal():
    from modules._lib import metabase_client

    filas = [{"id_bodega": 51, "total_kg": 300000.0}]
    with patch.object(svc, "disponible", return_value=True), \
         patch.object(metabase_client, "fetch_dataset_estado",
                      return_value=(filas, True)):
        out = svc.inventario_por_etapa_a_fecha("2026-07-02")
    assert out["disponible"] is True and out["hilo"] == 300000.0
    import time as _t
    ts, _v = svc._INVENTARIO_ASOF_CACHE["2026-07-02"]
    assert (_t.time() - ts) < 2


# ── El stock de QUÍMICOS: la cadena de tres escalones tiene red ─────────────

def test_el_stock_de_quimicos_usa_el_ultimo_bueno_si_formulas_no_contesta():
    """Sin esto, un timeout bajaba el Stock Quí. ~70k de una recarga a la otra."""
    import inspect

    from modules.informes import queries as q

    src = inspect.getsource(q.informe_balance)
    assert "_VQX_ULTIMO_BUENO" in src
    assert "no contestó el stock de químicos" in src
    # …y el aviso viaja con las advertencias, que la pantalla muestra.
    assert "_quim_aviso" in src


def test_las_advertencias_del_balance_llegan_a_la_pantalla():
    from pathlib import Path

    html = Path("modules/informes/templates/informes/balance.html").read_text()
    assert "startswith('⚠ ASINFO')" in html
