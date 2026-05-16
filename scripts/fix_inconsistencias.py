"""Repara las inconsistencias que `listar_inconsistencias.py` muestra.

Dry-run por default — sin flags NO toca nada, sólo te dice qué haría.
Con `--apply` y confirmación por sección, ejecuta los UPDATEs.

Secciones:

  A) mov_doble reversados sin id_reverso
     - Si existe una fila reverso con id_original = alta.id_mov_doble →
       LINKEA (UPDATE alta.id_reverso = reverso.id_mov_doble). Es la
       reparación limpia: el reverso sí se creó, sólo faltó el UPDATE.
     - Si NO existe un reverso candidato → REVIERTE estado='activo'. La
       alta había sido marcada reversada por error (el reverso no se
       llegó a crear), así que se "des-marca".

  B) mov_doble con id_reverso apuntando a algo roto
     - Si la fila apuntada tiene id_original = alta.id_mov_doble y estado
       != 'reverso' → FIXEA su estado a 'reverso'. Caso típico: el reverso
       se registró pero quedó con estado='activo' por bug. Auto-id valida
       que es realmente nuestro reverso.
     - Si la fila apuntada tiene id_original distinto → NO toca. Reporta
       para revisión manual.

  C) Facturas con saldo ≠ importe - abono
     - Recalcula saldo = importe - abono y actualiza stat según el
       saldo nuevo (Z=0 abonos, A=parcial, T=cobrada total).
     - NO toca `abono` — eso requiere conciliar contra chequesxfact
     - sumar_chequesxfact y compararlo es lo que hace el modo `--auditoría`.

  D) Caja S sin clasificar — NO se auto-repara. Sólo te recuerda ir a
     `/gastos/clasificar/<id_caja>`.

Reglas:
  - Cada sección corre en su propio `db.tx()` — si una falla, rollback de
    esa sección, las otras siguen.
  - Confirmación por sección (no por fila — sería agonía con 26 filas).
  - Audit: cada UPDATE escribe el `id_mov_doble` reparado en `usuario_modifica`
    como `fix_inconsistencias_YYYYMMDD` para trazar después.

Uso:
    python scripts/fix_inconsistencias.py                  # dry-run de todas
    python scripts/fix_inconsistencias.py --solo A         # dry-run sólo sec A
    python scripts/fix_inconsistencias.py --apply --solo A # aplica sec A
    python scripts/fix_inconsistencias.py --apply --yes    # apply sin pregunta
    python scripts/fix_inconsistencias.py --auditoria      # extra: para C,
                                                           # cruzar contra chequesxfact
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import db  # noqa: E402

USUARIO_FIX = f"fix_inconsistencias_{date.today().strftime('%Y%m%d')}"


# ───────────────────────────────────────────────────────────────────────────
# Helpers
# ───────────────────────────────────────────────────────────────────────────
def _seccion(t: str) -> None:
    print(f"\n┌─ {t} " + "─" * max(0, 70 - len(t)))


def _money(x) -> str:
    return f"${float(x or 0):>12,.2f}"


def _confirmar(msg: str, yes_all: bool) -> bool:
    if yes_all:
        print(f"  {msg}  → autoconfirmado")
        return True
    resp = input(f"  {msg}  [y/N] ").strip().lower()
    return resp in ("y", "yes", "s", "si", "sí")


# ───────────────────────────────────────────────────────────────────────────
# A) mov_doble reversados sin id_reverso
# ───────────────────────────────────────────────────────────────────────────
def plan_seccion_A() -> tuple[list[dict], list[dict], list[dict]]:
    """Clasifica las 26 mov_doble reversados sin id_reverso en 3 buckets:

    - linkables:   tienen un reverso candidato → UPDATE alta.id_reverso.
    - cosmeticos:  la entidad subyacente (cheque/factura/compra) está REALMENTE
                   anulada — el mov_doble dice la verdad, sólo falta la fila
                   reverso para el audit. NO se toca data, sólo se reporta.
                   Auto-fix opcional: crear una fila reverso sintética que
                   documente "reverso retroactivo".
    - sospechosos: la entidad subyacente está activa pero el mov_doble dice
                   reversado. Inconsistencia REAL — revisar caso por caso.

    El motivo de partir asi: en el 2026-05-15 vimos que `anular_por_error_de_carga`
    anula el cheque (stat='X') pero a veces falla en registrar la fila reverso
    del mov_doble. En esos casos la realidad subyacente sí está reversada;
    sólo falta el audit. NO debemos revertir esos a 'activo'.
    """
    rows = db.fetch_all(
        """
        SELECT m.id_mov_doble, m.tipo, m.fecha_operacion,
               m.origen_table, m.origen_id, m.destino_table, m.destino_id,
               m.importe,
               (SELECT r.id_mov_doble
                  FROM scintela.mov_doble r
                 WHERE r.id_original = m.id_mov_doble
                   AND r.estado = 'reverso'
                 ORDER BY r.id_mov_doble DESC
                 LIMIT 1) AS reverso_candidato,
               (SELECT ch.stat FROM scintela.cheque ch
                 WHERE m.origen_table='cheque' AND ch.id_cheque=m.origen_id) AS cheque_stat,
               (SELECT cp.stat FROM scintela.compra cp
                 WHERE m.origen_table='compra' AND cp.id_compra=m.origen_id) AS compra_stat,
               (SELECT cp.stat FROM scintela.compra cp
                 WHERE m.destino_table='compra' AND cp.id_compra=m.destino_id) AS compra_dest_stat
          FROM scintela.mov_doble m
         WHERE m.estado = 'reversado'
           AND m.id_reverso IS NULL
         ORDER BY m.id_mov_doble
        """
    ) or []
    linkables = []
    cosmeticos = []
    sospechosos = []
    for r in rows:
        if r["reverso_candidato"]:
            linkables.append(r)
            continue
        # ¿La entidad subyacente está anulada?
        entidad_anulada = (
            (r["origen_table"] == "cheque" and r["cheque_stat"] == "X") or
            (r["origen_table"] == "compra" and r["compra_stat"] == "Y") or
            (r["destino_table"] == "compra" and r["compra_dest_stat"] == "Y")
        )
        # Para cheque_aplicado_a_factura el cheque puede estar X o no — si X,
        # el reverso es coherente; si no, raro.
        if entidad_anulada:
            cosmeticos.append(r)
        else:
            sospechosos.append(r)
    return linkables, cosmeticos, sospechosos


def imprimir_plan_A(linkables, cosmeticos, sospechosos) -> None:
    _seccion(
        f"A) {len(linkables)} linkables · {len(cosmeticos)} cosméticos · "
        f"{len(sospechosos)} sospechosos"
    )
    if linkables:
        print("  ↳ LINKEAR (existe reverso candidato → UPDATE alta.id_reverso):")
        for r in linkables[:10]:
            print(f"     #{r['id_mov_doble']:<5} {r['tipo']:<28} "
                  f"{_money(r['importe'])}  → id_reverso={r['reverso_candidato']}")
        if len(linkables) > 10:
            print(f"     ... y {len(linkables) - 10} más")
    if cosmeticos:
        print("  ↳ COSMÉTICO (la entidad ya está anulada — el mov_doble "
              "dice la verdad, sólo falta el reverso del audit):")
        for r in cosmeticos[:10]:
            ent = (f"cheque#{r['origen_id']}" if r["origen_table"] == "cheque"
                   else f"{r['origen_table']}#{r['origen_id']}")
            print(f"     #{r['id_mov_doble']:<5} {r['tipo']:<28} "
                  f"{_money(r['importe'])}  ({ent} stat='X')")
        if len(cosmeticos) > 10:
            print(f"     ... y {len(cosmeticos) - 10} más")
        print("  · Para los cosméticos, este script NO toca data (sería "
              "peor crearles un 'activo'). Sólo se reporta.")
    if sospechosos:
        print("  ↳ SOSPECHOSO (mov_doble dice reversado pero la entidad "
              "sigue activa — bug real):")
        for r in sospechosos:
            print(f"     #{r['id_mov_doble']:<5} {r['tipo']:<28} "
                  f"{_money(r['importe'])}  "
                  f"orig:{r['origen_table']}#{r['origen_id']}  "
                  f"dest:{r['destino_table']}#{r['destino_id']}")
        print("  · REVISAR a mano antes de aplicar. No automatizo.")


def aplicar_seccion_A(linkables, cosmeticos, sospechosos) -> int:
    """SÓLO aplica los linkables — el resto requiere decisión humana."""
    if not linkables:
        return 0
    with db.tx() as conn:
        n = 0
        for r in linkables:
            db.execute(
                """
                UPDATE scintela.mov_doble
                   SET id_reverso = %s
                 WHERE id_mov_doble = %s
                   AND estado='reversado'
                   AND id_reverso IS NULL
                """,
                (int(r["reverso_candidato"]), int(r["id_mov_doble"])),
                conn=conn,
            )
            n += 1
        return n


# ───────────────────────────────────────────────────────────────────────────
# B) link roto — reverso apuntado existe pero estado≠'reverso'
# ───────────────────────────────────────────────────────────────────────────
def plan_seccion_B() -> tuple[list[dict], list[dict]]:
    rows = db.fetch_all(
        """
        SELECT m.id_mov_doble AS alta_id, m.tipo, m.id_reverso,
               r.id_mov_doble AS rev_id, r.estado AS rev_estado,
               r.id_original AS rev_id_original, r.tipo AS rev_tipo
          FROM scintela.mov_doble m
          JOIN scintela.mov_doble r ON r.id_mov_doble = m.id_reverso
         WHERE m.estado = 'reversado'
           AND r.estado <> 'reverso'
         ORDER BY m.id_mov_doble
        """
    ) or []
    # auto-id válido si el reverso linkeado dice que ES de esta alta
    fixables    = [r for r in rows if r["rev_id_original"] == r["alta_id"]]
    no_fixables = [r for r in rows if r["rev_id_original"] != r["alta_id"]]
    return fixables, no_fixables


def imprimir_plan_B(fixables, no_fixables) -> None:
    _seccion(f"B) {len(fixables)} fixables + {len(no_fixables)} para revisar")
    for r in fixables:
        print(f"  ↳ FIX  alta#{r['alta_id']} ({r['tipo']}) — su reverso "
              f"#{r['rev_id']} pasa de estado={r['rev_estado']!r} → 'reverso'")
    for r in no_fixables:
        print(f"  ↳ SKIP alta#{r['alta_id']} apunta a #{r['rev_id']} pero "
              f"id_original de #{r['rev_id']} es {r['rev_id_original']} — "
              f"link corrupto, revisar manualmente")


def aplicar_seccion_B(fixables) -> int:
    if not fixables:
        return 0
    with db.tx() as conn:
        n = 0
        for r in fixables:
            db.execute(
                """
                UPDATE scintela.mov_doble
                   SET estado = 'reverso'
                 WHERE id_mov_doble = %s
                   AND id_original = %s
                   AND estado <> 'reverso'
                """,
                (int(r["rev_id"]), int(r["alta_id"])),
                conn=conn,
            )
            n += 1
        return n


# ───────────────────────────────────────────────────────────────────────────
# C) facturas con saldo ≠ importe - abono
# ───────────────────────────────────────────────────────────────────────────
def plan_seccion_C(auditoria: bool = False) -> list[dict]:
    """Lista facturas a reparar.

    Reparación default: saldo = importe - abono, stat recalculado.
    Si `auditoria=True`, además cruza contra SUM(chequesxfact) — útil
    cuando sospechás que el `abono` también está mal.
    """
    rows = db.fetch_all(
        """
        SELECT f.id_factura,
               COALESCE(f.numf::text,'') AS numf,
               COALESCE(f.codigo_cli,'') AS codigo_cli,
               COALESCE(f.importe,0)::numeric(14,2) AS importe,
               COALESCE(f.abono,0)::numeric(14,2)   AS abono,
               COALESCE(f.saldo,0)::numeric(14,2)   AS saldo,
               COALESCE(f.stat,'') AS stat,
               (SELECT COALESCE(SUM(cf.importe),0)::numeric(14,2)
                  FROM scintela.chequesxfact cf
                 WHERE cf.id_fact = f.id_factura) AS suma_cf
          FROM scintela.factura f
         WHERE COALESCE(f.stat,'') NOT IN ('X','Y')
           AND ABS(COALESCE(f.importe,0)
                   - COALESCE(f.abono,0)
                   - COALESCE(f.saldo,0)) > 0.01
           AND NOT (
             COALESCE(f.stat,'') = 'T'
             AND ABS(COALESCE(f.saldo,0)) < 0.01
             AND COALESCE(f.abono,0) >= COALESCE(f.importe,0)
           )
         ORDER BY ABS(COALESCE(f.importe,0)
                      - COALESCE(f.abono,0)
                      - COALESCE(f.saldo,0)) DESC
        """
    ) or []
    return rows


def _propuesta_C(stat: str, importe: float, abono: float,
                 saldo_actual: float) -> tuple[float, str, str]:
    """Devuelve (nuevo_saldo, nuevo_stat, motivo).

    Reglas (TMT 2026-05-15, después de ver la data real):

    - Si stat='T' (cobrada total) — la fact está PAGADA. Respetamos eso:
      saldo debe ser 0. NO tocamos abono (puede tener sobre-pago histórico
      del backfill DBF — es real, lo perdemos si normalizamos).
    - Si stat='A' (parcial):
        - Si abono >= importe → debería ser T con saldo=0.
        - Si abono < importe  → saldo = importe - abono (>0).
    - Si stat='Z' (emitida sin abonos) y abono>0 → es A o T según corresponda.
    - Si stat='' (legacy) — tratar como A.

    Nunca dejamos saldo negativo. Si los números dicen abono>importe, eso
    se respeta como sobre-pago y saldo=0; el delta queda como rastro en el
    abono (que es lo que en dBase se hacía).
    """
    if stat == "T":
        return 0.0, "T", "stat=T → saldo=0 (cobrada total)"
    # Para A / Z / vacío: lógica común.
    if abono >= importe - 0.01:
        return 0.0, "T", "abono≥importe → ascender a T con saldo=0"
    if abono > 0.01:
        return round(importe - abono, 2), "A", "abono parcial → saldo=imp-abono"
    return round(importe, 2), "Z", "sin abonos → Z con saldo=importe"


def imprimir_plan_C(rows, auditoria: bool) -> None:
    _seccion(f"C) {len(rows)} facturas con saldo invariant roto")
    cambios_stat = 0
    cambios_solo_saldo = 0
    for r in rows[:20]:
        imp = float(r["importe"] or 0)
        ab  = float(r["abono"] or 0)
        sa  = float(r["saldo"] or 0)
        suma_cf = float(r["suma_cf"] or 0)
        nuevo_saldo, nuevo_stat, _ = _propuesta_C(r["stat"], imp, ab, sa)
        if nuevo_stat != r["stat"]:
            cambios_stat += 1
        elif abs(nuevo_saldo - sa) > 0.01:
            cambios_solo_saldo += 1
        flag = ""
        if auditoria and abs(suma_cf - ab) > 0.01:
            flag = f"  ⚠ abono({ab:,.2f}) ≠ Σchequesxfact({suma_cf:,.2f})"
        print(f"  fact#{r['id_factura']:<5} numf={r['numf']:<8} "
              f"stat={r['stat']!s}→{nuevo_stat}  "
              f"imp={_money(imp).strip()} abono={_money(ab).strip()} "
              f"saldo {_money(sa).strip()}→{_money(nuevo_saldo).strip()}"
              f"{flag}")
    if len(rows) > 20:
        print(f"  ... y {len(rows) - 20} más")
    print(f"  · {cambios_stat} cambios de stat, {cambios_solo_saldo} sólo saldo.")
    print("  · NO se toca `abono` — mantiene sobre-pagos legacy intactos.")


def aplicar_seccion_C(rows) -> int:
    if not rows:
        return 0
    with db.tx() as conn:
        n = 0
        for r in rows:
            imp = float(r["importe"] or 0)
            ab  = float(r["abono"] or 0)
            sa  = float(r["saldo"] or 0)
            nuevo_saldo, nuevo_stat, _ = _propuesta_C(r["stat"], imp, ab, sa)
            db.execute(
                """
                UPDATE scintela.factura
                   SET saldo = %s,
                       stat  = %s,
                       usuario_modifica = %s,
                       fecha_modifica   = CURRENT_TIMESTAMP
                 WHERE id_factura = %s
                """,
                (nuevo_saldo, nuevo_stat, USUARIO_FIX, int(r["id_factura"])),
                conn=conn,
            )
            n += 1
        return n


# ───────────────────────────────────────────────────────────────────────────
# Entry
# ───────────────────────────────────────────────────────────────────────────
SECCIONES = ("A", "B", "C")


def main() -> int:
    for k in ("DB_HOST", "DB_NAME", "DB_USER", "DB_PASSWORD"):
        if not os.environ.get(k):
            print(f"ERROR: falta {k} en el entorno.")
            return 2

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="Ejecutar UPDATEs (sin esto sólo es dry-run).")
    ap.add_argument("--yes", action="store_true",
                    help="Auto-confirmar cada sección sin preguntar.")
    ap.add_argument("--solo", default="",
                    help=f"lista separada por coma de secciones a tocar "
                         f"({','.join(SECCIONES)}).")
    ap.add_argument("--auditoria", action="store_true",
                    help="Para C, además compara `abono` contra "
                         "SUM(chequesxfact). Sólo informativo.")
    args = ap.parse_args()

    sel = (args.solo.upper().split(",") if args.solo
           else list(SECCIONES))
    sel = [s.strip() for s in sel if s.strip()]
    for s in sel:
        if s not in SECCIONES:
            print(f"ERROR: sección desconocida: {s!r}. "
                  f"Válidos: {','.join(SECCIONES)}")
            return 2

    print(f"=== fix_inconsistencias — modo "
          f"{'APPLY' if args.apply else 'DRY-RUN'} ===")

    total_aplicado = 0

    # --- A ---
    if "A" in sel:
        linkables, cosmeticos, sospechosos = plan_seccion_A()
        imprimir_plan_A(linkables, cosmeticos, sospechosos)
        if args.apply and linkables:
            msg = (f"Aplicar A: linkear {len(linkables)}. "
                   f"(Cosméticos y sospechosos NO se tocan.)")
            if _confirmar(msg, args.yes):
                n = aplicar_seccion_A(linkables, cosmeticos, sospechosos)
                total_aplicado += n
                print(f"  ✓ Aplicado: {n} updates.")
            else:
                print("  · Sec A saltada.")

    # --- B ---
    if "B" in sel:
        fixables, no_fixables = plan_seccion_B()
        imprimir_plan_B(fixables, no_fixables)
        if args.apply and fixables:
            msg = f"Aplicar B: fixear {len(fixables)} reversos."
            if _confirmar(msg, args.yes):
                n = aplicar_seccion_B(fixables)
                total_aplicado += n
                print(f"  ✓ Aplicado: {n} updates.")
            else:
                print("  · Sec B saltada.")

    # --- C ---
    if "C" in sel:
        rows = plan_seccion_C(auditoria=args.auditoria)
        imprimir_plan_C(rows, auditoria=args.auditoria)
        if args.apply and rows:
            msg = (f"Aplicar C: recalcular saldo+stat en {len(rows)} "
                   f"facturas (no toca `abono`).")
            if _confirmar(msg, args.yes):
                n = aplicar_seccion_C(rows)
                total_aplicado += n
                print(f"  ✓ Aplicado: {n} updates.")
            else:
                print("  · Sec C saltada.")

    print()
    print("═" * 70)
    if args.apply:
        print(f"  Total actualizado: {total_aplicado} filas.")
        print("  Re-corré `python scripts/check_salud_dia.py` para "
              "verificar que los contadores bajaron.")
    else:
        print("  DRY-RUN — nada modificado. Para aplicar:")
        print("    python scripts/fix_inconsistencias.py --apply           "
              "(pregunta por sección)")
        print("    python scripts/fix_inconsistencias.py --apply --yes     "
              "(sin preguntar)")
    print("═" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
