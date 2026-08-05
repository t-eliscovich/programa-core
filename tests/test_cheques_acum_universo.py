"""El ACUM de /cheques también es del UNIVERSO filtrado, no de la página.

Mismo bug que /facturas (05/08/2026): /cheques pagina de a 500 y el corrido se
acumulaba en Python sobre las filas que habían llegado, así que arrancaba de
cero en cada página. Dueña: "a nadie le importa la pagina visible".

Acá hay una vuelta de tuerca: la lista tiene 3 órdenes server-side (fecha de
depósito, importe, cargado). La window del ACUM tiene que usar EXACTAMENTE el
mismo ORDER BY que la página — si no, el corrido salta en el borde.
"""

from __future__ import annotations

from datetime import date
from unittest.mock import patch

import db
from modules.cheques import queries as cq


def _sql_de(orden=""):
    visto = {}

    def _spy(sql, params=None, conn=None):
        visto.setdefault("sql", " ".join(sql.split()).lower())
        return []

    with patch.object(db, "fetch_all", _spy):
        cq.buscar(orden=orden)
    return visto["sql"]


def test_el_acum_lo_calcula_el_sql_antes_del_limit():
    sql = _sql_de()
    assert "with filtrados as (" in sql
    assert "sum(coalesce(fi.importe, 0)) over (" in sql, \
        "el ACUM tiene que salir de una window del SQL"
    assert sql.index("over (") < sql.index("limit %(limite)s"), \
        "la window se evalúa ANTES del LIMIT/OFFSET"


def test_la_window_usa_el_mismo_orden_que_la_pagina():
    """Los dos ORDER BY tienen que ser el MISMO, en los 5 órdenes."""
    for orden, expr in (
        ("", "coalesce(fi.fechad, fi.fecha) asc nulls first, fi.id_cheque asc"),
        ("importe_desc", "fi.importe desc nulls last, fi.id_cheque desc"),
        ("importe_asc", "fi.importe asc nulls last, fi.id_cheque asc"),
        ("cargado_desc", "fi.dia_ingreso desc nulls last, fi.id_cheque desc"),
        ("cargado_asc", "fi.dia_ingreso asc nulls last, fi.id_cheque asc"),
    ):
        sql = _sql_de(orden)
        assert sql.count("order by " + expr) == 2, (
            f"orden={orden!r}: la window y la página tienen que ordenar igual "
            f"por {expr!r}"
        )


def test_el_acum_no_se_recalcula_sobre_la_pagina():
    """Página 2: el corrido viene de la base con lo de las páginas anteriores."""
    filas = [
        {"id_cheque": 1, "fecha": date(2026, 8, 1), "fechad": date(2026, 8, 1),
         "importe": 100.0, "saldo_acumulado": 5100.0},
        {"id_cheque": 2, "fecha": date(2026, 8, 2), "fechad": date(2026, 8, 2),
         "importe": 200.0, "saldo_acumulado": 5300.0},
    ]
    with patch.object(db, "fetch_all", lambda *a, **kw: [dict(f) for f in filas]):
        out = cq.buscar(limite=2, offset=500)
    assert [r["saldo_acumulado"] for r in out] == [5100.0, 5300.0]
    assert 300.0 not in [r["saldo_acumulado"] for r in out]


def test_no_se_reordena_en_python_despues_de_paginar():
    """El orden que llega del SQL es el que se muestra.

    Reordenar en Python después del LIMIT mezcla las filas DENTRO de la página
    y las desalinea del corrido.
    """
    filas = [
        {"id_cheque": 9, "fecha": date(2026, 8, 9), "fechad": date(2026, 8, 9),
         "importe": 10.0, "saldo_acumulado": 10.0},
        {"id_cheque": 1, "fecha": date(2026, 8, 1), "fechad": date(2026, 8, 1),
         "importe": 20.0, "saldo_acumulado": 30.0},
    ]
    with patch.object(db, "fetch_all", lambda *a, **kw: [dict(f) for f in filas]):
        out = cq.buscar()
    assert [r["id_cheque"] for r in out] == [9, 1]
