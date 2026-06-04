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

    TMT 2026-05-20 v2 — antes solo calculaba 9 campos (cart, deuda,
    banco, gasto, retiro, kvent, uvent, kcom, ucom). El snapshot
    insertaba ESOS 9 y los otros 16 columnas de scintela.historia
    quedaban en NULL/0, lo que daba snapshots "mayo 2026" con
    ANTICIPOS=0, STOCK=0, MAQUINARIA=0 etc. (Federico #4).

    Ahora también calcula: anticipos, stock (vsto=MP+PROD), uqui
    (stock químicos), maquinaria, realty (terrenos+edificios),
    patrimonio, utilidad, retiros acumulados.

    Si `informe_balance()` falla, fallback a los queries simples
    originales (defensivo).
    """
    primer_dia = fecha_cierre.replace(day=1)

    def safe_one(sql: str, params=None) -> dict:
        try:
            return db.fetch_one(sql, params) or {}
        except Exception as e:
            log.warning("Query falló (campo queda en 0): %s — %s", sql.split()[1] if sql else "?", e)
            return {}

    # Fuente única: informe_balance() ya calcula TODOS los KPIs que el
    # balance muestra en pantalla. Lo usamos en lugar de duplicar la
    # lógica acá. Si falla por cualquier motivo, los campos avanzados
    # quedan en 0 (fallback al comportamiento legacy).
    try:
        from modules.informes import queries as _iq
        bal = _iq.informe_balance() or {}
    except Exception as e:  # noqa: BLE001
        log.warning("informe_balance falló — campos avanzados en 0: %s", e)
        bal = {}

    # Cartera viva al cierre — saldos > 0 con stat válido.
    # Bug #2 fix (2026-06-04): excluir usuario_crea='asinfo-backfill' —
    # las facturas backfilleadas de Asinfo son históricas ya contabilizadas
    # (mismo filtro que el resto de informes/queries.py).
    cart = safe_one(
        """
        SELECT COALESCE(SUM(saldo), 0) AS v
        FROM scintela.factura
        WHERE COALESCE(saldo, 0) > 0
          AND (stat IS NULL OR stat IN ('Z','A','',' '))
          AND COALESCE(usuario_crea, '') <> 'asinfo-backfill'
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

    # Ventas del mes — kg y USD.
    # Bug #2 fix (2026-06-04): excluir 'asinfo-backfill' (ver nota en cart).
    ventas = safe_one(
        """
        SELECT COALESCE(SUM(kg), 0)      AS kvent,
               COALESCE(SUM(importe), 0) AS uvent
        FROM scintela.factura
        WHERE fecha >= %s AND fecha <= %s
          AND (stat IS NULL OR stat IN ('Z','A','T','P','',' '))
          AND COALESCE(usuario_crea, '') <> 'asinfo-backfill'
        """,
        (primer_dia, fecha_cierre),
    )

    # Compras del mes — kg y USD.
    # Bug #2 fix (2026-06-04): excluir 'asinfo-backfill' (ver nota en cart).
    compras = safe_one(
        """
        SELECT COALESCE(SUM(kg), 0)      AS kcom,
               COALESCE(SUM(importe), 0) AS ucom
        FROM scintela.compra
        WHERE fecha >= %s AND fecha <= %s
          AND (stat IS NULL OR stat <> 'Y')
          AND COALESCE(usuario_crea, '') <> 'asinfo-backfill'
        """,
        (primer_dia, fecha_cierre),
    )

    # Si bal está completo, sobrescribimos los KPIs ya conocidos para
    # mantener UNA sola fuente de verdad (lo que /informes/balance
    # muestra ES lo que se snapshotea).
    cart_bal   = bal.get("totc", 0) + bal.get("totf", 0)
    deuda_bal  = bal.get("totp", 0)
    # Bug #1 fix (2026-06-04): incluir la CAJA en `banco`, igual que
    # balance_components_as_of (la columna `banco` del snapshot representa
    # "bancos + caja"). Sin esto, TOTAL ACTIVO del Histórico omitía la caja
    # y se rompía la identidad ACTIVO − PASIVO = PATRIMONIO por el monto de
    # la caja (visible a mitad de mes; al cierre la caja suele ser ≈ 0).
    banco_bal  = bal.get("salbanc", banco)
    if bal:
        banco_bal = float(bal.get("salbanc") or 0) + float(bal.get("salcaj") or 0)

    return {
        "fecha":     fecha_cierre,
        "cart":      float(cart_bal or cart),
        "deuda":     float(deuda_bal or deuda),
        "banco":     float(banco_bal or banco),
        "gasto":     float(gasto),
        "retiro":    float(retiro),
        "kvent":     float(ventas.get("kvent") or 0),
        "uvent":     float(ventas.get("uvent") or 0),
        "kcom":      float(compras.get("kcom") or 0),
        "ucom":      float(compras.get("ucom") or 0),
        # TMT 2026-05-20 v3 — campos que ANTES quedaban en 0 (Federico #4).
        # CUIDADO con stock vs ustock: en scintela.historia,
        #   stock  = KG de MP+Producto Terminado
        #   ustock = US$ del stock (lo que el template muestra como
        #            'STOCK MP+PROD' en miles de dólares).
        # bal.vsto es US$, así que lo mapeamos a ustock (no a stock).
        "anticipos":  float(bal.get("antic", 0) or 0),
        "ustock":     float(bal.get("vsto", 0) or 0),
        "uqui":       float(bal.get("vqx", 0) or 0),
        "maquinaria": float(bal.get("umaq", 0) or 0),
        "realty":     float(bal.get("uact", 0) or 0),
        # Federico 2026-05-22 — PATRIMONIO NETO de retiros. informe_balance()
        # arma totl con +URET (truco del dBase para que utilidad=PATR-PATANT
        # incluya los retiros del mes). El dBase lo corrige al cerrar —
        # INFORMES.PRG L1347: `REPLA PATRIMONIO WITH PATR-URET`. El snapshot
        # web se saltaba ese paso → el patrimonio guardado quedaba inflado
        # por el monto exacto de los retiros. Esto es justamente el
        # "Patrimonio neto" que muestra la pantalla Resultados (= patr-uret).
        "patrimonio": float(bal.get("patr", 0) or 0) - float(bal.get("uret", 0) or retiro),
        "usuti":      float(bal.get("utilidad", 0) or 0),
        "usret":      float(bal.get("uret", 0) or retiro),
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
    """Inserta un nuevo snapshot. Devuelve el id_historia generado.

    TMT 2026-05-20 v2 — agregadas columnas anticipos, stock, uqui,
    maquinaria, realty, patrimonio, usret, usuti. Sin estas el
    histórico mostraba 0,0 en mes corriente (Federico #4).
    """
    # TMT 2026-05-20 v3 — defaults + nombres de columnas correctos.
    # OJO: scintela.historia tiene 'stock' (kg) y 'ustock' (US$). El
    # template del histórico lee 'ustock'. calcular_kpis() popula
    # 'ustock' (no 'stock'). 'stock' se deja en NULL — el campo de kg
    # no se snapshota desde el balance.
    kpis_full = {
        "anticipos": 0, "ustock": 0, "uqui": 0,
        "maquinaria": 0, "realty": 0, "patrimonio": 0,
        "usret": 0, "usuti": 0,
        **kpis,
    }
    # Federico 2026-05-21 -- carry-forward de los campos de produccion
    # que vienen de TINT.BAT (ktej/ktin/utej/utin). calcular_kpis() NO
    # los computa; sin esto el snapshot nuevo los dejaba en NULL/0 y
    # rompia la TINTORERIA y el $/kg de /flujo-produccion. Copiamos los
    # valores del ultimo snapshot que si los tenga.
    _carry = db.fetch_one(
        """
        SELECT ktej, ktin, utej, utin
          FROM scintela.historia
         WHERE COALESCE(ktej, 0) <> 0 OR COALESCE(ktin, 0) <> 0
            OR COALESCE(utej, 0) <> 0 OR COALESCE(utin, 0) <> 0
         ORDER BY fecha_crea DESC NULLS LAST, id_historia DESC
         LIMIT 1
        """
    ) or {}
    for _campo in ("ktej", "ktin", "utej", "utin"):
        kpis_full[_campo] = _carry.get(_campo)
    row = db.execute_returning(
        """
        INSERT INTO scintela.historia
            (fecha, cart, deuda, banco, gasto, retiro,
             kvent, uvent, kcom, ucom,
             anticipos, ustock, uqui, maquinaria, realty,
             patrimonio, usret, usuti,
             ktej, ktin, utej, utin,
             usuario_crea)
        VALUES (%(fecha)s, %(cart)s, %(deuda)s, %(banco)s, %(gasto)s, %(retiro)s,
                %(kvent)s, %(uvent)s, %(kcom)s, %(ucom)s,
                %(anticipos)s, %(ustock)s, %(uqui)s, %(maquinaria)s, %(realty)s,
                %(patrimonio)s, %(usret)s, %(usuti)s,
                %(ktej)s, %(ktin)s, %(utej)s, %(utin)s,
                %(usuario)s)
        RETURNING id_historia
        """,
        {**kpis_full, "usuario": usuario[:50]},
    )
    return int(row["id_historia"]) if row else 0


def actualizar_snapshot(id_historia: int, kpis: dict, usuario: str = "snapshot_auto") -> int:
    """Sobreescribe un snapshot existente — sólo si el caller pasó --force."""
    kpis_full = {
        "anticipos": 0, "ustock": 0, "uqui": 0,
        "maquinaria": 0, "realty": 0, "patrimonio": 0,
        "usret": 0, "usuti": 0,
        **kpis,
    }
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
               anticipos = %(anticipos)s,
               ustock = %(ustock)s,
               uqui = %(uqui)s,
               maquinaria = %(maquinaria)s,
               realty = %(realty)s,
               patrimonio = %(patrimonio)s,
               usret = %(usret)s,
               usuti = %(usuti)s,
               fecha_modifica = CURRENT_TIMESTAMP,
               usuario_modifica = %(usuario)s
         WHERE id_historia = %(id_historia)s
        """,
        {**kpis_full, "id_historia": id_historia, "usuario": usuario[:50]},
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
