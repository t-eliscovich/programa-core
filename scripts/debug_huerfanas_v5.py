#!/usr/bin/env python3
"""V5 — bajar TODA la definicion JSON de card 199 + listar tablas factura/cliente
+ buscar directo numf=175639 en la tabla factura_cliente para confirmar que
existe en Asinfo Y ver su estado."""
from __future__ import annotations

import json
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

import requests

from modules._lib import metabase_client as mc


def main():
    url = os.environ.get("METABASE_URL", "").strip().rstrip("/")
    card_id = os.environ.get("ASINFO_CARD_FACTURAS", "").strip()
    print(f"METABASE_URL={url}  CARD_ID={card_id}")

    tok = mc._session_token or mc._login(requests)
    if not tok:
        print("NO TOKEN"); return
    h = {"X-Metabase-Session": tok}

    # 1) Listar tablas que tengan 'factur' (TODAS, no top 30)
    print("\n=== Todas las tablas con 'factur' ===")
    sql = """
        SELECT TABLE_SCHEMA, TABLE_NAME
          FROM INFORMATION_SCHEMA.TABLES
         WHERE TABLE_TYPE='BASE TABLE' AND TABLE_NAME LIKE '%factur%'
         ORDER BY TABLE_NAME
    """
    for t in mc.fetch_dataset(2, sql, max_results=200):
        print(f"  {t.get('TABLE_SCHEMA')}.{t.get('TABLE_NAME')}")

    # 2) Bajar el JSON COMPLETO de la card del env var
    print(f"\n=== Card {card_id} JSON dataset_query ===")
    r = requests.get(f"{url}/api/card/{card_id}", headers=h, timeout=20)
    r.raise_for_status()
    data = r.json() or {}
    print("Name:", data.get("name"))
    print("Database:", data.get("database_id"))
    print("Query type:", (data.get("dataset_query") or {}).get("type"))
    print("Full dataset_query:")
    print(json.dumps(data.get("dataset_query"), indent=2, ensure_ascii=False)[:3000])

    # 3) Columnas de dbo.factura_cliente (si existe)
    print("\n=== Columnas de dbo.factura_cliente ===")
    cols = mc.fetch_dataset(2, """
        SELECT COLUMN_NAME, DATA_TYPE
          FROM INFORMATION_SCHEMA.COLUMNS
         WHERE TABLE_SCHEMA='dbo' AND TABLE_NAME='factura_cliente'
         ORDER BY ORDINAL_POSITION
    """, max_results=200)
    for c in cols:
        print(f"  {c.get('COLUMN_NAME')}: {c.get('DATA_TYPE')}")

    # 4) Buscar la 175639 directamente
    print("\n=== Buscar factura 175639 en dbo.factura_cliente ===")
    rows = mc.fetch_dataset(2, """
        SELECT TOP 5 *
          FROM dbo.factura_cliente
         WHERE secuencial LIKE '%175639%'
            OR numero LIKE '%175639%'
            OR numfact LIKE '%175639%'
            OR num_factura LIKE '%175639%'
            OR id_factura_cliente IN (SELECT id_factura_cliente FROM dbo.factura_cliente WHERE 1=0)
    """, max_results=10)
    for r0 in rows:
        for k, v in r0.items():
            print(f"  {k}: {v}")
        print("  ---")


if __name__ == "__main__":
    main()
