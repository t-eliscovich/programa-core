"""Genera el snapshot mensual de `scintela.historia`.

En el legacy dBase, al cerrar el mes se escribía una fila en HISTORIA.DBF
con los KPIs financieros: cartera viva, deuda viva, saldo bancos, gastos
del mes, kg y USD vendidos/comprados, etc. Esta tabla alimenta:

  - El balance histórico (`/informes/balance`)
  - El chart "Evolución cartera vs deuda" del dashboard del Dueño
  - El comparativo Real vs Meta de `/iniciales/comparativo`

En el nuevo app esto NO se estaba poblando — la tabla quedaba con datos
viejos del dump. Este script lo arregla.

Reglas:
  - Genera el snapshot del **mes anterior** al de la fecha actual (cuando
    se corre el día 2 del mes M, snapshot el último día de M-1).
  - Si ya existe fila con esa `fecha`, NO la pisa (el cierre manual o el
    legacy dBase tienen prioridad). Pasar `--force` para sobreescribir.
  - Se registra en `scintela.ejecuciones_tareas` para idempotencia y
    auditoría — misma mecánica que `procesa_provisiones_mensual.py`.

Campos calculados (otros quedan NULL):

  fecha       último día del mes que se snapshotea
  cart        SUM(factura.saldo) WHERE saldo > 0 AND stat IN (NULL,Z,A)
  deuda       SUM(posdat.importe) WHERE banc <> 9
  banco       SUM(transacciones_bancarias.saldo) — saldo más reciente por banco
  gasto       SUM(xgast.importe) WHERE fecha en el mes
  retiro      SUM(retiros.ret) WHERE fecha en el mes
  kvent       SUM(factura.kg)     WHERE fecha en el mes Y stat válido
  uvent       SUM(factura.importe) WHERE fecha en el mes Y stat válido
  kcom        SUM(compra.kg)      WHERE fecha en el mes
  ucom        SUM(compra.importe) WHERE fecha en el mes

Uso:

    python scripts/snapshot_historia_mensual.py             # estándar
    python scripts/snapshot_historia_mensual.py --periodo 2026-03
    python scripts/snapshot_historia_mensual.py --force     # sobreescribe
    python scripts/snapshot_historia_mensual.py --dry-run   # imprime, no escribe
"""
from __future__ import annotations

import argparse
import logging
import os
import socket
import sys
from calendar import monthrange
from datetime import date

from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db  # noqa: E402

EXIT_OK = 0
EXIT_ERROR = 1

log = logging.getLogger("snapshot_historia_mensual")


def _periodo_de(fecha: date) -> str:
    return fecha.strftime("%Y-%m")


def _ultimo_dia_mes_anterior(hoy: date) -> date:
    """Si hoy es 2026-04-15, devuelve 2026-03-31."""
    if hoy.month == 1:
        anio, mes = hoy.year - 1, 12
    else:
        anio, mes = hoy.year, hoy.month - 1
    _, ultimo_dia = monthrange(anio, mes)
    return date(anio, mes, ultimo_dia)


def _host() -> str:
    try:
        return socket.gethostname()[:60]
    except Exception:
        return "desconocido"


