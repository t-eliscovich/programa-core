"""Pasada diaria de invariantes — un solo lugar para mirar si la DB está sana.

Read-only, no toca nada. Tira un reporte semaforizado por sección:

  [OK]    nada que reportar
  [WARN]  cosa que mirar pero no urgente
  [ERROR] invariante roto — bug real, revisar antes de cerrar el día

Secciones:

  1. CAJA       — egresos S sin clasificar como gasto este mes.
  2. GASTOS     — reparto V1..V9 + concentración en V9.
  3. BANCOS     — saldo running de cada banco vs sum(deltas) esperado.
  4. MOV_DOBLE  — filas huérfanas, reversos sin id_original, etc.
  5. CHEQUES    — stat inconsistente con aplicaciones / endosos vivos.
  6. FACTURAS   — saldo != importe - abono (running invariant).
  7. POSDAT     — banc=9 con saldo>0 (legacy lock) o anuladas con banc<>9.
  8. PROVISIONES — SR=3300/día aplicado los días hábiles del mes corriente.

Read-only, no toca nada. Buen complemento a:
  - scripts/validar_reversos.py  (integridad del dispatcher de reverso)
  - scripts/check_gastos_caja.py  (foco en clasificación V1..V9)
  - scripts/snapshot_resultados.py (diff antes/después de operaciones)

Uso:
    python scripts/check_salud_dia.py
    python scripts/check_salud_dia.py --solo bancos,cheques
    python scripts/check_salud_dia.py --verbose       # más detalle por sección
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date as _d
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import db  # noqa: E402

# ───────────────────────────────────────────────────────────────────────────
# Marcadores y helpers de print
# ───────────────────────────────────────────────────────────────────────────
OK    = "[OK]   "
WARN  = "[WARN] "
ERROR = "[ERR]  "

_resultados: list[tuple[str, str, str]] = []   # (seccion, status, msg)


def _seccion(titulo: str) -> None:
    print(f"\n┌─ {titulo} " + "─" * max(0, 60 - len(titulo)))


def _reporte(seccion: str, status: str, msg: str) -> None:
    print(f"  {status}{msg}")
    _resultados.append((seccion, status, msg))


def _money(x) -> str:
    return f"${float(x or 0):>14,.2f}"


# ───────────────────────────────────────────────────────────────────────────
# 1) CAJA — egresos S sin clasificar
# ───────────────────────────────────────────────────────────────────────────
def check_caja(verbose: bool = False) -> None:
    _seccion("1) CAJA — egresos S sin clasificar (mes corriente)")
    huerfanos = db.fetch_all(
        """
        SELECT c.id_caja, c.fecha, ABS(c.importe) AS imp, c.concepto,
               COALESCE(c.usuario_crea,'') AS user
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
    if not huerfanos:
        _reporte("CAJA", OK, "Todos los egresos del mes están clasificados.")
        return
    n = len(huerfanos)
    total = sum(float(h["imp"] or 0) for h in huerfanos)
    nivel = WARN if n <= 5 else ERROR
    _reporte("CAJA", nivel,
             f"{n} egresos sin clasificar ({_money(total).strip()}).")
    if verbose or n <= 10:
        for h in huerfanos[:10]:
            print(f"     caja#{h['id_caja']:>5}  {h['fecha']}  "
                  f"{_money(h['imp'])}  {(h['concepto'] or '')[:50]}")
        if n > 10:
            print(f"     ... y {n - 10} más")


