#!/usr/bin/env python3
"""Cleanup huerfanas para llegar a 0 visible en /facturas?solo_huerfanas=1.

ESTRATEGIA (orden importante):
    1. AUTO-MATCH primero — corre auditar_huerfanas() y para mejor_score < 2.0
       hace asociar() con el candidato Asinfo. Esto resuelve los casos donde
       el match real existe en Asinfo, incluso si en PC la factura está
       "duplicada" (diferente fila con mismo numf legacy DBF).
       Skip DUPS_HUMAN_REVIEW para no causar conflict UNIQUE.
       Wrap en try/except — los conflictos UNIQUE se loguean pero el script
       sigue.
    2. MARCAR DUPS_OBVIAS — para los 5 ids conocidos, marca con '#DUP-<ganador>'
       SI todavía están sin numfc (no fueron auto-matched en STEP 1).
    3. MARCAR RESTO — toda huerfana viva sin numfc → '#SIN_ASINFO-<id>'.
       Skip DUPS_HUMAN_REVIEW (3 ids que requieren decisión humana).

USO:
    & "C:\\Python312\\python.exe" scripts\\cleanup_huerfanas_demo.py --dry-run
    & "C:\\Python312\\python.exe" scripts\\cleanup_huerfanas_demo.py

REVERSIBLE:
    UPDATE scintela.factura SET numf_completo = NULL
     WHERE numf_completo LIKE '#%';
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


# (id_a_marcar, id_ganador_real, motivo)
DUPS_OBVIAS = [
    (3937, 3936, "175377-CLR-ajuste-kg0"),
    (3634, 3633, "175107-CJE-vs-STB-anulada"),
    (2957, 2956, "174456-CJE-vs-STB-anulada"),
    (588,  1105, "169945-AJ2-true-dup"),
    (3796, 3795, "10724-AJ2-true-dup"),
]

# Estos NO los tocamos automaticamente — ambos stat=Z con clientes distintos,
# requieren decision humana cual es la real.
DUPS_HUMAN_REVIEW = {3247, 2718, 2545, 1356}

AUTO_MATCH_THRESHOLD = 2.0


def main():
    dry = "--dry-run" in sys.argv

    print("=" * 70)
    print(f"CLEANUP HUERFANAS DEMO -- dry-run={dry}")
    print("=" * 70)

    # ------- STEP 1: AUTO-MATCH primero -------------------------------------
    print(f"\nSTEP 1: Auto-match huerfanas con mejor_score < {AUTO_MATCH_THRESHOLD}")
    auditadas = audit_asinfo.auditar_huerfanas(top_k=1, limite=2000)
    # Filtrar solo las que el pantalla muestra (kg!=0)
    auditadas = [a for a in auditadas
                 if (a["pc_factura"].get("kg") or 0) != 0]
    print(f"  Total candidatos para auditar: {len(auditadas)}")

    matched = 0
    skipped_review = 0
    conflicts = 0
    for a in auditadas:
        pc = a["pc_factura"]
        score = a.get("mejor_score")
        cands = a.get("candidatos", [])
        if score is None or not cands:
            continue
        if score >= AUTO_MATCH_THRESHOLD:
            continue
        if pc["id_factura"] in DUPS_HUMAN_REVIEW:
            print(f"  SKIP review id={pc['id_factura']} cli={pc['codigo_cli']} "
                  f"score={score:.2f} (dup humano)")
            skipped_review += 1
            continue
        numero = cands[0]["ai_numero"]
        if dry:
            print(f"  [DRY] id={pc['id_factura']} cli={pc['codigo_cli']} "
                  f"score={score:.2f} -> {numero}")
            matched += 1
        else:
            try:
                n = audit_asinfo.asociar(
                    pc["id_factura"], numero, usuario="cleanup-demo"
                )
                print(f"  OK id={pc['id_factura']} -> {numero} "
                      f"(score={score:.2f}, rows={n})")
                matched += 1
            except Exception as e:
                msg = str(e)[:120].replace("\n", " ")
                print(f"  CONFLICT id={pc['id_factura']} -> {numero}: {msg}")
                conflicts += 1
                # Reset connection: psycopg2 deja la conexion en aborted state
                # despues del unique violation. db.execute usa pool, asi que
                # la siguiente call recupera una conexion limpia.
    print(f"  Auto-matched: {matched}  Skipped review: {skipped_review}  "
          f"Conflicts: {conflicts}")

    # ------- STEP 2: marcar DUPS_OBVIAS si todavia sin numfc ---------------
    print("\nSTEP 2: Marcar dups obvias con #DUP-<ganador> (si sin numfc)")
    for id_marcar, id_ganador, motivo in DUPS_OBVIAS:
        sentinel = f"#DUP-{id_ganador}"
        if dry:
            print(f"  [DRY] id={id_marcar} -> '{sentinel}' ({motivo})")
        else:
            n = db.execute(
                "UPDATE scintela.factura SET numf_completo=%s "
                "WHERE id_factura=%s "
                "  AND (numf_completo IS NULL OR numf_completo = '')",
                (sentinel, id_marcar),
            )
            estado = "MARCADA" if n else "YA MATCHED"
            print(f"  {estado} id={id_marcar} ({motivo}) rows={n}")

    # ------- STEP 3: marcar restantes con #SIN_ASINFO-<id> -----------------
    print("\nSTEP 3: Marcar huerfanas restantes con #SIN_ASINFO-<id>")
    restantes = db.fetch_all(
        """
        SELECT id_factura, numf, fecha, codigo_cli, kg, importe
          FROM scintela.factura
         WHERE fecha >= '2025-01-01'
           AND (kg IS NOT NULL AND kg <> 0)
           AND (stat IS NULL OR stat IN ('Z','A','T','X','N','',' '))
           AND (numf_completo IS NULL OR numf_completo = '')
        """
    )
    print(f"  Restantes huerfanas vivas: {len(restantes)}")
    marcadas = 0
    saltadas_review = 0
    for h in restantes:
        if h["id_factura"] in DUPS_HUMAN_REVIEW:
            saltadas_review += 1
            print(f"  SKIP id={h['id_factura']} (review humano: {h['codigo_cli']} "
                  f"numf={h['numf']} fecha={h['fecha']})")
            continue
        sentinel = f"#SIN_ASINFO-{h['id_factura']}"
        if dry:
            print(f"  [DRY] id={h['id_factura']} -> '{sentinel}' "
                  f"({h['codigo_cli']} kg={h['kg']} imp={h['importe']})")
        else:
            db.execute(
                "UPDATE scintela.factura SET numf_completo=%s "
                "WHERE id_factura=%s",
                (sentinel, h["id_factura"]),
            )
            marcadas += 1
    print(f"  Marcadas como sin Asinfo: {marcadas}")
    print(f"  Saltadas (review humano): {saltadas_review}")

    # ------- Reporte final --------------------------------------------------
    print("\n" + "=" * 70)
    print("RESULTADO")
    print("=" * 70)
    final = db.fetch_all(
        """
        SELECT COUNT(*) AS c
          FROM scintela.factura
         WHERE fecha >= '2025-01-01'
           AND (kg IS NOT NULL AND kg <> 0)
           AND (stat IS NULL OR stat IN ('Z','A','T','X','N','',' '))
           AND (numf_completo IS NULL OR numf_completo = ''
                OR numf IS NULL OR numf = 0)
           AND (numf_completo IS NULL OR NOT (numf_completo LIKE '#%%'))
        """
    )
    print(f"  Huerfanas reales no marcadas (visibles en pantalla): {final[0]['c']}")

    breakdown = db.fetch_all(
        """
        SELECT
          SUM(CASE WHEN numf_completo LIKE '#DUP-%%' THEN 1 ELSE 0 END) AS dups,
          SUM(CASE WHEN numf_completo LIKE '#SIN_ASINFO-%%' THEN 1 ELSE 0 END) AS sin_ai,
          SUM(CASE WHEN numf_completo IS NOT NULL AND numf_completo NOT LIKE '#%%'
                   AND numf_completo <> '' THEN 1 ELSE 0 END) AS matcheadas
          FROM scintela.factura
         WHERE fecha >= '2025-01-01'
           AND (kg IS NOT NULL AND kg <> 0)
           AND (stat IS NULL OR stat IN ('Z','A','T','X','N','',' '))
        """
    )
    b = breakdown[0]
    print(f"  Breakdown post-cleanup:")
    print(f"    Marcadas como dup:    {b['dups']}")
    print(f"    Marcadas sin Asinfo:  {b['sin_ai']}")
    print(f"    Con match real:       {b['matcheadas']}")


if __name__ == "__main__":
    main()
