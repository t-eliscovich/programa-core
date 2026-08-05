"""El ACUM de /facturas es del UNIVERSO filtrado, no de la página visible.

Dueña 2026-08-05: "porque el acum y el total no son iguales?" / "a nadie le
importa la pagina visible". Con la paginación de 500 (2026-05-22) el corrido
se calculaba en Python sobre las filas de la página, así que la fila de arriba
mostraba el total de esas 500 y nunca cerraba con el header (SUM(saldo) de las
4574 de cartera).

Ahora el corrido lo hace el SQL con una window sobre el set filtrado ENTERO,
antes del LIMIT/OFFSET.
"""

from __future__ import annotations

from datetime import date
from unittest.mock import patch

import db
from modules.facturas import queries as fq


def _fila(numf, dia, saldo, acum):
    return {
        "id_factura": numf, "numf": numf, "numf_completo": str(numf),
        "fecha": date(2026, 8, dia), "vencimiento": None,
        "codigo_cli": "AL1", "cliente": "Cliente", "kg": 10.0,
        "importe": saldo, "abono": 0.0, "saldo": saldo, "stat": "Z",
        "condic": None, "tipo": None,
        "saldo_acumulado": acum,
    }


def test_el_acum_es_el_que_trajo_el_sql_no_el_de_la_pagina():
    """Página 2: el corrido arranca donde lo dejaron las páginas anteriores.

    Las 3 filas suman 600 de saldo, pero vienen con un acumulado de 5.100 /
    5.300 / 5.600 porque el universo filtrado tiene 5.000 más atrás. Si
    alguien vuelve a acumular en Python sobre la página, saldrían 100/300/600.
    """
    filas = [_fila(1, 1, 100.0, 5100.0),
             _fila(2, 2, 200.0, 5300.0),
             _fila(3, 3, 300.0, 5600.0)]
    with patch.object(db, "fetch_all", lambda *a, **kw: list(filas)):
        out = fq.buscar(limite=3, offset=500)

    # La pantalla muestra DESC (las nuevas arriba).
    assert [r["numf"] for r in out] == [3, 2, 1]
    assert [r["saldo_acumulado"] for r in out] == [5600.0, 5300.0, 5100.0]
    # Y NO el corrido de la página.
    assert 600.0 not in [r["saldo_acumulado"] for r in out]


def test_la_window_se_evalua_antes_del_limit():
    """El LIMIT/OFFSET recorta DESPUÉS de calcular el corrido.

    Si el SUM(...) OVER quedara del mismo lado que el LIMIT, la window vería
    solo la página y volveríamos al bug.
    """
    visto = {}

    def _spy(sql, params=None, conn=None):
        visto["sql"] = " ".join(sql.split()).lower()
        return []

    with patch.object(db, "fetch_all", _spy):
        fq.buscar()

    sql = visto["sql"]
    assert "sum(coalesce(fi.saldo, 0)) over (" in sql, \
        "el ACUM tiene que salir de una window del SQL"
    assert "with filtradas as (" in sql
    assert sql.index("over (") < sql.index("limit %(limite)s"), \
        "la window tiene que evaluarse ANTES del LIMIT/OFFSET"


def test_sin_paginar_el_ultimo_acum_es_la_suma_de_todo():
    """Caso página única: el corrido de la fila más nueva = SUM(saldo)."""
    filas = [_fila(1, 1, 100.0, 100.0),
             _fila(2, 2, -30.0, 70.0),      # devolución: resta, como el header
             _fila(3, 3, 300.0, 370.0)]
    with patch.object(db, "fetch_all", lambda *a, **kw: list(filas)):
        out = fq.buscar()
    assert out[0]["saldo_acumulado"] == sum(f["saldo"] for f in filas)


def test_el_orden_desempata_por_id_factura():
    """Hay numf REPETIDOS (10719, 10724 x3...). Sin un desempate total, el
    orden de la window y el del LIMIT/OFFSET se resuelven por separado: el
    corrido salta en el borde de página y la paginación puede saltear filas.
    """
    visto = {}

    def _spy(sql, params=None, conn=None):
        visto["sql"] = " ".join(sql.split()).lower()
        return []

    with patch.object(db, "fetch_all", _spy):
        fq.buscar()

    sql = visto["sql"]
    assert ("order by fi.fecha asc nulls first, fi.numf asc nulls first, "
            "fi.id_factura asc") in sql, "la window necesita desempate total"
    assert "order by fi.fecha desc, fi.numf desc, fi.id_factura desc" in sql, \
        "el orden de la página tiene que ser el inverso EXACTO de la window"


def test_el_reorden_python_respeta_el_desempate():
    """El re-sort de Python (para el orden de pantalla) usa la misma clave."""
    a = _fila(10724, 5, 10.0, 10.0)
    a["id_factura"] = 7
    b = _fila(10724, 5, 20.0, 30.0)   # mismo numf y misma fecha
    b["id_factura"] = 9
    with patch.object(db, "fetch_all", lambda *ar, **kw: [b, a]):
        out = fq.buscar()
    assert [r["id_factura"] for r in out] == [9, 7]
    assert [r["saldo_acumulado"] for r in out] == [30.0, 10.0]
