"""Test guard: las queries del balance que sumen scintela.factura/compra
del mes en curso DEBEN filtrar usuario_crea='asinfo-backfill'.

Bug recurrente: el backfill de Asinfo importa filas históricas que ya
están en scintela.historia. Si una query las suma como live, doble-cuenta.
La memoria 'Backfill Asinfo: excluir de cálculos live' lo documenta.

Este test parsea el código y se asegura que cada bloque `FROM scintela.factura`
o `FROM scintela.compra` que tenga un filtro `WHERE ... fecha ...` también
contenga `usuario_crea` cerca (= NO_BACKFILL_WHERE aplicado).

Si tu cambio rompe este test, agregá `AND {NO_BACKFILL_WHERE}` al WHERE.
"""
from __future__ import annotations

import os
import re
import sys

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


def _split_queries(src: str) -> list[tuple[int, str]]:
    """Devuelve [(line_num_starting, query_block)] para cada FROM scintela.X."""
    out: list[tuple[int, str]] = []
    lines = src.splitlines()
    for i, line in enumerate(lines, start=1):
        if re.search(r"FROM\s+scintela\.(factura|compra|xgast)\b", line):
            # captura hasta encontrar el cierre de la query (paréntesis o """)
            block_lines = []
            depth = 0
            for j in range(i - 1, min(i + 40, len(lines))):
                block_lines.append(lines[j])
                if '"""' in lines[j] and j != i - 1:
                    break
                # stop si encontramos GROUP/ORDER/LIMIT que cierran el query
            out.append((i, "\n".join(block_lines)))
    return out


def test_balance_queries_use_no_backfill_filter():
    """Cada bloque FROM scintela.factura/compra con filtro de fecha debe
    excluir asinfo-backfill, salvo whitelist documentada."""
    path = os.path.join(_REPO_ROOT, "modules", "informes", "queries.py")
    with open(path, encoding="utf-8") as f:
        src = f.read()

    # Whitelist: queries que NO necesitan el filtro (por motivo claro).
    # Si agregás algo acá, justificá en el comentario.
    WHITELIST_SUBSTRINGS = (
        # Detalle de compras por id (no es agregación del mes):
        "ORDER BY fecha ASC, id_compra ASC",
        # Compras individuales para drill-down:
        "WHERE num_calc = %s",
        # Query interna del cron de provisiones (no balance):
        "first_match",
        # SELECT por id_compra puntual:
        "WHERE id_compra =",
        # ventas_mes_corriente_kg_fisico(): NO debe filtrar backfill a
        # propósito (kg físicas reales para stock). Contract test espejo:
        # test_ventas_mes_corriente_kg_fisico_NO_filtra_backfill.
        "kg-fisico-incluye-todo",
    )

    failures: list[str] = []
    for line_num, block in _split_queries(src):
        # Sólo nos interesan los que filtran fecha (= cálculo live del mes).
        if not re.search(r"\bfecha\b.*(=|>=|<|BETWEEN|EXTRACT)", block):
            continue
        # Skip si la query está en whitelist
        if any(w in block for w in WHITELIST_SUBSTRINGS):
            continue
        # Debe contener 'usuario_crea' (el filtro de backfill) o
        # NO_BACKFILL_WHERE (la constante).
        if "usuario_crea" not in block and "NO_BACKFILL_WHERE" not in block:
            # Acotar el snippet a las primeras 8 líneas
            snippet = "\n".join(block.splitlines()[:8])
            failures.append(
                f"Línea {line_num}: query sin filtro asinfo-backfill\n"
                f"---\n{snippet}\n---"
            )

    assert not failures, (
        "Las siguientes queries del balance suman scintela.factura/compra "
        "del mes pero NO excluyen usuario_crea='asinfo-backfill'. Agregá "
        "`AND {NO_BACKFILL_WHERE}` al WHERE:\n\n"
        + "\n\n".join(failures)
    )
