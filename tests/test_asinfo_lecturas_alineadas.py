"""Las dos patas del balance vencen JUNTAS (TMT 2026-07-31).

Se midió contra Asinfo: las recepciones de importación NO son parciales. Las 18
del mes entraron al 100 %, con `indicador_tiene_recepcion_parcial = false` y la
fecha_creacion y la fecha_modificacion del documento a 1-3 segundos una de la
otra. Los lotes aparecen en el saldo de bodega en el mismo instante (IM-0000565,
recibida hoy 08:58: 24.494,24 kg facturados = 24.494,24 recibidos = 24.494,24 en
bodega). O sea: en el ERP el anticipo y el stock SÍ se mueven al mismo tiempo.

El desfase lo poníamos nosotros. Cada pata venía de su propia caché de 5 minutos
y cada una vencía por su lado, así que había ventanas de hasta 5 minutos con una
pata al día y la otra no. Una importación son ~24.500 kg × ~3 US$/kg ≈ 74.000 de
utilidad — el tamaño del salto 609 → 688 que vio Alex.
"""
from unittest.mock import patch

import pytest

from modules.asinfo import service as sv


@pytest.fixture(autouse=True)
def _limpio():
    sv._PAR_BALANCE_TS = 0.0
    sv._INVENTARIO_ETAPA_CACHE.clear()
    sv._STOCK_LOTE_TOTALES_CACHE.clear()
    sv._IMPORT_CACHE.clear()
    yield
    sv._PAR_BALANCE_TS = 0.0
    sv._INVENTARIO_ETAPA_CACHE.clear()
    sv._STOCK_LOTE_TOTALES_CACHE.clear()
    sv._IMPORT_CACHE.clear()


def _espias(inv=None, imps=None):
    return (
        patch.object(sv, "inventario_por_etapa",
                     return_value=inv if inv is not None else {"disponible": True}),
        patch.object(sv, "importaciones_asinfo",
                     return_value=imps if imps is not None else []),
    )


def test_refresca_las_dos_de_una_sola_vez():
    p1, p2 = _espias()
    with p1 as m_inv, p2 as m_imp:
        assert sv.alinear_lecturas_del_balance() is True
    assert m_inv.call_count == 1 and m_imp.call_count == 1


def test_tira_las_dos_caches_a_la_vez():
    """Si sólo se invalidara una, el desfase seguiría igual."""
    sv._INVENTARIO_ETAPA_CACHE["all"] = (1.0, {"viejo": True})
    sv._STOCK_LOTE_TOTALES_CACHE["all"] = (1.0, [{"viejo": True}])
    sv._IMPORT_CACHE["l400"] = (1.0, [{"viejo": True}])
    p1, p2 = _espias()
    with p1, p2:
        sv.alinear_lecturas_del_balance()
    assert "all" not in sv._INVENTARIO_ETAPA_CACHE
    assert "all" not in sv._STOCK_LOTE_TOTALES_CACHE
    assert sv._IMPORT_CACHE == {}


def test_no_martilla_metabase_en_cada_request():
    """Sigue siendo UNA vuelta cada 5 minutos, no una por pantalla."""
    p1, p2 = _espias()
    with p1 as m_inv, p2:
        sv.alinear_lecturas_del_balance()
        for _ in range(20):
            assert sv.alinear_lecturas_del_balance() is False
    assert m_inv.call_count == 1


def test_pasados_los_5_minutos_vuelve_a_alinear():
    p1, p2 = _espias()
    with p1 as m_inv, p2:
        sv.alinear_lecturas_del_balance()
        sv._PAR_BALANCE_TS -= sv._PAR_BALANCE_TTL_SECS + 1
        assert sv.alinear_lecturas_del_balance() is True
    assert m_inv.call_count == 2


def test_si_asinfo_contesto_mal_reintenta_a_los_30s():
    """Desalineadas es peor que viejas: no esperar 5 minutos para reintentar."""
    p1, p2 = _espias()
    with p1, p2, \
         patch.object(sv, "_cache_ok", return_value=False):
        sv.alinear_lecturas_del_balance()
    import time as _t
    falta = sv._PAR_BALANCE_TTL_SECS - (_t.time() - sv._PAR_BALANCE_TS)
    assert 0 < falta <= sv._FALLO_TTL_SECS + 1


def test_si_algo_explota_no_tumba_el_balance():
    def explota():
        raise RuntimeError("Metabase caído")

    with patch.object(sv, "inventario_por_etapa", side_effect=explota), \
         patch.object(sv, "importaciones_asinfo", return_value=[]):
        assert sv.alinear_lecturas_del_balance() is True   # no levanta


def test_el_balance_alinea_antes_de_calcular():
    """El enganche: si nadie la llama, la función no sirve de nada."""
    import inspect
    from modules.informes import queries as q
    src = inspect.getsource(q.informe_balance)
    cabeza = src[:src.index("_totf = totf()")]
    assert "alinear_lecturas_del_balance" in cabeza
