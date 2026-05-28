#!/usr/bin/env python3
"""V4 — confirmación: las huérfanas existen en Asinfo pero con estado
'Rechazado por el SRI' o anulada. Query directo a la tabla factura_cliente
de Asinfo via Metabase fetch_dataset (database_id=2)."""
from __future__ import annotations
import os, sys
from datetime import date

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
for _env in (".env.prod", ".env"):
    _p = os.path.join(_ROOT, _env)
    if os.path.isfile(_p):
        try:
            from dotenv import load_dotenv; load_dotenv(_p, override=False)
        except ImportError: pass
        break

import db; db.init_pool()
from modules._lib import metabase_client


def main():
    # Descubrir el schema correcto buscando tablas con "factur"
    print("Tablas Asinfo con 'factur' o 'cliente':")
    tbls = metabase_client.fetch_dataset(2, """
        SELECT TABLE_SCHEMA, TABLE_NAME
          FROM INFORMATION_SCHEMA.TABLES
         WHERE TABLE_TYPE='BASE TABLE'
           AND (TABLE_NAME LIKE '%factur%' OR TABLE_NAME LIKE '%client%')
         ORDER BY TABLE_NAME
    """)
    for t in tbls:
        print(f"  {t.get('TABLE_SCHEMA')}.{t.get('TABLE_NAME')}")

    # Test: buscar factura 175639 (la que Tamara mostró)
    print("\n" + "=" * 80)
    print("BUSQUEDA DIRECTA: numf=175639 en factura_cliente")
    print("=" * 80)
    # Probar el nombre más probable
    for tabla in ("dbo.factura_cliente", "dbo.factura", "dbo.facturas"):
        sql = f"""
            SELECT TOP 5 *
              FROM {tabla}
             WHERE secuencial LIKE '%175639%'
                OR numero LIKE '%175639%'
                OR num_factura LIKE '%175639%'
                OR numfact LIKE '%175639%'
        """
        try:
            rows = metabase_client.fetch_dataset(2, sql)
            if rows:
                print(f"\n[{tabla}] {len(rows)} filas:")
                # Imprimir todas las columnas para entender el shape
                for r in rows[:2]:
                    for k, v in r.items():
                        print(f"  {k}: {v}")
                    print("  ---")
                break
        except Exception as e:
            print(f"[{tabla}] error: {e}")

    # Mostrar el SQL de la card 199 (la que usa Programa Core para Asinfo)
    print("\n" + "=" * 80)
    print("SQL DE LA CARD 199 (la que PC usa via service.facturas_periodo)")
    print("=" * 80)
    try:
        import requests, os
        from modules._lib import metabase_client as mc
        url = os.environ.get("METABASE_URL", "").strip().rstrip("/")
        tok = mc._session_token or mc._login(requests)
        if tok:
            r = requests.get(f"{url}/api/card/199",
                             headers={"X-Metabase-Session": tok}, timeout=20)
            r.raise_for_status()
            data = r.json() or {}
            print("Card name:", data.get("name"))
            print("Database id:", data.get("database_id"))
            q = (data.get("dataset_query") or {}).get("native", {}) or {}
            sql = q.get("query") or "(no SQL)"
            print("\n--- SQL ---")
            print(sql[:4000])
    except Exception as e:
        print(f"Error fetching card 199: {e}")


if __name__ == "__main__":
    main()
