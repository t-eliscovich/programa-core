"""Anular en bloque los cheques creados en una operación fallida.

Caso TMT 2026-05-15: el flujo multi-cheque creó 4 cheques (N° 002, 003,
004, 005 del cliente PUE) y aplicó parcialmente. El FIFO falló dejando
$140 sin distribuir y la usuaria quedó con 4 cheques colgados.

Este script:
  1. Lista los cheques candidatos a anular (filtra por cliente, fecha,
     o ids explícitos).
  2. Para cada uno: borra sus aplicaciones (chequesxfact) revirtiendo el
     abono/saldo/stat de la factura afectada, anula la posdat hermana, y
     marca el cheque con stat='X' (anulado).
  3. Marca los mov_doble correspondientes como 'reversado'.

Todo dentro de una sola tx por cheque (atomicity).

Usos:
    # DRY-RUN del día actual, cliente PUE
    python scripts/limpiar_cheques_fallidos.py --cliente PUE --hoy

    # APPLY con IDs explícitos
    python scripts/limpiar_cheques_fallidos.py --apply --ids 1916,1917,1918,1919

    # APPLY del cliente PUE de hoy
    python scripts/limpiar_cheques_fallidos.py --apply --cliente PUE --hoy

    # APPLY rango de fechas
    python scripts/limpiar_cheques_fallidos.py --apply --cliente PUE --desde 2026-05-15 --hasta 2026-05-15
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

import db  # noqa: E402


def cheques_candidatos(
    *,
    ids: list[int] | None,
    cliente: str | None,
    desde: str | None,
    hasta: str | None,
) -> list[dict]:
    """Lista cheques (no anulados) que matchean los filtros."""
    cond = ["COALESCE(c.stat, '') NOT IN ('X', 'Y')"]
    params: list = []
    if ids:
        placeholder = ",".join(["%s"] * len(ids))
        cond.append(f"c.id_cheque IN ({placeholder})")
        params.extend(ids)
    if cliente:
        cond.append("UPPER(TRIM(c.codigo_cli)) = %s")
        params.append(cliente.upper().strip())
    if desde:
        cond.append("c.fecha >= %s::date")
        params.append(desde)
    if hasta:
        cond.append("c.fecha <= %s::date")
        params.append(hasta)
    where_sql = " AND ".join(cond)
    return db.fetch_all(
        f"""
        SELECT c.id_cheque, c.no_cheque, c.codigo_cli, c.fecha,
               c.importe, c.stat,
               (SELECT COUNT(*) FROM scintela.chequesxfact x
                  WHERE x.id_cheque = c.id_cheque)         AS n_aplicaciones,
               (SELECT COALESCE(SUM(x.importe), 0)
                  FROM scintela.chequesxfact x
                  WHERE x.id_cheque = c.id_cheque)         AS total_aplicado
          FROM scintela.cheque c
         WHERE {where_sql}
         ORDER BY c.id_cheque
        """,
        tuple(params),
    ) or []


def anular_cheque(id_cheque: int, motivo: str, usuario: str) -> dict:
    """Anula un cheque + sus aplicaciones + posdat hermana. Atomic.

    Replica la lógica de `cheques.queries.anular_error_carga` pero
    inline para no depender del módulo Flask al correr el script.
    """
    with db.tx() as conn:
        ch = db.fetch_one(
            "SELECT id_cheque, no_cheque, codigo_cli, importe, stat "
            "FROM scintela.cheque WHERE id_cheque = %s FOR UPDATE",
            (id_cheque,),
            conn=conn,
        )
        if not ch:
            raise ValueError(f"Cheque {id_cheque} no existe.")
        if (ch.get("stat") or "").upper() == "X":
            return {"id_cheque": id_cheque, "skipped": True,
                    "razon": "ya estaba anulado (stat=X)"}

        # 1) Aplicaciones — revertir cada una en su factura.
        aplics = db.fetch_all(
            "SELECT id_chequexfact, id_fact, importe "
            "FROM scintela.chequesxfact WHERE id_cheque = %s "
            "ORDER BY id_fact",
            (id_cheque,),
            conn=conn,
        ) or []
        n_aplics = 0
        for a in aplics:
            imp_ap = float(a["importe"] or 0)
            f = db.fetch_one(
                "SELECT id_factura, numf, importe, abono "
                "FROM scintela.factura WHERE id_factura = %s FOR UPDATE",
                (a["id_fact"],),
                conn=conn,
            )
            if not f:
                # factura desaparecida; sólo borrar la aplicación
                db.execute(
                    "DELETE FROM scintela.chequesxfact "
                    "WHERE id_chequexfact = %s",
                    (a["id_chequexfact"],),
                    conn=conn,
                )
                continue
            nuevo_abono = float(f.get("abono") or 0) - imp_ap
            nuevo_saldo = float(f.get("importe") or 0) - nuevo_abono
            if nuevo_saldo <= 0.01:
                nuevo_stat = "T"
            elif nuevo_abono > 0.01:
                nuevo_stat = "A"
            else:
                nuevo_stat = "Z"
            db.execute(
                "DELETE FROM scintela.chequesxfact "
                "WHERE id_chequexfact = %s",
                (a["id_chequexfact"],),
                conn=conn,
            )
            db.execute(
                "UPDATE scintela.factura "
                "   SET abono = %s, saldo = %s, stat = %s, "
                "       usuario_modifica = %s "
                " WHERE id_factura = %s",
                (nuevo_abono, nuevo_saldo, nuevo_stat,
                 usuario, a["id_fact"]),
                conn=conn,
            )
            n_aplics += 1

        # 2) Posdat hermana si era postergado.
        db.execute(
            "UPDATE scintela.posdat "
            "   SET anulada = TRUE, "
            "       motivo_anulacion = LEFT(%s, 200), "
            "       fecha_anulacion = CURRENT_TIMESTAMP, "
            "       usuario_modifica = %s "
            " WHERE COALESCE(banc, 0) = 0 AND num = %s "
            "   AND (anulada IS NOT TRUE OR anulada IS NULL)",
            (f"anular cheque #{id_cheque} (script limpieza)", usuario, id_cheque),
            conn=conn,
        )

        # 3) Anular el cheque.
        marca = f"[X] anulado por script — {motivo}"
        db.execute(
            "UPDATE scintela.cheque "
            "   SET stat = 'X', fechaout = CURRENT_DATE, "
            "       observacion = RIGHT(COALESCE(observacion || ' | ', '') || %s, 200), "
            "       usuario_modifica = %s, fecha_modifica = CURRENT_TIMESTAMP "
            " WHERE id_cheque = %s",
            (marca, usuario, id_cheque),
            conn=conn,
        )

        # 4) mov_doble: marcar 'cheque_creado' y 'cheque_aplicado_a_factura'
        # del cheque como reversados.
        db.execute(
            "UPDATE scintela.mov_doble "
            "   SET estado = 'reversado' "
            " WHERE estado = 'activo' "
            "   AND ((origen_table = 'cheque' AND origen_id = %s) "
            "    OR  (destino_table = 'cheque' AND destino_id = %s))",
            (id_cheque, id_cheque),
            conn=conn,
        )

        return {
            "id_cheque": id_cheque,
            "no_cheque": ch.get("no_cheque"),
            "importe": float(ch.get("importe") or 0),
            "n_aplicaciones_revertidas": n_aplics,
            "skipped": False,
        }


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--apply", action="store_true",
                   help="Aplicar cambios (default dry-run).")
    p.add_argument("--ids", help="IDs de cheque separados por coma.")
    p.add_argument("--cliente", help="Código de cliente (3-5 chars).")
    p.add_argument("--desde", help="Fecha desde (YYYY-MM-DD).")
    p.add_argument("--hasta", help="Fecha hasta (YYYY-MM-DD).")
    p.add_argument("--hoy", action="store_true",
                   help="Atajo: --desde y --hasta = hoy.")
    p.add_argument("--motivo", default="multi-cheque fallido — limpieza",
                   help="Motivo de la anulación (queda en observación).")
    p.add_argument("--usuario", default="limpieza_script",
                   help="Marca en usuario_modifica.")
    args = p.parse_args()

    if args.hoy:
        hoy = date.today().isoformat()
        args.desde = args.desde or hoy
        args.hasta = args.hasta or hoy

    ids: list[int] | None = None
    if args.ids:
        try:
            ids = [int(x.strip()) for x in args.ids.split(",") if x.strip()]
        except ValueError:
            print("ERROR: --ids debe ser una lista de enteros separados por coma.")
            return 1

    if not ids and not args.cliente:
        print("ERROR: pasá --ids o --cliente (con o sin --hoy/--desde/--hasta).")
        return 1

    candidatos = cheques_candidatos(
        ids=ids, cliente=args.cliente,
        desde=args.desde, hasta=args.hasta,
    )
    mode = "APPLY" if args.apply else "DRY-RUN (no escribe nada)"
    print(f"=== Limpiar cheques fallidos · {mode} ===")
    print(f"Filtros: ids={ids} cliente={args.cliente} "
          f"desde={args.desde} hasta={args.hasta}")
    print()
    if not candidatos:
        print("Nada que hacer — 0 cheques matchean los filtros (ya anulados?).")
        return 0

    print(f"Cheques candidatos ({len(candidatos)}):")
    print(f"  {'id':>6}  {'no_chq':>8}  {'cli':<6}  {'fecha':10}  "
          f"{'importe':>12}  {'stat':<4}  {'n_aplic':>7}  {'tot_aplic':>12}")
    total = 0.0
    for c in candidatos:
        total += float(c["importe"] or 0)
        print(f"  {c['id_cheque']:>6}  {(c.get('no_cheque') or '?'):>8}  "
              f"{(c['codigo_cli'] or ''):<6}  {str(c['fecha']):10}  "
              f"$ {float(c['importe'] or 0):>10,.2f}  "
              f"{(c['stat'] or '?'):<4}  "
              f"{c['n_aplicaciones']:>7}  "
              f"$ {float(c['total_aplicado'] or 0):>10,.2f}")
    print(f"  {'':>6}  {'':>8}  {'':<6}  {'TOTAL':10}  $ {total:>10,.2f}")
    print()

    if not args.apply:
        print(">>> DRY-RUN — sólo listado. Revisá los cheques arriba.")
        print(">>> Si están bien, volvé a correr con --apply.")
        return 0

    print(f"Anulando {len(candidatos)} cheques (motivo: {args.motivo!r})…")
    print()
    n_ok = 0
    n_skipped = 0
    n_err = 0
    for c in candidatos:
        try:
            r = anular_cheque(int(c["id_cheque"]),
                              motivo=args.motivo,
                              usuario=args.usuario)
            if r.get("skipped"):
                n_skipped += 1
                print(f"  [SKIP] cheque #{r['id_cheque']}: {r.get('razon')}")
            else:
                n_ok += 1
                print(f"  [OK]   cheque #{r['id_cheque']} "
                      f"N° {r.get('no_cheque')!r} "
                      f"$ {r['importe']:,.2f} — "
                      f"{r['n_aplicaciones_revertidas']} aplic. revertidas")
        except Exception as e:  # noqa: BLE001
            n_err += 1
            print(f"  [ERR]  cheque #{c['id_cheque']}: {type(e).__name__}: {e}")

    print()
    print(f"=== Resumen: {n_ok} anulados · {n_skipped} skipped · {n_err} errores ===")
    if n_err > 0:
        print(">>> Hubo errores. Revisá y corré de nuevo (es idempotente).")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
