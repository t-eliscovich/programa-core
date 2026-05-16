"""Backfill: clasificar egresos de caja como gastos V1..V9 retroactivamente.

Para cada egreso (tipo='S') que NO tenga `mov_doble` linkeándolo a `xgast`,
intenta auto-clasificarlo usando `gastos.queries.sugerir_categoria` (el
mismo diccionario de keywords que usa el form manual y el dispatcher
on-the-fly).

  - Si el concepto matchea un keyword (PINTURA→V6, LUZ→V5, SUELDOS→V7,
    etc.), crea fila xgast + mov_doble linkeando caja→xgast (y anula la
    compra falsa si existe, igual que el flow manual).
  - Si NO matchea, lo deja como está. Esos van a aparecer con el botón
    `ⓘ Clasificar` en /caja para que la usuaria los procese a mano.

Idempotente: si ya hay mov_doble caja→xgast para una caja, no la toca.

Uso:
    python scripts/backfill_clasificar_caja.py --dry-run         # default
    python scripts/backfill_clasificar_caja.py --apply
    python scripts/backfill_clasificar_caja.py --apply --mes-actual
    python scripts/backfill_clasificar_caja.py --apply --desde 2026-01-01
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

import db  # noqa: E402
from modules.gastos import queries as gastos_q  # noqa: E402


def candidatos(desde: str | None, hasta: str | None) -> list[dict]:
    """Egresos S sin mov_doble caja→xgast en el rango."""
    cond = []
    params: list = []
    if desde:
        cond.append("c.fecha >= %s::date")
        params.append(desde)
    if hasta:
        cond.append("c.fecha <= %s::date")
        params.append(hasta)
    extra = (" AND " + " AND ".join(cond)) if cond else ""
    # TMT 2026-05-15: excluir prefijos NO-gasto (PICH/INTER/RR/IN./INHB)
    # — esos son transfers/retiros/dolares/capital, no gastos. La data
    # legacy puede no tener mov_doble retroactivo, así que filtramos por
    # prefijo. Pasamos como parámetros para evitar líos con `%` y psycopg2.
    PREFIJOS_NO_GASTO = ["PICH%", "INTER%", "RR%", "IN.%", "INHB%"]
    no_like_clauses = " AND ".join(
        ["UPPER(TRIM(COALESCE(c.concepto, ''))) NOT LIKE %s"]
        * len(PREFIJOS_NO_GASTO)
    )
    return db.fetch_all(
        f"""
        SELECT c.id_caja, c.fecha, c.importe, c.concepto, c.clave
          FROM scintela.caja c
         WHERE c.tipo='S'
           AND NOT EXISTS (
             SELECT 1 FROM scintela.mov_doble md
              WHERE md.origen_table='caja'
                AND md.origen_id=c.id_caja
                AND md.destino_table='xgast'
                AND md.estado='activo'
           )
           AND {no_like_clauses}
           {extra}
         ORDER BY c.fecha, c.id_caja
        """,
        tuple(PREFIJOS_NO_GASTO + params),
    ) or []


def run(*, apply: bool, desde: str | None, hasta: str | None,
        usuario: str) -> dict:
    egresos = candidatos(desde, hasta)
    n_total = len(egresos)
    n_matched = 0
    n_skipped = 0
    n_creados = 0
    n_errores = 0
    n_compras_anuladas = 0

    por_num: dict[int, int] = {n: 0 for n in range(1, 10)}
    sin_match: list[dict] = []

    for c in egresos:
        concepto = (c.get("concepto") or "").strip()
        num = gastos_q.sugerir_categoria(concepto)
        if num is None:
            n_skipped += 1
            sin_match.append({
                "id_caja": c["id_caja"],
                "fecha": str(c["fecha"]),
                "importe": float(c["importe"] or 0),
                "concepto": concepto,
            })
            continue
        n_matched += 1
        por_num[num] += 1
        if not apply:
            continue
        try:
            r = gastos_q.clasificar_desde_caja(
                id_caja=int(c["id_caja"]),
                num=int(num),
                usuario=usuario,
            )
            if not r.get("ya_existia"):
                n_creados += 1
                if r.get("compra_anulada"):
                    n_compras_anuladas += 1
        except Exception as e:
            n_errores += 1
            print(f"[ERROR] caja #{c['id_caja']} ({concepto!r}): {e}")

    return {
        "n_total": n_total,
        "n_matched": n_matched,
        "n_skipped": n_skipped,
        "n_creados": n_creados,
        "n_errores": n_errores,
        "n_compras_anuladas": n_compras_anuladas,
        "por_num": por_num,
        "sin_match": sin_match,
    }


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--apply", action="store_true",
                   help="Aplicar cambios (default dry-run).")
    p.add_argument("--desde", help="Fecha desde (YYYY-MM-DD).")
    p.add_argument("--hasta", help="Fecha hasta (YYYY-MM-DD).")
    p.add_argument("--mes-actual", action="store_true",
                   help="Atajo: --desde primer día del mes en curso.")
    p.add_argument("--usuario", default="backfill_clasif",
                   help="Marcado en xgast.usuario_crea + mov_doble.usuario.")
    p.add_argument("--max-no-match", type=int, default=20,
                   help="Cuántos sin-match mostrar (default 20).")
    args = p.parse_args()

    if args.mes_actual and not args.desde:
        from datetime import date
        hoy = date.today()
        args.desde = hoy.replace(day=1).isoformat()

    mode = "APPLY" if args.apply else "DRY-RUN (no escribe nada)"
    rango = f"desde={args.desde or 'inicio'} hasta={args.hasta or 'hoy'}"
    print(f"=== Backfill clasificar caja → xgast · {mode} · {rango} ===")
    print()

    r = run(
        apply=args.apply,
        desde=args.desde,
        hasta=args.hasta,
        usuario=args.usuario,
    )

    print(f"Egresos analizados:      {r['n_total']}")
    print(f"Con keyword match:       {r['n_matched']}")
    print(f"Sin match (manual):      {r['n_skipped']}")
    if args.apply:
        print(f"Filas xgast creadas:     {r['n_creados']}")
        print(f"Compras falsas anuladas: {r['n_compras_anuladas']}")
        print(f"Errores:                 {r['n_errores']}")
    print()
    print("Distribución por categoría:")
    LABELS = {
        1: "V1 Personal tej", 2: "V2 Gas/Comb tej", 3: "V3 Varios tej",
        4: "V4 Personal tin", 5: "V5 Gas/Comb tin", 6: "V6 Varios tin",
        7: "V7 Personal adm", 8: "V8 Servicios adm", 9: "V9 Varios adm",
    }
    for n in range(1, 10):
        cnt = r["por_num"][n]
        if cnt:
            print(f"  {LABELS[n]:25s} {cnt:5d}")

    if r["sin_match"]:
        print()
        print(f"Sin match (primeros {args.max_no_match} de "
              f"{len(r['sin_match'])}):")
        for s in r["sin_match"][: args.max_no_match]:
            print(f"  #{s['id_caja']:6d} {s['fecha']} "
                  f"$ {s['importe']:>10,.2f}  {s['concepto']!r}")

    print()
    if not args.apply:
        print(">>> Ejecuté DRY-RUN. Revisá lo de arriba y si pinta bien,")
        print(">>> volvé a correr con --apply para hacer los cambios.")
    else:
        print(">>> Done. Si algo salió mal, podés reversar entrando a")
        print(">>> /historial y reversando los mov_doble tipo "
              "'caja_s_to_xgast'.")

    return 0 if r["n_errores"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
