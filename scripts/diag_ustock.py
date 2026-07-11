"""Diagnóstico ustock=0 live (TMT 2026-06-02).

Por qué existe:
    /admin/dbase-sync 2026-06-02 reportó ustock snap=7.8M live=0 → drift 100%.
    El cálculo live de vsto vive en informe_balance() y depende de:
        - hist (último snapshot historia)
        - inic (iniciales_mes_actual)
        - kg_facturas_pc_no_sincronizadas()

    Este script tira los 4 inputs en crudo para ver dónde queda 0.

Cómo correrlo:
    LOCAL apuntando a prod (más rápido):
        cd "/Programa Core" && python scripts/diag_ustock.py

    O en EC2 vía SSM:
        ssm send-command document=AWS-RunPowerShellScript \\
          parameters='commands=["cd C:\\\\programa-core; C:\\\\Python312\\\\python.exe scripts\\\\diag_ustock.py"]'
"""
from __future__ import annotations

import os
import sys
from datetime import date

# Asegurar import del root del proyecto
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import db  # noqa: E402


def banner(s: str) -> None:
    print()
    print("─" * 70)
    print(f" {s}")
    print("─" * 70)


def main() -> None:
    hoy = date.today()
    print(f"hoy = {hoy}  mesnum={hoy.month}  yy_full={hoy.year}  yy_2dig={hoy.year % 100}")

    # ─── 1. Último snapshot historia ─────────────────────────────────────
    banner("1. scintela.historia — último snapshot (lo que el drift llama 'snap')")
    rows = db.fetch_all(
        """
        SELECT fecha, stock, ustock, hilado, tejido, terminado,
               banco, cart, deuda, patrimonio
        FROM scintela.historia
        ORDER BY fecha DESC
        LIMIT 3
        """
    ) or []
    for r in rows:
        print(f"  fecha={r.get('fecha')}  stock={r.get('stock')}  ustock={r.get('ustock')}")
        print(f"     hilado={r.get('hilado')}  tejido={r.get('tejido')}  terminado={r.get('terminado')}")
        print(f"     banco={r.get('banco')}  cart={r.get('cart')}  deuda={r.get('deuda')}  patr={r.get('patrimonio')}")
        print()

    # ─── 2. iniciales del mes actual ─────────────────────────────────────
    banner(f"2. scintela.iniciales — exacto mes actual (mesnum={hoy.month})")
    # iniciales.yy puede ser 2-dig o 4-dig — probar ambos
    for yy_val in (hoy.year % 100, hoy.year):
        rows = db.fetch_all(
            """
            SELECT id_iniciales, mesnum, yy, hilado, tejido, terminado,
                   kprog, um, uk, uf
            FROM scintela.iniciales
            WHERE mesnum = %s AND yy = %s
            ORDER BY id_iniciales DESC
            LIMIT 5
            """,
            (hoy.month, yy_val),
        ) or []
        print(f"  yy={yy_val}: {len(rows)} filas")
        for r in rows:
            print(f"     id={r.get('id_iniciales')}  hilado={r.get('hilado')}  tejido={r.get('tejido')}  terminado={r.get('terminado')}  kprog={r.get('kprog')}")
            print(f"        um={r.get('um')}  uk={r.get('uk')}  uf={r.get('uf')}")

    # ─── 3. Fallback (más reciente con datos reales) ─────────────────────
    banner("3. scintela.iniciales — fallback (la más reciente con datos)")
    rows = db.fetch_all(
        """
        SELECT id_iniciales, mesnum, yy, hilado, tejido, terminado, kprog, um, uk, uf
        FROM scintela.iniciales
        WHERE COALESCE(kprog, 0) > 0 OR COALESCE(hilado, 0) > 0
        ORDER BY yy DESC, mesnum DESC, id_iniciales DESC
        LIMIT 5
        """
    ) or []
    for r in rows:
        print(f"  id={r.get('id_iniciales')}  mesnum={r.get('mesnum')}  yy={r.get('yy')}  hilado={r.get('hilado')}  tejido={r.get('tejido')}  terminado={r.get('terminado')}")

    # ─── 4. Resumen iniciales ────────────────────────────────────────────
    banner("4. scintela.iniciales — resumen")
    r = db.fetch_one(
        """
        SELECT COUNT(*) AS n,
               MIN(yy) AS min_yy, MAX(yy) AS max_yy,
               MAX(id_iniciales) AS max_id
        FROM scintela.iniciales
        """
    ) or {}
    print(f"  filas={r.get('n')}  yy=[{r.get('min_yy')}..{r.get('max_yy')}]  max_id={r.get('max_id')}")

    # ─── 5. kg_facturas_pc_no_sincronizadas ──────────────────────────────
    banner("5. kg_facturas_pc_no_sincronizadas (lo que descuenta del terminado live)")
    r = db.fetch_one(
        """
        SELECT COALESCE(SUM(kg), 0) AS total,
               COUNT(*) AS n_facturas
        FROM scintela.factura
        WHERE COALESCE(usuario_crea, '') NOT IN ('dbf-import', 'asinfo-backfill')
          AND (stat IS NULL OR stat <> 'X')
        """
    ) or {}
    kg_pc = float(r.get('total') or 0)
    print(f"  kg_facturas_pc = {kg_pc:,.0f}  ({r.get('n_facturas')} facturas)")

    # ─── 6. Cálculo final ────────────────────────────────────────────────
    banner("6. Simulación stock_total_us live")
    try:
        from modules.informes.queries import (
            historia_ultimo_mes,
            informe_balance,
            iniciales_mes_actual,
            kg_facturas_pc_no_sincronizadas,
        )
        hist = historia_ultimo_mes() or {}
        inic = iniciales_mes_actual() or {}
        kg_pc = kg_facturas_pc_no_sincronizadas()
        h_hilado = float(hist.get("hilado") or 0) if "hilado" in hist else 0.0
        h_tejido_kg = float(hist.get("tejido") or 0) if "tejido" in hist else 0.0
        h_terminado_kg = float(hist.get("terminado") or 0) if "terminado" in hist else 0.0
        if h_hilado == 0 and h_tejido_kg == 0 and h_terminado_kg == 0:
            print("  → hist sin desglose, usando inic como fallback")
            h_hilado = float(inic.get("hilado") or 0)
            h_tejido_kg = float(inic.get("tejido") or 0)
            h_terminado_kg = float(inic.get("terminado") or 0)
        print(f"  pre-descuento: h_hilado={h_hilado:,.0f}  h_tejido={h_tejido_kg:,.0f}  h_terminado={h_terminado_kg:,.0f}")
        h_terminado_post = max(0.0, h_terminado_kg - kg_pc)
        print(f"  post-descuento kg_pc={kg_pc:,.0f}: h_terminado={h_terminado_post:,.0f}")
        if h_terminado_kg > 0 and h_terminado_post == 0:
            print("  ⚠ TERMINADO se fue a 0 por kg_facturas_pc — probable causa raíz")
        bal = informe_balance() or {}
        comp = bal.get("diagnostico", {}).get("componentes", {})
        print(f"  → informe_balance().vsto = {comp.get('vsto')}")
    except Exception as e:
        print(f"  ✗ Error: {e}")

    print()


if __name__ == "__main__":
    main()
