"""Smoke test de scripts/sync_dbase_actual.py — sin DB real.

Ejercita los code paths principales con mocks. No mide cobertura, solo
verifica que el script importa, parsea args, y que los helpers de
coerción funcionan como esperamos en los pre-mortem cases (encoding,
fechas TEXT, importes con coma, stat legacy remap).

Correr: python scripts/_test_sync_dbase_actual.py
"""

from __future__ import annotations

import sys
import tempfile
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import import_dbf  # noqa: E402
import sync_dbase_actual  # noqa: E402

FAILS = []


def check(cond, msg):
    if cond:
        print(f"  ✓ {msg}")
    else:
        print(f"  ✗ {msg}")
        FAILS.append(msg)


# ─── Coerciones robustas ──────────────────────────────────────────────────


def test_date_robusto():
    print("\n[test] _date_robusto — pre-mortem 2f")
    check(import_dbf._date_robusto(date(2026, 5, 20)) == date(2026, 5, 20), "datetime.date pass-through")
    check(import_dbf._date_robusto("20/05/2026") == date(2026, 5, 20), "DD/MM/YYYY string")
    check(import_dbf._date_robusto("2026-05-20") == date(2026, 5, 20), "YYYY-MM-DD string")
    check(import_dbf._date_robusto("20-05-2026") == date(2026, 5, 20), "DD-MM-YYYY string")
    check(import_dbf._date_robusto("20/05/26") == date(2026, 5, 20), "DD/MM/YY string (corto)")
    check(import_dbf._date_robusto("") is None, "string vacío")
    check(import_dbf._date_robusto(None) is None, "None")
    check(import_dbf._date_robusto("#REF!") is None, "Excel ref error")
    check(import_dbf._date_robusto("garbage") is None, "string no parseable")


def test_num_robusto():
    print("\n[test] _num_robusto — pre-mortem 2g")
    check(import_dbf._num_robusto(1234.56) == 1234.56, "float pass-through")
    check(import_dbf._num_robusto(0) == 0, "0")
    check(import_dbf._num_robusto("1234.56") == 1234.56, "ISO decimal")
    check(import_dbf._num_robusto("1.234,56") == 1234.56, "es-EC (coma decimal)")
    check(import_dbf._num_robusto("1234,56") == 1234.56, "coma decimal sin miles")
    check(import_dbf._num_robusto("(123.45)") == -123.45, "paréntesis = negativo")
    check(import_dbf._num_robusto("") is None, "string vacío")
    check(import_dbf._num_robusto(None) is None, "None")
    check(import_dbf._num_robusto("garbage", default=0) == 0, "default si no parseable")


def test_stat_legacy_remap():
    print("\n[test] _STAT_LEGACY_MAP + _remap_stat — pre-mortem 2a")
    # CHEQUE: V→B, Y→None, *→None
    check(import_dbf._remap_stat("V", import_dbf._STAT_LEGACY_MAP_CHEQUE) == "B", "cheque stat=V → B")
    check(
        import_dbf._remap_stat("W", import_dbf._STAT_LEGACY_MAP_CHEQUE) == "B",
        "cheque stat=W → B (confirmado dueña 2026-05-21)",
    )
    check(
        import_dbf._remap_stat("Y", import_dbf._STAT_LEGACY_MAP_CHEQUE) is None, "cheque stat=Y → None (skip)"
    )
    check(import_dbf._remap_stat("*", import_dbf._STAT_LEGACY_MAP_CHEQUE) is None, "cheque stat=* → None")
    check(
        import_dbf._remap_stat("B", import_dbf._STAT_LEGACY_MAP_CHEQUE) == "B",
        "cheque stat=B (válido) pass-through",
    )
    check(
        import_dbf._remap_stat("Z", import_dbf._STAT_LEGACY_MAP_CHEQUE) == "Z",
        "cheque stat=Z (cartera moderno) pass-through",
    )
    # FACTURA: V→A
    check(import_dbf._remap_stat("V", import_dbf._STAT_LEGACY_MAP_FACTURA) == "A", "factura stat=V → A")
    # Genérico
    check(import_dbf._remap_stat("Y", import_dbf._STAT_LEGACY_MAP_GENERIC) is None, "genérico stat=Y → None")
    # None / "" no rompen
    check(import_dbf._remap_stat(None, import_dbf._STAT_LEGACY_MAP_CHEQUE) is None, "None stat → None")


def test_mappers_skip_legacy():
    print("\n[test] mappers retornan None si stat legacy → skipear")
    # _map_cheque con stat='Y' debería devolver None.
    rec_y = {"STAT": "Y", "FECHA": date(2026, 5, 20), "IMPORTE": 100}
    out = import_dbf._map_cheque(rec_y)
    check(out is None, "_map_cheque con stat=Y devuelve None")
    # _map_cheque con stat='V' debería devolver dict con stat='B'.
    rec_v = {"STAT": "V", "FECHA": date(2026, 5, 20), "IMPORTE": 100, "CLIENTE": "BED"}
    out = import_dbf._map_cheque(rec_v)
    check(out is not None and out["stat"] == "B", "_map_cheque con stat=V remapea a B")
    # _map_factura con stat='Y'
    rec_y_f = {"STAT": "Y", "FECHA": date(2026, 5, 20), "IMPORTE": 200}
    out = import_dbf._map_factura(rec_y_f)
    check(out is None, "_map_factura con stat=Y devuelve None")


