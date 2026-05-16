"""Restaurar el opening histórico de bancos después de que un recompute
desde-cero lo borró. EMERGENCIA — TMT 2026-05-12.

Cómo funciona: comparás el saldo ACTUAL (después del bug) con el saldo
ESPERADO (que vos sabés de memoria o de un screenshot anterior). El script
calcula el delta y se lo suma a CADA fila del running de ese banco, así
todas las corridas vuelven a estar alineadas.

Uso:
    # Ver los saldos actuales:
    python scripts/restaurar_saldos_bancos.py

    # Aplicar la corrección (Pichincha esperado = 2280906.19):
    python scripts/restaurar_saldos_bancos.py --banco 10 --saldo-esperado 2280906.19 --apply

    # Para varios bancos a la vez:
    python scripts/restaurar_saldos_bancos.py --banco 10 --saldo-esperado 2280906.19 \\
                                              --banco 32 --saldo-esperado 3761.19 \\
                                              --apply

Sin --apply hace dry-run y te muestra el delta que va a aplicar.
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


def saldo_actual(no_banco: int) -> tuple[float, int]:
    """Devuelve (saldo_stored última fila, número de filas) del banco."""
    row = db.fetch_one(
        """
        SELECT (SELECT t.saldo FROM scintela.transacciones_bancarias t
                 WHERE t.no_banco = %s
                 ORDER BY t.fecha DESC, t.id_transaccion DESC LIMIT 1) AS saldo,
               (SELECT COUNT(*) FROM scintela.transacciones_bancarias
                 WHERE no_banco = %s) AS n
        """,
        (no_banco, no_banco),
    ) or {}
    return float(row.get("saldo") or 0), int(row.get("n") or 0)


def nombre_banco(no_banco: int) -> str:
    row = db.fetch_one(
        "SELECT nombre FROM scintela.banco WHERE no_banco = %s",
        (no_banco,),
    )
    return (row or {}).get("nombre", f"banco #{no_banco}") or f"banco #{no_banco}"


def listar_bancos_relevantes() -> list[dict]:
    """Bancos con movimientos — para mostrar en el dry-run."""
    return db.fetch_all(
        """
        SELECT b.no_banco, COALESCE(b.nombre,'') AS nombre,
               (SELECT t.saldo FROM scintela.transacciones_bancarias t
                 WHERE t.no_banco = b.no_banco
                 ORDER BY t.fecha DESC, t.id_transaccion DESC LIMIT 1) AS saldo,
               (SELECT COUNT(*) FROM scintela.transacciones_bancarias
                 WHERE no_banco = b.no_banco) AS n
          FROM scintela.banco b
         WHERE EXISTS (SELECT 1 FROM scintela.transacciones_bancarias
                       WHERE no_banco = b.no_banco)
         ORDER BY b.no_banco
        """
    ) or []


def aplicar_offset(no_banco: int, offset: float) -> int:
    """Suma `offset` a TODAS las filas del banco. Devuelve filas afectadas."""
    with db.tx() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE scintela.transacciones_bancarias "
            "   SET saldo = COALESCE(saldo, 0) + %s "
            " WHERE no_banco = %s",
            (offset, no_banco),
        )
        return cur.rowcount


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--banco", type=int, action="append", default=[],
                    help="no_banco a corregir (repetible: --banco 10 --banco 32)")
    ap.add_argument("--saldo-esperado", type=float, action="append", default=[],
                    help="saldo que DEBERÍA tener cada banco (en el mismo orden que --banco)")
    ap.add_argument("--apply", action="store_true",
                    help="Ejecuta el UPDATE. Sin esto, dry-run.")
    args = ap.parse_args()

    if not args.banco:
        print()
        print("═══ Bancos con movimientos — saldos actuales ═══")
        for b in listar_bancos_relevantes():
            print(f"  no_banco={b['no_banco']:>3} · {b['nombre']:<20} "
                  f"saldo_actual={float(b.get('saldo') or 0):>15,.2f}  "
                  f"({b.get('n')} mov.)")
        print()
        print("Para corregir un banco:")
        print("  python scripts/restaurar_saldos_bancos.py "
              "--banco <N> --saldo-esperado <V> --apply")
        return 0

    if len(args.banco) != len(args.saldo_esperado):
        print("Cantidad de --banco y --saldo-esperado tiene que coincidir.",
              file=sys.stderr)
        return 2

    print()
    print(f"═══ Plan de restauración ({'APLY' if args.apply else 'DRY RUN'}) ═══")
    for no_banco, esperado in zip(args.banco, args.saldo_esperado):
        actual, n_filas = saldo_actual(no_banco)
        nombre = nombre_banco(no_banco)
        offset = esperado - actual
        print()
        print(f"  Banco no_banco={no_banco} · {nombre}")
        print(f"    Filas: {n_filas}")
        print(f"    Saldo actual:    {actual:>15,.2f}")
        print(f"    Saldo esperado:  {esperado:>15,.2f}")
        print(f"    Offset a aplicar: {offset:>+15,.2f} (a cada fila)")

        if args.apply:
            if abs(offset) < 0.01:
                print("    ✓ Ya está en el saldo esperado, no toco nada.")
                continue
            n = aplicar_offset(no_banco, offset)
            saldo_post, _ = saldo_actual(no_banco)
            print(f"    ▶ UPDATE: {n} fila(s) afectada(s). Saldo nuevo: {saldo_post:,.2f}")

    if not args.apply:
        print()
        print("═══ DRY RUN — no se tocó nada. ═══")
        print("Para ejecutar, agregá --apply al comando.")
    else:
        print()
        print("✓ Listo. Refrescá /bancos para verificar.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
