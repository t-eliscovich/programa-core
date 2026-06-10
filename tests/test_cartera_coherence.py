"""Contract tests post-revert toggle (TMT 2026-06-10).

Convención canónica:
    - El balance es 100% live. Listado de facturas/cheques y balance suman
      las MISMAS filas (sin filtros backfill).
    - `usuario_crea='asinfo-backfill'` queda como marker informativo, NO
      como filtro contable.
    - El endpoint `/admin/health/cartera-coherence` valida que
      `informes.totf() == facturas.contar_filtrado().total_saldo` (modulo
      redondeo) y `informes.totc() == SUM` de cheques en cartera_total.

Estos tests inspeccionan el source (no requieren DB) y aseguran que NO
vuelvan a aparecer filtros `asinfo-backfill` en queries del balance.
"""
from __future__ import annotations

import inspect


# ---------------------------------------------------------------------------
# Las queries del balance NO deben filtrar asinfo-backfill (convención live)
# ---------------------------------------------------------------------------


def test_totf_no_filtra_asinfo_backfill():
    """`totf()` debe sumar TODAS las facturas vivas, incluyendo backfill.

    Si alguien re-introduce el filtro, el balance subestima cartera por las
    facturas Asinfo cargadas en el mes y no coincide con dBase.
    """
    from modules.informes import queries as iq
    src = inspect.getsource(iq.totf)
    assert "asinfo-backfill" not in src, (
        "totf() introdujo filtro 'asinfo-backfill'. "
        "Convención live (TMT 2026-06-10): facturas Asinfo cuentan SIEMPRE "
        "en cartera live. El marker queda informativo, no de filtro."
    )


def test_totc_no_filtra_asinfo_backfill():
    from modules.informes import queries as iq
    src = inspect.getsource(iq.totc)
    assert "asinfo-backfill" not in src, (
        "totc() introdujo filtro 'asinfo-backfill'. Mismo motivo que totf()."
    )


def test_anticipos_no_filtra_asinfo_backfill():
    from modules.informes import queries as iq
    src = inspect.getsource(iq.anticipos)
    assert "asinfo-backfill" not in src, (
        "anticipos() introdujo filtro 'asinfo-backfill'."
    )


def test_facturas_buscar_no_filtra_asinfo_backfill():
    """`facturas.buscar()` ya no acepta el param `incluir_backfill`.

    Post revert toggle (TMT 2026-06-10): el balance es 100% live → lista y
    balance siempre coinciden. Si alguien re-introduce el toggle, los
    listados se desincronizan del balance.
    """
    from modules.facturas import queries as fq
    sig = inspect.signature(fq.buscar)
    assert "incluir_backfill" not in sig.parameters, (
        "facturas.buscar() volvió a tener 'incluir_backfill'. "
        "Toggle revertido — balance es live, lista debe coincidir sin filtros."
    )
    src = inspect.getsource(fq.buscar)
    assert "asinfo-backfill" not in src, (
        "facturas.buscar() tiene filtro asinfo-backfill — debe sumar todo."
    )


def test_facturas_contar_filtrado_no_filtra_asinfo_backfill():
    from modules.facturas import queries as fq
    sig = inspect.signature(fq.contar_filtrado)
    assert "incluir_backfill" not in sig.parameters
    src = inspect.getsource(fq.contar_filtrado)
    assert "asinfo-backfill" not in src


def test_cheques_buscar_no_filtra_asinfo_backfill():
    from modules.cheques import queries as cq
    sig = inspect.signature(cq.buscar)
    assert "incluir_backfill" not in sig.parameters
    src = inspect.getsource(cq.buscar)
    assert "asinfo-backfill" not in src


# ---------------------------------------------------------------------------
# Endpoint cartera-coherence existe (Capa 4 de protección)
# ---------------------------------------------------------------------------


def test_endpoint_cartera_coherence_existe():
    from modules.admin_dbase import health_audit_view as hav
    assert hasattr(hav, "cartera_coherence")


def test_endpoint_health_all_incluye_cartera_coherence():
    from modules.admin_dbase import health_audit_view as hav
    src = inspect.getsource(hav.health_all)
    assert "cartera_coherence" in src


# ---------------------------------------------------------------------------
# Constante NO_BACKFILL_WHERE preservada (queda para compras_mes_corriente
# y otras queries del MES en curso que SÍ filtran backfill — para no
# double-contar el mes con los backfills históricos)
# ---------------------------------------------------------------------------


def test_marker_canonico_asinfo_backfill_unchanged():
    """El marker canónico es 'asinfo-backfill'. Algunas queries SIGUEN
    filtrándolo (ventas/compras del MES en curso, snapshot mensual) — eso
    es correcto para no double-contar histórico vs movimientos del mes.
    Solo TOTF/TOTC/anticipos/retiros LIVE no filtran (convención balance live).
    """
    from modules.informes import queries as iq
    assert "asinfo-backfill" in iq.NO_BACKFILL_WHERE, (
        "NO_BACKFILL_WHERE perdió 'asinfo-backfill'."
    )
