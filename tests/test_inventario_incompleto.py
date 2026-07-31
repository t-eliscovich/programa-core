"""El inventario de Asinfo INCOMPLETO ya no mueve la utilidad (TMT 2026-07-31).

Alex, 31/07 por la mañana: *"en tu sistema la utilidad estaba en 576 y este
momento está en 545… ahora subió a 609… subió a 688"*, todo en diez minutos.

La causa: `inventario_por_etapa` combina TRES consultas a Metabase (las bodegas
51/52/53 + el material en máquinas de tejeduría + el de tintura). Si se caía UNA
—la de fabricación tarda 21 s contra un timeout de 20— el inventario salía sin
el material en proceso: ~45.000 kg de hilo, unos 135.000 dólares de utilidad. Y
como el número era plausible, el guard viejo (que sólo atajaba "no vino NADA")
lo dejaba pasar **y lo cacheaba cinco minutos**.

Ahora, si falta una fuente se devuelve el ÚLTIMO INVENTARIO COMPLETO: un número
quieto de hace tres minutos es mucho mejor que uno fresco y equivocado.
"""
from unittest.mock import patch

import pytest

from modules.asinfo import service as svc


@pytest.fixture(autouse=True)
def _limpio():
    """Cada test arranca sin cachés ni último-bueno."""
    svc._INVENTARIO_ETAPA_CACHE.clear()
    svc._OK_POR_CACHE.clear()
    svc._ULTIMO_INVENTARIO_BUENO = None
    yield
    svc._INVENTARIO_ETAPA_CACHE.clear()
    svc._OK_POR_CACHE.clear()
    svc._ULTIMO_INVENTARIO_BUENO = None


BODEGAS = [
    {"id_bodega": 51, "bodega": "HILO", "lotes": 10, "total_kg": 300000.0},
    {"id_bodega": 52, "bodega": "CRUDA", "lotes": 10, "total_kg": 200000.0},
    {"id_bodega": 53, "bodega": "TERMINADA", "lotes": 10, "total_kg": 100000.0},
]
WIP52 = {"resumen": {"saldo": 45880.0}}
WIP53 = {"resumen": {"saldo": 12000.0}}


def _correr(*, ok_bodegas=True, ok_52=True, ok_53=True):
    """inventario_por_etapa con las tres fuentes mockeadas y su estado."""
    def _fab(id_bodega):
        svc._OK_POR_CACHE[(id(svc._FABRICACION_CACHE), id_bodega)] = (
            ok_52 if id_bodega == 52 else ok_53)
        return WIP52 if id_bodega == 52 else WIP53

    def _tot():
        svc._OK_POR_CACHE[(id(svc._STOCK_LOTE_TOTALES_CACHE), "all")] = ok_bodegas
        return BODEGAS

    with patch.object(svc, "disponible", return_value=True), \
         patch.object(svc, "stock_asinfo_lote_totales", _tot), \
         patch.object(svc, "fabricacion_proceso", _fab):
        return svc.inventario_por_etapa()


def test_con_las_tres_fuentes_ok_el_inventario_sale_completo():
    inv = _correr()
    assert inv["disponible"] is True
    assert inv["hilo_total"] == 300000.0 + 45880.0     # bodega + en máquinas
    assert inv["cruda_total"] == 200000.0 + 12000.0
    assert not inv.get("de_cache_vieja")


def test_si_falla_el_material_en_maquinas_NO_devuelve_un_inventario_corto():
    """Éste es el bug: 45.880 kg de hilo ≈ 135.000 US$ de utilidad."""
    _correr()                                   # 1ª vuelta buena
    svc._INVENTARIO_ETAPA_CACHE.clear()         # vence el TTL
    inv = _correr(ok_52=False)                  # se cae la de tejeduría
    assert inv["hilo_total"] == 345880.0        # el último bueno, no 300.000
    assert inv["de_cache_vieja"] is True


def test_si_fallan_las_bodegas_tampoco():
    _correr()
    svc._INVENTARIO_ETAPA_CACHE.clear()
    inv = _correr(ok_bodegas=False)
    assert inv["de_cache_vieja"] is True
    assert inv["total"] == 657880.0


def test_sin_ningun_inventario_bueno_previo_se_declara_no_disponible():
    """Recién arrancada la app no hay red: mejor caer al dBase que inventar."""
    inv = _correr(ok_53=False)
    assert inv["disponible"] is False
    assert inv["hilo_total"] == 0.0


def test_el_incompleto_se_cachea_30_segundos_no_5_minutos():
    """El fix del 29/07 daba 30 s al fracaso; acá se lavaba a 300. Ya no."""
    import time as _t

    _correr()
    svc._INVENTARIO_ETAPA_CACHE.clear()
    _correr(ok_52=False)
    ts, _valor = svc._INVENTARIO_ETAPA_CACHE["all"]
    edad = _t.time() - ts
    # Entra ENVEJECIDO: le quedan ~30 s de vida, no 300.
    assert svc._INVENTARIO_ETAPA_TTL_SECS - edad <= svc._FALLO_TTL_SECS + 2


def test_el_bueno_se_cachea_el_TTL_completo():
    import time as _t

    _correr()
    ts, _valor = svc._INVENTARIO_ETAPA_CACHE["all"]
    assert (_t.time() - ts) < 2


def test_cache_ok_ante_la_duda_dice_que_si():
    """Una caché que todavía no pasó por _cache_put no es una alarma."""
    assert svc._cache_ok(svc._STOCK_LOTE_TOTALES_CACHE, "nunca-vista") is True


def test_el_balance_avisa_cuando_valua_con_el_dbase():
    """El fallback mueve ~460.000: tiene que verse en la pantalla."""
    import inspect

    from modules.informes import queries as q

    src = inspect.getsource(q.informe_balance)
    assert "⚠ ASINFO no contestó el inventario" in src
    assert "_stock_aviso" in src
    # …y el template lo deja pasar.
    from pathlib import Path
    html = Path("modules/informes/templates/informes/balance.html").read_text()
    assert "startswith('⚠ ASINFO')" in html
