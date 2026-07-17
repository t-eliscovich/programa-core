"""Corre el ciclo mensual de procedures contables.

Hace dos cosas y las trackea en `scintela.ejecuciones_tareas`:

    1. CALL scintela.procesa_provisiones(CURRENT_DATE)
    2. SELECT scintela.actualizar_amortizacion()

Diseño:

- Idempotente. El mismo mes puede llamarse N veces — sólo la primera
  ejecución exitosa registra estado='O'. Las siguientes ven el UNIQUE
  (tarea, periodo) y salen en silencio con exit 0.
- Cada tarea corre en su propia tx con su propia fila en
  ejecuciones_tareas. Si procesa_provisiones falla, actualizar_amortizacion
  todavía se intenta. El reporte final lista qué quedó verde / rojo.
- Exit 0 si todas las tareas están terminadas con 'O' al final (incluyendo
  las que ya corrieron antes). Exit 1 si alguna tarea quedó en 'E' este
  run. Cualquier exit != 0 es una señal al scheduler / a cron para que
  alerte.
- Si las procedures no existen todavía (DB nuevita, migración no corrió,
  schema viejo), exit 2 con mensaje claro. Distinguible de "algo se rompió"
  (exit 1) y de "todo OK" (exit 0).

Uso desde cron / Windows Task Scheduler:

    python scripts/procesa_provisiones_mensual.py            # estándar
    python scripts/procesa_provisiones_mensual.py --periodo 2026-03
    python scripts/procesa_provisiones_mensual.py --force    # re-intenta aunque ya haya 'O'

Ver `intela-aws-deploy` skill para la config del Scheduled Task en Windows.
"""
from __future__ import annotations

import argparse
import logging
import os
import socket
import sys
from datetime import date, datetime

from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from psycopg2.extras import RealDictCursor  # noqa: E402

import db  # noqa: E402

# Exit codes — los usa el scheduler para saber si alertar.
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_MISSING_PROC = 2

# Las tareas que este script corre, en orden. Cada entrada es
# (nombre_tarea, sql_call). Si sumás una tarea nueva, acá es el único
# lugar que toca — la lógica de tracking es genérica.
TAREAS = (
    ("procesa_provisiones",    "CALL scintela.procesa_provisiones(%s)"),
    ("actualizar_amortizacion", "SELECT scintela.actualizar_amortizacion()"),
    ("snapshot_historia",       "PYTHON:snapshot_historia"),
)

log = logging.getLogger("procesa_provisiones_mensual")


def _periodo_de(fecha: date) -> str:
    """Formato 'YYYY-MM' usado como UNIQUE key en ejecuciones_tareas."""
    return fecha.strftime("%Y-%m")


def _host() -> str:
    """Host donde corre el proceso — útil para el postmortem si hay varios runners."""
    try:
        return socket.gethostname()[:60]
    except Exception:
        return "desconocido"


def _intentar_reservar(tarea: str, periodo: str, host: str) -> int | None:
    """INSERT ... ON CONFLICT DO NOTHING RETURNING id_ejecucion.

    Devuelve el id si este proceso se adueñó del slot, None si ya existía
    (es decir: otra corrida en el mismo periodo con estado O, E o R).
    """
    row = db.execute_returning(
        """
        INSERT INTO scintela.ejecuciones_tareas (tarea, periodo, estado, host)
        VALUES (%s, %s, 'R', %s)
        ON CONFLICT ON CONSTRAINT uq_ejecuciones_tareas_periodo DO NOTHING
        RETURNING id_ejecucion
        """,
        (tarea, periodo, host),
    )
    return row["id_ejecucion"] if row else None


def _estado_actual(tarea: str, periodo: str) -> dict | None:
    """Estado actual del slot (cuando ya estaba reservado)."""
    return db.fetch_one(
        """
        SELECT id_ejecucion, estado, iniciado_en, terminado_en, mensaje
          FROM scintela.ejecuciones_tareas
         WHERE tarea = %s AND periodo = %s
        """,
        (tarea, periodo),
    )


def _reset_slot(tarea: str, periodo: str, host: str) -> int:
    """Borrar el slot existente y reservar uno nuevo (usado con --force).

    Devuelve el id_ejecucion del slot recién creado. Hace el DELETE + INSERT
    en la misma transacción para que nunca queden filas huérfanas si algo
    revienta entre medio.
    """
    with db.tx() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "DELETE FROM scintela.ejecuciones_tareas WHERE tarea = %s AND periodo = %s",
            (tarea, periodo),
        )
        cur.execute(
            """
            INSERT INTO scintela.ejecuciones_tareas (tarea, periodo, estado, host)
            VALUES (%s, %s, 'R', %s)
            RETURNING id_ejecucion
            """,
            (tarea, periodo, host),
        )
        row = cur.fetchone()
    return int(row["id_ejecucion"])


def _marcar_ok(id_ejecucion: int) -> None:
    db.execute(
        """
        UPDATE scintela.ejecuciones_tareas
           SET estado = 'O',
               terminado_en = CURRENT_TIMESTAMP,
               mensaje = NULL
         WHERE id_ejecucion = %s
        """,
        (id_ejecucion,),
    )


