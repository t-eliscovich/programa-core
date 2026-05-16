"""Backfill `batch_id` para filas de scintela.mov_doble que se insertaron
ANTES de que existiera la columna (migración 0031).

Contexto: cuando la 0031 no estaba aplicada, `mov_doble.registrar` cazaba
el "column batch_id does not exist" y caía a un INSERT sin batch_id como
fallback gracioso (mov_doble.py:139-177). Resultado: cualquier operación
multi-fila de esa época quedó como filas sueltas en /historial en vez de
agruparse en un batch.

Este script:
  1. Verifica que la columna batch_id YA exista (si no, levanta).
  2. Mira las filas con batch_id IS NULL.
  3. Las clusteriza por (usuario, fecha_creacion) dentro de una ventana de
     N segundos (default 5). Sólo tipos que típicamente batchean.
  4. Para cada cluster de >=2 filas, asigna un UUID fresco.

Idempotente — sólo toca filas con batch_id IS NULL. Re-corridas no rompen.

Dry-run por default (no UPDATEa). Usar --apply para escribir.

Uso:
    python scripts/backfill_batch_id.py                 # dry-run
    python scripts/backfill_batch_id.py --apply         # ejecutar
    python scripts/backfill_batch_id.py --ventana 10    # cluster window 10s
"""
from __future__ import annotations

import argparse
import os
import sys
import uuid
from collections import defaultdict
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import db  # noqa: E402

# Tipos que el código de hoy mete en un batch. Si una fila tiene uno de
# estos tipos + batch_id NULL + un vecino temporal cercano del mismo
# usuario, asumimos que pertenecían a la misma operación de UI.
#
# Conservador a propósito: NO incluimos tipos "directos" (caja_e_simple,
# banco_ch_directo, ...) ni transferencias/endosos/aportes que son ops
# unitarias por naturaleza. Mejor under-batch que sobre-batch.
TIPOS_BATCHEABLES = {
    "cheque_creado",
    "cheque_aplicado_a_factura",
    "cheque_anticipo_espejo",
    "reverso_compra_anulada",   # compra con multi-side-effect
    "reverso_cheque_aplicacion",
}


def _verificar_columna() -> None:
    row = db.fetch_one(
        """
        SELECT 1 AS ok
          FROM information_schema.columns
         WHERE table_schema='scintela'
           AND table_name='mov_doble'
           AND column_name='batch_id'
        """
    )
    if not row:
        print(
            "ERROR: la columna scintela.mov_doble.batch_id NO existe. "
            "Corré primero `python scripts/migrate.py` para aplicar 0031.",
            file=sys.stderr,
        )
        sys.exit(2)


def _candidatos(tipos_extra: set[str] | None = None) -> list[dict]:
    """Filas con batch_id NULL, de tipos batcheables, ordenadas temporal."""
    tipos = set(TIPOS_BATCHEABLES) | set(tipos_extra or set())
    placeholders = ",".join(["%s"] * len(tipos))
    return db.fetch_all(
        f"""
        SELECT id_mov_doble,
               fecha_creacion,
               COALESCE(usuario, '')        AS usuario,
               tipo,
               origen_table, origen_id,
               destino_table, destino_id,
               importe, concepto
          FROM scintela.mov_doble
         WHERE batch_id IS NULL
           AND tipo IN ({placeholders})
         ORDER BY fecha_creacion ASC, id_mov_doble ASC
        """,
        tuple(sorted(tipos)),
    ) or []


def _clusterizar(rows: list[dict], ventana_seg: int) -> list[list[dict]]:
    """Agrupa filas en clusters por (usuario, ventana temporal).

    Una fila inicia un nuevo cluster si:
      - usuario distinto al cluster actual, O
      - su fecha_creacion está más de `ventana_seg` después de la PRIMERA
        fila del cluster (no de la última — evita que una cadena larga
        de inserts engulla rows lejanos).
    """
    clusters: list[list[dict]] = []
    actual: list[dict] = []
    for r in rows:
        if not actual:
            actual = [r]
            continue
        head = actual[0]
        mismo_user = (r["usuario"] == head["usuario"])
        # `fecha_creacion` es timestamptz en PG → datetime en psycopg2.
        delta = (r["fecha_creacion"] - head["fecha_creacion"]).total_seconds()
        if mismo_user and 0 <= delta <= ventana_seg:
            actual.append(r)
        else:
            clusters.append(actual)
            actual = [r]
    if actual:
        clusters.append(actual)
    # Sólo nos interesan clusters de >1 — un row solo no es un batch.
    return [c for c in clusters if len(c) > 1]


def _resumen_cluster(c: list[dict]) -> str:
    head = c[0]
    total = sum(float(r.get("importe") or 0) for r in c)
    tipos = sorted({r["tipo"] for r in c})
    delta = (c[-1]["fecha_creacion"] - head["fecha_creacion"]).total_seconds()
    return (
        f"  cluster de {len(c):2} filas  "
        f"user={head['usuario']:<10}  "
        f"inicio={head['fecha_creacion']}  "
        f"Δt={delta:>4.1f}s  "
        f"total=${total:>12,.2f}  "
        f"tipos={','.join(tipos)}"
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--apply", action="store_true",
        help="Ejecutar UPDATEs (sin esto sólo es dry-run).",
    )
    ap.add_argument(
        "--ventana", type=int, default=5,
        help="Ventana en segundos para clusterizar (default 5).",
    )
    ap.add_argument(
        "--tipo-extra", action="append", default=[],
        help="Agregar tipos extra a la lista batcheable (puede repetirse).",
    )
    args = ap.parse_args()

    for k in ("DB_HOST", "DB_NAME", "DB_USER", "DB_PASSWORD"):
        if not os.environ.get(k):
            print(f"ERROR: falta {k} en el entorno.")
            return 2

    _verificar_columna()
    rows = _candidatos(set(args.tipo_extra))
    print(f"Candidatos con batch_id NULL: {len(rows)}")
    if not rows:
        print("Nada para backfillear.")
        return 0

    clusters = _clusterizar(rows, args.ventana)
    print(f"Clusters detectados (>=2 filas, ventana {args.ventana}s): "
          f"{len(clusters)}")
    if not clusters:
        print("No hay grupos plausibles. Quizás eran ops sueltas — listo.")
        return 0

    n_filas_total = sum(len(c) for c in clusters)
    print(f"Filas que recibirían un batch_id: {n_filas_total}")
    print()

    for c in clusters:
        print(_resumen_cluster(c))

    if not args.apply:
        print(
            "\nDry-run terminado. Si los clusters tienen sentido, "
            "re-corré con --apply."
        )
        return 0

    # UPDATE — un UUID por cluster, dentro de UNA tx.
    print("\nAplicando UPDATEs...")
    with db.tx() as conn:
        for c in clusters:
            bid = str(uuid.uuid4())
            ids = [int(r["id_mov_doble"]) for r in c]
            placeholders = ",".join(["%s"] * len(ids))
            db.execute(
                f"""
                UPDATE scintela.mov_doble
                   SET batch_id = %s::uuid
                 WHERE id_mov_doble IN ({placeholders})
                   AND batch_id IS NULL
                """,
                tuple([bid, *ids]),
                conn=conn,
            )
            print(f"  → batch {bid[:8]}…  {len(ids)} filas")

    print(f"\nListo. {len(clusters)} batches asignados, "
          f"{n_filas_total} filas actualizadas.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