# ───────────────────────────────────────────────────────────────────────────
# 2) GASTOS — V1..V9 + concentración en V9
# ───────────────────────────────────────────────────────────────────────────
def check_gastos(verbose: bool = False) -> None:
    _seccion("2) GASTOS — reparto V1..V9 (mes corriente)")
    rows = db.fetch_all(
        """
        SELECT COALESCE(num,0) AS num,
               COUNT(*) AS n,
               SUM(importe)::numeric(14,2) AS total
          FROM scintela.xgast
         WHERE fecha >= date_trunc('month', CURRENT_DATE)
           AND COALESCE(stat,'') NOT IN ('Y','X')
         GROUP BY num
        """
    ) or []
    totales = {int(r["num"]): float(r["total"] or 0) for r in rows}
    total = sum(totales.values())
    if total == 0:
        _reporte("GASTOS", OK, "Sin gastos en el mes — nada que evaluar.")
        return
    v9_pct = totales.get(9, 0) / total * 100
    sin_num = totales.get(0, 0)

    if v9_pct > 50:
        _reporte("GASTOS", ERROR,
                 f"V9 = {v9_pct:.0f}% del total — clasificación demasiado "
                 f"gruesa.")
    elif v9_pct > 35:
        _reporte("GASTOS", WARN,
                 f"V9 = {v9_pct:.0f}% del total — mirá si hay conceptos "
                 f"chicos que están cayendo ahí.")
    else:
        _reporte("GASTOS", OK,
                 f"V9 en {v9_pct:.0f}% del total — distribución sana.")

    if sin_num > 0:
        _reporte("GASTOS", WARN,
                 f"{_money(sin_num).strip()} sin num asignado.")

    if totales.get(2, 0) == 0 and totales.get(1, 0) > 0:
        _reporte("GASTOS", WARN,
                 "V2 (gas/comb tejeduría) = $0 pero V1 tiene plata.")

    if verbose:
        print()
        for n in range(1, 10):
            v = totales.get(n, 0)
            pct = v / total * 100
            print(f"     V{n}: {_money(v)} ({pct:>4.1f}%)")


# ───────────────────────────────────────────────────────────────────────────
# 3) BANCOS — saldo running consistente (walk-forward con ancla)
# ───────────────────────────────────────────────────────────────────────────
def check_bancos(verbose: bool = False) -> None:
    """Para cada banco con transacciones:

    1. Ancla = primera fila con saldo running válido (no NULL, no 0 absurdo).
    2. Recorre desde ahí hacia adelante (orden fecha + id_transaccion).
    3. Recalcula saldo_esperado = saldo_anterior + _signed_delta(doc, imp).
    4. Compara contra el saldo running ACTUAL en cada fila.
    5. Reporta primera divergencia > 0.01 — eso es donde el reverso falló.

    Esta es la misma lógica que scripts/validar_reversos.py usa para su
    cross-check. Más confiable que un sum global (que es sensible al sign
    de la primera fila legacy).
    """
    _seccion("3) BANCOS — saldo running consistente (walk-forward)")

    # DOCS_ENTRADA igual que bank_helpers.signo_documento (NO incluye 'AC' —
    # acreditaciones se modelan como banco_ac_directo en historial pero
    # bank_helpers las trata como egreso). Mantener en sync con bank_helpers.
    DOCS_ENTRADA = {"DE", "TR", "XX", "NC", "IN"}

    def _signed_delta(doc, imp):
        imp = float(imp or 0)
        if imp < 0:
            return imp
        return imp if (doc or "").upper().strip() in DOCS_ENTRADA else -imp

    bancos = db.fetch_all(
        """
        SELECT b.no_banco, COALESCE(b.nombre,'') AS nombre
          FROM scintela.banco b
         WHERE EXISTS (
           SELECT 1 FROM scintela.transacciones_bancarias t
            WHERE t.no_banco = b.no_banco
         )
         ORDER BY b.no_banco
        """
    ) or []
    if not bancos:
        _reporte("BANCOS", WARN, "No encontré bancos con transacciones.")
        return

    drift_count = 0
    for b in bancos:
        n = int(b["no_banco"])
        nombre = b["nombre"][:10]
        filas = db.fetch_all(
            """
            SELECT id_transaccion, fecha, documento, importe, saldo
              FROM scintela.transacciones_bancarias
             WHERE no_banco = %s
             ORDER BY fecha ASC, id_transaccion ASC
            """,
            (n,),
        ) or []
        if not filas:
            continue
        # Ancla = primera fila con saldo válido. Si todas son NULL, no podemos.
        ancla_i = next(
            (i for i, f in enumerate(filas) if f.get("saldo") is not None),
            None,
        )
        if ancla_i is None:
            _reporte("BANCOS", WARN, f"#{n} {nombre}: sin ancla — todas las "
                                     f"filas tienen saldo=NULL.")
            continue
        saldo_calc = float(filas[ancla_i]["saldo"] or 0)
        primer_drift = None
        for f in filas[ancla_i + 1:]:
            delta = _signed_delta(f["documento"], f["importe"])
            saldo_calc = round(saldo_calc + delta, 2)
            saldo_real = f.get("saldo")
            if saldo_real is None:
                continue   # gap → seguimos calculando
            if abs(saldo_calc - float(saldo_real)) > 0.01:
                primer_drift = (f, saldo_calc, float(saldo_real))
                break
        if primer_drift is None:
            ultimo = filas[-1]
            _reporte(
                "BANCOS", OK,
                f"#{n} {nombre}: saldo {_money(ultimo.get('saldo')).strip()} "
                f"consistente con walk-forward desde ancla.",
            )
        else:
            f, calc, real = primer_drift
            drift_count += 1
            diff = abs(calc - real)
            _reporte(
                "BANCOS", ERROR,
                f"#{n} {nombre}: primer drift en tx#{f['id_transaccion']} "
                f"({f['fecha']}, {f.get('documento')}). "
                f"running={_money(real).strip()} vs calculado="
                f"{_money(calc).strip()} (Δ {_money(diff).strip()}). "
                f"Reverso roto a partir de ahí.",
            )
    if drift_count == 0:
        _reporte("BANCOS", OK, "Todos los bancos cuadran.")


