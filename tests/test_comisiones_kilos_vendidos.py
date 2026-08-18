"""Los KILOS debajo del importe de ventas en /comisiones.

TMT 2026-08-18, dueña: *"en ventas y en cobranzas puedes poner los kg
abajo"*. Las metas de venta se miden EN KILOS, así que la pantalla de
comisiones habla la misma unidad que las metas.

Terminó siendo SÓLO ventas, y eso también se protege acá. La cobranza es
plata y no tiene kilos propios: habría que imputarle los de la factura
que pagó cada cheque, prorrateando por `chequesxfact`. Medido sobre
agosto 2026 (consola SQL, 18/08): el 43 % de lo cobrado —$259.971 de 476
cheques— son cheques del `dbf-import` que NUNCA tuvieron factura
vinculada, porque el dBase no guardaba ese vínculo. El prorrateo daba
36.640 kg contra 82.000 kg vendidos: 45 % corto, al lado de una cobranza
que casi iguala a la venta. Se le ofreció a la dueña estimar el hueco al
$/kg del mes (66.280 kg) y dijo que no: kilos sólo donde el dato es
exacto.

El test que protege eso es `test_cobranzas_no_inventa_kilos`: si alguien
vuelve a agregar kilos a la cobranza, que sea una decisión y no un
descuido.
"""
from __future__ import annotations

import os
import sys

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from modules.comisiones import queries as q  # noqa: E402
from modules.comisiones import views as v  # noqa: E402


@pytest.fixture()
def sql_de_lista(monkeypatch):
    """El SQL que ejecuta `queries.lista`."""
    capturado = {}

    def fake_fetch_all(sql, params=None, **kw):
        capturado["sql"] = sql
        return []

    monkeypatch.setattr(q.db, "fetch_all", fake_fetch_all)
    q.lista(anio=2026, mes=8)
    return capturado["sql"]


def test_ventas_suma_los_kilos_facturados(sql_de_lista):
    """`factura.kg` del mes, con la MISMA cara que el importe.

    Los kilos tienen que salir de la misma rama que las ventas —mismo
    filtro de fecha, mismo `stat <> 'X'`— o la pantalla mostraría kilos
    de un universo de facturas distinto al del importe de arriba.
    """
    assert "SUM(f.kg)" in sql_de_lista, "ventas_mes no suma factura.kg"
    assert "AS ventas_kg" in sql_de_lista, "la fila no expone ventas_kg"


def test_los_kilos_salen_del_mismo_bloque_que_el_importe(sql_de_lista):
    """Un solo SELECT sobre `factura`: importe y kilos no pueden divergir."""
    bloque = sql_de_lista[sql_de_lista.index("ventas_mes AS ("):]
    bloque = bloque[: bloque.index("SELECT v.codigo")]
    assert "SUM(f.importe)" in bloque and "SUM(f.kg)" in bloque, (
        "los kilos se calculan fuera del bloque de ventas: pueden quedar "
        "sobre otro universo de facturas que el importe"
    )


def test_cobranzas_no_inventa_kilos(sql_de_lista):
    """La cobranza NO trae kilos — decisión de la dueña, 18/08.

    Si vuelve a aparecer, que sea a propósito: leer el docstring de arriba
    y el comentario de `queries.lista` antes de tocar este test.
    """
    assert "AS cobranzas_kg" not in sql_de_lista
    # El nombre de la tabla aparece en el comentario que explica POR QUÉ no
    # se usa: lo que no puede aparecer es la tabla misma.
    assert "scintela.chequesxfact" not in sql_de_lista, (
        "la comisión no mira el vínculo cheque↔factura: el 43 % de lo "
        "cobrado no lo tiene"
    )


def test_el_total_de_arriba_es_la_suma_de_la_columna(monkeypatch):
    """La franja de arriba suma los kilos de las filas, no los recalcula.

    Es el mismo invariante que ya cumplen ventas/cobranzas/comisión: el
    total tiene que SER la suma de lo que está debajo, o la pantalla se
    contradice a sí misma.
    """
    filas = [
        {"codigo": "EDG", "ventas_mes": 100, "ventas_kg": 11,
         "cobranzas_mes": 50, "comision_mes": 0.5},
        {"codigo": "PPR", "ventas_mes": 200, "ventas_kg": 22.5,
         "cobranzas_mes": 60, "comision_mes": 0.6},
    ]
    capturado = {}

    monkeypatch.setattr(v.queries, "lista", lambda **kw: filas)
    monkeypatch.setattr(
        v, "render_template",
        lambda plantilla, **ctx: capturado.update(ctx) or "",
    )
    from flask import Flask

    flask_app = Flask(__name__)
    # `lista` va decorada con @requiere_login/@requiere_permiso: se llama a
    # la función de adentro, que es la que arma los totales.
    fn = v.lista
    while hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__
    with flask_app.test_request_context("/comisiones?anio=2026&mes=8"):
        fn()

    assert capturado["totales"]["ventas_kg"] == pytest.approx(33.5)
    assert "cobranzas_kg" not in capturado["totales"]


def test_el_csv_exporta_los_kilos():
    """Si están en la pantalla, están en el CSV: la dueña baja el CSV para
    pegarlo al lado de las metas, que también están en kilos."""
    import inspect

    fuente = inspect.getsource(v.lista)
    assert '("ventas_kg", "Ventas mes (kg)")' in fuente