def test_mes_a_num():
    print("\n[test] _mes_a_num — pre-mortem 2e (typos históricos)")
    check(import_dbf._mes_a_num("Jan") == 1, "EN: Jan → 1")
    check(import_dbf._mes_a_num("Ene") == 1, "ES: Ene → 1")
    check(import_dbf._mes_a_num("Apr") == 4, "EN: Apr → 4")
    check(import_dbf._mes_a_num("Abr") == 4, "ES: Abr → 4")
    check(import_dbf._mes_a_num("Dec") == 12, "EN: Dec → 12")
    check(import_dbf._mes_a_num("Dic") == 12, "ES: Dic → 12")
    check(import_dbf._mes_a_num("JÔL") == 7, "typo histórico JÔL → 7")
    check(import_dbf._mes_a_num("EDT") == 3, "typo histórico EDT → 3")
    check(import_dbf._mes_a_num(None) is None, "None")
    check(import_dbf._mes_a_num("XYZ") is None, "string desconocido")


# ─── sync_dbase_actual helpers ────────────────────────────────────────────


def test_source_dir_check():
    print("\n[test] verificar_source_dir")
    with tempfile.TemporaryDirectory() as tmp:
        ok, msg = sync_dbase_actual.verificar_source_dir(Path(tmp))
        check(not ok and "no contiene" in msg.lower(), "directorio vacío → error")
        # Crear un DBF dummy
        (Path(tmp) / "DUMMY.DBF").write_bytes(b"\x03")
        ok, msg = sync_dbase_actual.verificar_source_dir(Path(tmp))
        check(ok and "1 archivos" in msg, "directorio con 1 DBF → OK")

    fake = Path("/no/such/path/__nope__")
    ok, msg = sync_dbase_actual.verificar_source_dir(fake)
    check(not ok and "no existe" in msg.lower(), "directorio inexistente → error")


def test_argv_parsing():
    print("\n[test] CLI args parsing")
    # Verificar que el ArgumentParser acepta los flags documentados.
    ap = __import__("argparse").ArgumentParser()
    ap.add_argument("--source", default="x")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--only", default="")
    ap.add_argument("--encoding", default=None)
    ap.add_argument("--skip-backup", action="store_true")
    ap.add_argument("--skip-pre-checks", action="store_true")
    ap.add_argument("--skip-post-checks", action="store_true")
    ap.add_argument("--skip-backfills", action="store_true")
    args = ap.parse_args(
        [
            "--source",
            "/tmp",
            "--dry-run",
            "--only",
            "FACTURAS.DBF",
            "--encoding",
            "cp1252",
            "--skip-backup",
            "--skip-pre-checks",
            "--skip-post-checks",
            "--skip-backfills",
        ]
    )
    check(args.source == "/tmp", "--source")
    check(args.dry_run is True, "--dry-run")
    check(args.only == "FACTURAS.DBF", "--only")
    check(args.encoding == "cp1252", "--encoding")
    check(args.skip_backup, "--skip-backup")
    check(args.skip_pre_checks, "--skip-pre-checks")
    check(args.skip_post_checks, "--skip-post-checks")
    check(args.skip_backfills, "--skip-backfills")


def test_table_map_completeness():
    print("\n[test] TABLE_MAP cubre las tablas críticas")
    expected = {
        "FACTURAS.DBF",
        "CHEQUES.DBF",
        "POSDAT.DBF",
        "CAJA.DBF",
        "DOLARES.DBF",
        "ACTIVOS.DBF",
        "HISTORIA.DBF",
        "INICIALE.DBF",
        "COMPRAS.DBF",
        "FLUJO.DBF",
        "PICHINCH.DBF",
        "INTER.DBF",
        "XGAST.DBF",
        "RETIROS.DBF",
        "TINTO.DBF",
    }
    actual = set(import_dbf.TABLE_MAP.keys())
    missing = expected - actual
    check(
        not missing,
        f"todos los DBFs críticos en TABLE_MAP (faltarían: {sorted(missing) if missing else 'none'})",
    )


def test_encoding_constants():
    print("\n[test] encoding constants disponibles")
    check(import_dbf._DEFAULT_ENCODING == "cp850", "default encoding es cp850")
    check("cp1252" in import_dbf._TRY_ENCODINGS, "cp1252 en TRY_ENCODINGS")
    check("latin-1" in import_dbf._TRY_ENCODINGS, "latin-1 en TRY_ENCODINGS")
    check("utf-8" in import_dbf._TRY_ENCODINGS, "utf-8 en TRY_ENCODINGS")


def test_min_migration_pinned():
    print("\n[test] MIN_MIGRATION pinned al último deploy")
    check(
        sync_dbase_actual.MIN_MIGRATION >= "0039",
        f"MIN_MIGRATION={sync_dbase_actual.MIN_MIGRATION} ≥ 0039 (incluye conciliacion_manual_log)",
    )


# ─── RUN ──────────────────────────────────────────────────────────────────


def main():
    print("=" * 60)
    print(" SMOKE TEST: sync_dbase_actual")
    print("=" * 60)

    test_date_robusto()
    test_num_robusto()
    test_stat_legacy_remap()
    test_mappers_skip_legacy()
    test_mes_a_num()
    test_source_dir_check()
    test_argv_parsing()
    test_table_map_completeness()
    test_encoding_constants()
    test_min_migration_pinned()

    print("\n" + "=" * 60)
    if FAILS:
        print(f"  ✗ {len(FAILS)} FALLO(S)")
        for f in FAILS:
            print(f"    - {f}")
        sys.exit(1)
    print("  ✓ TODO OK")
    sys.exit(0)


if __name__ == "__main__":
    main()
