"""Contract tests — convención FINAL de cartera Asinfo (decisión dueña, TMT 2026-06-10).

Historia del día (3 flips):
    1. mañana: filtro asinfo-backfill en TOTF/TOTC/etc (bug utilidad inflada)
    2. 18:10 revert: balance 100% live, sin filtros
    3. noche (FINAL, palabra de la dueña): "solo si alguien aprieta CARGAR al
       programa cuentan; si no, pertenecen a la lista Asinfo sin cargar en PC.
       Si se hace una carga de dBase, eso gana por sobre todo."
       (y: "¿si no para qué existe el botón cargar?")

Convención canónica resultante (mig 0087):
    - 'asinfo-carga'    (botón Cargar)         → CUENTA en cartera/balance.
    - 'asinfo-backfill' (automático/histórico) → NO cuenta. Solo auditoría.
    - 'dbf-import'      (sync dBase)           → cuenta, y GANA: el sync
      absorbe la copia asinfo si el DBF trae la misma factura (import_dbf).
    - Lista (vista=cartera) y balance aplican el MISMO filtro → el health
      /admin/health/cartera-coherence sigue exigiendo lista == balance.

Estos tests inspeccionan el source (no requieren DB).
"""
from __future__ import annotations

import inspect

_BACKFILL_WHERE = "<> 'asinfo-backfill'"


def test_totf_filtra_solo_backfill_automatico():
    """totf() excluye 'asinfo-backfill' pero NO 'asinfo-carga'."""
    from modules.informes import queries as iq
    src = inspect.getsource(iq.totf)
    assert _BACKFILL_WHERE in src, "totf() perdió el filtro asinfo-backfill"
    sql = src[src.find("SELECT COALESCE"):]
    assert "asinfo-carga" not in sql, (
        "totf() no debe excluir asinfo-carga (el botón Cargar cuenta)"
    )


def test_totc_filtra_backfill():
    from modules.informes import queries as iq
    assert _BACKFILL_WHERE in inspect.getsource(iq.totc)


def test_anticipos_filtra_backfill():
    from modules.informes import queries as iq
    assert _BACKFILL_WHERE in inspect.getsource(iq.anticipos)


def test_retiros_filtran_backfill():
    from modules.informes import queries as iq
    assert _BACKFILL_WHERE in inspect.getsource(iq.retiros_total_mes_actual)
    assert _BACKFILL_WHERE in inspect.getsource(iq.retiros_total_anual)


def test_facturas_buscar_cartera_filtra_backfill():
    """Lista vista=cartera aplica el mismo filtro (lista == balance, sin toggle)."""
    from modules.facturas import queries as fq
    sig = inspect.signature(fq.buscar)
    assert "incluir_backfill" not in sig.parameters, "no reintroducir el toggle"
    src = inspect.getsource(fq.buscar)
    assert "f.usuario_crea, '') <> 'asinfo-backfill'" in src


def test_facturas_contar_filtrado_cartera_filtra_backfill():
    from modules.facturas import queries as fq
    sig = inspect.signature(fq.contar_filtrado)
    assert "incluir_backfill" not in sig.parameters
    src = inspect.getsource(fq.contar_filtrado)
    assert "f.usuario_crea, '') <> 'asinfo-backfill'" in src


def test_cheques_buscar_no_filtra_asinfo_backfill():
    """Cheques no tienen carga Asinfo — la lista no filtra; totc() lleva
    el guard defensivo barato."""
    from modules.cheques import queries as cq
    sig = inspect.signature(cq.buscar)
    assert "incluir_backfill" not in sig.parameters
    src = inspect.getsource(cq.buscar)
    assert "c.usuario_crea, '') <> 'asinfo-backfill'" not in src


def test_carga_manual_marca_asinfo_carga():
    """Los endpoints del botón Cargar marcan 'asinfo-carga', NO backfill."""
    from modules.facturas import views as fv
    for fn in (fv.cargar_desde_asinfo, fv.cargar_desde_asinfo_bulk):
        src = inspect.getsource(fn)
        assert "asinfo-carga" in src, f"{fn.__name__} no marca asinfo-carga"
        assert "usuario='asinfo-backfill'" not in src


def test_sync_preserva_asinfo_carga_y_dbase_gana():
    """delete_where preserva ambos markers; import_one absorbe duplicados."""
    import importlib.util
    from pathlib import Path
    path = Path(__file__).resolve().parents[1] / "scripts" / "import_dbf.py"
    spec = importlib.util.spec_from_file_location("_imp_dbf_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    where_sql, _ = mod.TABLE_MAP["FACTURAS.DBF"]["delete_where"]
    assert "asinfo-carga" in where_sql, (
        "el sync borraría las facturas cargadas con el botón Cargar"
    )
    src = inspect.getsource(mod.import_one)
    assert "dBase gana" in src or "dBase GANA" in src, (
        "falta el dedup 'dBase gana' post-insert en import_one"
    )


def test_endpoint_cartera_coherence_existe():
    from modules.admin_dbase import health_audit_view as hav
    assert hasattr(hav, "cartera_coherence")


def test_endpoint_health_all_incluye_cartera_coherence():
    from modules.admin_dbase import health_audit_view as hav
    src = inspect.getsource(hav.health_all)
    assert "cartera_coherence" in src


def test_health_whitelist_incluye_asinfo_carga():
    from modules.admin_dbase import health_audit_view as hav
    assert "asinfo-carga" in hav._USUARIOS_CONOCIDOS


def test_marker_canonico_asinfo_backfill_unchanged():
    """NO_BACKFILL_WHERE sigue para las queries del MES en curso."""
    from modules.informes import queries as iq
    assert "asinfo-backfill" in iq.NO_BACKFILL_WHERE
