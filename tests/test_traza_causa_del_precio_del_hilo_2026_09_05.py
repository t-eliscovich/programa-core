"""El $/kg del hilado no "cambia": entra plata por algún lado, y la foto dice por cuál.

Tamara 2026-09-05, después de que los recargos tardíos (AC 39 y MD 1, 6.849,51)
entraran al precio a las 11:44 y la traza dijera "cambió el $/kg de 3 etapas
+8.604": *"asegurate de tener todas las variables del proceso trackeadas"*.

Desde la mig 0244 cada foto guarda el desglose de las compras del mes que arman
el $/kg —importaciones recibidas, compras locales, el botón, recargos tardíos—
y el detalle con nombres en `hilado_insumos`. Con dos fotos se sabe por dónde
entró la plata, y el renglón (y la nota del día, que usa la misma función) lo
dice antes de las cifras.
"""
from __future__ import annotations

import json
import os
import sys
from unittest.mock import patch

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from modules.informes import traza as t  # noqa: E402

ANT = {"compras_import_us": 63180.60, "compras_local_us": 0.0,
       "al_precio_us": 21253.0, "recargos_tardios_us": 0.0,
       "hilado_insumos": json.dumps({"recargos_tardios": []})}
HOY = {"compras_import_us": 63180.60, "compras_local_us": 0.0,
       "al_precio_us": 21253.0, "recargos_tardios_us": 6849.51,
       "hilado_insumos": json.dumps({"recargos_tardios": [
           {"id_compra": 307, "codigo": "MD 1", "importe": 1340.35},
           {"id_compra": 308, "codigo": "MD 1", "importe": 2850.0},
           {"id_compra": 310, "codigo": "AC 39", "importe": 2659.16}]})}


def test_los_recargos_tardios_se_nombran_por_su_importacion():
    assert t.causa_tarifa(HOY, ANT) == (
        "recargos de MD 1 y AC 39 al precio del hilo (+6.849,51)")


def test_el_boton_y_la_importacion_tambien_tienen_nombre():
    hoy = dict(ANT, al_precio_us=42506.0, compras_import_us=65787.48)
    assert t.causa_tarifa(hoy, ANT) == (
        "compra al precio del hilo, a mano (+21.253,00) y "
        "importación recibida (+2.606,88)")
    loc = dict(ANT, compras_local_us=1500.0)
    assert t.causa_tarifa(loc, ANT) == "compra local de hilo (+1.500,00)"


def test_sin_movimiento_o_sin_desglose_no_dice_nada():
    mezcla = "sin compras nuevas · kilos entre bodega y máquinas"
    assert t.causa_tarifa(ANT, ANT) == mezcla
    assert t.causa_tarifa(dict(ANT, recargos_tardios_us=0.4), ANT) == mezcla
    assert t.causa_tarifa(HOY, None) == ""
    assert t.causa_tarifa(HOY, {"compras_us": 1.0}) == ""     # foto vieja
    # hilado_insumos ya deserializado (jsonb → dict) también sirve
    hoy = dict(HOY, hilado_insumos=json.loads(HOY["hilado_insumos"]))
    assert "MD 1 y AC 39" in t.causa_tarifa(hoy, ANT)


def test_el_renglon_de_la_revaluacion_dice_la_causa_antes_de_las_cifras():
    movs = [{"componente": "vsto", "doc_id": "#stock:tarifa", "tipo": "cambio",
             "etiqueta": "cambió el $/kg de 3 etapas",
             "regla": "Revaluación de stock", "aporte": 8604.0,
             "familia": "utilidad"}]
    with patch.object(t.db, "fetch_all", return_value=[]):
        out = t.resumir(movs, 8604.0, {}, causa_tarifa=t.causa_tarifa(HOY, ANT))
    assert out[0]["texto"] == (
        "recargos de MD 1 y AC 39 al precio del hilo (+6.849,51) "
        "· cambió el $/kg de 3 etapas")
    # Sin causa, el renglón queda como siempre.
    with patch.object(t.db, "fetch_all", return_value=[]):
        out = t.resumir(movs, 8604.0, {})
    assert out[0]["texto"] == "cambió el $/kg de 3 etapas"


