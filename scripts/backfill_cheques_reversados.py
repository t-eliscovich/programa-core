"""Backfill: marca como stat='X' (Reversado) los cheques que quedaron
sin aplicaciones después de un `reverso_cheque_aplicacion`.

Pedido TMT 2026-05-14: cheques como #1899 quedaron en stat='P' aunque
su aplicación a factura fue reversada. La nueva regla en
`desaplicar_factura` auto-anula al cheque cuando queda sin aplicaciones;
este script aplica la misma regla retroactivamente a cheques históricos.

USO:
    python scripts/backfill_cheques_reversados.py            # dry-run
    python scripts/backfill_cheques_reversados.py --aplicar  # ejecuta

Criterio para marcar como reversado:
    1. cheque.stat IN ('Z', 'P')  — todavía vivo en cartera
    2. cheque tiene al menos un mov_doble 'reverso_cheque_aplicacion'
    3. cheque NO tiene chequesxfact (sin aplicaciones activas)
    4. cheque NO está depositado (B/A), endosado (E), rebotado (1/2/3/R)
       — esos tienen otros flujos.
"""
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import db  # noqa: E402


def encontrar_candidatos() -> list[dict]:
    """Cheques en cartera con aplicación reversada y sin chequesxfact viva."""
    return db.fetch_all(
        """
        SELECT
            c.id_cheque,
            c.no_cheque,
            c.codigo_cli,
            COALESCE(cli.nombre, '') AS cliente,
            c.fecha,
            c.fechad,
            c.importe,
            c.stat,
            (SELECT COUNT(*) FROM scintela.mov_doble md
              WHERE md.origen_table = 'cheque'
                AND md.origen_id = c.id_cheque
                AND md.tipo = 'reverso_cheque_aplicacion')   AS reversos_aplicacion,
            (SELECT COUNT(*) FROM scintela.chequesxfact cxf
              WHERE cxf.id_cheque = c.id_cheque)              AS aplicaciones_activas
        FROM scintela.cheque c
        LEFT JOIN scintela.cliente cli ON cli.codigo_cli = c.codigo_cli
        WHERE c.stat IN ('Z', 'P')
          AND EXISTS (
              SELECT 1 FROM scintela.mov_doble md
               WHERE md.origen_table = 'cheque'
                 AND md.origen_id = c.id_cheque
                 AND md.tipo = 'reverso_cheque_aplicacion'
          )
          AND NOT EXISTS (
              SELECT 1 FROM scintela.chequesxfact cxf
               WHERE cxf.id_cheque = c.id_cheque
          )
        ORDER BY c.id_cheque
        """
    ) or []


def aplicar_backfill(rows: list[dict], usuario: str = "backfill") -> int:
    """Aplica stat='X' a cada candidato. Atómico por fila."""
    n = 0
    for r in rows:
        id_cheque = int(r["id_cheque"])
        marca = "[X] backfill 2026-05-14: aplicación reversada, sin aplicaciones vivas"
        with db.tx() as conn:
            db.execute(
                "UPDATE scintela.cheque "
                "SET stat='X', fechaout=%s, "
                "    observacion = RIGHT(COALESCE(observacion || ' | ', '') || %s, 200), "
                "    usuario_modifica=%s, fecha_modifica=CURRENT_TIMESTAMP "
                "WHERE id_cheque=%s AND stat IN ('Z','P')",
                (date.today(), marca, usuario, id_cheque),
                conn=conn,
            )
        n += 1
    return n


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--aplicar", action="store_true",
                   help="ejecuta los UPDATEs (sin esto, dry-run)")
    p.add_argument("--usuario", default="backfill",
                   help="usuario_modifica a registrar")
    args = p.parse_args()

    candidatos = encontrar_candidatos()
    if not candidatos:
        print("No se encontraron cheques candidatos. Nada para hacer.")
        return 0

    print(f"Encontrados {len(candidatos)} candidato(s):")
    print()
    total_imp = 0.0
    for r in candidatos:
        imp = float(r.get("importe") or 0)
        total_imp += imp
        print(f"  id={r['id_cheque']:>5}  N°={r.get('no_cheque') or '—':>10}  "
              f"stat='{r['stat']}'  importe=${imp:>10,.2f}  "
              f"{r.get('codigo_cli','')} {r.get('cliente','')[:30]}")
        print(f"         reversos_aplicacion={r['reversos_aplicacion']}  "
              f"aplicaciones_activas={r['aplicaciones_activas']}")
    print()
    print(f"Total importe a marcar como reversado: ${total_imp:,.2f}")
    print()

    if not args.aplicar:
        print("DRY-RUN. Ningún cambio aplicado.")
        print("Para ejecutar:  python scripts/backfill_cheques_reversados.py --aplicar")
        return 0

    n = aplicar_backfill(candidatos, usuario=args.usuario)
    print(f"✓ Aplicado: {n} cheque(s) marcado(s) como stat='X' (Reversado).")
    print("Ahora aparecen con badge 'Reversado' en /cheques y no suman a los KPIs.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
