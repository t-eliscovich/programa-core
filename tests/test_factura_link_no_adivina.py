"""El link a una factura no puede abrir la factura de otro cliente.

Dueña 26/08/2026, abriendo `/facturas/10919`: *"el link me manda a cualquier
lado. Es hasta otro cliente"*.

`numf` es el número visible y **no es único**: al 26/08/2026 hay 2.064 números
repetidos entre 4.416 facturas — el 12% del total —, porque las notas de
entrega y las de crédito llevan su propia numeración y chocan con las facturas
viejas. El 10919 es la NTEN del mostrador de ese día ($5,53) y también una
devolución de AGL del 03/06 (−$462,15).

`por_id` elegía sola con `ORDER BY id_factura ASC`: SIEMPRE la más vieja, sin
decir que había otra. El arreglo va en la RUTA y no en cada link — hay diez
lugares que linkean una factura, y el próximo que se escriba se equivocaría
igual.
"""
from __future__ import annotations

import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from modules.facturas import queries, views  # noqa: E402


def test_la_ruta_no_elige_cuando_el_numero_da_para_dos():
    """Con dos candidatas manda a la lista, no abre una a dedo."""
    fuente = inspect.getsource(views.detalle)
    assert "las_del_mismo_numero" in fuente, (
        "la ruta no chequea si el número está repetido")
    i = fuente.index("las_del_mismo_numero")
    trozo = fuente[i:i + 420]
    assert "len(candidatas) > 1" in trozo
    assert "redirect" in trozo, "con dos candidatas tiene que mandar a elegir"


def test_con_una_sola_candidata_abre_normal():
    """El 88% de las facturas no está repetido: ésas se abren como siempre."""
    fuente = inspect.getsource(views.detalle)
    assert "queries.por_id(id_factura)" in fuente


def test_el_numero_completo_gana_siempre():
    """Quien manda `?doc=` no puede caer en la pantalla de elegir."""
    fuente = inspect.getsource(views.detalle)
    i_doc = fuente.index("por_numf_completo")
    i_amb = fuente.index("las_del_mismo_numero")
    assert i_doc < i_amb, "el desempate tiene que probarse ANTES de la ambigüedad"


def test_las_del_mismo_numero_las_trae_de_la_mas_nueva_a_la_mas_vieja():
    """Si hay que elegir, arriba va la de hoy — que es la que uno buscaba."""
    sql = " ".join(inspect.getsource(queries.las_del_mismo_numero).split())
    assert "WHERE f.numf = %s" in sql
    assert "ORDER BY f.fecha DESC" in sql
    assert "cliente" in sql, "sin el cliente no se puede elegir cuál es"


def test_la_lista_manda_el_numero_completo():
    """Donde el número completo está a mano, se manda: así nadie ve la pantalla
    de elegir."""
    t = (Path(__file__).resolve().parents[1] / "modules" / "facturas" /
         "templates" / "facturas" / "lista.html").read_text(encoding="utf-8")
    i = t.index("url_for('facturas.detalle'")
    assert "doc=f.numf_completo" in t[i:i + 160], (
        "la lista tiene el número completo y no lo manda")
