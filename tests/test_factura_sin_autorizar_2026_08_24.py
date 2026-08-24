"""«Falta cargarla» y «todavía sin autorizar» no son lo mismo.

TMT 2026-08-24 (dueña), mirando la 001-099-000182519 en ámbar: *"me suena raro
porque van más de 5 mins"*. Tenía razón en sospechar, y no era la carga: Asinfo
la tenía en **estado 15**, que no está entre los que el programa importa
(`fc.estado IN (1, 4, 16)`, el WHERE de la card 199). O sea que el programa ni
la veía — y la pantalla igual decía "falta cargarla", que acusa a la carga de
algo que no hizo.

No es un caso raro de un día: de las seis facturas que estuvieron alguna vez en
estado 15, **cuatro son del 16/01/2025 y nunca salieron de ahí**. Puede
destrabarse solo en minutos, y puede quedarse trabado para siempre; en los dos
casos lo tiene que mirar quien factura, no quien carga.
"""
from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import patch

from modules.facturas import dia_despacho as dd

RAIZ = Path(__file__).resolve().parent.parent
TPL = (RAIZ / "modules/facturas/templates/facturas/dia_despacho.html").read_text(
    encoding="utf-8")


def test_los_estados_que_se_importan_son_los_de_la_card():
    """Si esta lista se despega del WHERE de la card, la pantalla miente."""
    assert dd.ESTADOS_IMPORTABLES == (1, 4, 16)


def test_la_consulta_pide_los_que_NO_se_importan():
    sql = []
    with patch("modules._lib.metabase_client.fetch_dataset",
               side_effect=lambda db, q, **k: sql.append(q) or []):
        assert dd._docs_sin_autorizar("2026-08-24") == set()
    q = sql[0]
    assert "fc.estado NOT IN (1, 4, 16)" in q
    assert "fc.estado <> 0" in q, "el 0 es la emisión fallida: ya se descarta"
    assert "fc.fecha = '2026-08-24'" in q


def test_la_fecha_se_valida_antes_de_entrar_al_sql():
    """La fecha va como literal: se valida entera, como en el resto."""
    import pytest
    with pytest.raises(ValueError):
        dd._dia("2026-08-24'; DROP TABLE factura_cliente--")


def test_la_pantalla_los_nombra_distinto():
    assert "todavía sin autorizar" in TPL
    assert "falta cargarla" in TPL
    # y el que no está autorizado NO dice además "falta cargarla"
    bloque = TPL[TPL.index("{% if g.sin_autorizar %}"):]
    assert bloque.index("{% elif g.docs != g.en_pc %}") < bloque.index("{% endif %}")


def test_el_cuadre_los_resta_por_su_propio_renglon():
    """La cuenta tiene que seguir cerrando: se parte el balde, no se pierde."""
    fuente = (RAIZ / "modules/facturas/dia_despacho.py").read_text(encoding="utf-8")
    assert "- kg_sin_autorizar" in fuente
    assert '"sin_autorizar": {"kg": kg_sin_autorizar' in fuente
    assert "− todavía sin autorizar en Asinfo" in TPL


def test_una_guia_con_las_dos_cosas_no_cae_en_sin_autorizar():
    """Si de sus documentos SÓLO algunos están sin autorizar, el otro sí falta
    cargarlo: la guía va al balde de la carga, que es el que se arregla solo."""
    fuente = (RAIZ / "modules/facturas/dia_despacho.py").read_text(encoding="utf-8")
    assert re.search(r"all\(x in sin_autorizar_docs\s*\n?\s*for x in faltan\)",
                     fuente), "el criterio tiene que ser TODOS, no alguno"
