"""Un fracaso de Metabase NO se puede cachear como si fuera un dato.

REGRESIÓN (TMT 2026-07-29). `metabase_client.fetch_dataset` devuelve [] tanto
cuando la query no tiene filas como cuando hubo timeout/401/500.
`stock_asinfo_lote_totales` cacheaba ese [] por 5 minutos sin mirar si Metabase
había contestado, así que UN hipo dejaba el stock en cero toda la ventana: el
balance caía a los kg del dBase y la Utilidad Real se mostraba ~495.000 más
baja, SIN NINGÚN AVISO (196.010 en vez de 687.519; se "arregló sola" al vencer
el TTL).

No se saca la caché del fracaso — con Metabase caído, no cachear haría que cada
request dispare la query pesada (20 s de timeout c/u). Se guarda con ventana
CORTA: 30 s en vez de 300 s.
"""
from __future__ import annotations

import time
from unittest.mock import patch

from modules._lib import metabase_client


def _sin_red(monkeypatch):
    """Env de Metabase configurado pero la red falla siempre."""
    for k, v in (("METABASE_URL", "http://metabase.test"),
                 ("METABASE_USERNAME", "u"), ("METABASE_PASSWORD", "p")):
        monkeypatch.setenv(k, v)


def test_fetch_dataset_estado_distingue_caido_de_vacio(monkeypatch):
    """ok=False si Metabase no contestó; ok=True si contestó 0 filas."""
    _sin_red(monkeypatch)

    with patch.object(metabase_client, "_login", return_value="tok"), \
            patch("requests.post", side_effect=TimeoutError("boom")):
        filas, ok = metabase_client.fetch_dataset_estado(2, "select 1")
    assert filas == []
    assert ok is False, "un timeout tiene que reportarse como fracaso"

    class _RespVacia:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"data": {"cols": [{"name": "a"}], "rows": []}}

    with patch.object(metabase_client, "_login", return_value="tok"), \
            patch("requests.post", return_value=_RespVacia()):
        filas, ok = metabase_client.fetch_dataset_estado(2, "select 1")
    assert filas == []
    assert ok is True, "0 filas NO es un fracaso: se cachea normal"


def test_fetch_dataset_estado_no_rompe_mocks_de_fetch_dataset():
    """Los tests existentes mockean `fetch_dataset`. Tienen que seguir andando
    (el mock no marca estado → ok=True → se cachea como dato bueno)."""
    with patch.object(metabase_client, "fetch_dataset", return_value=[{"x": 1}]):
        filas, ok = metabase_client.fetch_dataset_estado(2, "select 1")
    assert filas == [{"x": 1}]
    assert ok is True


def test_cache_put_acorta_la_ventana_del_fracaso():
    """El éxito dura el TTL completo; el fracaso, 30 s."""
    from modules.asinfo import service as svc

    ttl = 300
    cache: dict = {}

    svc._cache_put(cache, "all", ["dato"], True, ttl)
    edad_ok = time.time() - cache["all"][0]
    assert edad_ok < 1, "el éxito se guarda fresco"
    assert (ttl - edad_ok) > 290, "el camino feliz mantiene el TTL de siempre"

    svc._cache_put(cache, "all", [], False, ttl)
    restante = ttl - (time.time() - cache["all"][0])
    assert 25 < restante <= svc._FALLO_TTL_SECS + 1, (
        f"el fracaso tiene que vencer en ~{svc._FALLO_TTL_SECS}s, "
        f"no en {restante:.0f}s"
    )


def test_stock_lote_totales_no_envenena_la_cache_5_minutos(monkeypatch):
    """El caso real: Metabase se cae en la query de bodegas."""
    from modules.asinfo import service as svc

    svc._STOCK_LOTE_TOTALES_CACHE.clear()
    monkeypatch.setattr(svc.metabase_client, "fetch_dataset",
                        lambda *a, **k: [])
    monkeypatch.setattr(svc.metabase_client, "ultimo_fetch_ok",
                        lambda: False)

    assert svc.stock_asinfo_lote_totales() == []
    guardado = svc._STOCK_LOTE_TOTALES_CACHE.get("all")
    assert guardado is not None, "igual se cachea (no martillar Metabase)"
    restante = svc._STOCK_LOTE_TTL_SECS - (time.time() - guardado[0])
    assert restante <= svc._FALLO_TTL_SECS + 1, (
        "el fracaso no puede quedar vigente 5 minutos — eso es lo que dejó la "
        "utilidad ~495k abajo el 29/07"
    )


def test_stock_lote_totales_cachea_normal_cuando_metabase_contesta(monkeypatch):
    """Contramuestra: si Metabase contesta, el TTL es el de siempre."""
    from modules.asinfo import service as svc

    svc._STOCK_LOTE_TOTALES_CACHE.clear()
    filas = [{"id_bodega": 51, "bodega": "HILO", "lotes": 3, "total_kg": 1000.0}]
    monkeypatch.setattr(svc.metabase_client, "fetch_dataset",
                        lambda *a, **k: filas)
    monkeypatch.setattr(svc.metabase_client, "ultimo_fetch_ok", lambda: True)

    out = svc.stock_asinfo_lote_totales()
    assert out and out[0]["total_kg"] == 1000.0
    restante = svc._STOCK_LOTE_TTL_SECS - (
        time.time() - svc._STOCK_LOTE_TOTALES_CACHE["all"][0])
    assert restante > svc._FALLO_TTL_SECS + 10, (
        "el camino feliz no puede haberse acortado — sería más lento"
    )
