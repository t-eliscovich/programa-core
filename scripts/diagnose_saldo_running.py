"""Diagnóstico del saldo running en transacciones_bancarias.

TMT 2026-05-29: durante el E2E del Anular grupo, el saldo a conciliar
saltó de $2,557,969.47 a $2,514,376.99 (-$43,592.48). Necesito verificar
si la cadena de saldos está coherente o si recompute_saldos_desde está
mal calculando.

Estrategia:
  1. Tomar los últimos N movs por fecha, ordenados ASC.
  2. Para cada uno, calcular el saldo esperado = saldo_previo + signed_delta.
  3. Comparar con el saldo grabado.
  4. Reportar discrepancias.
"""
import os
import sys

sys.path.insert(0, r"C:\programa-core")
import db

_BANCO_PICHINCHA = 10
_SIGNOS_C = ("DE", "TR", "AC", "NC", "IN", "XX")
_SIGNOS_D = ("CH", "ND", "DB", "GS", "PA")


def _signed_delta(documento: str, importe: float) -> float:
    doc = (documento or "").upper()
    if doc in _SIGNOS_C:
        return importe
    if doc in _SIGNOS_D:
        return -importe
    return 0.0


def main():
    db.init_pool()

    print("=" * 60)
    print("DIAGNÓSTICO SALDO RUNNING — PICHINCHA")
    print("=" * 60)

    # 1) Último mov + saldo actual.
    ult = db.fetch_one(
        """
        SELECT id_transaccion, fecha, documento, importe, saldo, concepto
          FROM scintela.transacciones_bancarias
         WHERE no_banco = %s AND saldo IS NOT NULL
         ORDER BY fecha DESC, id_transaccion DESC LIMIT 1
        """,
        (_BANCO_PICHINCHA,),
    )
    if ult:
        print(f"Último mov:    id={ult['id_transaccion']}  fecha={ult['fecha']}")
        print(f"               doc={ult['documento']}  importe={float(ult['importe']):.2f}")
        print(f"               saldo={float(ult['saldo']):.2f}")
        print(f"               concepto={(ult['concepto'] or '')[:50]}")
    print()

    # 2) Verificar cadena de los últimos 50 movs.
    movs = db.fetch_all(
        """
        SELECT id_transaccion, fecha, documento, importe, saldo, concepto
          FROM scintela.transacciones_bancarias
         WHERE no_banco = %s AND saldo IS NOT NULL
         ORDER BY fecha DESC, id_transaccion DESC LIMIT 50
        """,
        (_BANCO_PICHINCHA,),
    ) or []
    movs = list(reversed(movs))  # ASC

    if not movs:
        print("Sin movs con saldo.")
        return

    print(f"Verificando cadena de saldos (últimos {len(movs)} movs):")
    print("-" * 60)
    discrepancias = []
    saldo_prev = None
    for m in movs:
        importe = float(m["importe"] or 0)
        doc = m["documento"]
        saldo_grabado = float(m["saldo"] or 0)
        delta = _signed_delta(doc, importe)
        if saldo_prev is not None:
            saldo_esperado = round(saldo_prev + delta, 2)
            diff = round(saldo_grabado - saldo_esperado, 2)
            if abs(diff) > 0.01:
                discrepancias.append({
                    "id": m["id_transaccion"],
                    "fecha": str(m["fecha"]),
                    "doc": doc,
                    "importe": importe,
                    "delta_esperado": delta,
                    "saldo_prev": saldo_prev,
                    "saldo_grabado": saldo_grabado,
                    "saldo_esperado": saldo_esperado,
                    "diff": diff,
                })
        saldo_prev = saldo_grabado

    if not discrepancias:
        print(f"OK — cadena coherente. Último saldo: {saldo_prev:.2f}")
    else:
        print(f"⚠ {len(discrepancias)} discrepancias encontradas:")
        for d in discrepancias[:20]:
            print(
                f"  id={d['id']} fecha={d['fecha']} doc={d['doc']} importe={d['importe']:.2f}"
            )
            print(
                f"    saldo_prev={d['saldo_prev']:.2f} + delta({d['delta_esperado']:+.2f}) = esperado {d['saldo_esperado']:.2f}"
            )
            print(
                f"    grabado={d['saldo_grabado']:.2f}  diff={d['diff']:+.2f}"
            )
    print()

    # 3) Saldo a conciliar calculado.
    row = db.fetch_one(
        """
        SELECT
          COUNT(*) AS n,
          COALESCE(SUM(CASE WHEN t.documento IN ('CH','ND','DB','GS','PA')
                            THEN -t.importe ELSE t.importe END), 0) AS signed_pend
          FROM scintela.transacciones_bancarias t
         WHERE t.no_banco = %s
           AND TRIM(COALESCE(t.stat,'')) <> '*'
           AND NOT EXISTS (
               SELECT 1 FROM scintela.banco_conciliacion_match m
                WHERE m.id_transaccion = t.id_transaccion AND m.deshecho_en IS NULL
           )
        """,
        (_BANCO_PICHINCHA,),
    ) or {}
    pend_signed = float(row.get("signed_pend") or 0)
    saldo_pc = float(ult["saldo"]) if ult else 0
    saldo_a_conciliar = round(saldo_pc - pend_signed, 2)
    print(f"Saldo PC libros (último mov):      {saldo_pc:>16,.2f}")
    print(f"Pendientes PC signed:              {pend_signed:>16,.2f}")
    print(f"SALDO A CONCILIAR (calculado):     {saldo_a_conciliar:>16,.2f}")
    print()


if __name__ == "__main__":
    main()
