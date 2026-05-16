"""Listado detallado de las filas que `check_salud_dia.py` marca como ERR.

El check te dice "hay 26 mov_doble rotos + 17 facturas con saldo malo";
este script te muestra CUÁLES son, con suficiente info para que la dueña
las triage una por una (o las pase al desarrollador con id concretos).

Read-only — no modifica nada. Si querés UPDATEs auto, hacelos a mano con
SQL (este script sólo diagnostica) o vía el wizard de cada módulo.

Secciones:
  A) mov_doble reversados sin id_reverso (link roto al reverso).
  B) mov_doble reversados con id_reverso a un mov inexistente o no-reverso.
  C) Facturas con saldo != importe - abono.
  D) Caja S sin clasificar del mes (huérfanos).

Uso:
    python scripts/listar_inconsistencias.py
    python scripts/listar_inconsistencias.py --solo facturas
    python scripts/listar_inconsistencias.py --csv salida.csv
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import db  # noqa: E402


# ───────────────────────────────────────────────────────────────────────────
# helpers
# ───────────────────────────────────────────────────────────────────────────
def _seccion(titulo: str) -> None:
    print(f"\n┌─ {titulo} " + "─" * max(0, 70 - len(titulo)))


def _money(x) -> str:
    return f"${float(x or 0):>12,.2f}"


def _trunc(s, n) -> str:
    s = s or ""
    return (s[:n - 1] + "…") if len(s) > n else s.ljust(n)


# ───────────────────────────────────────────────────────────────────────────
# A) mov_doble reversados sin id_reverso
# ───────────────────────────────────────────────────────────────────────────
def listar_md_sin_reverso() -> list[dict]:
    return db.fetch_all(
        """
        SELECT m.id_mov_doble, m.fecha_operacion, m.fecha_creacion,
               m.tipo, m.origen_table, m.origen_id,
               m.destino_table, m.destino_id, m.importe,
               COALESCE(m.concepto,'') AS concepto,
               COALESCE(m.usuario,'')  AS usuario
          FROM scintela.mov_doble m
         WHERE m.estado = 'reversado'
           AND m.id_reverso IS NULL
         ORDER BY m.fecha_creacion DESC, m.id_mov_doble DESC
        """
    ) or []


def imprimir_md_sin_reverso(rows: list[dict]) -> None:
    _seccion(f"A) mov_doble reversados SIN id_reverso ({len(rows)} filas)")
    if not rows:
        print("  (vacío — todos los reversados tienen link)")
        return
    print(f"  {'id':<6} {'fecha':<10} {'tipo':<28} "
          f"{'orig':<11} {'orig#':<7} {'dest':<11} {'dest#':<7} "
          f"{'importe':>11}  concepto")
    print("  " + "─" * 110)
    for r in rows:
        print(f"  #{r['id_mov_doble']:<5} "
              f"{r['fecha_operacion']!s:<10} "
              f"{_trunc(r['tipo'], 28)} "
              f"{_trunc(r['origen_table'], 11)} "
              f"#{str(r['origen_id'] or '')[:6]:<6} "
              f"{_trunc(r['destino_table'], 11)} "
              f"#{str(r['destino_id'] or '')[:6]:<6} "
              f"{_money(r['importe']):>11}  "
              f"{_trunc(r['concepto'], 50)}")


# ───────────────────────────────────────────────────────────────────────────
# B) mov_doble reversados con id_reverso apuntando a algo roto
# ───────────────────────────────────────────────────────────────────────────
def listar_md_link_roto() -> list[dict]:
    return db.fetch_all(
        """
        SELECT m.id_mov_doble, m.fecha_operacion, m.tipo,
               m.origen_table, m.origen_id, m.destino_table, m.destino_id,
               m.importe, COALESCE(m.concepto,'') AS concepto,
               m.id_reverso,
               (SELECT estado FROM scintela.mov_doble r
                 WHERE r.id_mov_doble = m.id_reverso) AS estado_reverso,
               (SELECT 1 FROM scintela.mov_doble r
                 WHERE r.id_mov_doble = m.id_reverso) IS NULL AS reverso_falta
          FROM scintela.mov_doble m
         WHERE m.estado = 'reversado'
           AND m.id_reverso IS NOT NULL
           AND NOT EXISTS (
             SELECT 1 FROM scintela.mov_doble r
              WHERE r.id_mov_doble = m.id_reverso
                AND r.estado = 'reverso'
           )
         ORDER BY m.fecha_creacion DESC, m.id_mov_doble DESC
        """
    ) or []


def imprimir_md_link_roto(rows: list[dict]) -> None:
    _seccion(f"B) mov_doble con id_reverso apuntando a algo roto "
             f"({len(rows)} filas)")
    if not rows:
        print("  (vacío)")
        return
    for r in rows:
        if r["reverso_falta"]:
            estado = "(NO existe)"
        else:
            estado = f"existe pero estado={r['estado_reverso']!r}"
        print(f"  #{r['id_mov_doble']}  {r['fecha_operacion']}  "
              f"{r['tipo']}  →  id_reverso={r['id_reverso']} {estado}")
        print(f"     orig: {r['origen_table']}#{r['origen_id']}  "
              f"dest: {r['destino_table']}#{r['destino_id']}  "
              f"{_money(r['importe']).strip()}  {(r['concepto'] or '')[:70]}")


# ───────────────────────────────────────────────────────────────────────────
# C) Facturas con saldo != importe - abono
# ───────────────────────────────────────────────────────────────────────────
def listar_facturas_drift() -> list[dict]:
    return db.fetch_all(
        """
        SELECT f.id_factura,
               COALESCE(f.numf::text,'') AS numf,
               COALESCE(f.codigo_cli,'') AS codigo_cli,
               COALESCE(cl.nombre,'')    AS cliente,
               f.fecha, f.vencimiento,
               COALESCE(f.importe,0)::numeric(14,2) AS importe,
               COALESCE(f.abono,0)::numeric(14,2)   AS abono,
               COALESCE(f.saldo,0)::numeric(14,2)   AS saldo,
               (COALESCE(f.importe,0) - COALESCE(f.abono,0) - COALESCE(f.saldo,0))::numeric(14,2)
                                                    AS diff,
               COALESCE(f.stat,'') AS stat
          FROM scintela.factura f
          LEFT JOIN scintela.cliente cl ON cl.codigo_cli = f.codigo_cli
         WHERE COALESCE(f.stat,'') NOT IN ('X','Y')
           AND ABS(COALESCE(f.importe,0) - COALESCE(f.abono,0) - COALESCE(f.saldo,0)) > 0.01
           AND NOT (
             COALESCE(f.stat,'') = 'T'
             AND ABS(COALESCE(f.saldo,0)) < 0.01
             AND COALESCE(f.abono,0) >= COALESCE(f.importe,0)
           )
         ORDER BY ABS(COALESCE(f.importe,0) - COALESCE(f.abono,0) - COALESCE(f.saldo,0)) DESC
        """
    ) or []


def imprimir_facturas_drift(rows: list[dict]) -> None:
    _seccion(f"C) Facturas con saldo ≠ importe − abono ({len(rows)} filas)")
    if not rows:
        print("  (vacío — todas las facturas activas cumplen el invariante)")
        return
    print(f"  {'id':<7} {'numf':<8} {'stat':<5} {'fecha':<10} "
          f"{'importe':>11} {'abono':>11} {'saldo':>11} {'diff':>11}  "
          f"{'cliente':<25}")
    print("  " + "─" * 110)
    total_diff = 0.0
    for r in rows:
        diff = float(r["diff"] or 0)
        total_diff += abs(diff)
        print(f"  #{r['id_factura']:<5} "
              f"{_trunc(r['numf'], 8)} "
              f"{r['stat']:<4} "
              f"{r['fecha']!s:<10} "
              f"{_money(r['importe']):>11} "
              f"{_money(r['abono']):>11} "
              f"{_money(r['saldo']):>11} "
              f"{_money(diff):>11}  "
              f"{_trunc((r['cliente'] or r['codigo_cli']), 25)}")
    print(f"\n  Suma absoluta de drifts: {_money(total_diff).strip()}")
    print(f"  Hint SQL para una fila:")
    print(f"    SELECT cf.* FROM scintela.chequesxfact cf "
          f"WHERE cf.id_fact = <id_factura>;")


# ───────────────────────────────────────────────────────────────────────────
# D) Caja S del mes sin mov_doble (huérfanas)
# ───────────────────────────────────────────────────────────────────────────
def listar_caja_huerfanas() -> list[dict]:
    return db.fetch_all(
        """
        SELECT c.id_caja, c.fecha, ABS(c.importe)::numeric(12,2) AS imp,
               COALESCE(c.concepto,'') AS concepto,
               COALESCE(c.clave,'')    AS clave,
               COALESCE(c.usuario_crea,'') AS usuario
          FROM scintela.caja c
         WHERE c.tipo = 'S'
           AND c.fecha >= date_trunc('month', CURRENT_DATE)
           AND COALESCE(c.usuario_crea,'') <> 'dbf-import'
           AND NOT EXISTS (
             SELECT 1 FROM scintela.mov_doble md
              WHERE (md.origen_table='caja' AND md.origen_id=c.id_caja)
                 OR (md.destino_table='caja' AND md.destino_id=c.id_caja)
           )
         ORDER BY c.fecha DESC, c.id_caja DESC
        """
    ) or []


def imprimir_caja_huerfanas(rows: list[dict]) -> None:
    _seccion(f"D) Caja S del mes sin clasificar ({len(rows)} filas)")
    if not rows:
        print("  (vacío — todas las egresos del mes están clasificadas)")
        return
    for r in rows:
        print(f"  caja#{r['id_caja']:<5} {r['fecha']}  "
              f"{_money(r['imp']):>11}  user={r['usuario']:<10}  "
              f"{(r['concepto'] or '')[:55]}")
    print(f"\n  Andá a /gastos/clasificar/<id_caja> para cada una.")


# ───────────────────────────────────────────────────────────────────────────
# CSV opcional
# ───────────────────────────────────────────────────────────────────────────
def export_csv(path: Path, data: dict[str, list[dict]]) -> None:
    """Vuelca todas las secciones a un CSV con columna `seccion`."""
    rows: list[dict] = []
    for sec, lst in data.items():
        for r in lst:
            rows.append({"seccion": sec, **{k: v for k, v in r.items()}})
    if not rows:
        print(f"\nNo hay filas para exportar.")
        return
    # Union de claves para el header
    keys = ["seccion"]
    for r in rows:
        for k in r:
            if k not in keys:
                keys.append(k)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=keys, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in keys})
    print(f"\nCSV exportado: {path}")


# ───────────────────────────────────────────────────────────────────────────
# Entry
# ───────────────────────────────────────────────────────────────────────────
SECCIONES = {
    "md_sin_reverso": (listar_md_sin_reverso, imprimir_md_sin_reverso),
    "md_link_roto":   (listar_md_link_roto,   imprimir_md_link_roto),
    "facturas":       (listar_facturas_drift, imprimir_facturas_drift),
    "caja":           (listar_caja_huerfanas, imprimir_caja_huerfanas),
}


def main() -> int:
    for k in ("DB_HOST", "DB_NAME", "DB_USER", "DB_PASSWORD"):
        if not os.environ.get(k):
            print(f"ERROR: falta {k} en el entorno.")
            return 2

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--solo", default="",
                    help="lista separada por coma "
                         f"({','.join(SECCIONES)}).")
    ap.add_argument("--csv", default="",
                    help="path a un CSV donde volcar todo (incl. detalle).")
    args = ap.parse_args()
    sel = (args.solo.split(",") if args.solo else list(SECCIONES))
    sel = [s.strip().lower() for s in sel if s.strip()]
    for s in sel:
        if s not in SECCIONES:
            print(f"ERROR: sección desconocida: {s!r}. "
                  f"Válidos: {','.join(SECCIONES)}")
            return 2

    print(f"=== listar_inconsistencias — Programa Core ===")
    datos: dict[str, list[dict]] = {}
    for s in sel:
        fetch, show = SECCIONES[s]
        rows = fetch()
        datos[s] = rows
        show(rows)

    print(f"\n{'═' * 72}")
    n_total = sum(len(v) for v in datos.values())
    print(f"  Total filas listadas: {n_total}")
    print(f"  Cuando arregles algo, re-corré `check_salud_dia.py` para "
          f"confirmar que bajó el contador.")

    if args.csv:
        export_csv(Path(args.csv), datos)

    return 0


if __name__ == "__main__":
    sys.exit(main())