def calcular_kpis(fecha_cierre: date) -> dict:
    """Calcula los KPIs del mes que termina en `fecha_cierre`.

    Todas las queries son defensivas (COALESCE). Si una tabla no existe
    o está vacía, el campo queda en 0.
    """
    primer_dia = fecha_cierre.replace(day=1)

    def safe_one(sql: str, params=None) -> dict:
        try:
            return db.fetch_one(sql, params) or {}
        except Exception as e:
            log.warning("Query falló (campo queda en 0): %s — %s", sql.split()[1] if sql else "?", e)
            return {}

    # Cartera viva al cierre — saldos > 0 con stat válido
    cart = safe_one(
        """
        SELECT COALESCE(SUM(saldo), 0) AS v
        FROM scintela.factura
        WHERE COALESCE(saldo, 0) > 0
          AND (stat IS NULL OR stat IN ('Z','A','',' '))
        """
    ).get("v") or 0

    # Deuda viva al cierre — posdat abiertas
    deuda = safe_one(
        """
        SELECT COALESCE(SUM(importe), 0) AS v
        FROM scintela.posdat
        WHERE COALESCE(banc, 0) <> 9
        """
    ).get("v") or 0

    # Saldo total de bancos al cierre — última transacción <= fecha_cierre por banco
    banco = safe_one(
        """
        WITH ult AS (
            SELECT no_banco, saldo, fecha,
                   ROW_NUMBER() OVER (PARTITION BY no_banco ORDER BY fecha DESC, id_transaccion DESC) AS rn
            FROM scintela.transacciones_bancarias
            WHERE fecha <= %s
        )
        SELECT COALESCE(SUM(saldo), 0) AS v
        FROM ult
        WHERE rn = 1
        """,
        (fecha_cierre,),
    ).get("v") or 0

    # Gastos del mes
    gasto = safe_one(
        """
        SELECT COALESCE(SUM(importe), 0) AS v
        FROM scintela.xgast
        WHERE fecha >= %s AND fecha <= %s
        """,
        (primer_dia, fecha_cierre),
    ).get("v") or 0

    # Retiros del mes
    retiro = safe_one(
        """
        SELECT COALESCE(SUM(ret), 0) AS v
        FROM scintela.retiros
        WHERE fecha >= %s AND fecha <= %s
        """,
        (primer_dia, fecha_cierre),
    ).get("v") or 0

    # Ventas del mes — kg y USD
    ventas = safe_one(
        """
        SELECT COALESCE(SUM(kg), 0)      AS kvent,
               COALESCE(SUM(importe), 0) AS uvent
        FROM scintela.factura
        WHERE fecha >= %s AND fecha <= %s
          AND (stat IS NULL OR stat IN ('Z','A','T','P','',' '))
        """,
        (primer_dia, fecha_cierre),
    )

    # Compras del mes — kg y USD
    compras = safe_one(
        """
        SELECT COALESCE(SUM(kg), 0)      AS kcom,
               COALESCE(SUM(importe), 0) AS ucom
        FROM scintela.compra
        WHERE fecha >= %s AND fecha <= %s
          AND (stat IS NULL OR stat <> 'Y')
        """,
        (primer_dia, fecha_cierre),
    )

    return {
        "fecha":  fecha_cierre,
        "cart":   float(cart),
        "deuda":  float(deuda),
        "banco":  float(banco),
        "gasto":  float(gasto),
        "retiro": float(retiro),
        "kvent":  float(ventas.get("kvent") or 0),
        "uvent":  float(ventas.get("uvent") or 0),
        "kcom":   float(compras.get("kcom") or 0),
        "ucom":   float(compras.get("ucom") or 0),
    }


def existe_snapshot(fecha: date) -> dict | None:
    return db.fetch_one(
        """
        SELECT id_historia, fecha, usuario_crea
        FROM scintela.historia
        WHERE fecha = %s
        """,
        (fecha,),
    )


def insertar_snapshot(kpis: dict, usuario: str = "snapshot_auto") -> int:
    """Inserta un nuevo snapshot. Devuelve el id_historia generado."""
    row = db.execute_returning(
        """
        INSERT INTO scintela.historia
            (fecha, cart, deuda, banco, gasto, retiro,
             kvent, uvent, kcom, ucom, usuario_crea)
        VALUES (%(fecha)s, %(cart)s, %(deuda)s, %(banco)s, %(gasto)s, %(retiro)s,
                %(kvent)s, %(uvent)s, %(kcom)s, %(ucom)s, %(usuario)s)
        RETURNING id_historia
        """,
        {**kpis, "usuario": usuario[:50]},
    )
    return int(row["id_historia"]) if row else 0


