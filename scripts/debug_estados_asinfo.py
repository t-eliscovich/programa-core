#!/usr/bin/env python3
"""V6 — descubrir mapeo de fc.estado en dbo.factura_cliente.

1) Histograma de fc.estado (cuantas facturas por valor)
2) Para cada estado, una muestra (numero, fecha)
3) Buscar especificamente:
     - 175639 (sabemos: Rechazado por SRI) -> deberia decirnos qué int
     - NTEN-10460 (sabemos: Contabilizado) -> int de contabilizado
4) Buscar tabla 'dbo.estado' o similar que tenga el nombre del estado"""
from __future__ import annotations

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
for _env in (".env.prod", ".env"):
    _p = os.path.join(_ROOT, _env)
    if os.path.isfile(_p):
        try:
            from dotenv import load_dotenv; load_dotenv(_p, override=False)
        except ImportError: pass
        break

from modules._lib import metabase_client as mc


def main():
    print("=== Histograma de fc.estado en dbo.factura_cliente ===")
    rows = mc.fetch_dataset(2, """
        SELECT estado, COUNT(*) AS n
          FROM dbo.factura_cliente
         WHERE fecha >= '2025-01-01'
         GROUP BY estado
         ORDER BY estado
    """)
    for r in rows:
        print(f"  estado={r.get('estado')} count={r.get('n')}")

    print("\n=== Tablas que podrían describir los estados ===")
    rows = mc.fetch_dataset(2, """
        SELECT TABLE_SCHEMA, TABLE_NAME
          FROM INFORMATION_SCHEMA.TABLES
         WHERE TABLE_TYPE='BASE TABLE'
           AND (TABLE_NAME LIKE '%estado%' OR TABLE_NAME LIKE '%tipo_fact%')
         ORDER BY TABLE_NAME
    """)
    for r in rows:
        print(f"  {r.get('TABLE_SCHEMA')}.{r.get('TABLE_NAME')}")

    print("\n=== Buscar la 175639 (Rechazado por SRI segun Tamara) ===")
    rows = mc.fetch_dataset(2, """
        SELECT TOP 3 fc.id_factura_cliente, fc.numero, fc.fecha, fc.estado, fc.id_documento
          FROM dbo.factura_cliente fc
         WHERE fc.numero LIKE '%175639%'
    """)
    for r in rows:
        print(f"  {r}")

    print("\n=== Buscar NTEN-10460 (Contabilizado) ===")
    rows = mc.fetch_dataset(2, """
        SELECT TOP 3 fc.id_factura_cliente, fc.numero, fc.fecha, fc.estado, fc.id_documento
          FROM dbo.factura_cliente fc
         WHERE fc.numero LIKE '%10460%'
           AND fc.id_documento = 251
    """)
    for r in rows:
        print(f"  {r}")

    print("\n=== Si hay tabla dbo.factura_clienteSRI, qué tiene ===")
    rows = mc.fetch_dataset(2, """
        SELECT TOP 30 COLUMN_NAME, DATA_TYPE
          FROM INFORMATION_SCHEMA.COLUMNS
         WHERE TABLE_SCHEMA='dbo' AND TABLE_NAME='factura_clienteSRI'
         ORDER BY ORDINAL_POSITION
    """)
    for r in rows:
        print(f"  {r.get('COLUMN_NAME')}: {r.get('DATA_TYPE')}")


if __name__ == "__main__":
    main()
