#!/usr/bin/env python3
"""Diagnostico: por que las 31 facturas PC sin numf no matchean Asinfo.

Para cada huerfana (kg!=0, numf=0, numf_completo NULL), prueba 5 estrategias
contra el set de filas Asinfo del periodo 2025-01-01 a 2026-05-28 (card 199).

Standalone read-only. Correr en EC2:
    & "C:\\Python312\\python.exe" scripts\\debug_huerfanas.py
"""
from __future__ import annotations

import os
import sys
from collections import defaultdict
from datetime import date, timedelta

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

for _env in (".env.prod", ".env"):
    _p = os.path.join(_ROOT, _env)
    if os.path.isfile(_p):
        try:
            from dotenv import load_dotenv
            load_dotenv(_p, override=False)
        except ImportError:
            pass
        break

import db

db.init_pool()

from modules.asinfo import service as asinfo_service


def _norm_cli(s):
    return (s or "").strip().upper()


def _norm_fecha(d):
    if d is None:
        return None
    s = str(d)
    return s[:10]


def main():
    print("=" * 100)
    print("DEBUG HUERFANAS - busqueda multi-estrategia en Asinfo card 199")
    print("=" * 100)

    huerfanas = db.fetch_all(
        """
        SELECT id_factura AS id, codigo_cli, fecha, kg, importe, numf, numf_completo, stat
          FROM scintela.factura
         WHERE numf = 0
           AND numf_completo IS NULL
           AND kg <> 0
           AND fecha >= '2025-01-01'
           AND stat IN ('Z','A','')
         ORDER BY fecha DESC, id_factura DESC
        """
    )
    print(f"Huerfanas encontradas: {len(huerfanas)}")
    if not huerfanas:
        return

    desde = date(2025, 1, 1)
    hasta = date(2026, 5, 28)
    print(f"Pidiendo Asinfo facturas_periodo({desde}, {hasta})...")
    rows = asinfo_service.facturas_periodo(desde, hasta)
    print(f"Asinfo filas: {len(rows)}")

    # Indices
    idx_compuesto = defaultdict(list)        # (cli, fecha, round(kg,2)) -> rows
    idx_por_cli_fecha = defaultdict(list)    # (cli, fecha) -> rows
    idx_por_fecha_kg = defaultdict(list)     # (fecha, round(kg,2)) -> rows  (cualquier cli)
    idx_por_cli_fecha_importe = defaultdict(list)  # (cli, fecha, round(usd,2)) -> rows

    for r in rows:
        cli = _norm_cli(r.get("cliente_codigo"))
        f = _norm_fecha(r.get("fecha"))
        try:
            kg = float(r.get("kg") or 0)
        except (TypeError, ValueError):
            kg = 0.0
        try:
            usd = float(r.get("usd") or 0)
        except (TypeError, ValueError):
            usd = 0.0
        if not (cli and f):
            continue
        idx_compuesto[(cli, f, round(kg, 2))].append(r)
        idx_por_cli_fecha[(cli, f)].append(r)
        idx_por_fecha_kg[(f, round(kg, 2))].append(r)
        idx_por_cli_fecha_importe[(cli, f, round(usd, 2))].append(r)

    stats = defaultdict(int)
    sin_nada = []

    for h in huerfanas:
        cli = _norm_cli(h["codigo_cli"])
        f = _norm_fecha(h["fecha"])
        try:
            kg = float(h["kg"] or 0)
        except (TypeError, ValueError):
            kg = 0.0
        try:
            imp = float(h["importe"] or 0)
        except (TypeError, ValueError):
            imp = 0.0

        strat = None
        ai_row = None

        # 1: compuesto exacto
        cand = idx_compuesto.get((cli, f, round(kg, 2)), [])
        if cand:
            strat, ai_row = "1_exacto(cli,fecha,kg)", cand[0]

        # 2: fecha +/-1 / +/-2
        if not strat and f:
            try:
                d0 = date.fromisoformat(f)
                for delta in (1, -1, 2, -2):
                    f2 = (d0 + timedelta(days=delta)).isoformat()
                    cand = idx_compuesto.get((cli, f2, round(kg, 2)), [])
                    if cand:
                        strat = f"2_fecha+/-{delta}(cli,kg)"
                        ai_row = cand[0]
                        break
            except ValueError:
                pass

        # 3: any cliente, mismo (fecha,kg)
        if not strat:
            cand = idx_por_fecha_kg.get((f, round(kg, 2)), [])
            if cand:
                strat = "3_any_cli(fecha,kg)"
                ai_row = cand[0]

        # 4: (cli,fecha) any kg
        cli_fecha_rows = idx_por_cli_fecha.get((cli, f), [])
        if not strat and cli_fecha_rows:
            strat = "4_cli_fecha_any_kg"
            ai_row = cli_fecha_rows[0]

        # 5: por importe (cli,fecha,usd +/-0.50)
        if not strat:
            for delta_usd in (0.0, 0.5, -0.5, 1.0, -1.0):
                cand = idx_por_cli_fecha_importe.get((cli, f, round(imp + delta_usd, 2)), [])
                if cand:
                    strat = "5_importe(cli,fecha,usd+/-0.5)"
                    ai_row = cand[0]
                    break

        if not strat:
            strat = "NO_MATCH"
            sin_nada.append(h)

        stats[strat] += 1

        ai_desc = "-"
        if ai_row:
            ai_desc = (
                f"tipo={ai_row.get('tipo')} num={ai_row.get('numero')} "
                f"cli={ai_row.get('cliente_codigo')} fecha={_norm_fecha(ai_row.get('fecha'))} "
                f"kg={ai_row.get('kg')} usd={ai_row.get('usd')}"
            )
        # Cuantos candidatos hay en index cli+fecha (util para entender contexto)
        n_cli_fecha = len(cli_fecha_rows)
        print(
            f"id={h['id']} cli={cli} fecha={f} kg={kg} imp={imp} | "
            f"strat={strat} | n_cli_fecha={n_cli_fecha} | asinfo={ai_desc}"
        )

    print("\n" + "=" * 100)
    print("SUMMARY")
    print("=" * 100)
    for k in sorted(stats):
        print(f"  {k}: {stats[k]}")

    if sin_nada:
        print(f"\nNINGUNA estrategia encontro Asinfo para {len(sin_nada)} huerfanas.")
        print("Detalle de las que no matchean ni siquiera por cli+fecha:")
        for h in sin_nada:
            cli = _norm_cli(h["codigo_cli"])
            f = _norm_fecha(h["fecha"])
            # cuantas filas asinfo existen para ese cli en TODO el rango
            cli_total = sum(1 for r in rows if _norm_cli(r.get("cliente_codigo")) == cli)
            # primeras 3 fechas asinfo para ese cli (para ver cercanas)
            cli_fechas = sorted({_norm_fecha(r.get("fecha"))
                                 for r in rows if _norm_cli(r.get("cliente_codigo")) == cli})
            cercanas = [d for d in cli_fechas if d and abs((date.fromisoformat(d) - date.fromisoformat(f)).days) <= 7] if f else []
            print(
                f"  id={h['id']} cli={cli} fecha={f} kg={h['kg']} imp={h['importe']} | "
                f"asinfo_rows_para_cli={cli_total} | fechas_asinfo_±7d={cercanas}"
            )


if __name__ == "__main__":
    main()
