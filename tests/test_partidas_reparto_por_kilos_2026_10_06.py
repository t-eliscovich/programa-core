"""Una importación partida entra con SU parte de la plata, partida por partida.

Tamara 06/10/2026, MH 71-72 (IM-0000646/647/648, 71.880 kg, anticipos
153.499,23): el cruce cuelga los anticipos de UNA partida (la de fecha más
cercana). A las 08:25 llegó otra, de 24.300 kg: entró "sin costo", al $/kg
promedio, y la utilidad subió +77 mil hasta las 08:31, cuando llegaron las
otras dos con la plata. *"Fijate por qué no se cruzó, tenemos que arreglar."*

Ahora:
  · el stock: cada partida que llega trae plata × kg / kg total
    (24.300 kg → 51.892,59, a 2,1355 el kilo);
  · el anticipo: se descuenta esa MISMA parte, así netean al centavo;
  · la conversión automática espera a que lleguen todas.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from modules.importaciones import service as isvc
from modules.informes import queries as q

KG = {"IM-0000646": 24300.0, "IM-0000647": 23790.0, "IM-0000648": 23790.0}
PLATA = 153499.23


def _imps(llegaron: set[str], plata_en="IM-0000648"):
    filas = []
    for i, im in enumerate(sorted(KG), start=1):
        llego = im in llegaron
        filas.append({
            "im_numero": im, "fecha": "2026-08-27", "prov_cod_asinfo": "EXT0059",
            "nota": f"INV HY5464-26 ( MH 71-72 ) ---{i}",
            "recibida": llego, "fecha_recepcion": "2026-10-06" if llego else None,
            "kg": KG[im], "prov": "MH", "numero": 71, "numero_hasta": 72,
            "codigo": "MH 71-72",
            "fuente": "anticipo" if im == plata_en else None,
            "importe_programa": PLATA if im == plata_en else None,
        })
    isvc.adjuntar_grupo_partidas(filas)
    return filas


def test_las_hermanas_se_reconocen_aunque_no_hayan_llegado_todas():
    filas = _imps({"IM-0000646"})
    # El grupo "de siempre" se descarta (recibidas en meses distintos: una
    # sí y dos no), pero las hermanas quedan anotadas.
    assert filas[0]["partida_ims"] == sorted(KG)
    assert filas[0]["partida_kg"] == 71880.0


def test_la_primera_partida_trae_su_parte_de_la_plata():
    rows = _imps({"IM-0000646"})
    with patch.object(isvc, "importaciones_con_cruce", return_value=rows):
        r = isvc.costo_hilado_recibido_mes(2026, 10)
    assert r["kg"] == 24300.0 and r["kg_con_costo"] == 24300.0
    assert r["us"] == pytest.approx(PLATA * 24300 / 71880, abs=0.01)
    assert r["us"] / r["kg"] == pytest.approx(2.1355, abs=0.0001)
    assert r["sin_costo"] == []


def test_cuando_llegan_todas_suma_la_plata_entera():
    rows = _imps(set(KG))
    with patch.object(isvc, "importaciones_con_cruce", return_value=rows):
        r = isvc.costo_hilado_recibido_mes(2026, 10)
    assert r["kg_con_costo"] == 71880.0
    assert r["us"] == pytest.approx(PLATA, abs=0.02)


def test_sin_plata_cargada_los_kilos_esperan_con_nombre():
    rows = _imps({"IM-0000646"}, plata_en=None)
    with patch.object(isvc, "importaciones_con_cruce", return_value=rows):
        r = isvc.costo_hilado_recibido_mes(2026, 10)
    assert r["kg_con_costo"] == 0 and r["us"] == 0
    assert r["sin_costo"] == [{"codigo": "MH 71-72", "kg": 24300.0}]


def test_compra_y_anticipos_no_se_cuentan_dos_veces():
    rows = _imps(set(KG))
    rows[0].update(fuente="compra", importe_programa=PLATA)   # ya convertida
    with patch.object(isvc, "importaciones_con_cruce", return_value=rows):
        r = isvc.costo_hilado_recibido_mes(2026, 10)
    assert r["us"] == pytest.approx(PLATA, abs=0.02)


def _recepcion_del_anticipo(llegaron):
    index = {}
    for r in _imps(llegaron):
        index.setdefault(("MH", 71), []).append(r)
        index.setdefault(("MH", 72), []).append(r)
    ants = [{"id_dolares": 3358, "cta": "MH", "concepto": "71/26",
             "fecha": "2026-09-02", "importe": 121569.17},
            {"id_dolares": 3492, "cta": "MH", "concepto": "71/26",
             "fecha": "2026-09-29", "importe": 31930.06}]
    with patch.object(isvc, "_index_importaciones_por_codigo", return_value=index):
        isvc.adjuntar_recepcion_asinfo(ants)
    return ants


def test_el_anticipo_cuenta_recibido_desde_la_primera_partida():
    ants = _recepcion_del_anticipo({"IM-0000646"})
    a = ants[0]
    assert a["fecha_recepcion_im"] == "2026-10-06"
    assert a["kg_recibido_im"] == 24300.0 and a["kg_partidas_im"] == 71880.0
    assert a["partidas_completas_im"] is False
    assert _recepcion_del_anticipo(set(KG))[0]["partidas_completas_im"] is True


@pytest.fixture
def _limpio():
    q._ANTIC_RECIBIDOS_ULTIMO_BUENO = None
    q._ANTIC_RECIBIDOS_VISTOS.clear()
    q._ANTIC_RECIBIDOS_PARTE.clear()
    yield
    q._ANTIC_RECIBIDOS_ULTIMO_BUENO = None
    q._ANTIC_RECIBIDOS_VISTOS.clear()
    q._ANTIC_RECIBIDOS_PARTE.clear()


def test_el_descuento_del_anticipo_es_la_misma_parte_que_entra_al_stock(_limpio):
    vivos = [{"id_dolares": 3358, "cta": "MH", "importe": 121569.17},
             {"id_dolares": 3492, "cta": "MH", "importe": 31930.06}]

    def _rec(filas):
        for f in filas:
            f.update(im_numero="IM-0000648", fecha_recepcion_im="2026-10-06",
                     kg_recibido_im=24300.0, kg_partidas_im=71880.0,
                     partidas_completas_im=False)
    with patch("modules.dolares.queries.anticipos_vivos",
               return_value=[dict(v) for v in vivos]), \
         patch("modules.importaciones.service.adjuntar_recepcion_asinfo",
               side_effect=_rec), \
         patch("modules.importaciones.autobap.config", return_value={}):
        total = q.anticipos_con_mercaderia_recibida()
    assert total == pytest.approx(PLATA * 24300 / 71880, abs=0.02)


def test_la_conversion_automatica_espera_a_que_lleguen_todas():
    from modules.importaciones import autobap

    base = {"id_dolares": 3358, "fecha": "2026-09-02", "cta": "MH",
            "concepto": "71/26", "importe": 121569.17, "ref": 71,
            "anio_ref": 2026, "im_numero": "IM-0000648",
            "fecha_recepcion_im": "2026-10-06", "kg_im": 23790.0}
    index = {("MH", 71): [{"im_numero": "IM-0000648", "fecha": "2026-08-27",
                           "nota": "INV HY5464-26 ( MH 71-72 ) ---3"}]}
    cfg = {"fecha_corte": "2026-07-01"}

    def _correr(fila):
        with patch("modules.dolares.queries.anticipos_vivos", return_value=[fila]), \
             patch("modules.importaciones.service._index_importaciones_por_codigo",
                   return_value=index), \
             patch("modules.importaciones.service.adjuntar_recepcion_asinfo"), \
             patch("modules.dolares.anio.adjuntar_anio"):
            return autobap.pendientes(cfg)
    assert _correr(dict(base, partidas_completas_im=False))["grupos"] == []
    assert len(_correr(dict(base, partidas_completas_im=True))["grupos"]) == 1
    assert len(_correr(dict(base))["grupos"]) == 1      # sin partidas: como siempre