def _marcar_error(id_ejecucion: int, mensaje: str) -> None:
    # No usar db.tx aquí — el tx de la tarea probablemente ya murió.
    # execute() abre su propio conn del pool.
    db.execute(
        """
        UPDATE scintela.ejecuciones_tareas
           SET estado = 'E',
               terminado_en = CURRENT_TIMESTAMP,
               mensaje = %s
         WHERE id_ejecucion = %s
        """,
        (mensaje[:2000], id_ejecucion),
    )


def _ejecutar_tarea(tarea: str, sql_call: str, fecha: date) -> None:
    """Corre la procedure/function PostgreSQL que corresponde a la tarea.

    Usa db.tx() para aislar el call en su propia transacción. Si la procedure
    levanta, la tx se rollbackea y propagamos la excepción. db.tx() yields
    a raw psycopg2 connection, por eso abrimos el cursor a mano en vez de
    usar db.execute() (que tomaría un conn distinto del pool y no
    participaría de esta misma transacción).

    Casos especiales:
    - "PYTHON:snapshot_historia" → invoca el Python helper en vez de SQL.
    """
    # Caso especial: snapshot historia es Python, no SQL
    if sql_call == "PYTHON:snapshot_historia":
        # Asegurar que scripts/ esté en sys.path — corremos desde varios
        # contextos (cron Windows, pytest, manual) y no siempre lo está.
        scripts_dir = os.path.dirname(os.path.abspath(__file__))
        if scripts_dir not in sys.path:
            sys.path.insert(0, scripts_dir)
        from snapshot_historia_mensual import _ultimo_dia_mes_anterior
        fecha_cierre = _ultimo_dia_mes_anterior(fecha)
        # 2026-06-04 — usar crear_snapshot_historia (calcula el balance
        # `as_of` el último día del mes via balance_components_as_of) en
        # lugar de ejecutar() (que tomaba el balance LIVE de hoy, día 1-2 del
        # mes siguiente, e inflaba/desfasaba los saldos del cierre). El
        # camino as_of además incluye la caja en `banco` y excluye
        # 'asinfo-backfill'. Idempotente: salta si ya existe el mes.
        from modules.informes.queries import crear_snapshot_historia
        result = crear_snapshot_historia(
            fecha_cierre.year, fecha_cierre.month, usuario=f"cron_{tarea}"
        )
        log.info(
            "snapshot_historia %s -> aplicado=%s (id=%s, %s)",
            fecha_cierre, result.get("aplicado"), result.get("id_historia"),
            result.get("razon", ""),
        )
        return

    # procesa_provisiones toma (fecha); actualizar_amortizacion no toma args.
    params = (fecha,) if "%s" in sql_call else ()
    with db.tx() as conn, conn.cursor() as cur:
        cur.execute(sql_call, params)


