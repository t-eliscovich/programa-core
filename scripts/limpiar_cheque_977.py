"""Limpiar el cheque #977 que quedó en estado inconsistente.

Estado actual (post bug):
- cheque #977: stat='X' (Eliminado) — debería ser 'Z' (cartera).
- compra a ACR ($50.409,93) creada al endosar: sigue ACTIVA (debería estar anulada).
- mov_doble 'endoso_cheque_a_proveedor' #5434: estado='activo' — debería estar reversado.
- Hay un mov_doble 'reverso_cheque_administrativo' creado por el flujo incorrecto.

Este script:
1. Anula la compra a ACR creada por el endoso (stat='Y').
2. Pone el cheque #977 de vuelta en stat='Z' (cartera), limpia prov/fechaout.
3. Marca el mov_doble 'endoso_cheque_a_proveedor' como 'reversado' y linkea
   al mov_doble 'reverso_cheque_administrativo' que ya existe (lo reciclamos).
4. Cambia el tipo del reverso a 'reverso_endoso_cheque' para que el historial
   refleje correctamente lo que pasó.

USO: dry-run por default. Con --apply hace los cambios.

    python scripts/limpiar_cheque_977.py
    python scripts/limpiar_cheque_977.py --apply
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import db  # noqa: E402

ID_CHEQUE = 977


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--apply", action="store_true",
                   help="Aplica los cambios. Sin esto, dry-run.")
    args = p.parse_args()
    dry = not args.apply

    print(f"┌─ Limpieza cheque #{ID_CHEQUE} — {'DRY RUN' if dry else 'APLICANDO'}")
    print("└─")

    # 1) Estado actual del cheque
    ch = db.fetch_one(
        """
        SELECT id_cheque, no_cheque, stat, prov, fechaout, codigo_cli, importe
          FROM scintela.cheque WHERE id_cheque = %s
        """,
        (ID_CHEQUE,),
    )
    if not ch:
        print(f"ERROR: cheque #{ID_CHEQUE} no existe.")
        return 1
    print("\nCheque actual:")
    print(f"  stat={ch['stat']}  prov={ch.get('prov')}  fechaout={ch.get('fechaout')}")
    print(f"  cliente original={ch.get('codigo_cli')}  importe={ch.get('importe')}")

    # 2) Buscar mov_doble del endoso
    md_endoso = db.fetch_one(
        """
        SELECT id_mov_doble, destino_table, destino_id, estado, importe
          FROM scintela.mov_doble
         WHERE origen_table = 'cheque'
           AND origen_id    = %s
           AND tipo         = 'endoso_cheque_a_proveedor'
         ORDER BY id_mov_doble DESC LIMIT 1
        """,
        (ID_CHEQUE,),
    )
    print("\nmov_doble endoso original:")
    if md_endoso:
        print(f"  id={md_endoso['id_mov_doble']}  estado={md_endoso['estado']}  "
              f"dest=({md_endoso['destino_table']} #{md_endoso['destino_id']})  "
              f"importe={md_endoso['importe']}")
    else:
        print("  NO ENCONTRADO — uy.")

    # 3) Buscar compra hermana
    id_compra = md_endoso.get("destino_id") if md_endoso and md_endoso.get("destino_table") == "compra" else None
    if id_compra is None:
        row = db.fetch_one(
            """
            SELECT id_compra, stat, importe, codigo_prov
              FROM scintela.compra
             WHERE comprobante = %s
               AND cuenta_pagada = 'E'
             ORDER BY id_compra DESC LIMIT 1
            """,
            (f"CH{ch.get('no_cheque') or ID_CHEQUE}"[:20],),
        )
        if row:
            id_compra = row["id_compra"]
            print("\nCompra hermana (encontrada por comprobante):")
            print(f"  id={id_compra}  stat={row['stat']}  prov={row['codigo_prov']}  importe={row['importe']}")
    else:
        compra_data = db.fetch_one(
            "SELECT stat, importe, codigo_prov FROM scintela.compra WHERE id_compra=%s",
            (id_compra,),
        )
        print(f"\nCompra hermana (id_compra={id_compra}):")
        if compra_data:
            print(f"  stat={compra_data['stat']}  prov={compra_data['codigo_prov']}  importe={compra_data['importe']}")

    # 4) Buscar el mov_doble de reverso incorrecto que el bug creó
    md_reverso_actual = db.fetch_one(
        """
        SELECT id_mov_doble, tipo, estado, id_original
          FROM scintela.mov_doble
         WHERE origen_table = 'cheque'
           AND origen_id    = %s
           AND tipo LIKE 'reverso_cheque_%%'
         ORDER BY id_mov_doble DESC LIMIT 1
        """,
        (ID_CHEQUE,),
    )
    print("\nmov_doble del reverso (creado por el bug):")
    if md_reverso_actual:
        print(f"  id={md_reverso_actual['id_mov_doble']}  tipo={md_reverso_actual['tipo']}  "
              f"estado={md_reverso_actual['estado']}  id_original={md_reverso_actual['id_original']}")
    else:
        print("  no hay reverso registrado — solo el cheque cambió de stat.")

    if dry:
        print("\n┌─ ACCIONES A APLICAR (dry):")
        if id_compra:
            print(f"│  1. UPDATE compra #{id_compra} SET stat='Y'  + observación 'REVERSO endoso ch{ID_CHEQUE}'")
        else:
            print("│  1. (no hay compra que anular)")
        print(f"│  2. UPDATE cheque #{ID_CHEQUE} SET stat='Z', prov=NULL, fechaout=NULL")
        if md_reverso_actual:
            print(f"│  3. UPDATE mov_doble #{md_reverso_actual['id_mov_doble']} SET tipo='reverso_endoso_cheque'")
            if md_endoso:
                print(f"│  4. UPDATE mov_doble #{md_reverso_actual['id_mov_doble']} SET id_original={md_endoso['id_mov_doble']}")
                print(f"│  5. UPDATE mov_doble #{md_endoso['id_mov_doble']} (endoso original) SET estado='reversado', id_reverso={md_reverso_actual['id_mov_doble']}")
        else:
            if md_endoso:
                print(f"│  3. INSERT mov_doble nuevo (reverso_endoso_cheque) linkeado a #{md_endoso['id_mov_doble']}")
                print(f"│  4. UPDATE mov_doble #{md_endoso['id_mov_doble']} SET estado='reversado'")
        print("└─")
        print("\nVolvé a correr con --apply para ejecutar.")
        return 0

    # APLICAR
    print("\n┌─ APLICANDO CAMBIOS:")
    with db.tx() as conn:
        # 1) Anular compra
        if id_compra:
            db.execute(
                """
                UPDATE scintela.compra
                   SET stat='Y',
                       observacion = COALESCE(observacion, '') ||
                                     E'\n[REVERSO endoso ch' || %s || ' — limpieza manual 2026-05-13]',
                       usuario_modifica='limpieza_script',
                       fecha_modifica=CURRENT_TIMESTAMP
                 WHERE id_compra=%s
                """,
                (ID_CHEQUE, id_compra),
                conn=conn,
            )
            print(f"│  ✓ Compra #{id_compra} anulada (stat='Y').")
        # 2) Restaurar cheque
        db.execute(
            """
            UPDATE scintela.cheque
               SET stat='Z', prov=NULL, fechaout=NULL,
                   observacion = RIGHT(
                     COALESCE(observacion || ' | ', '') ||
                     '[LIMPIEZA reverso endoso 2026-05-13 — stat X→Z]', 500),
                   usuario_modifica='limpieza_script',
                   fecha_modifica=CURRENT_TIMESTAMP
             WHERE id_cheque=%s
            """,
            (ID_CHEQUE,),
            conn=conn,
        )
        print(f"│  ✓ Cheque #{ID_CHEQUE} restaurado a cartera (stat='Z').")
        # 3) Arreglar mov_doble
        if md_reverso_actual:
            db.execute(
                """
                UPDATE scintela.mov_doble
                   SET tipo='reverso_endoso_cheque',
                       id_original=%s
                 WHERE id_mov_doble=%s
                """,
                (md_endoso["id_mov_doble"] if md_endoso else None,
                 md_reverso_actual["id_mov_doble"]),
                conn=conn,
            )
            print(f"│  ✓ mov_doble #{md_reverso_actual['id_mov_doble']} cambiado a tipo='reverso_endoso_cheque' + id_original linkeado.")
            if md_endoso:
                db.execute(
                    """
                    UPDATE scintela.mov_doble
                       SET estado='reversado', id_reverso=%s
                     WHERE id_mov_doble=%s
                    """,
                    (md_reverso_actual["id_mov_doble"], md_endoso["id_mov_doble"]),
                    conn=conn,
                )
                print(f"│  ✓ mov_doble #{md_endoso['id_mov_doble']} (endoso) marcado como 'reversado'.")
    print("└─\n✓ Limpieza completada. Verificá /cheques/977 y /compras (la fila a ACR ahora debe estar ANUL).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
