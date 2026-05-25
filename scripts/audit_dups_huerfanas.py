#!/usr/bin/env python3
"""Audit duplicados + huerfanas reales en scintela.factura.

QUE HACE:
    1. Lista todos los numfs duplicados (COUNT > 1) en factura post 2025-01-01.
       Para cada uno muestra cada fila con id, fecha, cliente, kg, importe, stat,
       numf_completo -> permite ver si son IGUALES (true dup) o representan
       cosas distintas.
    2. Lista las huerfanas reales (kg!=0, numf>0 sin numf_completo) y para cada
       una busca el mejor candidato Asinfo via auditar_huerfanas().

USO:
    & "C:\\Python312\\python.exe" scripts\\audit_dups_huerfanas.py

Standalone, solo lee, no modifica. Para tomar acciones correr otros scripts.
"""
from __future__ import annotations

import os
import sys

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
from modules.facturas import audit_asinfo


def main():
    print("=" * 70)
    print("PARTE 1: Duplicados en scintela.factura post 2025-01-01")
    print("=" * 70)
    dups = db.fetch_all(
        """
        SELECT numf, COUNT(*) AS c
          FROM scintela.factura
         WHERE numf > 0 AND fecha >= '2025-01-01'
         GROUP BY numf
        HAVING COUNT(*) > 1
         ORDER BY c DESC, numf DESC
        """
    )
    print(f"Total numfs duplicados: {len(dups)}")
    for d in dups:
        nu = d["numf"]
        rows = db.fetch_all(
            """
            SELECT id_factura, numf, numf_completo, fecha,
                   codigo_cli, kg, importe, stat, usuario_crea
              FROM scintela.factura
             WHERE numf = %s AND fecha >= '2025-01-01'
             ORDER BY id_factura
            """,
            (nu,),
        )
        print(f"\nNUMF {nu} -- {len(rows)} rows:")
        for r in rows:
            print(
                f"  id={r['id_factura']:5d} "
                f"fecha={r['fecha']} "
                f"cli={r['codigo_cli']:5s} "
                f"kg={r['kg']:8s} "
                f"imp={str(r['importe']):>10s} "
                f"stat={r['stat']!r:5s} "
                f"numfc={r['numf_completo']!r} "
                f"usuario={r['usuario_crea']!r}"
            )

    print()
    print("=" * 70)
    print("PARTE 2: Huerfanas reales (kg!=0, numf>0 sin numf_completo)")
    print("=" * 70)
    huerf = db.fetch_all(
        """
        SELECT f.id_factura, f.numf, f.fecha, f.codigo_cli,
               f.kg, f.importe, f.stat, f.usuario_crea
          FROM scintela.factura f
         WHERE f.fecha >= '2025-01-01'
           AND f.numf > 0
           AND (f.kg IS NOT NULL AND f.kg <> 0)
           AND (f.stat IS NULL OR f.stat IN ('Z','A','T','X','N','',' '))
           AND (f.numf_completo IS NULL OR f.numf_completo = '')
         ORDER BY f.fecha DESC, f.id_factura DESC
        """
    )
    print(f"Total huerfanas con numf>0 y kg!=0: {len(huerf)}\n")

    # Audit con candidatos
    print("Ejecutando auditar_huerfanas() (top-3 candidatos por cada)...")
    auditadas = audit_asinfo.auditar_huerfanas(top_k=3, limite=500)
    # Filtrar solo las que tienen kg!=0 (la pantalla)
    auditadas = [a for a in auditadas if (a["pc_factura"].get("kg") or 0) != 0]
    print(f"Huerfanas en audit (con candidatos): {len(auditadas)}\n")

    for a in auditadas:
        pc = a["pc_factura"]
        score = a.get("mejor_score")
        score_s = f"{score:.2f}" if score is not None else "N/A"
        print(
            f"id={pc['id_factura']:5d} "
            f"numf={pc['numf']:7d} "
            f"fecha={pc['fecha']} "
            f"cli={pc['codigo_cli']:5s} "
            f"kg={pc['kg']:8s} "
            f"imp={str(pc['importe']):>10s} "
            f"mejor_score={score_s}"
        )
        for c in a["candidatos"][:3]:
            print(
                f"   -> AI {c['ai_tipo']:15s} {c['ai_numero']:20s} "
                f"fecha={c['ai_fecha']} "
                f"cli={c['ai_cliente_codigo']:5s} "
                f"kg={c['ai_kg']} "
                f"usd={c['ai_usd']} "
                f"score={c['score']:.2f}"
            )

    print()
    print("=" * 70)
    print(f"RESUMEN: {len(dups)} numfs duplicados, {len(huerf)} huerfanas con numf>0+kg!=0")
    print("=" * 70)


if __name__ == "__main__":
    main()
