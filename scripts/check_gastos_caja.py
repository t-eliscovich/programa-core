"""Check de salud — gastos pagados con caja (clasificación V1..V9).

Mira los gastos del mes (o un rango) y dice:
  1. Cuántos caja S quedaron sin clasificar como gasto (mov_doble huérfano).
  2. Cómo se reparten los xgast por V1..V9 — el famoso $X en V9 que "se traga
     todo lo que empieza con GAS *" sale acá inmediatamente.
  3. Patrones sospechosos: el mismo prefijo de concepto cayendo en >1 num
     distinto (ej. "GAS *" en V5 y V9 a la vez) — señal de que el keyword
     mapper está confundido o hay clasificación manual heterogénea.
  4. La distribución actual del mes vs un "baseline" — V9 > 35% del total
     dispara warning.

Read-only — NO modifica nada. Sirve para correr antes de cerrar el día o
mes y entender dónde mirar.

Uso:
    python scripts/check_gastos_caja.py                 # mes actual
    python scripts/check_gastos_caja.py --mes 2026-04   # un mes concreto
    python scripts/check_gastos_caja.py --desde 2026-05-01 --hasta 2026-05-15
    python scripts/check_gastos_caja.py --top 30        # top patrones (default 15)
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from collections import defaultdict
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import db  # noqa: E402

CATEGORIAS_V19 = (
    (1, "V1 — Personal tejeduría"),
    (2, "V2 — Gas/Comb. tejeduría"),
    (3, "V3 — Gs. varios tejeduría"),
    (4, "V4 — Personal tintorería"),
    (5, "V5 — Gas/Comb. tintorería"),
    (6, "V6 — Gs. varios tintorería"),
    (7, "V7 — Personal admin"),
    (8, "V8 — Servicios admin"),
    (9, "V9 — Gs. varios admin"),
)

# Umbrales de "ojo con esto"
UMBRAL_V9_PCT = 35.0     # si V9 > 35% del total → warning
UMBRAL_V2_VACIO = True   # V2 = 0 con costos de tejeduría > 0 es raro
UMBRAL_HUERFANOS = 5     # > 5 caja S sin clasificar en el rango


def _parse_args():
    ap = argparse.ArgumentParser(description=__doc__)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--mes", help="YYYY-MM. Default: mes corriente.")
    g.add_argument("--desde", help="YYYY-MM-DD.")
    ap.add_argument("--hasta", help="YYYY-MM-DD. Requiere --desde.")
    ap.add_argument("--top", type=int, default=15,
                    help="Cuántos patrones sospechosos mostrar (default 15).")
    return ap.parse_args()


def _rango(args) -> tuple[str, str, str]:
    """Devuelve (desde, hasta, label) en formato YYYY-MM-DD."""
    from datetime import date as _d
    today = _d.today()
    if args.desde:
        if not args.hasta:
            print("ERROR: --desde requiere --hasta.", file=sys.stderr)
            sys.exit(2)
        return args.desde, args.hasta, f"{args.desde} → {args.hasta}"
    if args.mes:
        m = re.match(r"^(\d{4})-(\d{1,2})$", args.mes)
        if not m:
            print("ERROR: --mes debe ser YYYY-MM.", file=sys.stderr)
            sys.exit(2)
        y, mm = int(m.group(1)), int(m.group(2))
        from calendar import monthrange
        last = monthrange(y, mm)[1]
        return f"{y:04d}-{mm:02d}-01", f"{y:04d}-{mm:02d}-{last:02d}", args.mes
    # Mes corriente
    first = today.replace(day=1).isoformat()
    return first, today.isoformat(), f"{today.year}-{today.month:02d} (hasta hoy)"


def _bloque(titulo: str) -> None:
    print(f"\n┌─ {titulo} " + "─" * max(0, 60 - len(titulo)))


def main() -> int:
    for k in ("DB_HOST", "DB_NAME", "DB_USER", "DB_PASSWORD"):
        if not os.environ.get(k):
            print(f"ERROR: falta {k} en el entorno.")
            return 2

    args = _parse_args()
    desde, hasta, label = _rango(args)
    print(f"=== check_gastos_caja — rango: {label} ===")

    # ────────────────────────────────────────────────────────────────────
    # 1) Caja S sin clasificar como gasto (huérfanos)
    # ────────────────────────────────────────────────────────────────────
    huerfanos = db.fetch_all(
        """
        SELECT c.id_caja, c.fecha, ABS(c.importe) AS imp, c.concepto,
               COALESCE(c.usuario_crea,'') AS user
          FROM scintela.caja c
         WHERE c.tipo = 'S'
           AND c.fecha BETWEEN %s AND %s
           AND NOT EXISTS (
             SELECT 1 FROM scintela.mov_doble md
              WHERE (md.origen_table='caja' AND md.origen_id=c.id_caja)
                 OR (md.destino_table='caja' AND md.destino_id=c.id_caja)
           )
         ORDER BY c.fecha DESC, c.id_caja DESC
        """,
        (desde, hasta),
    ) or []
    # Filtra dbf-import (legacy, no son problema operativo).
    huerfanos_reales = [h for h in huerfanos if h["user"] != "dbf-import"]
    _bloque("1) Caja S sin clasificar como gasto")
    print(f"  Total en rango:    {len(huerfanos)}")
    print(f"  Excl. dbf-import:  {len(huerfanos_reales)}")
    if huerfanos_reales:
        print()
        for h in huerfanos_reales[:10]:
            print(f"    caja#{h['id_caja']:>5}  {h['fecha']}  "
                  f"${float(h['imp']):>10,.2f}  {(h['concepto'] or '')[:55]}")
        if len(huerfanos_reales) > 10:
            print(f"    ... y {len(huerfanos_reales) - 10} más")
        if len(huerfanos_reales) > UMBRAL_HUERFANOS:
            print(f"\n  ⚠ Hay {len(huerfanos_reales)} egresos sin clasificar — "
                  f"andá a /gastos/clasificar antes del cierre.")

    # ────────────────────────────────────────────────────────────────────
    # 2) Distribución V1..V9
    # ────────────────────────────────────────────────────────────────────
    vrows = db.fetch_all(
        """
        SELECT COALESCE(num, 0) AS num,
               COUNT(*) AS n,
               SUM(importe)::numeric(14,2) AS total
          FROM scintela.xgast
         WHERE fecha BETWEEN %s AND %s
           AND COALESCE(stat,'') NOT IN ('Y','X')
         GROUP BY num
         ORDER BY num
        """,
        (desde, hasta),
    ) or []
    totales = {int(r["num"]): float(r["total"] or 0) for r in vrows}
    counts  = {int(r["num"]): int(r["n"] or 0) for r in vrows}
    total_general = sum(totales.values())

    _bloque("2) Reparto V1..V9 del rango")
    print(f"  Total gastos del rango: ${total_general:,.2f}\n")
    print(f"  {'V':<5} {'Descripción':<28} {'$':>14}  {'%':>6}  {'n':>4}")
    for num, label_v in CATEGORIAS_V19:
        v = totales.get(num, 0.0)
        pct = (v / total_general * 100) if total_general else 0
        bar = "█" * int(pct / 2)  # 1 caracter por 2%
        n = counts.get(num, 0)
        print(f"  V{num:<4} {label_v:<28} ${v:>12,.0f}  {pct:>5.1f}%  {n:>4}  {bar}")
    sin_num = totales.get(0, 0.0)
    if sin_num > 0:
        print(f"  (sin num)                          ${sin_num:>12,.0f}  "
              f"{(sin_num / total_general * 100) if total_general else 0:>5.1f}%")

    # Warnings:
    v9_pct = (totales.get(9, 0) / total_general * 100) if total_general else 0
    if v9_pct > UMBRAL_V9_PCT:
        print(f"\n  ⚠ V9 concentra el {v9_pct:.1f}% (> {UMBRAL_V9_PCT}%) — "
              f"probable que el keyword 'GAS '/'GS ' esté tragando gastos "
              f"que en realidad son de otra categoría.")
    if UMBRAL_V2_VACIO and totales.get(2, 0) == 0 and totales.get(1, 0) > 0:
        print(f"  ⚠ V2 (gas/comb tejeduría) en $0 pero V1 tiene ${totales.get(1,0):,.0f}. "
              f"Raro — la tejeduría no consume nada de combustible/gas este rango.")

    # ────────────────────────────────────────────────────────────────────
    # 3) Patrones sospechosos — prefijos de concepto repartidos en >1 num
    # ────────────────────────────────────────────────────────────────────
    rows = db.fetch_all(
        """
        SELECT num, concepto, importe::numeric(12,2) AS importe
          FROM scintela.xgast
         WHERE fecha BETWEEN %s AND %s
           AND COALESCE(stat,'') NOT IN ('Y','X')
           AND concepto IS NOT NULL
        """,
        (desde, hasta),
    ) or []
    # Agrupar por las primeras 2 palabras del concepto (case-insensitive).
    grupos: dict[str, dict[int, dict]] = defaultdict(lambda: defaultdict(
        lambda: {"n": 0, "total": 0.0, "ejemplos": []}
    ))
    for r in rows:
        c = (r["concepto"] or "").strip().upper()
        parts = c.split()
        if not parts:
            continue
        key = " ".join(parts[:2]) if len(parts) >= 2 else parts[0]
        n = int(r["num"] or 0)
        g = grupos[key][n]
        g["n"] += 1
        g["total"] += float(r["importe"] or 0)
        if len(g["ejemplos"]) < 3:
            g["ejemplos"].append(c)

    # Patrones con >1 num + total > $100 son interesantes.
    multi_num = []
    for key, por_num in grupos.items():
        if len(por_num) > 1:
            total_pat = sum(g["total"] for g in por_num.values())
            if total_pat >= 100:
                multi_num.append((key, total_pat, por_num))
    multi_num.sort(key=lambda x: -x[1])

    _bloque("3) Patrones de concepto repartidos en >1 categoría")
    if not multi_num:
        print("  (todos los prefijos van consistentes a 1 sola V — bien)")
    else:
        print(f"  Mostrando top {min(args.top, len(multi_num))}:\n")
        for key, total_pat, por_num in multi_num[: args.top]:
            print(f"  prefijo {key!r}  total ${total_pat:,.2f}:")
            for num in sorted(por_num):
                g = por_num[num]
                ejs = ", ".join(g["ejemplos"][:2])
                vlabel = f"V{num}" if num else "(sin num)"
                print(f"     → {vlabel}  n={g['n']:>2}  ${g['total']:>10,.2f}  "
                      f"ej: {ejs[:60]}")
            print()

    # ────────────────────────────────────────────────────────────────────
    # 4) Top 10 conceptos en V9 — los que más pesan en "varios admin"
    # ────────────────────────────────────────────────────────────────────
    _bloque("4) Top conceptos en V9 (varios admin) — los más concentrados")
    top_v9 = db.fetch_all(
        """
        SELECT concepto, COUNT(*) AS n,
               SUM(importe)::numeric(14,2) AS total
          FROM scintela.xgast
         WHERE fecha BETWEEN %s AND %s
           AND num = 9
           AND COALESCE(stat,'') NOT IN ('Y','X')
         GROUP BY concepto
         ORDER BY total DESC
         LIMIT 12
        """,
        (desde, hasta),
    ) or []
    if top_v9:
        for r in top_v9:
            print(f"  ${float(r['total']):>10,.2f}  ({r['n']:>3} filas)  "
                  f"{(r['concepto'] or '')[:60]}")
    else:
        print("  (sin filas en V9)")

    print()
    print("─" * 70)
    print("Listo. Si V9 está sobre-concentrado, revisá "
          "modules/gastos/queries.py:KEYWORDS_TO_CATEGORIA — agregá patrones "
          "más finos (ej. 'GAS HOSP' → V9, 'GAS HOTEL' → V9, 'GAS TAXI' → V9, "
          "'GAS CELU' → V8, pero 'GAS IRENE' / 'GAS NONO' (persona) → V7).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
