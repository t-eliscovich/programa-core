"""Contract tests para el toggle UI 'Incluir Asinfo backfill' y la
coherencia entre balance LIVE y listados de cartera (TOTF / TOTC).

TMT 2026-06-10 — guard de regresión:
    - El listado de facturas debe excluir filas `usuario_crea='asinfo-backfill'`
      por default (toggle OFF) para coincidir con `informes.totf()`.
    - El listado de cheques: mismo patrón, debe coincidir con `informes.totc()`.
    - Cuando `incluir_backfill=True` se pasa, las queries deben devolver las
      filas backfill también.

Estos tests inspeccionan el source de las funciones (consistencia con la
constante `NO_BACKFILL_WHERE`) — los tests integration que requieren DB
viva quedan para CI cuando haya Postgres disponible.
"""
from __future__ import annotations

import inspect

# ---------------------------------------------------------------------------
# /facturas/buscar respeta el toggle
# ---------------------------------------------------------------------------


def test_facturas_buscar_acepta_param_incluir_backfill():
    """`modules.facturas.queries.buscar()` debe tener un param
    `incluir_backfill: bool = False`. Si lo borran, el listado vuelve a
    sumar facturas backfill y la lista se desincroniza del balance.
    """
    from modules.facturas import queries

    sig = inspect.signature(queries.buscar)
    assert "incluir_backfill" in sig.parameters, (
        "queries.buscar() perdió el param 'incluir_backfill'. "
        "Bug 2026-06-10 reabierto: el listado vuelve a incluir backfill por "
        "default y se desincroniza del balance TOTF."
    )
    default = sig.parameters["incluir_backfill"].default
    assert default is False, (
        f"queries.buscar() incluir_backfill default = {default!r}. "
        f"Debe ser False — convención canónica: por default OFF, lista "
        f"coincide con balance."
    )


def test_facturas_contar_filtrado_acepta_param_incluir_backfill():
    """`contar_filtrado()` también necesita el param (para el header
    'Mostrando X-Y de Z' coincida con balance)."""
    from modules.facturas import queries

    sig = inspect.signature(queries.contar_filtrado)
    assert "incluir_backfill" in sig.parameters, (
        "contar_filtrado() perdió 'incluir_backfill'."
    )


def test_facturas_buscar_filtra_backfill_en_sql():
    """El source de buscar() debe incluir el filtro de backfill (gated por
    el param). Si el filtro desaparece del SQL, el toggle no hace nada."""
    from modules.facturas import queries

    src = inspect.getsource(queries.buscar)
    assert "asinfo-backfill" in src, (
        "queries.buscar() perdió el filtro 'asinfo-backfill' en su SQL."
    )
    assert "incluir_backfill" in src, (
        "queries.buscar() perdió la condición de toggle en su SQL."
    )


def test_facturas_contar_filtrado_filtra_backfill_en_sql():
    from modules.facturas import queries

    src = inspect.getsource(queries.contar_filtrado)
    assert "asinfo-backfill" in src
    assert "incluir_backfill" in src


# ---------------------------------------------------------------------------
# /cheques/buscar respeta el toggle (defensivo: aún si no hay endpoint Asinfo
# de cheques hoy, el patrón queda en place para coherencia con TOTC)
# ---------------------------------------------------------------------------


def test_cheques_buscar_acepta_param_incluir_backfill():
    from modules.cheques import queries

    sig = inspect.signature(queries.buscar)
    assert "incluir_backfill" in sig.parameters, (
        "cheques.queries.buscar() perdió 'incluir_backfill'."
    )
    assert sig.parameters["incluir_backfill"].default is False


def test_cheques_buscar_filtra_backfill_en_sql():
    from modules.cheques import queries

    src = inspect.getsource(queries.buscar)
    assert "asinfo-backfill" in src
    assert "incluir_backfill" in src


# ---------------------------------------------------------------------------
# Endpoint /admin/health/cartera-coherence existe
# ---------------------------------------------------------------------------


def test_endpoint_cartera_coherence_existe():
    """El blueprint health_audit debe exponer la función cartera_coherence()."""
    from modules.admin_dbase import health_audit_view as hav

    assert hasattr(hav, "cartera_coherence"), (
        "Función cartera_coherence() falta en health_audit_view.py"
    )


def test_endpoint_health_all_incluye_cartera_coherence():
    """`/admin/health/all` debe consolidar las tres auditorías."""
    from modules.admin_dbase import health_audit_view as hav

    src = inspect.getsource(hav.health_all)
    assert "cartera_coherence" in src, (
        "/admin/health/all no llama a cartera_coherence()."
    )


# ---------------------------------------------------------------------------
# Smoke: la constante canónica del marker no cambió
# ---------------------------------------------------------------------------


def test_marker_canonico_asinfo_backfill_unchanged():
    """El marker canónico es 'asinfo-backfill' (con guión). Si alguien lo
    renombra, todos los filtros + el endpoint marcar-asinfo-hoy + el trigger
    DB + estos tests fallan en cascada.
    """
    from modules.informes import queries as iq

    assert "asinfo-backfill" in iq.NO_BACKFILL_WHERE, (
        f"NO_BACKFILL_WHERE perdió el marker 'asinfo-backfill'. "
        f"Valor actual: {iq.NO_BACKFILL_WHERE!r}"
    )
