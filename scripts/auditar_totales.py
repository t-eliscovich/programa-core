#!/usr/bin/env python3
"""Auditoría cruzada: totales de los listados vs lo que muestra el balance.

Corre las queries de cheques/facturas/compras y las compara con los totales
del informe de resultados (b.totc, b.totf, b.totp). Si no coinciden, marca
las diferencias para diagnosticar.

Uso:
    python scripts/auditar_totales.py
"""
from __future__ import annotations

import sys
from pathlib import Path

# Path al proyecto + cargar .env
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Cargar .env
from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

import db  # noqa: E402


def fmt(n: float) -> str:
    return f"$ {n:>15,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def diff(a: float, b: float) -> str:
    d = a - b
    if abs(d) < 0.01:
        return "  ✓"
    return f"  ✗ Δ {fmt(d)}"


def main():
    db.init_pool()
    print()
    print("=" * 80)
    print(" AUDITORÍA CRUZADA — totales de listados vs balance")
    print("=" * 80)

    # ===== CHEQUES =====
    print("\n[CHEQUES]")
    print("  Lo que muestra el listado de cheques (por estado):")
    rows = db.fetch_all(
        """
        SELECT
          CASE
            WHEN stat = 'Z'                THEN 'cartera'
            WHEN stat IN ('B','A')         THEN 'depositados'
            WHEN stat IN ('1','2','3','R') THEN 'devueltos'
            WHEN stat = 'D'                THEN 'daniela'
            WHEN stat = 'P'                THEN 'postergados'
            WHEN stat = 'V'                THEN 'internacional'
            WHEN stat IN ('X','Y','T')     THEN 'cerrados'
            ELSE 'otros'
          END                                  AS bucket,
          COUNT(*)                             AS n,
          COALESCE(SUM(importe), 0)            AS total
        FROM scintela.cheque
        GROUP BY 1
        ORDER BY 1
        """
    )
    for r in rows:
        print(f"    {r['bucket']:>16}  n={r['n']:>5}   {fmt(float(r['total']))}")

    # TOTC del balance — paridad INFORMES.PRG
    totc_row = db.fetch_one(
        """
        SELECT COALESCE(SUM(importe), 0) AS totc
        FROM scintela.cheque
        WHERE stat IN ('Z','1','2','3','P','D')
        """
    )
    totc = float(totc_row["totc"] or 0) if totc_row else 0
    print("\n  TOTC del BALANCE (paridad PRG: stat IN Z,1,2,3,P,D):")
    print(f"    {fmt(totc)}")

    # ===== FACTURAS =====
    print("\n[FACTURAS]")
    print("  Lo que muestra el listado de facturas (por vista):")
    rows = db.fetch_all(
        """
        SELECT
          CASE
            WHEN COALESCE(saldo, 0) > 0
                 AND (stat IS NULL OR stat IN ('Z','A','',' ')) THEN 'cartera'
            WHEN stat = 'T'                                      THEN 'canceladas'
            WHEN stat IN ('X','Y')                               THEN 'eliminadas'
            ELSE 'otras'
          END                                  AS bucket,
          COUNT(*)                             AS n,
          COALESCE(SUM(importe), 0)            AS total_importe,
          COALESCE(SUM(saldo), 0)              AS total_saldo
        FROM scintela.factura
        GROUP BY 1
        ORDER BY 1
        """
    )
    for r in rows:
        print(f"    {r['bucket']:>16}  n={r['n']:>5}   imp {fmt(float(r['total_importe']))}   saldo {fmt(float(r['total_saldo']))}")

    totf_row = db.fetch_one(
        """
        SELECT COALESCE(SUM(saldo), 0) AS totf
        FROM scintela.factura
        WHERE COALESCE(saldo, 0) > 0
          AND (stat IS NULL OR stat IN ('Z','A','',' '))
        """
    )
    totf = float(totf_row["totf"] or 0) if totf_row else 0
    print("\n  TOTF del BALANCE (paridad PRG: SUM(saldo) WHERE stat IN Z,A AND saldo>0):")
    print(f"    {fmt(totf)}")

    # ===== COMPRAS / POSDAT =====
    print("\n[COMPRAS]")
    rows = db.fetch_all(
        """
        SELECT
          CASE
            WHEN UPPER(COALESCE(tipo, '')) = 'K'
                 AND COALESCE(kg, 0) > 0.01           THEN 'produccion'
            WHEN UPPER(COALESCE(tipo, '')) = 'A'      THEN 'anticipos'
            WHEN COALESCE(stat, '') = 'Y'             THEN 'anuladas'
            ELSE 'compras'
          END                              AS bucket,
          COUNT(*)                         AS n,
          COALESCE(SUM(importe), 0)        AS total
        FROM scintela.compra
        GROUP BY 1
        ORDER BY 1
        """
    )
    for r in rows:
        print(f"    {r['bucket']:>16}  n={r['n']:>5}   {fmt(float(r['total']))}")

    print("\n[POSDAT — desglose por valor de `banc` (paridad PRG: BANC<>9 = pasivo)]")
    rows = db.fetch_all(
        """
        SELECT
          COALESCE(banc::text, 'NULL')     AS banc,
          COUNT(*)                         AS n,
          COALESCE(SUM(importe), 0)        AS total,
          MIN(fecha)                       AS desde,
          MAX(fecha)                       AS hasta
        FROM scintela.posdat
        GROUP BY 1
        ORDER BY 1
        """
    )
    for r in rows:
        flag = "← PAGADA (no suma TOTP)" if r['banc'] == '9' else "→ PASIVO" if r['banc'] != 'NULL' else "⚠ NULL"
        print(f"    banc={r['banc']:>5}  n={r['n']:>5}   {fmt(float(r['total']))}  {flag}")
    print(
        "\n    NB: El dBase usa BANC<>9 estricto (NULLs no entran). "
        "El nuevo app usa `banc <> 9 OR banc IS NULL` para no perderlas."
    )

    totp_row = db.fetch_one(
        """
        SELECT COALESCE(SUM(importe), 0) AS totp
        FROM scintela.posdat
        WHERE banc <> 9 OR banc IS NULL
        """
    )
    totp = float(totp_row["totp"] or 0) if totp_row else 0
    print("\n  TOTP del BALANCE (paridad PRG: SUM posdat WHERE banc<>9):")
    print(f"    {fmt(totp)}")

    # ===== Resumen final =====
    print("\n" + "=" * 80)
    print(" RESUMEN — coincidencia entre listados y balance")
    print("=" * 80)
    print(f"\n  TOTC (cheques en cartera Z+1+2+3+P+D)  →  {fmt(totc)}")
    print(f"  TOTF (facturas vivas saldo Z+A)         →  {fmt(totf)}")
    print(f"  TOTP (posdat pendientes banc<>9)        →  {fmt(totp)}")
    print(f"\n  Cartera total = TOTC + TOTF             →  {fmt(totc + totf)}")
    print()
    print("Si el balance muestra otros números, hay drift. Cosas que pueden hacer drift:")
    print("  - Filas con stat fuera del set canónico (sumarlas a 'otros').")
    print("  - factura.saldo NULL o legacy que no cumple stat IN ('Z','A').")
    print("  - posdat con banc IS NULL — algunas queries lo cuentan, otras no.")
    print()


if __name__ == "__main__":
    import contextlib
    try:
        main()
    finally:
        with contextlib.suppress(Exception):
            db.close_pool()
