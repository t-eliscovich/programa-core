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
#: el marcador que comparten la lista y los totales (ver
#: `_SQL_ESTADO_CUENTA_STAT_VIVO` en modules/informes/queries.py).
_STAT_VIVO_PLACEHOLDER = "__STAT_VIVO__"

_TPL = (
    Path(__file__).resolve().parents[1]
    / "modules" / "informes" / "templates" / "informes" / "estado_cuenta.html"
)


def _src_estado_cuenta():
    from modules.informes import queries as iq
    return inspect.getsource(iq.estado_cuenta_cliente)


def test_totales_facturas_mismo_filtro_que_la_lista():
    """El pie de facturas suma EXACTAMENTE lo listado: mismo universo.

    TMT 2026-08-13 — este test antes buscaba el literal `<> 'T'`, o sea la
    FORMA del bug de julio y no el invariante. Pasaba en verde mientras las
    ANULADAS se colaban por el mismo agujero (caso #176817 de CTE). Ahora fija
    lo que de verdad importa: la lista y los totales comparten LITERALMENTE el
    criterio de stat, así que no pueden divergir aunque cambie.
    """
    src = _src_estado_cuenta()
    i_lista = src.find("SELECT id_factura")
    i_tot = src.find("SUM(kg)")
    assert i_lista > 0 and i_tot > 0, "no encontré las dos queries de facturas"
    lista_sql = src[i_lista:src.find('"""', i_lista)]
    tot_sql = src[i_tot:src.find('"""', i_tot)]
    assert _STAT_VIVO_PLACEHOLDER in lista_sql, (
        "la lista de facturas dejó de usar el criterio de stat compartido"
    )
    assert _STAT_VIVO_PLACEHOLDER in tot_sql, (
        "los totales del pie deben usar EL MISMO criterio de stat que la "
        "lista — sino 'Totales (N facturas)' suma facturas que no están en "
        "la tabla y el último ACUM deja de cerrar con el pie"
    )
    assert _BACKFILL_WHERE in tot_sql, "totales del pie sin filtro backfill"


def test_criterio_de_stat_deja_afuera_totalizadas_y_anuladas():
    """El criterio compartido es una LISTA BLANCA: T y X quedan afuera.

    Las dos exclusiones tienen dueña y fecha: las totalizadas (stat T) desde
    el 11/06/2026, las ANULADAS (stat X) desde el 13/08/2026 — *"las facturas
    anuladas en Asinfo reflejan aún en los estados de cuenta... no deberíamos
    reflejar anuladas en ningún lado"*. Es lista blanca a propósito: un stat
    nuevo NO se muestra hasta que alguien lo agregue a mano.
    """
    from modules.informes import queries as iq
    criterio = iq._SQL_ESTADO_CUENTA_STAT_VIVO
    assert "IN ('Z','A','',' ')" in criterio.replace(" IN (", " IN ("), (
        "el criterio dejó de ser la lista blanca canónica de cartera"
    )
    for muerto in ("'T'", "'X'"):
        assert muerto not in criterio.split("IN (")[1], (
            f"stat {muerto} volvió a entrar al estado de cuenta"
        )


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
    """Los X se listan tachados pero no cuentan en 'Total (N cheques)'.

    TMT 2026-07-09: el cuerpo Facturas+Cheques se movió al partial compartido
    `_estado_cuenta_impreso.html` (commit 42b7fd3); el contador nch.activos
    vive ahí, no en estado_cuenta.html.
    """
    tpl = (_TPL.resolve().parent / "_estado_cuenta_impreso.html").read_text()
    assert "nch.activos" in tpl, "falta el contador de cheques no-anulados"
    assert "anulado" in tpl, "falta la leyenda 'anulado' en los cheques X"
    assert "Total ({{ nch.activos }}" in tpl, (
        "el pie de cheques debe contar solo los no-anulados"
    )
    # el contador viejo (todas las filas) no debe volver
    assert "Total ({{ data.cheques | length }}" not in tpl