# ───────────────────────────────────────────────────────────────────────────
# 4) MOV_DOBLE — integridad
# ───────────────────────────────────────────────────────────────────────────
def check_mov_doble(verbose: bool = False) -> None:
    _seccion("4) MOV_DOBLE — integridad")
    # a) reversos sin id_original
    n_huerfanos = db.fetch_one(
        """
        SELECT COUNT(*) AS n
          FROM scintela.mov_doble
         WHERE estado = 'reverso' AND id_original IS NULL
        """
    ) or {}
    nh = int(n_huerfanos.get("n") or 0)
    if nh:
        _reporte("MOV_DOBLE", ERROR,
                 f"{nh} reversos sin id_original (no se puede trazar a su alta).")
    else:
        _reporte("MOV_DOBLE", OK, "Reversos tienen id_original.")

    # b) reversados sin id_reverso
    n_rev_sin = db.fetch_one(
        """
        SELECT COUNT(*) AS n
          FROM scintela.mov_doble
         WHERE estado = 'reversado' AND id_reverso IS NULL
        """
    ) or {}
    nrs = int(n_rev_sin.get("n") or 0)
    if nrs:
        _reporte("MOV_DOBLE", ERROR,
                 f"{nrs} reversados sin id_reverso (link roto).")
    else:
        _reporte("MOV_DOBLE", OK, "Reversados tienen id_reverso.")

    # c) alta marcada como reversada pero el reverso ya no está activo
    inconsistente = db.fetch_one(
        """
        SELECT COUNT(*) AS n
          FROM scintela.mov_doble m
         WHERE m.estado='reversado' AND m.id_reverso IS NOT NULL
           AND NOT EXISTS (
             SELECT 1 FROM scintela.mov_doble r
              WHERE r.id_mov_doble = m.id_reverso AND r.estado='reverso'
           )
        """
    ) or {}
    ni = int(inconsistente.get("n") or 0)
    if ni:
        _reporte("MOV_DOBLE", ERROR,
                 f"{ni} altas marcadas 'reversado' pero el reverso linkeado "
                 f"no existe o no es estado='reverso'.")
    else:
        _reporte("MOV_DOBLE", OK, "Cadenas reversado↔reverso consistentes.")


