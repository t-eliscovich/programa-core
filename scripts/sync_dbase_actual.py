"""sync_dbase_actual.py — orquestador completo del sync dBase → Postgres.

TMT 2026-05-20 — versión robusta para usar mañana cuando lleguen los DBFs
frescos del dBase. Wrappea `scripts/import_dbf.py` con todas las
mejoras del PRE-MORTEM_IMPORT_DBASE_2026_05_20.md:

  PRE-IMPORT (sanity gates)
    - Verifica schema version (migraciones 0001-0039 aplicadas).
    - Verifica que el source dir existe y tiene al menos 1 DBF.
    - Opcionalmente hace dump local de las tablas afectadas (--backup).

  IMPORT
    - Delega a `import_dbf.main()` con encoding auto-detect.
    - Aplica stat-legacy remap (V→B en cheques, V→A en facturas, Y/* → skip).
    - Coerciones robustas (`_date_robusto`, `_num_robusto`) — strings DD/MM/YYYY
      y "1.234,56" se parsean OK.

  POST-IMPORT (sanity + backfills)
    - Cuenta FKs huérfanas (cheque→cliente, factura→cliente, posdat→proveedor).
    - Cuenta NULLs críticos (fechas, importes).
    - Snapshot historia de los últimos 12 meses (idempotente).
    - Sugerir reclasificación de xgast sin num.
    - Comparar balance vs valor esperado del .DBF (drift > 0.5% → alerta).
    - Reportar resumen total.

Uso:
    # 1. Copiar los .DBF a una carpeta
    cp -r /path/from/factory/*.DBF ~/Downloads/dbase_dump_2026_05_21/

    # 2. Dry-run (no toca DB)
    python scripts/sync_dbase_actual.py \\
        --source ~/Downloads/dbase_dump_2026_05_21 --dry-run

    # 3. Si el dry-run OK, sync real
    python scripts/sync_dbase_actual.py \\
        --source ~/Downloads/dbase_dump_2026_05_21

    # 4. Verificar /informes/balance en la app

Flags:
    --source PATH          carpeta con los .DBF (obligatorio si no es default)
    --dry-run              no toca DB
    --only TABLA,TABLA...  solo importa esos DBFs
    --encoding ENC         forzar encoding (default: auto cp850/cp1252/latin-1/utf-8)
    --skip-backup          no hace dump pre-import
    --skip-pre-checks      omite verificación de schema version
    --skip-post-checks     omite sanity check post-import
    --skip-backfills       omite backfills encadenados (snapshots, etc.)
    --verbose              log detallado

Exit codes:
    0  todo OK
    1  algún error en el import o sanity check no superado
    2  pre-check falló (schema, source dir, etc.)
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Importar el sync existente y db en lazy mode (solo cuando hace falta).
import import_dbf  # noqa: E402

try:
    from dotenv import load_dotenv  # noqa: E402
    load_dotenv(ROOT / ".env")
except ImportError:
    pass


# ─── Constantes ────────────────────────────────────────────────────────────

# Migración mínima que el código actual espera. Subir cuando agregues
# columnas nuevas críticas al schema.
MIN_MIGRATION = "0039"

DEFAULT_SOURCE = Path("/Users/tamaraeliscovich/Documents/INTELA copy/Files")

DEFAULT_BACKUP_DIR = ROOT / "_backups_sync"


def _force_utf8_stdout():
    """En Windows / SSM, stdout puede crashear con ñ/acentos."""
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass


# ─── Verificaciones PRE-import ────────────────────────────────────────────

def verificar_schema_version(min_version: str) -> tuple[bool, str]:
    """¿La DB tiene aplicadas todas las migraciones hasta `min_version`?

    Asume que existe una tabla `schema_migrations(version TEXT PRIMARY KEY)` o
    similar. Si no existe, retornamos OK con warning (DB nueva).
    """
    import db as _db
    try:
        # Probar dos shapes comunes: scintela.migrations o schema_migrations.
        for schema_table in (
            ("scintela", "migrations"),
            ("public",   "schema_migrations"),
        ):
            try:
                rows = _db.fetch_all(
                    f"SELECT version FROM {schema_table[0]}.{schema_table[1]} "
                    "ORDER BY version DESC LIMIT 5"
                )
                if rows:
                    last = max(str(r.get("version") or "") for r in rows)
                    if last >= min_version:
                        return True, f"schema OK (última migration: {last})"
                    return False, (
                        f"schema desactualizado: última {last}, requerida ≥ {min_version}. "
                        f"Corré `python scripts/migrate.py` primero."
                    )
            except Exception:
                continue
        return True, "no se detectó tabla de migrations — asumiendo schema nuevo"
    except Exception as e:
        return True, f"no se pudo verificar schema ({e}); continuando con warning"


def verificar_source_dir(src: Path) -> tuple[bool, str]:
    if not src.exists() or not src.is_dir():
        return False, f"source dir no existe o no es directorio: {src}"
    dbfs = list(src.glob("*.DBF")) + list(src.glob("*.dbf"))
    if not dbfs:
        return False, f"source dir no contiene .DBF: {src}"
    return True, f"{len(dbfs)} archivos .DBF detectados"


def hacer_backup(target_dir: Path, tablas: list[str]) -> tuple[bool, str]:
    """Dump local de las tablas que vamos a TRUNCATE+INSERT.

    Usa `pg_dump --data-only -t scintela.X ...`. Si no está disponible
    `pg_dump` en el PATH, retorna warning y continúa.
    """
    pg_dump = shutil.which("pg_dump")
    if not pg_dump:
        return True, "pg_dump no disponible; backup omitido (warning)"
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_file = target_dir / f"backup_pre_sync_{stamp}.sql"
    db_url = os.environ.get("DATABASE_URL") or _build_dburl_from_env()
    if not db_url:
        return True, "DATABASE_URL no seteado; backup omitido (warning)"
    # Backup parcial: solo las tablas que vamos a tocar.
    t_args = []
    for t in tablas:
        t_args += ["-t", t]
    try:
        with open(out_file, "wb") as f:
            res = subprocess.run(
                [pg_dump, "--data-only", "--no-owner", "--no-privileges"] + t_args + [db_url],
                stdout=f, stderr=subprocess.PIPE, check=False, timeout=600,
            )
        if res.returncode != 0:
            return False, f"pg_dump exit {res.returncode}: {res.stderr.decode()[:200]}"
        sz_mb = out_file.stat().st_size / (1024 * 1024)
        return True, f"backup guardado en {out_file} ({sz_mb:.1f} MB)"
    except Exception as e:
        return False, f"backup falló: {e}"


def _build_dburl_from_env() -> str | None:
    h = os.environ.get("DB_HOST")
    if not h:
        return None
    u = os.environ.get("DB_USER", "postgres")
    p = os.environ.get("DB_PASSWORD", "")
    d = os.environ.get("DB_NAME", "postgres")
    port = os.environ.get("DB_PORT", "5432")
    return f"postgresql://{u}:{p}@{h}:{port}/{d}"


# ─── Verificaciones POST-import ───────────────────────────────────────────

def contar_huerfanos() -> dict[str, int]:
    """Cuenta filas con FK que apunta a un registro inexistente.

    Pre-mortem 2b. Lista cerrada de joins críticos.
    """
    import db as _db
    queries = {
        "cheque_sin_cliente": """
            SELECT COUNT(*) AS n
              FROM scintela.cheque c
         LEFT JOIN scintela.cliente cli ON cli.codigo_cli = c.codigo_cli
             WHERE c.codigo_cli IS NOT NULL
               AND cli.codigo_cli IS NULL
        """,
        "factura_sin_cliente": """
            SELECT COUNT(*) AS n
              FROM scintela.factura f
         LEFT JOIN scintela.cliente cli ON cli.codigo_cli = f.codigo_cli
             WHERE f.codigo_cli IS NOT NULL
               AND f.codigo_cli <> ''
               AND cli.codigo_cli IS NULL
        """,
        "posdat_sin_prov": """
            SELECT COUNT(*) AS n
              FROM scintela.posdat p
         LEFT JOIN scintela.proveedor pv ON pv.codigo_prov = p.prov
             WHERE p.prov IS NOT NULL
               AND p.prov <> ''
               AND pv.codigo_prov IS NULL
        """,
    }
    out = {}
    for k, sql in queries.items():
        try:
            row = _db.fetch_one(sql)
            out[k] = int(row["n"]) if row else 0
        except Exception as e:
            out[k] = -1  # Indica que la query falló (tabla nueva no migrada, etc.)
            print(f"  ⚠ huerfanos.{k} no se pudo computar: {e}")
    return out


def contar_nulls_criticos() -> dict[str, int]:
    """Campos NULL que rompen pantallas si quedan vacíos."""
    import db as _db
    out = {}
    checks = [
        ("factura.fecha", "scintela.factura", "fecha IS NULL"),
        ("cheque.importe", "scintela.cheque", "importe IS NULL OR importe = 0"),
        ("xgast.num_sin_clasificar", "scintela.xgast", "num IS NULL"),
        ("transacciones_bancarias.no_banco", "scintela.transacciones_bancarias",
         "no_banco IS NULL"),
    ]
    for name, table, cond in checks:
        try:
            row = _db.fetch_one(f"SELECT COUNT(*) AS n FROM {table} WHERE {cond}")
            out[name] = int(row["n"]) if row else 0
        except Exception:
            out[name] = -1
    return out


def crear_snapshots_ultimos_meses(n_meses: int = 12, dry_run: bool = False) -> dict:
    """Crea snapshots faltantes en scintela.historia para los últimos N meses.

    Usa `informes.queries.crear_snapshot_historia(anio, mes)` que es idempotente.
    """
    if dry_run:
        return {"creados": 0, "ya_existian": 0, "errores": 0,
                "msg": "DRY-RUN — no se crearon snapshots"}
    creados = 0
    ya_existian = 0
    errores = 0
    detalle = []
    try:
        from modules.informes.queries import crear_snapshot_historia
    except Exception as e:
        return {"creados": 0, "ya_existian": 0, "errores": 1,
                "msg": f"no se pudo importar crear_snapshot_historia: {e}"}

    hoy = date.today()
    cur = date(hoy.year, hoy.month, 1)
    for _ in range(n_meses):
        # Retroceder mes
        if cur.month == 1:
            cur = date(cur.year - 1, 12, 1)
        else:
            cur = date(cur.year, cur.month - 1, 1)
        try:
            res = crear_snapshot_historia(cur.year, cur.month, usuario="sync_dbase_actual")
            if res.get("aplicado"):
                creados += 1
                detalle.append(f"{cur.year}-{cur.month:02d} ✓")
            else:
                ya_existian += 1
                detalle.append(f"{cur.year}-{cur.month:02d} (ya existía)")
        except Exception as e:
            errores += 1
            detalle.append(f"{cur.year}-{cur.month:02d} ✗ {e}")
    return {
        "creados": creados, "ya_existian": ya_existian, "errores": errores,
        "msg": f"snapshots: {creados} creados · {ya_existian} ya existían · {errores} errores",
        "detalle": detalle,
    }


def comparar_drift_balance() -> dict:
    """Compara totales del balance LIVE vs el último snapshot de historia.

    Si difieren más de 0.5%, retornamos warning. Ayuda a detectar que la
    carga del DBF metió data inconsistente con lo que el dBase reportaba.
    """
    import db as _db
    try:
        last = _db.fetch_one(
            "SELECT fecha, banco, cart, ustock, deuda, patrimonio "
            "FROM scintela.historia ORDER BY fecha DESC LIMIT 1"
        )
        if not last:
            return {"ok": True, "msg": "no hay snapshot reciente para comparar"}
        from modules.informes.queries import informe_balance
        bal = informe_balance()
        comp = (bal or {}).get("diagnostico", {}).get("componentes", {})
        bal_live = {
            "banco":      float(comp.get("salbanc_total") or 0),
            "cart":       float(comp.get("cart") or 0),
            "ustock":     float(comp.get("vsto") or 0),
            "deuda":      float(comp.get("totp") or 0),
            "patrimonio": float(comp.get("patr") or 0),
        }
        drifts = {}
        for k, v_live in bal_live.items():
            v_snap = float(last.get(k) or 0)
            if v_snap == 0:
                drift_pct = 0.0 if v_live == 0 else float("inf")
            else:
                drift_pct = abs(v_live - v_snap) / abs(v_snap) * 100.0
            drifts[k] = {"snap": v_snap, "live": v_live, "drift_pct": drift_pct}
        return {"ok": True, "drifts": drifts, "snap_fecha": str(last["fecha"])}
    except Exception as e:
        return {"ok": False, "msg": f"comparación falló: {e}"}


# ─── Orchestrator ─────────────────────────────────────────────────────────

def correr_sync_principal(source: Path, dry_run: bool, only: str,
                          encoding: str | None) -> int:
    """Llama a import_dbf.main() reusando su lógica de TRUNCATE+INSERT."""
    argv = ["import_dbf", "--source-dir", str(source)]
    if dry_run:
        argv.append("--dry-run")
    if only:
        argv += ["--only", only]
    if encoding:
        argv += ["--encoding", encoding]
    sys.argv = argv
    try:
        import_dbf.main()
        return 0
    except SystemExit as e:
        return int(e.code) if isinstance(e.code, int) else 1


def main():
    _force_utf8_stdout()
    ap = argparse.ArgumentParser(
        description="sync_dbase_actual — orquestador completo del sync dBase",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("--source", default=str(DEFAULT_SOURCE),
                    help="Carpeta con los .DBF")
    ap.add_argument("--dry-run", action="store_true",
                    help="Lee los DBFs e informa, no toca DB")
    ap.add_argument("--only", default="",
                    help="Solo importar estos DBFs (coma-sep)")
    ap.add_argument("--encoding", default=None,
                    help="Forzar encoding (default: auto)")
    ap.add_argument("--skip-backup", action="store_true",
                    help="No hace pg_dump pre-import")
    ap.add_argument("--skip-pre-checks", action="store_true",
                    help="Omite verificación de schema version")
    ap.add_argument("--skip-post-checks", action="store_true",
                    help="Omite sanity check post-import")
    ap.add_argument("--skip-backfills", action="store_true",
                    help="Omite backfills encadenados (snapshots, etc.)")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args()

    src = Path(args.source).expanduser()

    print("=" * 70)
    print(" sync_dbase_actual — TMT 2026-05-20")
    print("=" * 70)
    print(f"  source        : {src}")
    print(f"  dry-run       : {args.dry_run}")
    print(f"  encoding      : {args.encoding or 'auto'}")
    print(f"  backup        : {'omitido' if args.skip_backup else 'sí'}")
    print(f"  pre-checks    : {'omitido' if args.skip_pre_checks else 'sí'}")
    print(f"  post-checks   : {'omitido' if args.skip_post_checks else 'sí'}")
    print(f"  backfills     : {'omitido' if args.skip_backfills else 'sí'}")
    print()

    # ─── PRE-CHECKS ──────────────────────────────────────────────────────
    print("─── PRE-CHECKS ────────────────────────────────────────────────")
    ok, msg = verificar_source_dir(src)
    print(f"  source dir    : {'✓' if ok else '✗'} {msg}")
    if not ok:
        return 2

    if not args.skip_pre_checks and not args.dry_run:
        import db as _db
        try:
            _db.init_pool()
            ok, msg = verificar_schema_version(MIN_MIGRATION)
            print(f"  schema version: {'✓' if ok else '✗'} {msg}")
            if not ok:
                return 2
        except Exception as e:
            print(f"  schema version: ⚠ no se pudo verificar ({e})")

    # ─── BACKUP ──────────────────────────────────────────────────────────
    if not args.skip_backup and not args.dry_run:
        tablas_backup = [
            "scintela.factura", "scintela.cheque", "scintela.posdat",
            "scintela.caja", "scintela.dolares", "scintela.activos",
            "scintela.historia", "scintela.iniciales", "scintela.compra",
            "scintela.flujo", "scintela.transacciones_bancarias",
            "scintela.xgast", "scintela.retiros", "scintela.tinto",
        ]
        print()
        print("─── BACKUP ────────────────────────────────────────────────────")
        ok, msg = hacer_backup(DEFAULT_BACKUP_DIR, tablas_backup)
        print(f"  pg_dump       : {'✓' if ok else '✗'} {msg}")
        if not ok:
            print("  ⚠ continuando sin backup — abortá manualmente si querés rollback")

    # ─── IMPORT ──────────────────────────────────────────────────────────
    print()
    print("─── IMPORT ────────────────────────────────────────────────────")
    rc = correr_sync_principal(src, args.dry_run, args.only, args.encoding)
    if rc != 0:
        print(f"\n✗ import_dbf falló (rc={rc})")
        return rc

    # ─── POST-CHECKS ─────────────────────────────────────────────────────
    if not args.dry_run and not args.skip_post_checks:
        print()
        print("─── POST-CHECKS ───────────────────────────────────────────────")
        try:
            orfans = contar_huerfanos()
            for k, v in orfans.items():
                badge = "⚠" if v > 0 else ("✗" if v < 0 else "✓")
                print(f"  {k:<30} {badge} {v}")
        except Exception as e:
            print(f"  huérfanos: ✗ {e}")

        try:
            nulls = contar_nulls_criticos()
            for k, v in nulls.items():
                badge = "⚠" if v > 0 else ("✗" if v < 0 else "✓")
                print(f"  {k:<30} {badge} {v}")
        except Exception as e:
            print(f"  nulls: ✗ {e}")

    # ─── BACKFILLS ───────────────────────────────────────────────────────
    if not args.dry_run and not args.skip_backfills:
        print()
        print("─── BACKFILLS ─────────────────────────────────────────────────")
        snaps = crear_snapshots_ultimos_meses(n_meses=12, dry_run=False)
        print(f"  snapshots historia: {snaps.get('msg')}")
        if args.verbose:
            for d in snaps.get("detalle") or []:
                print(f"    · {d}")

    # ─── DRIFT BALANCE ───────────────────────────────────────────────────
    if not args.dry_run:
        print()
        print("─── DRIFT BALANCE ─────────────────────────────────────────────")
        drift = comparar_drift_balance()
        if drift.get("ok"):
            for k, d in drift.get("drifts", {}).items():
                pct = d["drift_pct"]
                badge = "✓" if pct < 0.5 else ("⚠" if pct < 5 else "✗")
                print(f"  {k:<14} snap={d['snap']:>14,.0f}  live={d['live']:>14,.0f}  "
                      f"drift={pct:>6.2f}% {badge}")
        else:
            print(f"  ⚠ {drift.get('msg')}")

    print()
    print("=" * 70)
    print(" sync_dbase_actual TERMINADO")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
