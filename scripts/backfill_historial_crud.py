"""Backfill retroactivo de mov_doble para compras / facturas / gastos.

Hasta el 2026-05-13, las funciones `compras.crear()`, `facturas.crear()` y
`gastos.crear()` NO registraban `mov_doble` en los caminos sin side-effect
bancario (compras a crédito, facturas emitidas, gastos al contado). Resultado:
miles de filas legacy + las creadas antes del fix de hoy no aparecen en
`/historial`.

Este script escanea las tres tablas, encuentra filas sin `mov_doble`
asociado, y crea un registro retroactivo:

  - `scintela.compra` sin mov_doble:
      - Si hay una `posdat` hermana (mismo prov + mismo num + importe ~=
        saldo) → tipo='compra_a_posdat', destino=posdat.
      - Si no hay posdat (compra pagada o sin contrapartida visible) →
        tipo='compra_backfill', auto-referencia (compra→compra).
  - `scintela.factura` sin mov_doble → tipo='factura_emitida', auto-ref.
  - `scintela.xgast` sin mov_doble → tipo='gasto_simple' (stat='A'/NULL)
    o 'gasto_a_posdat' (stat='P' con fechad).

Todos los registros llevan `metadata.backfill = true` y `concepto` arranca
con prefijo `[backfill] `. Si necesitás revertir todo:

    DELETE FROM scintela.mov_doble
     WHERE estado='activo' AND metadata->>'backfill' = 'true';

Uso:
    python scripts/backfill_historial_crud.py                   # DRY RUN
    python scripts/backfill_historial_crud.py --apply           # ejecuta
    python scripts/backfill_historial_crud.py --apply --limit 100
    python scripts/backfill_historial_crud.py --only compras    # una sola tabla
    python scripts/backfill_historial_crud.py --only facturas
    python scripts/backfill_historial_crud.py --only gastos

NO modifica las filas existentes — solo INSERTA en `scintela.mov_doble`.
NUNCA toca saldos, importes ni nada que afecte el balance.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import db  # noqa: E402
import mov_doble as _md  # noqa: E402


# ─────────────────────── Helpers ───────────────────────


def _stats() -> dict:
    return {
        "compras_total": 0,
        "compras_sin_md": 0,
        "compras_con_posdat": 0,
        "compras_self_ref": 0,
        "compras_skipped": 0,
        "facturas_total": 0,
        "facturas_sin_md": 0,
        "gastos_total": 0,
        "gastos_sin_md": 0,
        "gastos_self_ref": 0,
        "gastos_a_posdat": 0,
        "errores": 0,
    }


def _registrar_compra(conn, compra: dict, dry: bool) -> tuple[str, str]:
    """Devuelve (tipo_registrado, destino_descripcion). Si dry=True, no INSERTA."""
    cod_prov = (compra.get("codigo_prov") or "").upper().strip()
    num = compra.get("numero")
    importe = float(compra.get("importe") or 0)
    fecha = compra.get("fecha")
    if importe <= 0 or fecha is None:
        return ("skip", "importe/fecha vacíos")

    # ¿Hay posdat hermana? (mismo prov + mismo num + cualquier saldo positivo)
    posdat = db.fetch_one(
        """
        SELECT id_posdat, importe FROM scintela.posdat
         WHERE UPPER(TRIM(COALESCE(prov,''))) = %s
           AND num = %s
           AND COALESCE(banc,0) = 0
         LIMIT 1
        """,
        (cod_prov, num),
        conn=conn,
    )

    concepto_base = (
        compra.get("concepto") or compra.get("comprobante") or
        f"Compra #{num} {cod_prov}"
    )
    concepto = f"[backfill] {concepto_base}"[:200]
    usuario = compra.get("usuario_crea") or "backfill"

    if posdat and posdat.get("id_posdat"):
        if not dry:
            _md.registrar(
                conn=conn,
                tipo="compra_a_posdat",
                origen_table="compra",
                origen_id=compra["id_compra"],
                destino_table="posdat",
                destino_id=posdat["id_posdat"],
                importe=importe,
                fecha=fecha,
                concepto=concepto,
                usuario=usuario,
                metadata={"backfill": True, "codigo_prov": cod_prov,
                          "numero_compra": num},
            )
        return ("compra_a_posdat", f"posdat #{posdat['id_posdat']}")

    if not dry:
        _md.registrar(
            conn=conn,
            tipo="compra_backfill",
            origen_table="compra",
            origen_id=compra["id_compra"],
            destino_table="compra",
            destino_id=compra["id_compra"],
            importe=importe,
            fecha=fecha,
            concepto=concepto,
            usuario=usuario,
            metadata={"backfill": True, "codigo_prov": cod_prov,
                      "numero_compra": num},
        )
    return ("compra_backfill", "self-ref")


def _registrar_factura(conn, factura: dict, dry: bool) -> str:
    cod_cli = (factura.get("codigo_cli") or "").upper().strip()
    numf = factura.get("numf")
    importe = float(factura.get("importe") or 0)
    fecha = factura.get("fecha")
    if abs(importe) < 0.01 or fecha is None:
        return "skip"
    concepto = f"[backfill] Factura #{numf} {cod_cli}"[:200]
    usuario = factura.get("usuario_crea") or "backfill"
    if not dry:
        _md.registrar(
            conn=conn,
            tipo="factura_emitida",
            origen_table="factura",
            origen_id=factura["id_factura"],
            destino_table="factura",
            destino_id=factura["id_factura"],
            importe=importe,
            fecha=fecha,
            concepto=concepto,
            usuario=usuario,
            metadata={"backfill": True, "codigo_cli": cod_cli, "numf": numf,
                      "kg": float(factura.get("kg") or 0)},
        )
    return "factura_emitida"


def _registrar_gasto(conn, gasto: dict, dry: bool) -> str:
    importe = float(gasto.get("importe") or 0)
    fecha = gasto.get("fecha")
    if importe <= 0 or fecha is None:
        return "skip"
    stat = (gasto.get("stat") or "").upper()
    pagado = stat in ("A", "")  # NULL/A → pagado contado; P → pendiente
    tipo_md = "gasto_simple" if pagado else "gasto_a_posdat"
    doc = (gasto.get("doc") or "OTR").upper()
    concepto_raw = gasto.get("concepto") or ""
    concepto = f"[backfill] Gasto #{gasto.get('num')} {doc} — {concepto_raw}"[:200]
    usuario = gasto.get("usuario_crea") or "backfill"
    if not dry:
        _md.registrar(
            conn=conn,
            tipo=tipo_md,
            origen_table="xgast",
            origen_id=gasto["id_xgast"],
            destino_table="xgast",
            destino_id=gasto["id_xgast"],
            importe=importe,
            fecha=fecha,
            concepto=concepto,
            usuario=usuario,
            metadata={"backfill": True, "doc": doc,
                      "prov": gasto.get("prov") or "",
                      "pagado": pagado},
        )
    return tipo_md


# ─────────────────────── Main loops ───────────────────────


def procesar_compras(stats: dict, dry: bool, limit: int | None) -> list[dict]:
    """Compras sin mov_doble."""
    sample: list[dict] = []
    sql_limit = "" if not limit else f"LIMIT {int(limit)}"
    rows = db.fetch_all(
        f"""
        SELECT c.id_compra, c.fecha, c.codigo_prov, c.numero, c.importe,
               c.concepto, c.comprobante, c.usuario_crea
          FROM scintela.compra c
         WHERE c.stat IS DISTINCT FROM 'Y'
           AND NOT EXISTS (
               SELECT 1 FROM scintela.mov_doble md
                WHERE md.origen_table = 'compra'
                  AND md.origen_id    = c.id_compra
           )
         ORDER BY c.id_compra
         {sql_limit}
        """
    ) or []
    stats["compras_total"] = len(rows)
    with db.tx() as conn:
        for c in rows:
            try:
                tipo, dest = _registrar_compra(conn, c, dry)
                if tipo == "skip":
                    stats["compras_skipped"] += 1
                    continue
                stats["compras_sin_md"] += 1
                if tipo == "compra_a_posdat":
                    stats["compras_con_posdat"] += 1
                else:
                    stats["compras_self_ref"] += 1
                if len(sample) < 5:
                    sample.append({"id_compra": c["id_compra"],
                                   "prov": c.get("codigo_prov"),
                                   "importe": float(c.get("importe") or 0),
                                   "tipo": tipo, "destino": dest})
            except Exception as e:
                stats["errores"] += 1
                print(f"  ERROR compra id={c.get('id_compra')}: {e}")
        if dry:
            conn.rollback()
    return sample


def procesar_facturas(stats: dict, dry: bool, limit: int | None) -> list[dict]:
    sample: list[dict] = []
    sql_limit = "" if not limit else f"LIMIT {int(limit)}"
    rows = db.fetch_all(
        f"""
        SELECT f.id_factura, f.fecha, f.codigo_cli, f.numf, f.importe,
               f.kg, f.usuario_crea
          FROM scintela.factura f
         WHERE f.stat IS DISTINCT FROM 'X'
           AND f.stat IS DISTINCT FROM 'Y'
           AND NOT EXISTS (
               SELECT 1 FROM scintela.mov_doble md
                WHERE md.origen_table = 'factura'
                  AND md.origen_id    = f.id_factura
           )
         ORDER BY f.id_factura
         {sql_limit}
        """
    ) or []
    stats["facturas_total"] = len(rows)
    with db.tx() as conn:
        for f in rows:
            try:
                tipo = _registrar_factura(conn, f, dry)
                if tipo != "skip":
                    stats["facturas_sin_md"] += 1
                    if len(sample) < 5:
                        sample.append({"id_factura": f["id_factura"],
                                       "cli": f.get("codigo_cli"),
                                       "numf": f.get("numf"),
                                       "importe": float(f.get("importe") or 0)})
            except Exception as e:
                stats["errores"] += 1
                print(f"  ERROR factura id={f.get('id_factura')}: {e}")
        if dry:
            conn.rollback()
    return sample


def procesar_gastos(stats: dict, dry: bool, limit: int | None) -> list[dict]:
    sample: list[dict] = []
    sql_limit = "" if not limit else f"LIMIT {int(limit)}"
    rows = db.fetch_all(
        f"""
        SELECT g.id_xgast, g.fecha, g.doc, g.prov, g.num, g.importe,
               g.stat, g.concepto, g.usuario_crea
          FROM scintela.xgast g
         WHERE g.stat IS DISTINCT FROM 'Y'
           AND NOT EXISTS (
               SELECT 1 FROM scintela.mov_doble md
                WHERE md.origen_table = 'xgast'
                  AND md.origen_id    = g.id_xgast
           )
         ORDER BY g.id_xgast
         {sql_limit}
        """
    ) or []
    stats["gastos_total"] = len(rows)
    with db.tx() as conn:
        for g in rows:
            try:
                tipo = _registrar_gasto(conn, g, dry)
                if tipo == "skip":
                    continue
                stats["gastos_sin_md"] += 1
                if tipo == "gasto_simple":
                    stats["gastos_self_ref"] += 1
                else:
                    stats["gastos_a_posdat"] += 1
                if len(sample) < 5:
                    sample.append({"id_xgast": g["id_xgast"],
                                   "doc": g.get("doc"),
                                   "num": g.get("num"),
                                   "importe": float(g.get("importe") or 0),
                                   "tipo": tipo})
            except Exception as e:
                stats["errores"] += 1
                print(f"  ERROR gasto id={g.get('id_xgast')}: {e}")
        if dry:
            conn.rollback()
    return sample


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--apply", action="store_true",
                   help="Ejecuta los INSERTs (sin esto, solo dry-run).")
    p.add_argument("--limit", type=int, default=None,
                   help="Procesar solo N filas por tabla (para probar).")
    p.add_argument("--only", choices=("compras", "facturas", "gastos"),
                   default=None, help="Procesar solo una tabla.")
    args = p.parse_args()

    dry = not args.apply
    modo = "DRY RUN (no INSERTA)" if dry else "APLICANDO INSERTS"
    print(f"┌─ Backfill historial CRUD — {modo}")
    if args.limit:
        print(f"│  Limit: {args.limit} filas por tabla")
    if args.only:
        print(f"│  Solo: {args.only}")
    print("└─")

    stats = _stats()

    if not args.only or args.only == "compras":
        print("\n┌─ Compras sin mov_doble")
        sample = procesar_compras(stats, dry, args.limit)
        for s in sample:
            prov_str = (s['prov'] or '—')[:5]
            print(f"│  id={s['id_compra']:>6} prov={prov_str:<5} "
                  f"imp=${s['importe']:>12,.2f} → {s['tipo']} ({s['destino']})")
        print(f"└─ encontradas: {stats['compras_total']}  "
              f"a registrar: {stats['compras_sin_md']}  "
              f"(con posdat: {stats['compras_con_posdat']}, "
              f"self-ref: {stats['compras_self_ref']}, "
              f"skipped: {stats['compras_skipped']})")

    if not args.only or args.only == "facturas":
        print("\n┌─ Facturas sin mov_doble")
        sample = procesar_facturas(stats, dry, args.limit)
        for s in sample:
            print(f"│  id={s['id_factura']:>6} cli={s['cli']:<5} "
                  f"numf={s['numf']:>6} imp=${s['importe']:>12,.2f}")
        print(f"└─ encontradas: {stats['facturas_total']}  "
              f"a registrar: {stats['facturas_sin_md']}")

    if not args.only or args.only == "gastos":
        print("\n┌─ Gastos sin mov_doble")
        sample = procesar_gastos(stats, dry, args.limit)
        for s in sample:
            doc_str = (s['doc'] or '—')[:4]
            num_str = str(s['num']) if s['num'] is not None else '—'
            print(f"│  id={s['id_xgast']:>6} doc={doc_str:<4} "
                  f"num={num_str:>4} imp=${s['importe']:>12,.2f} → {s['tipo']}")
        print(f"└─ encontrados: {stats['gastos_total']}  "
              f"a registrar: {stats['gastos_sin_md']}  "
              f"(simple: {stats['gastos_self_ref']}, "
              f"posdat: {stats['gastos_a_posdat']})")

    print(f"\nTotal errores: {stats['errores']}")
    if dry:
        print("\n⚠ DRY RUN — nada se INSERTÓ. Volvé a correr con --apply para ejecutar.")
    else:
        print("\n✓ Backfill terminado. Verificá /historial.")
    return 0 if stats["errores"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