# ───────────────────────────────────────────────────────────────────────────
# 5) CHEQUES — stat inconsistente
# ───────────────────────────────────────────────────────────────────────────
def check_cheques(verbose: bool = False) -> None:
    _seccion("5) CHEQUES — estados inconsistentes")
    # a) stat='X' (anulado) con aplicaciones vivas en chequesxfact
    rows = db.fetch_all(
        """
        SELECT c.id_cheque, c.no_cheque, c.stat,
               COUNT(*) AS n_aplic, SUM(cf.importe) AS suma
          FROM scintela.cheque c
          JOIN scintela.chequesxfact cf ON cf.id_cheque = c.id_cheque
         WHERE c.stat = 'X'
         GROUP BY c.id_cheque, c.no_cheque, c.stat
        """
    ) or []
    if rows:
        total = sum(float(r["suma"] or 0) for r in rows)
        _reporte("CHEQUES", ERROR,
                 f"{len(rows)} cheques stat='X' con aplicaciones vivas "
                 f"({_money(total).strip()} en chequesxfact).")
        if verbose:
            for r in rows[:5]:
                print(f"     cheque#{r['id_cheque']} N°{r['no_cheque']}  "
                      f"n_aplic={r['n_aplic']}  ${float(r['suma'] or 0):,.2f}")
    else:
        _reporte("CHEQUES", OK,
                 "Cheques stat='X' no tienen aplicaciones vivas.")

    # b) stat='E' (endosado) pero compra hermana stat='Y'
    rows = db.fetch_all(
        """
        SELECT c.id_cheque, c.no_cheque
          FROM scintela.cheque c
          JOIN scintela.mov_doble md
            ON md.origen_table='cheque' AND md.origen_id=c.id_cheque
           AND md.tipo='endoso_cheque_a_proveedor'
           AND md.estado='activo'
          JOIN scintela.compra cp ON cp.id_compra = md.destino_id
         WHERE c.stat = 'E' AND COALESCE(cp.stat,'') = 'Y'
        """
    ) or []
    if rows:
        _reporte("CHEQUES", ERROR,
                 f"{len(rows)} cheques stat='E' con compra hermana anulada — "
                 f"se debería reversar el endoso.")
    else:
        _reporte("CHEQUES", OK,
                 "Endosos vivos linkeados a compras vivas.")


# ───────────────────────────────────────────────────────────────────────────
# 6) FACTURAS — saldo invariant
# ───────────────────────────────────────────────────────────────────────────
def check_facturas(verbose: bool = False) -> None:
    _seccion("6) FACTURAS — saldo = importe - abono")
    # Excluimos el caso legítimo de sobre-pago histórico (DBF backfill):
    # stat='T' (cobrada total) + saldo=0 + abono>importe. La fact ESTÁ
    # pagada; el delta abono-importe es rastro de sobre-pago legacy y NO
    # se considera drift. TMT 2026-05-16.
    rows = db.fetch_all(
        """
        SELECT id_factura, numf, importe, abono, saldo,
               (COALESCE(importe,0) - COALESCE(abono,0) - COALESCE(saldo,0)) AS diff
          FROM scintela.factura
         WHERE COALESCE(stat,'') NOT IN ('X','Y')
           AND ABS(COALESCE(importe,0) - COALESCE(abono,0) - COALESCE(saldo,0)) > 0.01
           AND NOT (
             COALESCE(stat,'') = 'T'
             AND ABS(COALESCE(saldo,0)) < 0.01
             AND COALESCE(abono,0) >= COALESCE(importe,0)
           )
         ORDER BY ABS(COALESCE(importe,0) - COALESCE(abono,0) - COALESCE(saldo,0)) DESC
         LIMIT 20
        """
    ) or []
    if rows:
        peor = float(rows[0]["diff"] or 0)
        _reporte("FACTURAS", ERROR,
                 f"{len(rows)} facturas con saldo ≠ importe-abono "
                 f"(peor: {_money(abs(peor)).strip()}).")
        if verbose:
            for r in rows[:5]:
                print(f"     fact#{r['id_factura']} numf={r['numf']}  "
                      f"imp={float(r['importe'] or 0):,.2f}  "
                      f"abono={float(r['abono'] or 0):,.2f}  "
                      f"saldo={float(r['saldo'] or 0):,.2f}  "
                      f"diff={float(r['diff'] or 0):,.2f}")
    else:
        _reporte("FACTURAS", OK,
                 "saldo = importe - abono en todas las facturas activas.")


