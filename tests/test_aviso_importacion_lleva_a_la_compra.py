"""El "ver →" de una novedad de importación lleva a LA compra.

TMT 2026-08-09, sobre la fila *"AC 33 · 22.993 kg · $ 73.983,89 · Se cargó a
compras · ver →"*: *"el link acá de ver me debería llevar a la pantalla
compras ya filtrada. No acá: /importaciones/automatico"*.

El aviso dice "se cargó a compras" y llevaba a la pantalla del PROCESO, donde
hay que volver a buscar la compra a ojo. Misma regla del 07/08 con los links
del historial: si al clickear hay que buscar la fila, el link no está
terminado.

Los frenos y los errores sí son del proceso: esos se quedan.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules.importaciones import autobap  # noqa: E402


def test_la_conversion_lleva_a_compras_filtrado():
    with patch.object(autobap.db, "fetch_one", return_value={"numero": 10144}):
        assert autobap._url_del_aviso("conversion", 496) == "/compras?q=10144"


def test_sin_numero_cae_en_la_ficha_de_la_compra():
    """232 compras (dbf-import) no tienen número: la ficha resuelve por id."""
    with patch.object(autobap.db, "fetch_one", return_value={"numero": 0}):
        assert autobap._url_del_aviso("conversion", 496) == "/compras/496"


def test_el_freno_y_el_error_se_quedan_en_el_proceso():
    assert autobap._url_del_aviso("freno", None) == "/importaciones/automatico"
    assert autobap._url_del_aviso("error", 496) == "/importaciones/automatico"


def test_si_la_base_falla_el_link_no_rompe_el_aviso():
    with patch.object(autobap.db, "fetch_one", side_effect=RuntimeError("boom")):
        assert autobap._url_del_aviso("conversion", 496) == "/compras/496"


def test_la_migracion_reapunta_las_viejas_por_la_clave():
    """La `clave` ya guarda el id ("importaciones:compra:<id>"), así que las
    novedades ya emitidas se arreglan sin adivinar."""
    sql = Path("migrations/0183_avisos_importacion_al_compra.sql").read_text(
        encoding="utf-8")
    assert "importaciones:compra:%" in sql
    assert "split_part(a.clave, ':', 3)" in sql
    # y NO toca los frenos ni los errores
    assert "AND a.url = '/importaciones/automatico'" in sql
