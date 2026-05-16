"""Purga los movimientos de prueba del día — DELETE FÍSICO.

Uso:
    python scripts/purgar_pruebas_hoy.py                # DRY RUN (no toca nada)
    python scripts/purgar_pruebas_hoy.py --fecha 2026-05-12   # dry-run de otra fecha
    python scripts/purgar_pruebas_hoy.py --fecha 2026-05-12 --apply   # ejecuta

Filtra las filas por **fecha del movimiento** = la fecha pasada (default: hoy)
y las borra de:
    - scintela.mov_doble (todas las filas, activas y reversas)
    - scintela.caja
    - scintela.transacciones_bancarias
    - scintela.retiros
    - scintela.capital
    - scintela.dolares (solo filas de hoy)
    - scintela.posdat (solo las creadas hoy)

Recalcula los saldos `running` de los bancos y caja afectados después del DELETE
para que no queden corridas rotas.

NO toca facturas, cheques, compras, clientes, proveedores — sólo los movs de
plata del día. Si en la fecha que vas a purgar también hay movs reales (no
solo pruebas), aborta sin tocar nada antes de pedirte confirmación.

Importante: el DELETE es físico — no hay forma de deshacer. El dry-run te
muestra exactamente qué se va a borrar antes de confirmar.
"""
from __future__ import annotations

import argparse
import sys
from datetime import date as _date
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# Permitir `import db` desde el root del proyecto.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import db  # noqa: E402

TABLAS = [
    # (display_name, query_listado, query_count, sql_delete, tabla_postfix_recompute)
    (
        "mov_doble",
        "SELECT id_mov_doble, fecha_operacion, tipo, importe, concepto, usuario "
        "FROM scintela.mov_doble WHERE fecha_operacion = %s "
        "ORDER BY id_mov_doble",
        "SELECT COUNT(*) AS n FROM scintela.mov_doble WHERE fecha_operacion = %s",
        "DELETE FROM scintela.mov_doble WHERE fecha_operacion = %s",
        None,
    ),
    (
        "caja",
        "SELECT id_caja, fecha, tipo, importe, concepto, usuario_crea "
        "FROM scintela.caja WHERE fecha = %s "
        "ORDER BY id_caja",
        "SELECT COUNT(*) AS n FROM scintela.caja WHERE fecha = %s",
        "DELETE FROM scintela.caja WHERE fecha = %s",
        "caja",
    ),
    (
        "transacciones_bancarias",
        "SELECT id_transaccion, fecha, no_banco, documento, importe, concepto "
        "FROM scintela.transacciones_bancarias WHERE fecha = %s "
        "ORDER BY id_transaccion",
        "SELECT COUNT(*) AS n FROM scintela.transacciones_bancarias WHERE fecha = %s",
        "DELETE FROM scintela.transacciones_bancarias WHERE fecha = %s",
        "banco",
    ),
    (
        "retiros",
        "SELECT id_retiro, fecha, ret, de, concepto "
        "FROM scintela.retiros WHERE fecha = %s "
        "ORDER BY id_retiro",
        "SELECT COUNT(*) AS n FROM scintela.retiros WHERE fecha = %s",
        "DELETE FROM scintela.retiros WHERE fecha = %s",
        None,
    ),
    (
        "capital",
        "SELECT id_capital, fecha, doc, importe, concepto "
        "FROM scintela.capital WHERE fecha = %s "
        "ORDER BY id_capital",
        "SELECT COUNT(*) AS n FROM scintela.capital WHERE fecha = %s",
        "DELETE FROM scintela.capital WHERE fecha = %s",
        None,
    ),
    (
        "dolares",
        "SELECT id_dolares, fecha, cta, importe, concepto "
        "FROM scintela.dolares WHERE fecha = %s "
        "ORDER BY id_dolares",
        "SELECT COUNT(*) AS n FROM scintela.dolares WHERE fecha = %s",
        "DELETE FROM scintela.dolares WHERE fecha = %s",
        None,
    ),
    (
        "posdat",
        "SELECT id_posdat, fecha, fechad, prov, importe, concepto "
        "FROM scintela.posdat WHERE fecha = %s "
        "ORDER BY id_posdat",
        "SELECT COUNT(*) AS n FROM scintela.posdat WHERE fecha = %s",
        "DELETE FROM scintela.posdat WHERE fecha = %s",
        None,
    ),
]