# ───────────────────────────────────────────────────────────────────────────
# 7) POSDAT — consistencia banc / anulada
# ───────────────────────────────────────────────────────────────────────────
def check_posdat(verbose: bool = False) -> None:
    _seccion("7) POSDAT — consistencia banc/anulada")
    # a) banc=9 (pagada) pero anulada IS NOT TRUE — debería estar libre o
    # explícitamente anulada. Ojo: legacy data tiene banc=9 sin anulada flag.
    n_pagadas = db.fetch_one(
        """
        SELECT COUNT(*) AS n
          FROM scintela.posdat
         WHERE COALESCE(banc,0) = 9
           AND COALESCE(anulada, FALSE) IS NOT TRUE
        """
    ) or {}
    np = int(n_pagadas.get("n") or 0)
    # banc=9 sin anulada es OK por design — sólo nota.
    _reporte("POSDAT", OK,
             f"banc=9 activas (legacy pagadas): {np} filas — esperado.")

    # b) Anuladas con banc≠9 — debería ser raro
    rows = db.fetch_all(
        """
        SELECT COUNT(*) AS n,
               SUM(importe)::numeric(14,2) AS total
          FROM scintela.posdat
         WHERE COALESCE(anulada, FALSE) = TRUE
           AND COALESCE(banc,0) <> 9
        """
    ) or []
    nm = int(rows[0]["n"] or 0) if rows else 0
    if nm:
        _reporte("POSDAT", WARN,
                 f"{nm} posdat anuladas con banc<>9 — flag legacy raro.")
    else:
        _reporte("POSDAT", OK, "Anuladas tienen banc=9.")


# ───────────────────────────────────────────────────────────────────────────
# 8) PROVISIONES — SR=3300/día aplicado
# ───────────────────────────────────────────────────────────────────────────
def check_provisiones(verbose: bool = False) -> None:
    """SR (SRI) se provisiona automáticamente $3,300/día hábil. La posdat es
    una fila template que se updatea sumando $3300 cada día hábil que pasa.

    Acá no podemos verificar el total acumulado (es de 2021, ya tiene muchos
    catch-ups manuales encima). Lo que sí podemos:

      a) ¿Existe la posdat SR (banc<>9, prov='YY', concepto 'SR%')?
      b) ¿Se modificó hace poco (<=2 días hábiles)?
      c) ¿La sistema_meta dice que el corredor diario corrió hoy?

    Si la posdat no se updatea por 3+ días hábiles, las corridas diarias se
    cayeron (cron muerto, exception silenciosa, etc.).
    """
    _seccion("8) PROVISIONES — corredor diario SR")
    row = db.fetch_one(
        """
        SELECT id_posdat, importe, fecha_modifica,
               (CURRENT_DATE - fecha_modifica::date)::int AS dias_sin_update
          FROM scintela.posdat
         WHERE COALESCE(banc,0) <> 9
           AND COALESCE(anulada,FALSE) IS NOT TRUE
           AND UPPER(TRIM(COALESCE(prov,''))) = 'YY'
           AND UPPER(COALESCE(concepto,'')) LIKE 'SR%'
         ORDER BY id_posdat
         LIMIT 1
        """
    )
    if not row:
        _reporte("PROVISIONES", WARN,
                 "No encontré la posdat de SRI provision (prov='YY' + 'SR%').")
        return
    dias_sin = row.get("dias_sin_update")
    fm = row.get("fecha_modifica")
    importe = float(row["importe"] or 0)
    if fm is None:
        _reporte("PROVISIONES", WARN,
                 f"posdat SRI {_money(importe).strip()} sin "
                 f"`fecha_modifica` — no puedo saber si corrió.")
        return
    if dias_sin is None or dias_sin <= 2:
        _reporte("PROVISIONES", OK,
                 f"posdat SRI {_money(importe).strip()} — última "
                 f"actualización {fm.date() if hasattr(fm,'date') else fm} "
                 f"({dias_sin}d atrás).")
    elif dias_sin <= 5:
        _reporte("PROVISIONES", WARN,
                 f"posdat SRI sin actualizar hace {dias_sin} días — quizá "
                 f"el cron se cayó este fin de semana.")
    else:
        _reporte("PROVISIONES", ERROR,
                 f"posdat SRI sin actualizar hace {dias_sin} días. El "
                 f"corredor `correr_provisiones_diarias()` no está corriendo. "
                 f"Revisá /informes/balance (auto-trigger) o cron.")

    # Bonus: ¿la sistema_meta tiene la marca del último día corrido?
    meta = db.fetch_one(
        """
        SELECT valor, actualizado
          FROM scintela.sistema_meta
         WHERE clave = 'provisiones_diarias_ult_fecha'
        """
    )
    if meta:
        _reporte("PROVISIONES", OK,
                 f"sistema_meta dice última corrida: {meta.get('valor')}.")


