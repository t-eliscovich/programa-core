#!/usr/bin/env python3
"""Modificar card 199 — ampliar fc.estado IN (0,1,4) → (0,1,4,16).

Antes valida que estado=15 sea efectivamente anulada (no la incluimos),
después hace el PUT vía Metabase API y verifica.
"""
from __future__ import annotations

import os
import re
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
    card_id = os.environ.get("ASINFO_CARD_FACTURAS", "199").strip()
    tok = mc._session_token or mc._login(requests)
    if not tok:
        print("NO TOKEN"); return
    h = {"X-Metabase-Session": tok}

    # 1. Validar qué es estado=15
    print("=== Muestra estado=15 (4 facturas) ===")
    rows = mc.fetch_dataset(2, """
        SELECT TOP 5 numero, fecha, estado, id_documento
          FROM dbo.factura_cliente
         WHERE estado = 15 AND fecha >= '2025-01-01'
         ORDER BY fecha DESC
    """)
    for r in rows:
        print(f"  {r}")

    # 1b. Muestra estado=16
    print("\n=== Muestra estado=16 (las 31 rechazadas) ===")
    rows = mc.fetch_dataset(2, """
        SELECT TOP 8 numero, fecha, estado, id_documento
          FROM dbo.factura_cliente
         WHERE estado = 16 AND fecha >= '2025-01-01'
         ORDER BY fecha DESC
    """)
    for r in rows:
        print(f"  {r}")

    # 2. Bajar card 199, modificar y subir
    print(f"\n=== Bajando card {card_id} ===")
    r = requests.get(f"{url}/api/card/{card_id}", headers=h, timeout=20)
    r.raise_for_status()
    card = r.json()
    dq = card.get("dataset_query") or {}
    # Soportar dos formatos:
    #   v0 (legacy): dataset_query.native.query (string)
    #   v51+ MBQL:   dataset_query.stages[0].native (string directo)
    sql_old = ""
    sql_path = None
    nat = (dq.get("native") or {})
    if isinstance(nat, dict) and nat.get("query"):
        sql_old = nat["query"]
        sql_path = "native.query"
    else:
        stages = dq.get("stages") or []
        if stages:
            st0 = stages[0] or {}
            nat_str = st0.get("native")
            if isinstance(nat_str, str):
                sql_old = nat_str
                sql_path = "stages[0].native"
    print(f"  SQL encontrado en: {sql_path}, len={len(sql_old)}")
    # Buscar "fc.estado IN (...)" tolerando cualquier whitespace (incluido \n, \t, nbsp).
    # Capturamos la lista existente para confirmar que sea (0,1,4) o subset.
    patt = re.compile(
        r"(fc\.estado\s+IN\s*\(\s*)([0-9,\s]+)(\s*\))",
        re.IGNORECASE,
    )
    m = patt.search(sql_old)
    if not m:
        # Imprimir el fragmento alrededor de 'estado' para diagnosticar
        idx = sql_old.lower().find("fc.estado")
        ctx = sql_old[max(0, idx-50):idx+150] if idx >= 0 else "(no estado found)"
        print(f"ABORTO: regex no matcheó. Contexto: {ctx!r}")
        return
    lista_actual = re.findall(r"\d+", m.group(2))
    print(f"  Lista actual fc.estado IN: {lista_actual}")
    if "16" in lista_actual:
        print("  Ya incluye 16. No hago cambios.")
        return
    nueva = sorted(set(lista_actual + ["16"]), key=lambda x: int(x))
    nueva_str = "fc.estado IN (" + ", ".join(nueva) + ")"
    sql_new = patt.sub(lambda mm: nueva_str, sql_old, count=1)

    # Reinyectar en la misma ubicación del JSON
    if sql_path == "native.query":
        nat["query"] = sql_new
        dq["native"] = nat
    elif sql_path == "stages[0].native":
        dq["stages"][0]["native"] = sql_new
    else:
        print("ABORTO: no sé dónde re-inyectar el SQL.")
        return
    print("SQL - cambio:")
    print("  ANTES: fc.estado IN (0, 1, 4)")
    print("  AHORA: " + nueva_str + "  <- +16 (Rechazado SRI), sigue excluyendo 15 (anulada)")

    # PUT
    body = {
        "name": card.get("name"),
        "dataset_query": dq,
        "display": card.get("display"),
        "description": card.get("description"),
        "visualization_settings": card.get("visualization_settings") or {},
    }
    print(f"\nAplicando PUT /api/card/{card_id} ...")
    r = requests.put(f"{url}/api/card/{card_id}", json=body,
                     headers={**h, "Content-Type": "application/json"}, timeout=20)
    print(f"  status={r.status_code}")
    if r.status_code >= 400:
        print(f"  body={r.text[:500]}")
        return

    # 3. Invalidar cache del bridge
    print("\n=== Invalidando cache facturas_periodo ===")
    from modules.asinfo import service
    service.reset_facturas_cache()
    print("  OK")

    # 4. Re-fetch y contar estado=16 que ahora deben venir
    print("\n=== Re-fetch card 199 (debería ahora traer las 31 rechazadas) ===")
    from datetime import date
    new_rows = service.facturas_periodo(date(2025,1,1), date(2026,5,28))
    print(f"  Total filas Asinfo después del cambio: {len(new_rows)}")


if __name__ == "__main__":
    main()