def listar(fecha: _date) -> dict:
    """Devuelve {tabla: [filas...]} para todas las tablas afectadas."""
    out = {}
    for nombre, sql_list, _, _, _ in TABLAS:
        out[nombre] = db.fetch_all(sql_list, (fecha,)) or []
    return out


def imprimir_plan(fecha: _date, plan: dict) -> int:
    """Imprime un dump del plan; devuelve cantidad total de filas a borrar."""
    total = 0
    print()
    print(f"═══ Plan de purga para fecha = {fecha.isoformat()} ═══")
    for nombre, filas in plan.items():
        print()
        print(f"── {nombre} ({len(filas)} fila{'s' if len(filas) != 1 else ''})")
        if not filas:
            continue
        # Imprime cada fila como dict en una línea
        for r in filas[:30]:
            keys = ", ".join(
                f"{k}={r[k]!r}"
                for k in r
                if r[k] is not None and not isinstance(r[k], bytes | bytearray)
            )
            print(f"   • {keys}")
        if len(filas) > 30:
            print(f"   … y {len(filas) - 30} fila(s) más")
        total += len(filas)
    print()
    print(f"═══ Total a borrar: {total} fila(s) ═══")
    return total


def aplicar(fecha: _date, plan: dict) -> None:
    """Ejecuta los DELETE en una sola transacción + recompute de saldos."""
    print()
    print(f"▶ Aplicando DELETE para fecha = {fecha.isoformat()}…")

    bancos_afectados: set[int] = set()
    # Tomar nota de qué bancos tienen filas para recomputar después.
    for r in plan.get("transacciones_bancarias", []):
        b = r.get("no_banco")
        if b is not None:
            bancos_afectados.add(int(b))

    with db.tx() as conn, conn.cursor() as cur:
        for nombre, _, _, sql_delete, _ in TABLAS:
            cur.execute(sql_delete, (fecha,))
            print(f"   • {nombre:28s} → {cur.rowcount} fila(s) borradas")

        # Recompute saldos bancarios afectados.
        # IMPORTANTE TMT 2026-05-12: recompute_saldos_desde(ancla=None) parte
        # de 0 y BORRA el opening histórico del banco — eso destruyó saldos
        # en una corrida anterior. Acá usamos ancla_fecha=fecha para que
        # recompute SOLO desde la fecha purgada, manteniendo el saldo
        # pre-purga de la fila anterior como punto de partida (que ya tiene
        # el opening incluido).
        if bancos_afectados:
            print()
            print(f"▶ Recomputando saldos running desde {fecha.isoformat()}…")
            try:
                import bank_helpers
                for no_banco in sorted(bancos_afectados):
                    n = bank_helpers.recompute_saldos_desde(
                        conn,
                        no_banco=int(no_banco),
                        no_cta=None,
                        ancla_id=None,
                        ancla_fecha=fecha,   # ← key fix: NO partir de 0
                    )
                    print(f"   • banco no_banco={no_banco}: {n} fila(s) tocadas")
            except Exception as e:
                print(f"   ⚠ Recompute falló: {e} — ejecutalo manual desde /bancos.")

    print()
    print("✓ Listo. Refrescá /bancos, /caja, /historial para verificar.")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument(
        "--fecha",
        default=_date.today().isoformat(),
        help="Fecha YYYY-MM-DD (default: hoy)",
    )
    ap.add_argument(
        "--apply",
        action="store_true",
        help="Ejecuta los DELETE. Sin esto, solo dry-run.",
    )
    args = ap.parse_args()

    try:
        fecha = _date.fromisoformat(args.fecha)
    except ValueError:
        print(f"Fecha inválida: {args.fecha!r}. Usá formato YYYY-MM-DD.", file=sys.stderr)
        return 2

    plan = listar(fecha)
    total = imprimir_plan(fecha, plan)

    if total == 0:
        print()
        print("Nada que borrar. Si esperabas filas, revisá la fecha pasada.")
        return 0

    if not args.apply:
        print()
        print("Esto fue DRY RUN — no se borró nada.")
        print("Para ejecutar de verdad:")
        print(f"   python scripts/purgar_pruebas_hoy.py --fecha {fecha.isoformat()} --apply")
        return 0

    aplicar(fecha, plan)
    return 0


if __name__ == "__main__":
    sys.exit(main())