# ───────────────────────────────────────────────────────────────────────────
# 9) CHEQUESXFACT — aplicaciones de cheque consistentes
# ───────────────────────────────────────────────────────────────────────────
def check_chequesxfact(verbose: bool = False) -> None:
    _seccion("9) CHEQUESXFACT — aplicaciones consistentes")
    tol = 0.01
    sobre_cheque = db.fetch_all(
        """
        SELECT cf.id_cheque, COALESCE(c.importe, 0) AS cheque_importe,
               SUM(COALESCE(cf.importe, 0)) AS aplicado, COUNT(*) AS n
          FROM scintela.chequesxfact cf
          JOIN scintela.cheque c ON c.id_cheque = cf.id_cheque
         GROUP BY cf.id_cheque, c.importe
        HAVING SUM(COALESCE(cf.importe, 0)) > COALESCE(c.importe, 0) + %(t)s
         ORDER BY SUM(COALESCE(cf.importe, 0)) - COALESCE(c.importe, 0) DESC
         LIMIT 20
        """, {"t": tol}) or []
    sobre_fact = db.fetch_all(
        """
        SELECT cf.id_fact, COALESCE(f.importe, 0) AS fact_importe,
               SUM(COALESCE(cf.importe, 0)) AS aplicado, COUNT(*) AS n
          FROM scintela.chequesxfact cf
          JOIN scintela.factura f ON f.id_factura = cf.id_fact
         GROUP BY cf.id_fact, f.importe
        HAVING SUM(COALESCE(cf.importe, 0)) > COALESCE(f.importe, 0) + %(t)s
         ORDER BY SUM(COALESCE(cf.importe, 0)) - COALESCE(f.importe, 0) DESC
         LIMIT 20
        """, {"t": tol}) or []
    dups = db.fetch_all(
        """
        SELECT id_cheque, id_fact, COALESCE(importe, 0) AS importe,
               fechaing, COUNT(*) AS n
          FROM scintela.chequesxfact
         GROUP BY id_cheque, id_fact, COALESCE(importe, 0), fechaing
        HAVING COUNT(*) > 1
         ORDER BY COUNT(*) DESC
         LIMIT 20
        """) or []
    if sobre_cheque:
        _reporte("CHEQUESXFACT", ERROR,
                 f"{len(sobre_cheque)} cheque(s) sobre-aplicados "
                 "(Σ aplicado > importe del cheque).")
    if sobre_fact:
        _reporte("CHEQUESXFACT", ERROR,
                 f"{len(sobre_fact)} factura(s) sobre-abonadas por cheques "
                 "(Σ aplicado > importe).")
    if dups:
        _reporte("CHEQUESXFACT", WARN,
                 f"{len(dups)} fila(s) exactamente duplicadas "
                 "(cheque+factura+importe+fecha) — posible doble-INSERT.")
    if not (sobre_cheque or sobre_fact or dups):
        _reporte("CHEQUESXFACT", OK,
                 "sin sobre-aplicaciones ni duplicados exactos.")
    if verbose:
        for r in (sobre_cheque[:5] + sobre_fact[:5] + dups[:5]):
            print("     ", dict(r))


