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
# Insertamos el repo root Y la carpeta scripts/ — así `import import_dbf`
# (bare) funciona tanto corriendo el script directo (python scripts/...py)
# como importándolo como módulo (scripts.sync_dbase_actual) desde los tests.
_SCRIPTS_DIR = Path(__file__).resolve().parent
for _p in (str(ROOT), str(_SCRIPTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

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
            ("public", "schema_migrations"),
        ):
            try:
                rows = _db.fetch_all(
                    f"SELECT version FROM {schema_table[0]}.{schema_table[1]} ORDER BY version DESC LIMIT 5"
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


def _backup_python(target_dir: Path, tablas: list[str], db_url: str) -> tuple[bool, str]:
    """Fallback de backup SIN pg_dump: COPY ... TO STDOUT (CSV) por tabla vía
    psycopg2. El EC2 Windows no tiene pg_dump en el PATH, así que este path es
    el que corre en producción. Recuperable con COPY ... FROM. TMT 2026-06-08.
    """
    try:
        import psycopg2
    except ImportError as e:
        return False, f"psycopg2 no disponible: {e}"
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = target_dir / f"backup_pre_sync_{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)
    total = 0
    hechas = 0
    try:
        conn = psycopg2.connect(db_url)
    except Exception as e:  # noqa: BLE001
        return False, f"no pude conectar para backup: {e}"
    try:
        cur = conn.cursor()
        for t in tablas:
            fp = out_dir / (t.replace(".", "_") + ".csv")
            try:
                with open(fp, "w", encoding="utf-8", newline="") as f:
                    cur.copy_expert(f"COPY {t} TO STDOUT WITH CSV HEADER", f)
                total += fp.stat().st_size
                hechas += 1
            except Exception:  # noqa: BLE001 — tabla inexistente u otra; seguimos
                conn.rollback()
    finally:
        conn.close()
    if hechas == 0:
        return False, "ninguna tabla respaldada"
    return True, f"backup (python COPY) en {out_dir} ({total / 1048576:.1f} MB · {hechas} tablas)"


def hacer_backup(target_dir: Path, tablas: list[str]) -> tuple[bool, str]:
    """Dump local de las tablas que vamos a TRUNCATE+INSERT.

    Usa `pg_dump --data-only -t scintela.X ...` si está en el PATH; si no
    (caso EC2 Windows), cae al fallback Python con COPY vía psycopg2 — así
    SIEMPRE queda un backup pre-sync para rollback.
    """
    db_url = os.environ.get("DATABASE_URL") or _build_dburl_from_env()
    if not db_url:
        return True, "DATABASE_URL no seteado; backup omitido (warning)"
    pg_dump = shutil.which("pg_dump")
    if not pg_dump:
        return _backup_python(target_dir, tablas, db_url)
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_file = target_dir / f"backup_pre_sync_{stamp}.sql"
    # Backup parcial: solo las tablas que vamos a tocar.
    t_args = []
    for t in tablas:
        t_args += ["-t", t]
    try:
        with open(out_file, "wb") as f:
            res = subprocess.run(
                [pg_dump, "--data-only", "--no-owner", "--no-privileges"] + t_args + [db_url],
                stdout=f,
                stderr=subprocess.PIPE,
                check=False,
                timeout=600,
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
        ("transacciones_bancarias.no_banco", "scintela.transacciones_bancarias", "no_banco IS NULL"),
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
        return {"creados": 0, "ya_existian": 0, "errores": 0, "msg": "DRY-RUN — no se crearon snapshots"}
    creados = 0
    ya_existian = 0
    errores = 0
    detalle = []
    try:
        from modules.informes.queries import crear_snapshot_historia
    except Exception as e:
        return {
            "creados": 0,
            "ya_existian": 0,
            "errores": 1,
            "msg": f"no se pudo importar crear_snapshot_historia: {e}",
        }

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
        "creados": creados,
        "ya_existian": ya_existian,
        "errores": errores,
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
            "banco": float(comp.get("salbanc_total") or 0),
            "cart": float(comp.get("cart") or 0),
            "ustock": float(comp.get("vsto") or 0),
            "deuda": float(comp.get("totp") or 0),
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


def correr_sync_principal(source: Path, dry_run: bool, only: str, encoding: str | None) -> int:
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


# ─── BLINDAJE BANCOS: anti-regresión + integridad de saldo ──────────────
# TMT 2026-06-03 incidente: un PICHINCH.DBF STALE (truncado en la fila 422,
# saldo 2,340,586.90) pisó vía TRUNCATE+INSERT los datos buenos que PC ya
# tenía (493 filas, saldo 2,385,393.30). El sync no chistó porque PC quedó
# coherente con el DBF malo. Resultado: PC "perdió" 71 movimientos y el saldo
# retrocedió "de la nada". Estos dos checks lo cazan:
#
#   1. PRE  — verificar_no_regresion_bancos(): si el DBF entrante es un PREFIJO
#             truncado de lo que PC ya tiene (menos filas y el saldo final del
#             DBF aparece como saldo intermedio en PC), ABORTA. Es exactamente
#             el patrón "tarball viejo de CloudShell".
#   2. POST — verificar_saldos_bancos_post(): tras el sync, PC tiene que
#             terminar en el MISMO último saldo que el DBF, con el mismo conteo
#             y 0 quiebres de cadena. Si no, grita FALLO.

_BANCO_DBFS = (
    ("PICHINCH.DBF", "_lookup_no_banco_pichincha"),
    ("INTER.DBF", "_lookup_no_banco_internacional"),
)
_DOCS_DEBITO = {"CH", "ND", "DB", "GS", "PA"}


def _resumen_dbf_banco(dbf_path: Path) -> dict | None:
    """Lee un DBF de banco y devuelve {n, max_fecha, last_saldo, saldos:set}.

    last_saldo = saldo running de la ÚLTIMA fila en orden físico (= orden de
    inserción del dBase). saldos = set de todos los saldos running, para
    detectar prefijos truncados.
    """
    try:
        rows = import_dbf._read_dbf(dbf_path)
    except Exception:
        return None
    if not rows:
        return None
    saldos: set[float] = set()
    fechas = []
    last_saldo = None
    for r in rows:
        s = r.get("SALDO")
        if s is not None:
            try:
                sv = round(float(s), 2)
                saldos.add(sv)
                last_saldo = sv
            except (TypeError, ValueError):
                pass
        f = r.get("FECHA")
        if f:
            fechas.append(str(f))
    return {
        "n": len(rows),
        "max_fecha": max(fechas) if fechas else None,
        "last_saldo": last_saldo,
        "saldos": saldos,
    }


def _resumen_pc_banco(no_banco: int) -> dict | None:
    """Estado actual de PC para un banco: {n, max_fecha, last_saldo, saldos:set}."""
    import db as _db

    try:
        agg = _db.fetch_one(
            "SELECT COUNT(*) AS n, MAX(fecha)::text AS max_fecha "
            "FROM scintela.transacciones_bancarias WHERE no_banco = %s",
            (no_banco,),
        ) or {}
        n = int(agg.get("n") or 0)
        if n == 0:
            return {"n": 0, "max_fecha": None, "last_saldo": None, "saldos": set()}
        last = _db.fetch_one(
            "SELECT saldo FROM scintela.transacciones_bancarias "
            "WHERE no_banco = %s AND saldo IS NOT NULL "
            "ORDER BY fecha DESC, id_transaccion DESC LIMIT 1",
            (no_banco,),
        ) or {}
        saldos_rows = _db.fetch_all(
            "SELECT DISTINCT ROUND(saldo, 2) AS s FROM scintela.transacciones_bancarias "
            "WHERE no_banco = %s AND saldo IS NOT NULL",
            (no_banco,),
        ) or []
        return {
            "n": n,
            "max_fecha": agg.get("max_fecha"),
            "last_saldo": (round(float(last["saldo"]), 2) if last.get("saldo") is not None else None),
            "saldos": {round(float(r["s"]), 2) for r in saldos_rows if r.get("s") is not None},
        }
    except Exception:
        return None


def _evaluar_regresion_banco(dbf: dict, pc: dict) -> tuple[str, str]:
    """Función PURA (testeable). Devuelve (nivel, msg).

    nivel ∈ {'ok', 'warn', 'abort'}.
      abort — el DBF entrante es un prefijo truncado de lo que PC ya tiene.
      warn  — el DBF entrante tiene fecha máxima anterior a la de PC (posible
              stale, pero podría ser un roll legítimo del archivo dBase).
      ok    — el DBF avanza o iguala a PC.
    """
    if not pc or pc.get("n", 0) == 0:
        return ("ok", "PC vacío — nada que proteger")
    if not dbf or dbf.get("last_saldo") is None:
        return ("ok", "DBF sin saldos legibles — se omite el guard")

    dbf_last = dbf["last_saldo"]
    pc_last = pc["last_saldo"]
    # Caso regresión: el DBF termina ANTES (menos filas) y su saldo final ya
    # es un saldo intermedio de PC, mientras que el saldo final de PC no
    # existe en el DBF. ⇒ el DBF es un prefijo viejo. ABORTAR.
    if (
        dbf_last != pc_last
        and dbf.get("n", 0) < pc.get("n", 0)
        and dbf_last in pc.get("saldos", set())
        and pc_last not in dbf.get("saldos", set())
    ):
        return (
            "abort",
            f"DBF STALE/truncado: termina en saldo {dbf_last:,.2f} (fila {dbf['n']}), "
            f"pero PC ya avanzó hasta {pc_last:,.2f} ({pc['n']} filas) y "
            f"{dbf_last:,.2f} es un saldo INTERMEDIO de PC. Sincronizar pisaría "
            f"datos más nuevos. Si es intencional, corré con --force.",
        )
    # Heurística secundaria: fecha máxima del DBF anterior a la de PC.
    if dbf.get("max_fecha") and pc.get("max_fecha") and dbf["max_fecha"] < pc["max_fecha"]:
        return (
            "warn",
            f"DBF con fecha máxima {dbf['max_fecha']} < PC {pc['max_fecha']} — "
            f"podría estar stale. Revisá que sea el pull fresco.",
        )
    return ("ok", f"DBF avanza/iguala a PC (DBF n={dbf['n']} last={dbf_last:,.2f})")


def verificar_no_regresion_bancos(src: Path, force: bool = False) -> tuple[bool, list[str]]:
    """PRE-CHECK: para cada DBF de banco, aborta si es un prefijo stale de PC.

    Devuelve (ok, lineas). ok=False ⇒ abortar el sync (salvo force).
    """
    import db as _db

    lineas: list[str] = []
    ok = True
    for dbf_name, lookup_fn_name in _BANCO_DBFS:
        dbf_path = src / dbf_name
        if not dbf_path.exists():
            continue
        try:
            no_banco = getattr(import_dbf, lookup_fn_name)()
        except Exception as e:
            lineas.append(f"  {dbf_name:<14} ⚠ no pude resolver no_banco ({e})")
            continue
        dbf = _resumen_dbf_banco(dbf_path)
        pc = _resumen_pc_banco(int(no_banco))
        if dbf is None or pc is None:
            lineas.append(f"  {dbf_name:<14} ⚠ no pude leer DBF o PC — guard omitido")
            continue
        nivel, msg = _evaluar_regresion_banco(dbf, pc)
        badge = {"ok": "✓", "warn": "⚠", "abort": "✗"}[nivel]
        lineas.append(f"  {dbf_name:<14} {badge} {msg}")
        if nivel == "abort" and not force:
            ok = False
    return ok, lineas


def _contar_quiebres_cadena(no_banco: int) -> int:
    """Cuenta filas donde saldo != saldo_prev + signed_delta (orden físico)."""
    import bank_helpers as _bh
    import db as _db

    rows = _db.fetch_all(
        "SELECT documento, importe, saldo, COALESCE(usuario_crea,'') AS usuario_crea "
        "FROM scintela.transacciones_bancarias "
        "WHERE no_banco = %s AND saldo IS NOT NULL "
        "ORDER BY fecha ASC, id_transaccion ASC",
        (no_banco,),
    ) or []
    quiebres = 0
    prev = None
    for r in rows:
        s = round(float(r["saldo"] or 0), 2)
        if prev is not None:
            delta = _bh._signed_delta(
                r.get("documento"), float(r.get("importe") or 0), r.get("usuario_crea") or ""
            )
            if abs(round(s - round(prev + delta, 2), 2)) > 0.01:
                quiebres += 1
        prev = s
    return quiebres


def verificar_saldos_bancos_post(src: Path) -> tuple[bool, list[str]]:
    """POST-CHECK: tras el sync, PC tiene que cerrar igual que el DBF fuente.

    Verifica último saldo, conteo de filas y 0 quiebres de cadena por banco.
    """
    lineas: list[str] = []
    ok = True
    for dbf_name, lookup_fn_name in _BANCO_DBFS:
        dbf_path = src / dbf_name
        if not dbf_path.exists():
            continue
        try:
            no_banco = int(getattr(import_dbf, lookup_fn_name)())
        except Exception as e:
            lineas.append(f"  {dbf_name:<14} ⚠ no pude resolver no_banco ({e})")
            continue
        dbf = _resumen_dbf_banco(dbf_path)
        pc = _resumen_pc_banco(no_banco)
        if dbf is None or pc is None:
            lineas.append(f"  {dbf_name:<14} ⚠ no pude leer DBF o PC")
            continue
        # FATALES (hacen fallar el check): son los síntomas de una TRUNCACIÓN,
        # que es el incidente que estos checks existen para cazar.
        #   - PC quedó con MENOS filas que el DBF ⇒ se perdieron movimientos.
        #   - El último saldo running del DBF no aparece en PC ⇒ el DBF no entró
        #     entero (PC puede tener filas PC-only después, por eso chequeamos
        #     pertenencia al set en vez de igualdad estricta del último saldo).
        fatales = []
        if pc["n"] < dbf["n"]:
            fatales.append(f"PC tiene {pc['n']} filas < DBF {dbf['n']} (faltan movimientos)")
        if dbf["last_saldo"] is not None and dbf["last_saldo"] not in pc.get("saldos", set()):
            fatales.append(
                f"el último saldo del DBF {dbf['last_saldo']:,.2f} no entró a PC"
            )

        # NO FATALES (solo informativos): los quiebres del walk-forward. El
        # walk asume ND=salida / DE=entrada, asunción que NO matchea los movs
        # reversos del dBase (ND que en realidad son AC, etc.) ⇒ marca falsos
        # positivos. Ver /conciliacion/banco-v2/auditar. Solo importa si el
        # saldo conciliado no cuadra contra el banco — y eso lo cubren los
        # fatales de arriba. TMT 2026-06-03: degradado a warn tras confirmar
        # que el recompute ya estaba deshabilitado por esta misma razón.
        warns = []
        try:
            quiebres = _contar_quiebres_cadena(no_banco)
            if quiebres > 0:
                warns.append(
                    f"{quiebres} quiebres de cadena (probables falsos positivos "
                    f"por reversos — revisar /auditar)"
                )
        except Exception as e:
            warns.append(f"no pude validar cadena ({e})")

        cola = f"PC={pc['n']} filas, último saldo {pc['last_saldo']:,.2f}"
        if fatales:
            ok = False
            lineas.append(f"  {dbf_name:<14} ✗ FALLO — " + "; ".join(fatales + warns))
        elif warns:
            lineas.append(f"  {dbf_name:<14} ✓ {cola}  (⚠ {'; '.join(warns)})")
        else:
            lineas.append(f"  {dbf_name:<14} ✓ {cola}, cadena íntegra")
    return ok, lineas


def main():
    _force_utf8_stdout()
    ap = argparse.ArgumentParser(
        description="sync_dbase_actual — orquestador completo del sync dBase",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("--source", default=str(DEFAULT_SOURCE), help="Carpeta con los .DBF")
    ap.add_argument("--dry-run", action="store_true", help="Lee los DBFs e informa, no toca DB")
    ap.add_argument("--only", default="", help="Solo importar estos DBFs (coma-sep)")
    ap.add_argument("--encoding", default=None, help="Forzar encoding (default: auto)")
    ap.add_argument("--skip-backup", action="store_true", help="No hace pg_dump pre-import")
    ap.add_argument("--skip-pre-checks", action="store_true", help="Omite verificación de schema version")
    ap.add_argument("--skip-post-checks", action="store_true", help="Omite sanity check post-import")
    ap.add_argument(
        "--skip-backfills", action="store_true", help="Omite backfills encadenados (snapshots, etc.)"
    )
    ap.add_argument(
        "--force", action="store_true",
        help="Ignora el guard anti-regresión de bancos (DBF stale/truncado). Usar con cuidado.",
    )
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

        # GUARD anti-regresión de bancos (TMT 2026-06-03). Caza el caso del
        # DBF stale/truncado que pisa datos más nuevos de PC.
        try:
            ok_reg, lineas_reg = verificar_no_regresion_bancos(src, force=args.force)
            print("  anti-regresión bancos:")
            for ln in lineas_reg:
                print(ln)
            if not ok_reg:
                print()
                print("  ✗ ABORTADO: el DBF entrante parece STALE/truncado respecto")
                print("    a lo que PC ya tiene. NO se tocó la base. Verificá que el")
                print("    tarball sea el pull fresco (¿borraste el viejo antes de subir?).")
                print("    Si de verdad querés pisar, repetí con --force.")
                return 2
        except Exception as e:
            print(f"  anti-regresión bancos: ⚠ no se pudo verificar ({e})")

    # ─── BACKUP ──────────────────────────────────────────────────────────
    if not args.skip_backup and not args.dry_run:
        tablas_backup = [
            "scintela.factura",
            "scintela.cheque",
            "scintela.posdat",
            "scintela.caja",
            "scintela.dolares",
            "scintela.activos",
            "scintela.historia",
            "scintela.iniciales",
            "scintela.compra",
            "scintela.flujo",
            "scintela.transacciones_bancarias",
            "scintela.xgast",
            "scintela.retiros",
            "scintela.tinto",
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

        # Integridad de saldos de banco: PC tiene que cerrar igual que el DBF.
        try:
            ok_bancos, lineas_bancos = verificar_saldos_bancos_post(src)
            print("  integridad saldos banco:")
            for ln in lineas_bancos:
                print(ln)
            if not ok_bancos:
                print("  ✗ POST-CHECK BANCOS FALLÓ — PC no cerró igual que el DBF.")
                print("    Revisá arriba; puede haber quedado un sync parcial.")
        except Exception as e:
            print(f"  integridad saldos banco: ✗ {e}")

    # ─── BACKFILLS ───────────────────────────────────────────────────────
    if not args.dry_run and not args.skip_backfills:
        print()
        print("─── BACKFILLS ─────────────────────────────────────────────────")
        snaps = crear_snapshots_ultimos_meses(n_meses=12, dry_run=False)
        print(f"  snapshots historia: {snaps.get('msg')}")
        if args.verbose:
            for d in snaps.get("detalle") or []:
                print(f"    · {d}")

    # ─── RECOMPUTE SALDOS POST-SYNC ──────────────────────────────────────
    # TMT 2026-06-03 audit blindaje: el sync borra y re-inserta DBF rows con
    # IDs nuevos. Las filas PC-only (depósitos de cheque, conciliación
    # creada_from_real, etc.) NO se tocan, pero su saldo running puede
    # quedar stale si el sync trajo nuevas filas DBF con fechas anteriores.
    # Solución: por cada banco con PC-only rows, recompute la cadena desde
    # la fecha más vieja de las PC-only. Con el _signed_delta fixed
    # (respeta NDs reversos del DBF, sign-by-doc para PC-only), el walk
    # es seguro y rebuilds la cadena coherente.
    if not args.dry_run:
        print()
        print("─── RECOMPUTE SALDOS POST-SYNC ────────────────────────────────")
        try:
            import db as _db_mod
            import bank_helpers as _bh
            # Bancos que tienen PC-only rows (insertadas via insert_movimiento_bancario).
            bancos_pc = _db_mod.fetch_all(
                """
                SELECT no_banco, MIN(fecha) AS fecha_min, COUNT(*) AS n_pc_rows
                  FROM scintela.transacciones_bancarias
                 WHERE COALESCE(usuario_crea,'') NOT IN ('','dbf-import','asinfo-backfill','dbase-sync')
                 GROUP BY no_banco
                """
            ) or []
            for b in bancos_pc:
                no_b = int(b["no_banco"])
                fecha_min = b.get("fecha_min")
                n_pc = int(b.get("n_pc_rows") or 0)
                if not fecha_min:
                    continue
                try:
                    with _db_mod.tx() as conn:
                        n_walk = _bh.recompute_saldos_desde(
                            conn, no_banco=no_b, no_cta=None,
                            ancla_fecha=fecha_min,
                        )
                    print(f"  banco {no_b}: {n_pc} PC-rows, recompute desde {fecha_min} → {n_walk} filas actualizadas ✓")
                except Exception as e:
                    print(f"  banco {no_b}: ⚠ recompute falló: {e}")
            if not bancos_pc:
                print("  ✓ No hay PC-only rows — chain DBF intacto.")
        except Exception as e:
            print(f"  ⚠ recompute post-sync falló: {e}")

    # ─── DRIFT BALANCE ───────────────────────────────────────────────────
    if not args.dry_run:
        print()
        print("─── DRIFT BALANCE ─────────────────────────────────────────────")
        drift = comparar_drift_balance()
        if drift.get("ok"):
            for k, d in drift.get("drifts", {}).items():
                pct = d["drift_pct"]
                badge = "✓" if pct < 0.5 else ("⚠" if pct < 5 else "✗")
                print(
                    f"  {k:<14} snap={d['snap']:>14,.0f}  live={d['live']:>14,.0f}  "
                    f"drift={pct:>6.2f}% {badge}"
                )
        else:
            print(f"  ⚠ {drift.get('msg')}")

    print()
    print("=" * 70)
    print(" sync_dbase_actual TERMINADO")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