def correr(
    *, periodo: str, fecha: date, force: bool = False
) -> tuple[int, list[tuple[str, str, str]]]:
    """Corre todas las TAREAS. Devuelve (exit_code, [(tarea, estado, mensaje)])."""
    host = _host()
    resultados: list[tuple[str, str, str]] = []
    hubo_error = False

    for tarea, sql_call in TAREAS:
        id_ejec = _intentar_reservar(tarea, periodo, host)

        if id_ejec is None:
            # Ya existe un slot — mirar su estado.
            slot = _estado_actual(tarea, periodo)
            if slot is None:
                # Race condition rarísima: el INSERT falló el CONFLICT pero
                # la fila desapareció antes del SELECT. Tratar como error suave.
                resultados.append((tarea, "E", "slot desapareció"))
                hubo_error = True
                continue

            if slot["estado"] == "O" and not force:
                resultados.append(
                    (tarea, "O", f"ya corrió ({slot['terminado_en']})")
                )
                continue

            if force:
                log.info("forzando re-ejecución de %s %s (estado previo=%s)",
                         tarea, periodo, slot["estado"])
                id_ejec = _reset_slot(tarea, periodo, host)
            else:
                # estado R (colgado) o E (error previo) — no pisamos sin --force.
                resultados.append(
                    (tarea, slot["estado"],
                     f"quedó en estado '{slot['estado']}' — usar --force para reintentar")
                )
                hubo_error = True
                continue

        # Llegamos acá con un slot nuestro (id_ejec) en estado R.
        try:
            log.info("ejecutando %s para periodo %s", tarea, periodo)
            _ejecutar_tarea(tarea, sql_call, fecha)
            _marcar_ok(id_ejec)
            resultados.append((tarea, "O", "ok"))
        except Exception as exc:
            detalle = f"{type(exc).__name__}: {exc}"
            log.exception("falló %s", tarea)
            # Intentar marcar el error — si falla también, seguimos igual.
            try:
                _marcar_error(id_ejec, detalle)
            except Exception:
                log.exception("no se pudo marcar el error en ejecuciones_tareas")
            resultados.append((tarea, "E", detalle))
            hubo_error = True

            # Si la procedure no existe, avisamos distinto al scheduler.
            lower = detalle.lower()
            if "does not exist" in lower or "no existe" in lower:
                return EXIT_MISSING_PROC, resultados

    return (EXIT_ERROR if hubo_error else EXIT_OK), resultados


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Corre procesa_provisiones + actualizar_amortizacion del mes."
    )
    p.add_argument(
        "--periodo",
        help="Periodo YYYY-MM a procesar (default: mes actual).",
    )
    p.add_argument(
        "--fecha",
        help="Fecha exacta YYYY-MM-DD a pasar a procesa_provisiones (default: hoy).",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="Re-ejecutar aunque haya una corrida previa ok/error en este periodo.",
    )
    p.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Log DEBUG.",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv or sys.argv[1:])

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-5s  %(name)s: %(message)s",
    )

    hoy = date.today()
    if args.fecha:
        fecha = datetime.strptime(args.fecha, "%Y-%m-%d").date()
    else:
        fecha = hoy

    if args.periodo:
        # Validar forma YYYY-MM.
        datetime.strptime(args.periodo, "%Y-%m")
        periodo = args.periodo
    else:
        periodo = _periodo_de(fecha)

    log.info("periodo=%s  fecha=%s  host=%s  force=%s",
             periodo, fecha.isoformat(), _host(), args.force)

    db.init_pool()

    exit_code, resultados = correr(periodo=periodo, fecha=fecha, force=args.force)

    # ── Tareas DIARIAS del cierre (fuera de `correr`, que se trackea por mes).
    #    Corren en CADA invocación del cron, son idempotentes, y NO afectan el
    #    exit code de provisiones. Independizan a PC del dBase en fin de mes:
    #    rollover (crea la fila de INICIALES del mes nuevo = cierre anterior) +
    #    write-back del stock de cierre + foto diaria del balance. ──
    try:
        from modules.informes.queries import (
            crear_snapshot_diario,
            rollover_y_writeback_iniciales,
        )
        _roll = rollover_y_writeback_iniciales()
        log.info("rollover/writeback iniciales -> %s", _roll)
        _snap = crear_snapshot_diario()
        log.info(
            "foto diaria -> aplicado=%s patrimonio=%s usuti=%s",
            _snap.get("aplicado"), _snap.get("patrimonio"), _snap.get("usuti"),
        )
        if _roll.get("rollover"):
            print(f"  [OK]  ROLLOVER: creó INICIALES del mes (cierre {_roll.get('rollover_desde')})")
        print(f"  [OK]  foto diaria + write-back (patrimonio={_snap.get('patrimonio')})")
    except Exception as _exc:  # noqa: BLE001
        log.exception("tareas diarias (rollover/writeback/foto) fallaron")
        print(f"  [ER]  tareas diarias (rollover/foto): {type(_exc).__name__}: {_exc}")

    # ── Puente compras de químicos formulas_app → PC (TMT 2026-07-17,
    #    pedido dueña: "automático total"). Idempotente y fail-soft: carga
    #    solo las facturas del mes que faltan en scintela.compra; cada una
    #    genera su posdat banc=0 (pasivo). Apagable con
    #    FORMULAS_COMPRAS_AUTOSYNC=0. NO afecta el exit code. ──
    try:
        from modules._lib import formulas_db as _fdb
        _fdb.init_pool()  # idempotente; no-op si ya está
        from modules.compras import formulas_bridge as _fb
        _rep = _fb.sincronizar_mes_actual()
        if _rep.get("apagado"):
            print("  [--]  puente formulas: apagado por env")
        elif not _rep.get("disponible"):
            print("  [--]  puente formulas: bridge no disponible")
        else:
            _n, _e = len(_rep.get("creadas") or []), len(_rep.get("errores") or [])
            print(f"  [OK]  puente formulas: {_n} compras cargadas, "
                  f"{_rep.get('ya_cargadas', 0)} ya estaban, {_e} errores, "
                  f"{_rep.get('dejadas_para_manana', 0)} de hoy para mañana")
            for _c in (_rep.get("creadas") or []):
                print(f"        + {_c['proveedor']} {_c['factura']} {_c['importe']:.2f}")
            for _x in (_rep.get("errores") or []):
                print(f"        ! {_x.get('proveedor')} {_x.get('factura')}: {_x.get('error')}")
    except Exception as _exc:  # noqa: BLE001
        log.exception("puente formulas falló")
        print(f"  [ER]  puente formulas: {type(_exc).__name__}: {_exc}")

    # Resumen legible en stdout para el log del scheduler.
    print()
    print(f"  periodo: {periodo}")
    print(f"  fecha:   {fecha.isoformat()}")
    print(f"  host:    {_host()}")
    print(f"  force:   {args.force}")
    print()
    for tarea, estado, mensaje in resultados:
        icono = "OK" if estado == "O" else ("ER" if estado == "E" else estado)
        print(f"  [{icono}]  {tarea}: {mensaje}")
    print()
    print(f"  exit {exit_code}")

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