# ───────────────────────────────────────────────────────────────────────────
# 10) REVERSIBILIDAD — todo mov_doble activo tiene un reverso posible
# ───────────────────────────────────────────────────────────────────────────
def check_reversibilidad(verbose: bool = False) -> None:
    _seccion("10) REVERSIBILIDAD — todo mov tiene reverso")
    try:
        from modules.historial.views import (
            _REVERSO_BLOQUEADO,
            _REVERSO_DISPATCH,
        )
    except Exception as e:  # noqa: BLE001
        _reporte("REVERSIBILIDAD", WARN,
                 f"no pude importar el dispatcher de reverso: {e}")
        return
    conocidos = set(_REVERSO_DISPATCH) | set(_REVERSO_BLOQUEADO)

    def _es_terminal(tipo: str) -> bool:
        # Mismos excluidos que validar_reversos.py: las entradas 'reverso_*'
        # son el deshacer de algo (no se re-reversan), y los movimientos
        # directos de caja/banco no pasan por el dispatcher.
        tipo = tipo or ""
        return (
            tipo.startswith("reverso_")
            or (tipo.startswith("caja_") and tipo.endswith("_directo"))
            or (tipo.startswith("banco_") and tipo.endswith("_directo"))
        )

    rows = db.fetch_all(
        """
        SELECT tipo, COUNT(*) AS n
          FROM scintela.mov_doble
         WHERE estado = 'activo'
         GROUP BY tipo
         ORDER BY COUNT(*) DESC
        """) or []
    huerfanos = [
        r for r in rows
        if (r.get("tipo") or "") not in conocidos
        and not _es_terminal(r.get("tipo") or "")
    ]
    if huerfanos:
        total = sum(int(r["n"] or 0) for r in huerfanos)
        tipos = ", ".join(str(r["tipo"]) for r in huerfanos[:8])
        _reporte("REVERSIBILIDAD", ERROR,
                 f"{len(huerfanos)} tipo(s) de mov SIN reverso ({total} movs "
                 f"activos): {tipos}.")
    else:
        _reporte("REVERSIBILIDAD", OK,
                 f"todos los mov activos son reversables "
                 f"({len(_REVERSO_DISPATCH)} tipos con handler, "
                 f"0 sin cobertura).")
    if verbose and huerfanos:
        for r in huerfanos:
            print(f"     tipo={r['tipo']}  n={r['n']}")


# ───────────────────────────────────────────────────────────────────────────
# Entry
# ───────────────────────────────────────────────────────────────────────────
ALL_CHECKS = {
    "caja":         check_caja,
    "gastos":       check_gastos,
    "bancos":       check_bancos,
    "mov_doble":    check_mov_doble,
    "cheques":      check_cheques,
    "facturas":     check_facturas,
    "posdat":       check_posdat,
    "provisiones":  check_provisiones,
    "chequesxfact": check_chequesxfact,
    "reversibilidad": check_reversibilidad,
}


def main() -> int:
    for k in ("DB_HOST", "DB_NAME", "DB_USER", "DB_PASSWORD"):
        if not os.environ.get(k):
            print(f"ERROR: falta {k} en el entorno.")
            return 2

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--solo", default="",
                    help="Lista separada por coma de checks a correr "
                         f"({','.join(ALL_CHECKS)}).")
    ap.add_argument("--verbose", action="store_true",
                    help="Más detalle por sección (top N filas).")
    args = ap.parse_args()

    sel = (args.solo.split(",") if args.solo else list(ALL_CHECKS))
    sel = [s.strip().lower() for s in sel if s.strip()]
    for s in sel:
        if s not in ALL_CHECKS:
            print(f"ERROR: check desconocido: {s!r}. "
                  f"Válidos: {','.join(ALL_CHECKS)}")
            return 2

    print(f"=== check_salud_dia — {_d.today().isoformat()} ===")
    for s in sel:
        ALL_CHECKS[s](verbose=args.verbose)

    # Resumen final
    print()
    print("═" * 64)
    n_err  = sum(1 for _, st, _ in _resultados if st.strip() == "[ERR]")
    n_warn = sum(1 for _, st, _ in _resultados if st.strip() == "[WARN]")
    n_ok   = sum(1 for _, st, _ in _resultados if st.strip() == "[OK]")
    print(f"  Resumen: {n_ok} OK · {n_warn} WARN · {n_err} ERR")
    if n_err == 0 and n_warn == 0:
        print("  ✓ Todo verde. Buen día.")
    elif n_err == 0:
        print("  ~ Sólo warnings — operable, pero conviene mirar.")
    else:
        print("  ✗ Hay errors — antes de cerrar el día, resolvé esto.")
    print("═" * 64)
    return 0 if n_err == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