def test_si_la_revaluacion_se_fundio_con_la_importacion_no_se_repite():
    """La llegada de la importación YA es la causa: no se dice dos veces."""
    movs = [
        {"componente": "antic", "doc_id": "#recibidos", "tipo": "cambio",
         "etiqueta": t.TXT_ANTICIPO_RECIBIDO, "regla": "Anticipos",
         "aporte": -73983.89, "familia": "utilidad"},
        {"componente": "vsto", "doc_id": "#stock", "tipo": "cambio",
         "etiqueta": "entró a hilado", "regla": "Stock",
         "aporte": 69873.36, "familia": "utilidad"},
        {"componente": "vsto", "doc_id": "#stock:tarifa", "tipo": "cambio",
         "etiqueta": "cambió el $/kg de 3 etapas",
         "regla": "Revaluación de stock", "aporte": 5152.21,
         "familia": "utilidad"},
    ]
    with patch.object(t.db, "fetch_all", return_value=[]):
        out = t.resumir(movs, 1041.68, {}, hasta="2026-08-10T11:26:40",
                        causa_tarifa="importación recibida (+73.983,89)")
    assert len(out) == 1
    assert out[0]["texto"] == "entró la mercadería de los anticipos"


def test_la_foto_guarda_el_desglose_y_el_detalle():
    bal = {"diagnostico": {"componentes": {"utilidad": 1.0}},
           "hilado_valuacion": {
               "compras": 19812.48, "compras_us": 91283.11, "kg_sin_costo": 0.0,
               "insumos": {"import_us": 63180.60, "local_us": 0.0,
                           "al_precio_us": 21253.0, "recargos_tardios_us": 6849.51,
                           "recargos_tardios": [{"id_compra": 310, "codigo": "AC 39",
                                                 "importe": 2659.16}],
                           "open_ukg": 3.043548}}}
    f = t._fila_desde_balance(bal)
    assert f["compras_import_us"] == pytest.approx(63180.60)
    assert f["al_precio_us"] == pytest.approx(21253.0)
    assert f["recargos_tardios_us"] == pytest.approx(6849.51)
    ins = json.loads(f["hilado_insumos"])
    assert ins["recargos_tardios"][0]["codigo"] == "AC 39"
    assert ins["open_ukg"] == pytest.approx(3.043548)
    # Un balance viejo, sin desglose: columnas en None y sin JSON.
    f0 = t._fila_desde_balance({"diagnostico": {"componentes": {"utilidad": 1.0}}})
    assert f0["recargos_tardios_us"] is None and f0["hilado_insumos"] is None


def test_la_nota_del_dia_lee_la_causa_de_las_dos_fotos():
    from modules.informes import dia
    filas = [dict(ANT, id_traza=10), dict(HOY, id_traza=20)]
    with patch.object(dia, "_rows", return_value=filas):
        c = dia._causa_tarifa_del_dia({"id_traza": 10}, {"id_traza": 20})
    assert "MD 1 y AC 39" in c
    assert dia._causa_tarifa_del_dia({}, {"id_traza": 20}) == ""
    with patch.object(dia, "_rows", side_effect=RuntimeError("sin base")):
        assert dia._causa_tarifa_del_dia({"id_traza": 10}, {"id_traza": 20}) == ""


def test_si_no_entro_plata_lo_dice():
    """El $/kg también se corre sin compras: cambia la mezcla bodega/máquinas."""
    assert t.causa_tarifa(dict(ANT), ANT) == "sin compras nuevas · kilos entre bodega y máquinas"
    # Fotos viejas (sin desglose) no dicen nada.
    assert t.causa_tarifa({"compras_us": 1.0}, {"compras_us": 1.0}) == ""
