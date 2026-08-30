"""La recepción SIN código del programa en la Nota avisa a la campanita.

TMT 2026-08-30 (dueña, sobre MTG3756): *"eso debería haber aparecido en la
campanita, para que Andrés vea y lo cargue"*. El 29/08 Asinfo recibió
16.113,6 kg (IM-653/654) con la Nota "MTG3756" pelada: sin código no cruza
con ninguna compra ni anticipo, los kilos entraron al stock sin su plata
(+39.974 de utilidad a las 08:26) y ninguna alarma dijo nada — la de "falta
plata" saltea a los grupos sin nada atribuido y espera 30 días.
"""
from __future__ import annotations

import os
import sys
from datetime import timedelta
from unittest.mock import patch

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from modules.importaciones import vigilancia as vig  # noqa: E402


def _fila(nota, *, dias_atras=1, kg=13104.0, im="IM-0000654",
          recibida=True, prov=None, numero=None, proveedor=""):
    from filters import today_ec
    rec = today_ec() - timedelta(days=dias_atras)
    return {
        "im_numero": im, "recibida": recibida,
        "fecha_recepcion": str(rec) if recibida else None,
        "fecha": str(rec - timedelta(days=2)),
        "nota": nota, "prov": prov, "numero": numero, "numero_hasta": None,
        "proveedor": proveedor,
        "grupo_id": im, "grupo_ims": [im], "grupo_kg": kg, "kg": kg,
        "compra": None, "anticipo": None,
    }


def test_recepcion_sin_codigo_es_un_caso_y_agrupa_las_partidas():
    rows = [
        _fila("MTG3756 ---2", im="IM-0000654", kg=13104.0),
        _fila("MTG3756 ---1", im="IM-0000653", kg=3009.6),
        # Con código: no es de acá.
        _fila("AYF02871 ( AI 48)", im="IM-0000650", prov="AI", numero=48),
        # Sin código pero NO recibida: todavía no entró al stock.
        _fila("XTG9999", im="IM-9", recibida=False),
    ]
    out = vig.importaciones_sin_codigo(rows=rows)
    assert len(out) == 1
    c = out[0]
    assert c["nota"] == "MTG3756"
    assert c["kg"] == 16113.6                     # las dos partidas, juntas
    assert sorted(c["ims"]) == ["IM-0000653", "IM-0000654"]


def test_mas_vieja_que_el_techo_no_avisa():
    """MTGE3755 (enero) y la INV de 2025 son historia, no una tarea."""
    rows = [_fila("MTGE3755", im="IM-0000526", dias_atras=200, kg=4070.0)]
    assert vig.importaciones_sin_codigo(rows=rows) == []
    assert len(vig.importaciones_sin_codigo(rows=rows, techo=0)) == 1


def test_si_no_se_puede_leer_no_inventa():
    with patch("modules.importaciones.service.importaciones_con_cruce",
               side_effect=RuntimeError("asinfo caido")):
        assert vig.importaciones_sin_codigo() == []


def test_avisa_a_la_campanita_una_vez_por_factura():
    vig._ultima_corrida = 0.0
    rows = [
        _fila("MTG3756 ---2", im="IM-0000654", kg=13104.0),
        _fila("MTG3756 ---1", im="IM-0000653", kg=3009.6),
    ]
    vistos = []
    with patch.object(vig, "_leer_importaciones", return_value=rows), \
         patch.object(vig, "importaciones_fuera_de_banda", return_value=[]), \
         patch.object(vig, "facturas_con_plata_en_una_sola", return_value=[]), \
         patch.object(vig, "_resolver_los_arreglados"), \
         patch("modules.avisos.queries.avisar",
               side_effect=lambda **kw: vistos.append(kw) or True):
        out = vig.revisar_si_toca()
    assert out["sin_codigo"] == 1 and out["avisados"] == 1
    a = vistos[0]
    assert a["fuente"] == "importaciones" and a["nivel"] == "alerta"
    # Idempotente por FACTURA del proveedor (las partidas comparten la Nota).
    assert a["clave"] == "import-sin-codigo:MTG3756"
    assert a["url"] == "/importaciones?anio=todos&q=MTG3756"
    assert a["titulo"] == ("MTG3756 · 16.114 kg llegaron sin código del "
                           "programa en la Nota. ¿Qué compra es?")
    assert "IM-" not in a["titulo"]


def test_el_aviso_se_resuelve_cuando_la_nota_ya_tiene_codigo():
    """dueña 2026-08-26: *"si un aviso ya se solucionó habría que avisar que
    se solucionó"* — cuando a la Nota le ponen el código, el mismo renglón
    pasa a 'listo'."""
    rows = [_fila("MTG3756 ( MH 74) ---2", im="IM-0000654",
                  prov="MH", numero=74)]
    resueltos = []
    with patch.object(vig, "_leer_costos",
                      return_value={"IM-0000654": {"costo": 1.0, "kg": 1.0,
                                                   "kg_sin_precio": 0.0}}), \
         patch.object(vig, "facturas_con_plata_en_una_sola", return_value=[]), \
         patch("modules.avisos.queries.abiertos_por_clave",
               side_effect=lambda pref: (
                   [{"id_aviso": 7, "clave": "import-sin-codigo:MTG3756"}]
                   if pref == "import-sin-codigo:" else [])), \
         patch("modules.avisos.queries.resolver",
               side_effect=lambda id_aviso, **kw: resueltos.append(
                   (id_aviso, kw)) or True):
        vig._resolver_los_arreglados(rows)
    assert len(resueltos) == 1
    id_aviso, kw = resueltos[0]
    assert id_aviso == 7
    assert kw["titulo"] == "MTG3756 · listo, la Nota ya tiene código"
