"""Queries para la conciliación bancaria."""
from __future__ import annotations

from datetime import date

import db


def cheques_depositados_rango(desde: date, hasta: date) -> list[dict]:
    """Cheques en stat='B' (depositado Pichincha) con fechad en el rango.

    Después de la migración 0013, el stat 'D' se usa para "Daniela" (gestión
    de cobranza). Los cheques depositados quedan en 'B' (vocabulario
    canónico 2026-04-29). Antes este filtro era stat='D' y no devolvía nada
    para conciliar.

    Se excluyen Z (en cartera), R (reversados), A (acreditados — cleared),
    P (postergados), D (Daniela). Sólo 'B' es el universo de cheques
    depositados a investigar contra el extracto del banco.
    """
    return db.fetch_all(
        """
        SELECT id_cheque, no_cheque, fecha, fechad, importe, codigo_cli
          FROM scintela.cheque
         WHERE stat = 'B'
           AND fechad BETWEEN %s AND %s
         ORDER BY fechad DESC, id_cheque DESC
        """,
        (desde, hasta),
    ) or []


def cheque_por_id(id_cheque: int) -> dict | None:
    return db.fetch_one(
        """
        SELECT id_cheque, no_cheque, codigo_cli, importe, stat, fechad
          FROM scintela.cheque
         WHERE id_cheque = %s
        """,
        (id_cheque,),
    )
