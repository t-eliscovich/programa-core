"""El anuncio de hilo esperando orden, en toda pantalla que muestre hilo.

TMT 2026-08-11 (dueña): *"podés en todos lados que mostramos hilo poner un
anuncio, por ejemplo en la pantalla de tejeduría"*.
"""
from unittest.mock import patch

from modules.asinfo import hilo_sin_of as hs

PANTALLAS = (
    "modules/informes/templates/informes/flujo_produccion.html",
    "modules/stock_asinfo/templates/stock_asinfo/fabricacion.html",
    "modules/tejeduria_asinfo/templates/tejeduria_asinfo/tab.html",
    "modules/informes/templates/informes/balance.html",
)


def _caso(**kw):
    base = {"numero": "OSM-000010458", "id_bodega": 51, "material": "hilo",
            "kg": 4860.0, "usuario": "mprima", "descripcion": "A PONCE",
            "creado": "2026-08-11 07:33"}
    base.update(kw)
    return base


def test_el_anuncio_esta_en_las_cuatro_pantallas_que_muestran_hilo():
    for p in PANTALLAS:
        assert 'include "_partials/hilo_esperando_orden.html"' in open(p).read(), p


def test_suma_los_despachos_del_dia():
    with patch.object(hs, "despachos_sin_of",
                      return_value=[_caso(), _caso(numero="OSM-000010462",
                                                   kg=3375.0)]):
        r = hs.resumen_de_hoy()
    assert r == {"kg": 8235.0, "n": 2, "material": "hilo"}


def test_sin_nada_esperando_el_anuncio_no_pinta():
    """Un cartel permanente deja de leerse a la semana."""
    with patch.object(hs, "despachos_sin_of", return_value=[]):
        assert hs.resumen_de_hoy() == {"kg": 0.0, "n": 0, "material": "hilo"}


def test_solo_mira_EL_DIA_no_la_ventana_de_siete():
    """El anuncio es de hoy: arrastrar los de ayer lo vuelve un inventario."""
    visto = {}
    with patch.object(hs, "despachos_sin_of",
                      side_effect=lambda **k: visto.update(k) or []):
        hs.resumen_de_hoy()
    assert visto == {"dias": 0}      # 0 = hoy; 1 arrancaría AYER a las 00:00


def test_con_hilo_y_tela_cruda_no_dice_una_cosa_por_otra():
    with patch.object(hs, "despachos_sin_of",
                      return_value=[_caso(), _caso(numero="OSTC-1", kg=400.0,
                                                   id_bodega=52,
                                                   material="tela cruda")]):
        assert hs.resumen_de_hoy()["material"] == "material"


def test_si_asinfo_se_cae_el_anuncio_no_inventa():
    with patch.object(hs, "despachos_sin_of", side_effect=RuntimeError("metabase")):
        assert hs.resumen_de_hoy() == {"kg": 0.0, "n": 0, "material": "hilo"}
