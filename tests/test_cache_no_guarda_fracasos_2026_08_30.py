"""Un fracaso de la fuente NO se cachea como si fuera dato (TMT 2026-08-30).

El mismo gotcha tres veces: `fetch_dataset` pelado devuelve [] tanto para
"Metabase caído" como para "0 filas", y el [] quedaba cacheado el TTL completo.
Hoy a la mañana la variante "para siempre" (`_descubrir_detalle_fp`) tuvo al
balance gritando "22 compras SIN kg" con los kg sanos en Asinfo. Estos son los
primos del mismo audit: el fracaso se cachea con la ventana corta de 30 s
(`_cache_put`), nunca con los 5 minutos del dato bueno.
"""
from __future__ import annotations

import os
import sys
from unittest.mock import patch

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from modules.asinfo import service as svc  # noqa: E402


def test_ingresos_fabricacion_caido_no_dice_disponible(monkeypatch):
    """Metabase caído ≠ "no hubo IFT": antes se cacheaba disponible=True con
    total 0 (los tercerizados desaparecían y el barrido no creaba el pasivo
    del maquilero)."""
    svc._PROD_TEJ_CACHE.clear()
    with patch.object(svc.metabase_client, "fetch_dataset_estado",
                      return_value=([], False)):
        out = svc.ingresos_fabricacion_mes(2026, 8)
    assert out["disponible"] is False
    # Y lo cacheado es el fracaso con ventana corta, no el dato del TTL entero.
    assert not svc._cache_ok(svc._PROD_TEJ_CACHE, ("ift", 2026, 8, 52))


def test_ingresos_fabricacion_cero_filas_si_es_dato(monkeypatch):
    """Asinfo contestó y no hubo ingresos: eso SÍ es disponible=True."""
    svc._PROD_TEJ_CACHE.clear()
    with patch.object(svc.metabase_client, "fetch_dataset_estado",
                      return_value=([], True)):
        out = svc.ingresos_fabricacion_mes(2026, 8)
    assert out["disponible"] is True and out["total_kg"] == 0.0
    assert svc._cache_ok(svc._PROD_TEJ_CACHE, ("ift", 2026, 8, 52))


def test_compras_locales_caido_no_cachea_el_vacio_como_bueno(monkeypatch):
    """El [] de un timeout hacía desaparecer las locales 5 min: el promedio
    ponderado del hilado perdía kg y US$ juntos y ningún guard saltaba."""
    svc._LOCALES_CACHE.clear()
    with patch.object(svc.metabase_client, "fetch_dataset_estado",
                      return_value=([], False)):
        assert svc.compras_locales_asinfo() == []
    clave = next(iter(svc._LOCALES_CACHE))
    assert not svc._cache_ok(svc._LOCALES_CACHE, clave)


def test_sucursal_por_direccion_fallo_reintenta_a_los_30s(monkeypatch):
    """El sello del TTL iba afuera del try: un hipo de la DB dejaba 5 min de
    mapa vacío y las facturas se grababan con el código de la matriz (AJO en
    vez de AJ2). El fracaso reintenta a los 30 s y conserva el último bueno."""
    import time as _t

    from modules.asinfo import aliases as al

    al._suc_cache = {}
    al._suc_cache_ts = 0.0
    llamadas = []

    def _falla(_sql):
        llamadas.append(1)
        raise RuntimeError("db caida")

    with patch.object(al.db, "fetch_all", side_effect=_falla):
        assert al.codigo_por_direccion(7) is None
    # El TTL quedó envejecido: vence a los ~30 s, no a los 300.
    assert _t.time() - al._suc_cache_ts >= al._CACHE_TTL_SECS - 31

    # Y cuando la DB vuelve, el mapa se llena en el próximo intento (forzamos
    # el vencimiento moviendo el sello, no el reloj).
    al._suc_cache_ts -= 31
    with patch.object(al.db, "fetch_all",
                      return_value=[{"id_direccion": 7, "codigo_cli": "AJ2"}]):
        assert al.codigo_por_direccion(7) == "AJ2"