def actualizar_snapshot(id_historia: int, kpis: dict, usuario: str = "snapshot_auto") -> int:
    """Sobreescribe un snapshot existente — sólo si el caller pasó --force."""
    return db.execute(
        """
        UPDATE scintela.historia
           SET cart = %(cart)s,
               deuda = %(deuda)s,
               banco = %(banco)s,
               gasto = %(gasto)s,
               retiro = %(retiro)s,
               kvent = %(kvent)s,
               uvent = %(uvent)s,
               kcom = %(kcom)s,
               ucom = %(ucom)s,
               fecha_modifica = CURRENT_TIMESTAMP,
               usuario_modifica = %(usuario)s
         WHERE id_historia = %(id_historia)s
        """,
        {**kpis, "id_historia": id_historia, "usuario": usuario[:50]},
    )


def ejecutar(fecha_cierre: date, *, force: bool = False, usuario: str = "snapshot_auto") -> dict:
    """API programática — llamable desde procesa_provisiones_mensual.py.

    Devuelve `{accion: 'inserted'|'updated'|'skipped', id_historia: int, kpis: dict}`.
    Lanza excepción si algo se rompe.
    """
    kpis = calcular_kpis(fecha_cierre)
    existing = existe_snapshot(fecha_cierre)
    if existing and not force:
        return {
            "accion":      "skipped",
            "id_historia": int(existing["id_historia"]),
            "kpis":        kpis,
            "razon":       f"ya existe (usuario_crea={existing.get('usuario_crea')})",
        }
    if existing and force:
        actualizar_snapshot(existing["id_historia"], kpis, usuario=usuario)
        return {"accion": "updated", "id_historia": int(existing["id_historia"]), "kpis": kpis}
    new_id = insertar_snapshot(kpis, usuario=usuario)
    return {"accion": "inserted", "id_historia": new_id, "kpis": kpis}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--periodo", help="YYYY-MM. Default: mes anterior al actual.")
    parser.add_argument("--force", action="store_true", help="Sobreescribe si ya existe.")
    parser.add_argument("--dry-run", action="store_true", help="Sólo imprime los KPIs, no escribe.")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-6s %(message)s",
    )

    # Determinar fecha de cierre
    if args.periodo:
        try:
            anio, mes = map(int, args.periodo.split("-"))
            _, ultimo_dia = monthrange(anio, mes)
            fecha_cierre = date(anio, mes, ultimo_dia)
        except (ValueError, IndexError):
            log.error("Periodo inválido: %r. Usá YYYY-MM.", args.periodo)
            return EXIT_ERROR
    else:
        fecha_cierre = _ultimo_dia_mes_anterior(date.today())

    log.info("Snapshot mes que cierra: %s (período %s)", fecha_cierre, _periodo_de(fecha_cierre))

    # Calcular KPIs
    try:
        kpis = calcular_kpis(fecha_cierre)
    except Exception as e:
        log.exception("Error calculando KPIs: %s", e)
        return EXIT_ERROR

    log.info("KPIs calculados:")
    for k, v in kpis.items():
        if k == "fecha":
            log.info("  %-8s %s", k, v)
        else:
            log.info("  %-8s %12.2f", k, v)

    if args.dry_run:
        log.info("--dry-run: no se escribe nada.")
        return EXIT_OK

    # Insert / update / skip
    try:
        existing = existe_snapshot(fecha_cierre)
        if existing and not args.force:
            log.info("Ya existe historia para %s (id=%s, usuario_crea=%s). Saltando. Usá --force.",
                     fecha_cierre, existing["id_historia"], existing.get("usuario_crea"))
            return EXIT_OK
        if existing and args.force:
            n = actualizar_snapshot(existing["id_historia"], kpis)
            log.info("Snapshot ACTUALIZADO (id=%s, %s filas)", existing["id_historia"], n)
        else:
            new_id = insertar_snapshot(kpis)
            log.info("Snapshot INSERTADO (id=%s)", new_id)
    except Exception as e:
        log.exception("Falló el INSERT/UPDATE: %s", e)
        return EXIT_ERROR

    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
