"""Contract tests — totales del estado de cuenta de cliente (TMT 2026-07-06).

Reporte dueña (screenshot EDU): "Los totales de estado de cuenta corregir
todos (muy mal están)". Tres bugs distintos en la misma pantalla:

1. El pie "Totales (66 facturas)" sumaba TODAS las facturas históricas del
   cliente (stat T incluidas, backfill incluido) aunque la tabla lista solo
   las no-T → kg/importe/abonado no correspondían a las filas visibles.
2. El buscador de la landing usaba `saldo > 0` (escondía NC/sobrepagos
   negativos) y no filtraba asinfo-backfill → EDU 278.036,86 en el buscador
   vs 241.781,01 en el top-10 y en el estado. Canónico: `<> 0` netea
   (modules/cartera/queries.py, "No divergir").
3. Los cheques X (anulados) se listaban y contaban ("3 cheques") pero el
   total SQL ya los excluía (20.000,00) → contador y total inconsistentes.

Estos tests inspeccionan el source (no requieren DB) — mismo patrón que
tests/test_cartera_coherence.py.
"""
from __future__ import annotations

import inspect
import re
from pathlib import Path

_BACKFILL_WHERE = "<> 'asinfo-backfill'"

_TPL = (
    Path(__file__).resolve().parents[1]
    / "modules" / "informes" / "templates" / "informes" / "estado_cuenta.html"
)


def _src_estado_cuenta():
    from modules.informes import queries as iq
    return inspect.getsource(iq.estado_cuenta_cliente)


def test_totales_facturas_mismo_filtro_que_la_lista():
    """El pie de facturas suma EXACTAMENTE lo listado: sin T, sin backfill."""
    src = _src_estado_cuenta()
    # la query de totales (la que tiene SUM(kg)) debe excluir stat T y backfill
    i = src.find("SUM(kg)")
    assert i > 0, "no encontré la query de totales de facturas"
    tot_sql = src[i:src.find('"""', i)]
    assert "<> 'T'" in tot_sql, (
        "los totales del pie deben excluir stat T (igual que la lista) — "
        "sino 'Totales (N facturas)' suma facturas que no están en la tabla"
    )
    assert _BACKFILL_WHERE in tot_sql, "totales del pie sin filtro backfill"


def test_lista_facturas_excluye_backfill():
    src = _src_estado_cuenta()
    i = src.find("SELECT id_factura")
    lista_sql = src[i:src.find('"""', i)]
    assert _BACKFILL_WHERE in lista_sql, (
        "la lista de facturas del estado de cuenta debe excluir asinfo-"
        "backfill (criterio canónico cartera, precedente NJL 2026-07-02)"
    )


def test_buscador_landing_netea_saldos_y_filtra_backfill():
    """buscar_clientes == top-10 == estado (los 3 el mismo número)."""
    from modules.informes import queries as iq
    src = inspect.getsource(iq.buscar_clientes)
    assert "<> 0" in src, (
        "buscar_clientes debe netear con saldo <> 0 (no > 0): con > 0 el "
        "buscador mostraba 278.036,86 y el top-10 241.781,01 para EDU"
    )
    assert not re.search(r"saldo,\s*0\)\s*>\s*0", src), (
        "quedó el filtro saldo > 0 en buscar_clientes"
    )
    assert _BACKFILL_WHERE in src


def test_top10_deudores_filtra_backfill():
    from modules.informes import queries as iq
    assert _BACKFILL_WHERE in inspect.getsource(iq.cartera_por_cliente)


def test_template_cheques_anulados_fuera_del_contador():
    """Los X se listan tachados pero no cuentan en 'Total (N cheques)'."""
    tpl = _TPL.read_text()
    assert "nch.activos" in tpl, "falta el contador de cheques no-anulados"
    assert "anulado" in tpl, "falta la leyenda 'anulado' en los cheques X"
    assert "Total ({{ nch.activos }}" in tpl, (
        "el pie de cheques debe contar solo los no-anulados"
    )
    # el contador viejo (todas las filas) no debe volver
    assert "Total ({{ data.cheques | length }}" not in tpl
