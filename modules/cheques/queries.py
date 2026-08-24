"""Consultas de cheques.

Vocabulario canónico (2026-04-29 — ver docs/SKILL_ADDENDUM_BATCH_18.md):

    Z = cartera (ingresado, no pasó nada)        -- estado inicial
    B = depositado en banco Pichincha            -- terminal feliz
    V = banco Internacional (LEGACY, no usar)
    1 = devuelto / rechazado #1                  -- sólo desde B
    2 = devuelto / rechazado #2 (alias de 1)     -- sólo desde B
    3 = segundo rechazo                          -- sólo desde 1
    D = Daniela (gestión de cobranza)            -- desde Z, no terminal
    P = postergado, nueva fecha                  -- sólo desde Z
    E = endosado a proveedor                     -- terminal: salió de nuestra cartera

Reglas de transición:
    - Alta: SIEMPRE Z.
    - Z → B: depositar (`depositar_lote`).
    - Z → P: postergar (`postergar`).
    - Z → D: pasar a Daniela (`marcar_daniela`).
    - Z/P/D → E: endosar a proveedor (`endosar`).
    - B → 1 / 2: rebote (`reversar` con stat origen B).
    - 1 → 3: segundo rebote (`reversar` con stat origen 1).
    - V está prohibido como destino. Históricos se respetan.

Migración 0013 remapea las filas legacy `stat='D'` (depositado genérico)
a `stat='B'`. Después de esa migración, 'D' es unambiguamente Daniela.
"""

import logging
from datetime import date, timedelta

import db
from filters import today_ec
from modules._lib import busqueda

# El saldo de una factura es importe − abono − retención, y esa cuenta vive en
# UN solo lugar (mig 0179, TMT 2026-08-07).
from modules.facturas import queries as _fact_q
from periodo_guard import asegurar_fecha_abierta

from . import concepto_cobro as _concepto_cobro
from . import nota_usuario as _nota_usuario

_LOG = logging.getLogger(__name__)

# Día de INGRESO de un cobro — la fecha con la que el dBase arma sus listados
# del día (`FECHING`). Es UNA sola definición porque hay DOS pantallas que la
# usan (el resumen de cobranza y el listado de cheques ingresados) y un
# "espeja a la otra" en el docstring no se mantiene solo: si divergen, dos
# informes del mismo día dan totales distintos y nadie se entera.
#
#   · `fecha_recibido` — lo cargado por las pantallas de PC (el alta hace
#     `fecha = fecha_recibido`, así que siempre está). PC NUNCA lo reescribe:
#     ni el depósito, ni el rebote, ni "volver a cartera" lo tocan.
#   · `fechaing` SOLO si la fila nació del dBase Y sigue siendo el FECHING que
#     trajo el import — ver abajo.
#   · `fecha` de último recurso, y SÓLO para filas que no nacieron del dBase.
#
# ⚠ TMT 2026-08-05 (Alex: "todo lo que dice CHEQUE y sin aplicar facturas… no
# ingresé ayer"). El resumen del 04/08 imprimía 58 cheques por $79.182,97
# cuando Alex había tipeado 12 por $5.017,16: los otros 46 ($74.165,81) eran
# cheques del dBase importados el 12/07 que él DEPOSITÓ el 04/08. Y salían
# todos "sin aplicar a facturas" porque el import nunca trajo las
# aplicaciones — o sea, la hoja que va a contabilidad decía que entraron
# $74.165 de cobranza sin destino que nadie había cobrado ese día.
#
# La causa: `fechaing` carga DOS significados a la vez y PC pisa uno con el
# otro. En el dBase FECHING es el día de INGRESO a cartera (`ALTAS.PRG` L30)
# y la salida es FECHOUT (`BANCOS.PRG` L1234); pero las rutas de depósito de
# PC (`depositar_lote`, la transición Z→B) escriben la fecha DE DEPÓSITO en
# `fechaing`. Depositar un cheque del dBase le borra el día en que entró y le
# escribe el de hoy → aparece como cobranza de hoy. Medido en producción:
# **459 cheques** con el FECHING pisado, ensuciando TODOS los días desde el
# 13/07 (30/07: 45 · 27/07: 50 · 04/08: 46 …).
#
# El discriminante es exacto y no necesita el DBF: un FECHING de verdad es
# SIEMPRE anterior o igual al momento en que el import creó la fila (no se
# puede haber recibido un cheque después de haberlo importado). Un valor
# pisado por un depósito es SIEMPRE posterior. Por eso la condición
# `c.fechaing <= c.fecha_crea`.
#
# Y cuando el FECHING está pisado o falta, la fila NO cae a `c.fecha`: en el
# dBase `fecha` es la fecha DEL CHEQUE (posdatada), así que el fallback no
# arreglaba el fantasma, lo MUDABA a otro día — es el mismo bug del 03/08 por
# la otra punta. Sin día de ingreso confiable, el cobro no es de ningún día y
# no se imprime en ninguna hoja.

#: 🚨 TMT 2026-08-09: *"el número de cheque que se muestra es del cheque real o
#: del programa?"* — y después: *"poné dep pich más que cheque #x"*. La mitad
#: de las filas de cobranza NO son cheques: NB 90/91 son depósito directo y NB
#: 99 es efectivo, y esos no tienen número. La pantalla igual los llamaba
#: "Cheque #102090" con el ID INTERNO, que se lee como si fuera el número
#: escrito en el papel. Medido: 1.116 sin número en DEP.PICH., 134 en EFECTIVO,
#: y 1.410 movimientos con el concepto "Cheque # de XXX", sin número.
#: ⭐ El nombre del banco ES el medio de cobro: DEP.PICH. → "Dep. Pich.".
_BANCO_SIN_NOMBRE = {"", "UKN"}      # 98 = legacy unknown: no dice nada


def _prolijo(nombre: str) -> str:
    """DEP.PICH. → Dep. Pich. · EFECTIVO → Efectivo."""
    txt = (nombre or "").strip().title().replace(".", ". ")
    return " ".join(txt.split()).strip()


def _nombre_banco(no_banco, conn=None) -> str:
    """Nombre del banco/medio (DEP.PICH., EFECTIVO, PICHINCHA…). '' si no está."""
    try:
        r = db.fetch_one(
            "SELECT COALESCE(nombre, '') AS nombre FROM scintela.banco "
            "WHERE no_banco = %s",
            (int(no_banco),), conn=conn)
    except Exception:  # noqa: BLE001 -- el alta no se cae por una etiqueta
        return ""
    return (r or {}).get("nombre") or ""


def etiqueta_cobro(row: dict | None) -> str:
    """Cómo se llama una fila de cobranza en pantalla.

    "Cheque 102345" cuando hay número escrito; si no, el MEDIO ("Dep. Pich.",
    "Efectivo", "Pichincha"). Vacío si no hay ni número ni banco con nombre —
    ahí el que llama decide (el historial cae al "#id").

    ⭐ UNA función para la etiqueta del historial y para el concepto que se
    graba al dar de alta: escribir dos veces la misma regla es cómo se llega a
    una fila que dice dos cosas distintas del mismo documento.
    """
    if not row:
        return ""
    no = str(row.get("no_cheque") or "").strip()
    if no and no != "0":
        return f"Cheque {no}"
    # 🚨 El 98 se llama "UKN" en el catálogo de bancos y no dice nada, pero
    # NO es un desconocido: es el espejo del saldo a favor del cliente (la
    # contrapartida negativa de un anticipo). Medido el 09/08: 175 filas, TODAS
    # con importe negativo. Antes salían como "Cheque #<id interno>".
    try:
        nb = int(row.get("no_banco") or 0)
    except (TypeError, ValueError):
        nb = 0
    if nb == 98:
        return "Saldo a favor"
    nombre = (row.get("banco_nombre") or "").strip().upper()
    if nombre in _BANCO_SIN_NOMBRE:
        return ""
    return _prolijo(nombre)


def etiqueta_cobro_fila(row: dict | None, conn=None) -> str:
    """`etiqueta_cobro` para una fila que NO trae el nombre del banco.

    🚨 TMT 2026-08-24. `etiqueta_cobro` es pura a propósito: lee
    `banco_nombre` de la fila y no toca la base, porque la llaman los listados
    que ya lo traen por JOIN. Pero `por_id` devuelve la columna
    `cheque.banco` (como `banco_texto`) y **esa columna está VACÍA en 1.386 de
    las 1.762 filas de depósito/efectivo**: el alta la escribe sólo si el
    caller la pasa. Confiar en ella dejaba la etiqueta en blanco y el que
    llamaba caía al "#id interno" — el mismo pecado que se fue a corregir.

    El nombre del medio vive en el catálogo `scintela.banco`, indexado por
    `no_banco`. Acá se resuelve por ahí y `banco_texto` queda de respaldo.
    """
    if not row:
        return ""
    nombre = (row.get("banco_nombre") or row.get("banco_texto") or "").strip()
    if not nombre:
        nombre = _nombre_banco(row.get("no_banco"), conn=conn)
    return etiqueta_cobro({**row, "banco_nombre": nombre})


# Qué cuenta como CHEQUE — la misma partición por medio que usa el resumen de
# cobranza (réplica de FINAL, ALTAS.PRG): NB 90/91 = depósito directo, NB 99 =
# efectivo, todo lo demás (banco emisor real, y el 98 = espejo de anticipo) es
# cheque. Compartida para que el listado de ingresados y el bucket CHEQUES del
# resumen NO puedan dar números distintos para el mismo día.
SQL_ES_CHEQUE = "COALESCE(c.no_banco, 0) NOT IN (90, 91, 99)"

#: Los MEDIOS de cobro del filtro de /cheques. La partición es la misma de
#: `SQL_ES_CHEQUE` (90/91 = depósito directo, 99 = efectivo) escrita como
#: condición por opción, para que el dropdown y el resumen del día no puedan
#: contestar cosas distintas. 🚨 El corte va por CÓDIGO enumerado y no por
#: `>= 90` a propósito: el 98 (espejo del saldo a favor) y el 97 (anticipo)
#: SON cobros con cheque detrás y tienen que seguir cayendo en "Cheques".
SQL_POR_MEDIO = {
    "cheques": SQL_ES_CHEQUE,
    "depositos": "COALESCE(c.no_banco, 0) IN (90, 91)",
    "efectivo": "COALESCE(c.no_banco, 0) = 99",
}

#: Filas que NUNCA van a tener un número de cheque escrito: no hay papel.
#: Se usa para no ofrecer el campo "N°" en el listado (ver `buscar`).
MEDIOS_SIN_NUMERO = (90, 91, 98, 99)

# Día en que el cheque SALIÓ de cartera (depósito, cobro en efectivo, endoso,
# anulación). UNA definición, importada — no copiada — igual que
# SQL_DIA_INGRESO. `fechaout` primero porque es la columna correcta en los dos
# orígenes: FECHOUT en el dBase (`BANCOS.PRG` L1234) y, desde el 05/08/2026,
# también lo que escribe PC al depositar.
#
# ⚠ El fallback a `fechaing` es para las ~1.200 filas depositadas por PC ANTES
# del 05/08/2026, cuando el depósito escribía la fecha ahí. No se migran: en
# esas filas `fechaing` ES la fecha de depósito, así que el COALESCE devuelve
# lo correcto sin tocar un solo dato. En las filas del dBase `fechaing` es
# FECHING (ingreso) — pero ahí `fechaout` está cargado y gana, que es
# justamente para lo que sirve el orden.
SQL_DIA_SALIDA = "COALESCE(c.fechaout, c.fechaing)"

SQL_DIA_INGRESO = """CASE
                       WHEN c.fecha_recibido IS NOT NULL
                            THEN c.fecha_recibido
                       WHEN COALESCE(c.usuario_crea, '')
                            IN ('dbf-import', 'reconcile-dbf')
                            THEN CASE
                                   WHEN c.fechaing
                                        <= COALESCE(c.fecha_crea, c.fechaing)
                                   THEN c.fechaing
                                 END
                       ELSE c.fecha
                     END"""


# scintela.cliente.observacion es varchar(200). Al trazar rebotes en la
# observacion del cliente hay que capar la longitud: con 3-4 rebotes acumulados
# (cada marca ~60 chars) se desborda. Cap del lado SQL con RIGHT(..., 200).
_OBS_CAP = 200

# TMT 2026-05-15: tolerancia para dar por "cerrada" una factura aunque
# queden centavos sin aplicar. Acordado con la dueña: hasta $50 de
# diferencia (positiva o negativa) se considera "olvidado" y la factura
# pasa a stat='T'. Por encima, queda en 'A' (parcialmente abonada) y se
# sigue viendo en cartera para futuras aplicaciones.
TOLERANCIA_CIERRE_USD = 50.0

# TMT 2026-07-30: "esto son monedas, no un sobrante". Arrancó en $1 (el mismo
# umbral con el que la cobranza no fabrica un espejo NB=98 por centavos) y la
# dueña lo subió a $5 el mismo día: *"lo que sobra, si es más de 5 dólares,
# ofrece dejarlo como anticipo (esto mueve de 1 a 5 dólares)"*. Por debajo se
# olvida sin preguntar; por encima se pregunta — ver `sobrante_a_anticipo` en
# `netear_cheques_con_anticipos`.
TOLERANCIA_CENTAVOS_USD = 5.00


# ⭐ Las familias NO se escriben acá: salen de `estados.py`, que es la tabla
# única (letra · qué significa · de qué lado está) y copia la estructura del
# dBase — CART='Z123PD' y ENBANC='BVWIJK', menos los estados muertos, que están
# declarados uno por uno con su motivo. TMT 2026-08-11 (dueña: "tiene que estar
# definida en un lugar... y tiene que imitar al dbase").
from .estados import (  # noqa: E402
    DESTINOS_DEPOSITO,  # a cuáles se puede depositar HOY
    EN_CAJA,
    LABEL_CORTO_ESTADO,  # cómo se llama cada estado en los menús
)
from .estados import EN_BANCO as STATS_DEPOSITADO  # noqa: E402
from .estados import EN_CARTERA as STATS_EN_CARTERA  # noqa: E402

# Stats terminales para EDITAR — es otra pregunta que "de qué lado está":
# son los que ya no admiten tocar ningún campo. '3' está en cartera y sin
# embargo no se edita.
STATS_TERMINALES_EDIT = ("X", "T", "R", "3", "E")


def _banco_real_para_deposito(virtual: int, conn=None) -> int:
    """Banco REAL destino de un deposito directo de cobranza.

    Paridad ALTAS.PRG L171: BASE = IIF(NB=90,'PICHINCHA','INTER'). Los
    codigos 90/91 del dropdown son "virtuales" (DEP.PICH / DEP.INTER); el
    movimiento bancario tiene que caer en el banco real. Lookup por nombre
    (excluyendo los virtuales >=90); fallback a los no_banco conocidos de
    la data 2026 (Pichincha=10, Internacional=32).
    """
    patron = "%PICHIN%" if virtual == 90 else "%INTER%"
    try:
        row = db.fetch_one(
            "SELECT no_banco FROM scintela.banco "
            "WHERE no_banco < 90 AND nombre ILIKE %s "
            "ORDER BY no_banco LIMIT 1",
            (patron,),
            conn=conn,
        )
        if row and row.get("no_banco") is not None:
            return int(row["no_banco"])
    except Exception:  # noqa: BLE001 — fallback duro abajo
        pass
    return 10 if virtual == 90 else 32


def _domingo_a_lunes(f: date) -> date:
    """Si fecha cae domingo (weekday=6 en Python, 1 en Clipper DOW), shift a lunes."""
    if f and f.weekday() == 6:
        from datetime import timedelta as _td

        return f + _td(days=1)
    return f


def _migrar_deposito_directo(*, ch: dict, nuevo_nb: int, banco_nombre: str, usuario: str) -> None:
    """Cambia el banco emisor de un cheque de depósito directo (90/91/99)
    MIGRANDO sus movimientos, en una sola transacción.

    TMT 2026-06-11 dueña: cheque cargado como 99 EFECTIVO que en realidad
    era DEP.PICH (o viceversa). No alcanza con cambiar la etiqueta: el alta
    generó side-effects (99 → entrada en caja; 90/91 → mov banco DE + link).

    Pasos:
      - viejo 99: compensa la entrada de caja con una salida 'S' (CORR).
      - viejo 90/91: compensa cada mov banco linkeado con 'ND' (CORR) y
        borra los links chequextransaccion.
      - nuevo 90/91: crea el mov 'DE' en el banco REAL + link + stat 'B'.
      - nuevo 99: crea la entrada de caja 'E' (CH.CLI) + stat 'C'.
      - actualiza no_banco/banco/stat del cheque + observación [E].
    """
    import bank_helpers
    import caja_helpers

    id_cheque = int(ch["id_cheque"])
    viejo_nb = int(ch.get("no_banco") or 0)
    cli = (ch.get("codigo_cli") or "").upper().strip()
    imp = float(ch.get("importe") or 0)
    fecha_mov = ch.get("fecha") or today_ec()
    doc = (ch.get("doc_banco") or "").strip()

    with db.tx() as conn:
        # ── compensar lo viejo ──────────────────────────────────────
        if viejo_nb == 99:
            caja_alta = db.fetch_one(
                "SELECT id_caja FROM scintela.caja "
                "WHERE id_cheque = %s AND tipo = 'E' AND ABS(importe - %s) < 0.01 "
                "ORDER BY id_caja LIMIT 1",
                (id_cheque, imp),
                conn=conn,
            )
            if caja_alta:
                caja_helpers.insert_movimiento_caja(
                    conn,
                    fecha=fecha_mov,
                    tipo="S",
                    importe=imp,
                    concepto=f"CORR ch{ch.get('no_cheque') or id_cheque} 99->{nuevo_nb}"[:80],
                    id_cheque=id_cheque,
                    usuario=usuario,
                )
        elif viejo_nb in (90, 91):
            movs = db.fetch_all(
                """
                SELECT DISTINCT tb.id_transaccion, tb.no_banco, tb.importe
                  FROM scintela.chequextransaccion cxt
                  JOIN scintela.transacciones_bancarias tb
                    ON tb.id_transaccion = cxt.id_transaccion
                 WHERE cxt.id_cheque = %s
                """,
                (id_cheque,),
                conn=conn,
            ) or []
            for m in movs:
                bank_helpers.insert_movimiento_bancario(
                    conn,
                    no_banco=int(m["no_banco"]),
                    no_cta=None,
                    fecha=fecha_mov,
                    documento="ND",
                    importe=abs(float(m.get("importe") or imp)),
                    concepto=f"CORR ch{ch.get('no_cheque') or id_cheque} {viejo_nb}->{nuevo_nb}"[:50],
                    prov=cli[:5] or None,
                    numreferencia=id_cheque,
                    usuario=usuario,
                )
            db.execute(
                "DELETE FROM scintela.chequextransaccion WHERE id_cheque = %s",
                (id_cheque,),
                conn=conn,
            )

        # ── crear lo nuevo ──────────────────────────────────────────
        stat_nuevo = "B"
        if nuevo_nb in (90, 91):
            banco_real = _banco_real_para_deposito(nuevo_nb, conn=conn)
            num_ref = (doc or str(id_cheque)).strip()
            mov = bank_helpers.insert_movimiento_bancario(
                conn,
                no_banco=banco_real,
                no_cta=None,
                fecha=fecha_mov,
                documento="DE",
                # TMT 2026-06-12 audit: faltaba importe= (mismo TypeError que
                # el hotfix 202bcbf, segundo lugar).
                importe=imp,
                concepto=f"1 ch.{cli}"[:50],
                prov=cli[:5] or None,
                # numreferencia es INTEGER en DB — doc no-numerico va NULL
                # (la referencia textual vive en cheque.doc_banco, regla #1
                # del matcher).
                numreferencia=int(num_ref) if num_ref.isdigit() else None,
                usuario=usuario,
            )
            if mov.get("id_transaccion"):
                db.execute(
                    """
                    INSERT INTO scintela.chequextransaccion
                        (id_cheque, id_transaccion, fecha, stat_ch, usuario_crea)
                    VALUES (%s, %s, %s, 'D', %s)
                    """,
                    (id_cheque, mov["id_transaccion"], fecha_mov, usuario),
                    conn=conn,
                )
        else:  # nuevo_nb == 99
            stat_nuevo = "C"
            caja_helpers.insert_movimiento_caja(
                conn,
                fecha=fecha_mov,
                tipo="E",
                importe=imp,
                concepto=f"CH.{cli}"[:80],
                id_cheque=id_cheque,
                usuario=usuario,
            )

        # ── actualizar el cheque ────────────────────────────────────
        db.execute(
            "UPDATE scintela.cheque "
            "SET no_banco=%s, banco=%s, stat=%s, "
            "    fechaing=%s, fechaout=%s, "
            "    observacion = COALESCE(observacion||' | ','')||%s, "
            "    usuario_modifica=%s, fecha_modifica=CURRENT_TIMESTAMP "
            "WHERE id_cheque=%s",
            (
                nuevo_nb,
                banco_nombre or None,
                stat_nuevo,
                fecha_mov if stat_nuevo == "B" else None,
                fecha_mov if stat_nuevo == "C" else None,
                f"[E] banco emisor {viejo_nb} -> {nuevo_nb} (movs migrados)",
                usuario,
                id_cheque,
            ),
            conn=conn,
        )


# ── Cambiar el MONTO de un cheque que YA está aplicado a facturas ───────────
# TMT 2026-08-24 (dueña): *"Cuando edito un cheque pero que ya fue aplicado a
# una factura, creo que no está funcionando bien. Cambié el monto"*. Era
# cierto: `editar()` cambiaba `cheque.importe` y NO tocaba ni `chequesxfact`
# ni `factura.abono/saldo/stat` (el comentario de 2026-05-27 lo decía como al
# pasar: *"chequesxfact y otras tablas relacionadas NO se ajustan
# automáticamente"*). El caso que ella pisó —cheque 4885 de IIA, id 102656,
# 1.200,00 aplicados enteros a la factura 001-099-000180286—: al bajarle el
# monto a 1.035,07 la factura seguía diciendo que le habían pagado 1.200,00.
# La cuenta del cliente perdía 164,93 sin que entrara ni saliera plata.
#
# Decisión de la dueña (24/08): que el programa AJUSTE SOLO la aplicación, y
# que ANTES muestre en pantalla qué va a hacer. De ahí las tres piezas:
#   1. `_plan_recorte`  — la cuenta pura, sin base de datos (testeable sola).
#   2. `plan_cambio_importe` — la misma cuenta con los datos de las facturas,
#      para la pantalla de confirmación.
#   3. `editar(..., ajustar_aplicaciones=True)` — la ejecuta, en la MISMA
#      transacción que el UPDATE del cheque.
#
# La regla del recorte: se recorta desde la ÚLTIMA aplicación hacia atrás. Si
# el cheque alcanzaba para tres facturas y ahora alcanza para dos, la que
# vuelve a quedar con saldo es la última que se pagó — no un prorrateo entre
# las tres, que dejaría a las tres a medio pagar.
# Si el monto SUBE no se toca ninguna factura: la diferencia queda como
# sobrante del cheque, para aplicar cuando ella quiera.


def aplicaciones_vivas(id_cheque: int, conn=None) -> list[dict]:
    """Las filas de `chequesxfact` del cheque, en orden de aplicación."""
    return (
        db.fetch_all(
            """
            SELECT cxf.id_chequexfact, cxf.id_fact, cxf.importe,
                   f.numf, f.numf_completo,
                   f.importe   AS fact_importe,
                   f.abono     AS fact_abono,
                   f.retencion AS fact_retencion,
                   f.saldo     AS fact_saldo,
                   f.stat      AS fact_stat
              FROM scintela.chequesxfact cxf
              LEFT JOIN scintela.factura f ON f.id_factura = cxf.id_fact
             WHERE cxf.id_cheque = %s
             ORDER BY cxf.fechaing, cxf.id_chequexfact
            """,
            (id_cheque,),
            conn=conn,
        )
        or []
    )


def _sin_cero_negativo(x: float) -> float:
    """`-0.0` es cero, y en pantalla "−0,00" se lee como un error.

    Sale de `round(4037.54 - 3967.32 - 70.22, 2)` — una factura cancelada al
    centavo. TMT 2026-08-24, visto en producción.
    """
    return 0.0 if abs(x) < 0.005 else x


def espejos_vivos(id_cheque: int, conn=None) -> list[dict]:
    """Los espejos de anticipo (NB=98, negativos) que cuelgan de este cheque.

    Son la contrapartida del sobrante: la parte del cheque que no fue a
    ninguna factura y quedó como saldo a favor del cliente. Los terminales
    (X/T/R) no cuentan — ya no netean nada.
    """
    return (
        db.fetch_all(
            """
            SELECT id_cheque, no_cheque, importe, stat
              FROM scintela.cheque
             WHERE id_cheque_padre = %s
               AND no_banco = 98
               AND UPPER(TRIM(COALESCE(stat, ''))) NOT IN ('X', 'T', 'R')
             ORDER BY id_cheque
            """,
            (id_cheque,),
            conn=conn,
        )
        or []
    )


def _plan_recorte(importe_nuevo, aplicaciones: list[dict]) -> list[dict]:
    """Cuánto hay que recortarle a cada aplicación para que entre en el monto nuevo.

    `aplicaciones` viene en ORDEN DE APLICACIÓN (la más vieja primero) y se
    recorta desde el final. Devuelve una fila por aplicación TOCADA.

    Trabaja en valor absoluto y le devuelve el signo del original: un cheque
    puede ser negativo (nota de crédito) y ahí "recortar" es subir hacia cero.
    Función PURA — no toca la base, se testea sola.
    """
    from decimal import Decimal as _D

    total = sum(_D(str(a.get("importe") or 0)) for a in aplicaciones)
    if total == 0:
        return []
    signo = _D(1) if total > 0 else _D(-1)
    falta = abs(total) - abs(_D(str(importe_nuevo)))
    if falta <= _D("0.005"):
        return []
    recortes: list[dict] = []
    for a in reversed(aplicaciones):
        if falta <= _D("0.005"):
            break
        ap = abs(_D(str(a.get("importe") or 0)))
        recorte = ap if ap < falta else falta
        queda = ap - recorte
        recortes.append(
            {
                "id_chequexfact": a.get("id_chequexfact"),
                "id_fact": a.get("id_fact"),
                "aplicado_antes": signo * ap,
                "aplicado_despues": signo * queda,
                "recorte": signo * recorte,
                "se_borra": queda < _D("0.005"),
            }
        )
        falta -= recorte
    return recortes


def plan_cambio_importe(id_cheque: int, importe_nuevo) -> dict | None:
    """Qué les pasa a las facturas si a este cheque le cambio el monto.

    Devuelve `None` si el cheque no tiene aplicaciones: ahí cambiarle el monto
    no le mueve nada a nadie y no hay nada que confirmar.
    """
    from decimal import Decimal as _D

    ch = db.fetch_one(
        "SELECT id_cheque, no_cheque, codigo_cli, importe, stat "
        "  FROM scintela.cheque WHERE id_cheque = %s",
        (id_cheque,),
    )
    if not ch:
        raise ValueError(f"Cheque {id_cheque} no existe.")
    aplic = aplicaciones_vivas(id_cheque)
    if not aplic:
        return None

    nuevo = _D(str(importe_nuevo))
    total_aplicado = sum(_D(str(a.get("importe") or 0)) for a in aplic)
    recortes = _plan_recorte(nuevo, aplic)

    # Una factura puede tener MÁS DE UNA aplicación del mismo cheque: los
    # recortes se suman POR FACTURA antes de recalcular su saldo.
    por_fact: dict = {}
    for r in recortes:
        por_fact[r["id_fact"]] = por_fact.get(r["id_fact"], _D(0)) + r["recorte"]

    filas: list[dict] = []
    vistas: set = set()
    for a in aplic:
        idf = a.get("id_fact")
        if idf in vistas:
            continue
        vistas.add(idf)
        recorte = por_fact.get(idf, _D(0))
        aplicado = sum(
            _D(str(x.get("importe") or 0)) for x in aplic if x.get("id_fact") == idf
        )
        abono_antes = float(a.get("fact_abono") or 0)
        abono_despues = _sin_cero_negativo(round(abono_antes - float(recorte), 2))
        # `round(-0.0, 2)` es -0.0 y la pantalla lo mostraba como "−0,00" en una
        # factura cancelada. Es cero: se muestra como cero.
        saldo_despues = _sin_cero_negativo(
            _fact_q.saldo_de(
                a.get("fact_importe"), abono_despues, a.get("fact_retencion")
            )
        )
        filas.append(
            {
                "id_fact": idf,
                "numf": a.get("numf"),
                "numf_completo": a.get("numf_completo") or a.get("numf"),
                "aplicado_antes": float(aplicado),
                "aplicado_despues": float(aplicado - recorte),
                "recorte": float(recorte),
                "abono_antes": abono_antes,
                "abono_despues": abono_despues,
                "saldo_antes": _sin_cero_negativo(float(a.get("fact_saldo") or 0)),
                "saldo_despues": saldo_despues,
                "stat_antes": (a.get("fact_stat") or "").strip().upper(),
                "stat_despues": _fact_q.stat_de(saldo_despues, abono_despues, tol=0.01),
                "toca": abs(float(recorte)) >= 0.005,
            }
        )

    total_recorte = sum(r["recorte"] for r in recortes) if recortes else _D(0)
    aplicado_despues = total_aplicado - total_recorte
    sobrante = nuevo - aplicado_despues
    # TMT 2026-08-24 (dueña): *"me debería quedar como anticipo la diferencia,
    # no sé dónde se van esos 164 de dif"*. El sobrante NO puede quedar
    # flotando adentro del cheque: engorda "Cheques a depositar" sin
    # contrapartida y la cuenta del cliente sube sin motivo. Va al mismo lugar
    # que el sobrante de la cobranza — el espejo NB=98 negativo.
    ya_en_espejos = sum(
        abs(_D(str(e.get("importe") or 0))) for e in espejos_vivos(id_cheque)
    )
    anticipo_nuevo = abs(sobrante) - ya_en_espejos if sobrante > 0 else _D(0)
    return {
        "id_cheque": id_cheque,
        "no_cheque": ch.get("no_cheque"),
        "codigo_cli": ch.get("codigo_cli"),
        "importe_antes": float(ch.get("importe") or 0),
        "importe_despues": float(nuevo),
        "total_aplicado": float(total_aplicado),
        "total_recorte": float(total_recorte),
        "aplicado_despues": float(aplicado_despues),
        "sobrante": float(sobrante),
        "anticipo_ya_hecho": float(ya_en_espejos),
        "anticipo_nuevo": float(anticipo_nuevo if anticipo_nuevo > _D("0.005") else 0),
        "facturas": filas,
        "n_facturas": len(filas),
        "toca_facturas": bool(recortes),
    }


def _ajustar_aplicaciones_al_importe(*, id_cheque, importe_nuevo, usuario, conn) -> dict:
    """Recorta las aplicaciones a facturas para que entren en el monto nuevo.

    Corre DENTRO de la transacción del `editar()` que cambió el importe: o
    quedan las dos cosas o no queda ninguna.
    """
    from decimal import Decimal as _D

    import mov_doble as _md

    aplic = aplicaciones_vivas(id_cheque, conn=conn)
    recortes = _plan_recorte(importe_nuevo, aplic)
    salida = {"facturas_tocadas": 0, "total_recortado": 0.0, "anticipo": 0.0,
              "id_cheque_anticipo": None}
    if not recortes:
        return {**salida, **_anticipar_sobrante(
            id_cheque=id_cheque, importe_nuevo=importe_nuevo,
            aplicaciones=aplic, usuario=usuario, conn=conn)}

    por_fact: dict = {}
    for r in recortes:
        if r["se_borra"]:
            db.execute(
                "DELETE FROM scintela.chequesxfact WHERE id_chequexfact = %s",
                (r["id_chequexfact"],),
                conn=conn,
            )
        else:
            db.execute(
                "UPDATE scintela.chequesxfact SET importe = %s, "
                "usuario_modifica = %s, fecha_modifica = CURRENT_TIMESTAMP "
                "WHERE id_chequexfact = %s",
                (r["aplicado_despues"], usuario, r["id_chequexfact"]),
                conn=conn,
            )
        por_fact[r["id_fact"]] = por_fact.get(r["id_fact"], _D(0)) + r["recorte"]

    total_recortado = _D(0)
    tocadas = 0
    for idf, recorte in por_fact.items():
        if abs(recorte) < _D("0.005"):
            continue
        f = db.fetch_one(
            "SELECT id_factura, numf, importe, abono, retencion "
            "  FROM scintela.factura WHERE id_factura = %s",
            (idf,),
            conn=conn,
        )
        if not f:
            continue
        total_recortado += recorte
        tocadas += 1
        nuevo_abono = round(float(f.get("abono") or 0) - float(recorte), 2)
        nuevo_saldo = _fact_q.saldo_de(f.get("importe"), nuevo_abono, f.get("retencion"))
        nuevo_stat = _fact_q.stat_de(nuevo_saldo, nuevo_abono, tol=0.01)
        db.execute(
            "UPDATE scintela.factura "
            "SET abono=%s, saldo=%s, stat=%s, usuario_modifica=%s "
            "WHERE id_factura=%s",
            (nuevo_abono, nuevo_saldo, nuevo_stat, usuario, idf),
            conn=conn,
        )
        # El mov original se marca 'reversado' SÓLO si la aplicación
        # desapareció entera; si quedó recortada, el reverso parcial se suma
        # al original y la cuenta cierra igual (mismo criterio que usa
        # `desaplicar_factura`, que siempre borra la fila entera).
        queda = db.fetch_one(
            "SELECT 1 AS x FROM scintela.chequesxfact "
            " WHERE id_cheque = %s AND id_fact = %s LIMIT 1",
            (id_cheque, idf),
            conn=conn,
        )
        md_orig = db.fetch_one(
            """
            SELECT id_mov_doble FROM scintela.mov_doble
             WHERE origen_table  = 'cheque'
               AND origen_id     = %s
               AND destino_table = 'factura'
               AND destino_id    = %s
               AND tipo          = 'cheque_aplicado_a_factura'
               AND estado        = 'activo'
             ORDER BY id_mov_doble DESC LIMIT 1
            """,
            (id_cheque, idf),
            conn=conn,
        )
        _md.registrar(
            conn=conn,
            tipo="reverso_cheque_aplicacion",
            origen_table="cheque",
            origen_id=id_cheque,
            destino_table="factura",
            destino_id=idf,
            importe=float(recorte),
            fecha=today_ec(),
            concepto=(
                f"AJUSTE por cambio de monto del cheque #{id_cheque} — "
                f"factura #{f.get('numf') or idf}"
            )[:200],
            usuario=usuario,
            metadata={
                "id_cheque": id_cheque,
                "id_factura": idf,
                "numf": f.get("numf"),
                "importe_recortado": float(recorte),
                "importe_nuevo_cheque": float(_D(str(importe_nuevo))),
                "saldo_factura_post": nuevo_saldo,
                "stat_factura_post": nuevo_stat,
            },
            id_original=(md_orig["id_mov_doble"] if (md_orig and not queda) else None),
        )

    # Si se recortó, el cheque quedó justo: sobrante 0 y nada que anticipar.
    # Igual pasa por acá para que la regla viva en UN solo lugar.
    return {
        "facturas_tocadas": tocadas,
        "total_recortado": float(total_recortado),
        **_anticipar_sobrante(
            id_cheque=id_cheque, importe_nuevo=importe_nuevo,
            aplicaciones=aplicaciones_vivas(id_cheque, conn=conn),
            usuario=usuario, conn=conn),
    }


def _anticipar_sobrante(*, id_cheque, importe_nuevo, aplicaciones, usuario, conn) -> dict:
    """Lo que el cheque no aplicó a ninguna factura va a ANTICIPO del cliente.

    TMT 2026-08-24 (dueña): *"quise cambiar ese cheque de 1200 a 1364,93. El
    problema es que me debería quedar como anticipo la diferencia... no sé
    dónde se van esos 164 de dif"*. Y no iban a ningún lado: el sobrante
    quedaba flotando adentro del cheque, engordando "Cheques a depositar" sin
    contrapartida. Ahora sale por la MISMA puerta que el sobrante de la
    cobranza — el cheque espejo NB=98 negativo (`crear_espejo_anticipo`).

    Sólo crea el espejo por lo que FALTA: si el cheque ya tenía uno (porque el
    sobrante venía de la carga original), se le suma la diferencia, no se
    duplica el anticipo entero.
    """
    from decimal import Decimal as _D

    aplicado = sum(_D(str(a.get("importe") or 0)) for a in aplicaciones)
    sobrante = _D(str(importe_nuevo)) - aplicado
    if sobrante <= _D("0.005"):
        return {"anticipo": 0.0, "id_cheque_anticipo": None}
    ya = sum(abs(_D(str(e.get("importe") or 0))) for e in espejos_vivos(id_cheque, conn=conn))
    falta = sobrante - ya
    if falta <= _D("0.005"):
        return {"anticipo": 0.0, "id_cheque_anticipo": None}

    ch = db.fetch_one(
        "SELECT no_cheque, codigo_cli, fecha, fechad, fecha_recibido, prov, clave "
        "  FROM scintela.cheque WHERE id_cheque = %s",
        (id_cheque,),
        conn=conn,
    ) or {}
    espejo = crear_espejo_anticipo(
        conn=conn,
        id_cheque_padre=id_cheque,
        no_cheque=ch.get("no_cheque") or "",
        fecha=ch.get("fecha") or today_ec(),
        fechad=ch.get("fechad"),
        fecha_recibido=ch.get("fecha_recibido"),
        codigo_cli=(ch.get("codigo_cli") or ""),
        importe_espejo=float(falta),
        prov=ch.get("prov"),
        clave=ch.get("clave"),
        usuario=usuario,
    )
    return {"anticipo": float(falta), "id_cheque_anticipo": espejo.get("id_cheque")}


def editar(
    id_cheque: int,
    *,
    concepto: str | None = None,
    observacion: str | None = None,
    nota_usuario: str | None = None,
    fechad: date | None = None,
    importe: float | None = None,
    no_cheque: str | None = None,
    doc_banco: str | None = None,
    no_banco: int | None = None,
    ajustar_aplicaciones: bool = False,
    usuario: str = "web",
) -> dict:
    """Edición *blanda* de un cheque.

    Decisión 2026-04-30 (addendum batch 22 §8): el dueño eligió el flujo
    "anular + reemitir" para corregir importe/cliente/banco. Esta función
    sólo permite tocar campos blandos:

      - `concepto`: prov/concepto del cheque (texto libre).
      - `observacion`: append-only con tag `[E]`.
      - `fechad`: SOLO si stat ∈ {Z, P, D} (todavía en cartera). Si la nueva
        fechad cae domingo, se shifta a lunes (paridad ALTAS.PRG L119).
      - `no_cheque` (TMT 2026-05-27 dueña): se cargó mal el número visible
        del cheque. Es solo texto identificatorio (no se usa para joins),
        max 10 chars. NO hay UNIQUE en DB — paridad con el alta original.
      - `doc_banco` (TMT 2026-05-27 dueña: 'no es lo mismo numero de documento
        que numero de cheque!! doc banco no es igual a cheque'). N° de
        comprobante/depósito/transferencia que da el banco; se propaga a
        numreferencia al depositar. varchar(40). Permitido vacío.

    Bloqueado siempre: codigo_cli, cuenta. Para corregir esos sigue siendo
    `anular_por_error_de_carga()` y crear uno nuevo (rompen integridad con
    chequesxfact y tx_bancarias).
      - `no_banco` (TMT 2026-06-11 dueña: 'dejame en cheques editar banco
        emisor'): editable SOLO si el cheque no tiene movimientos de
        banco/caja linkeados (sin chequextransaccion ni caja.id_cheque) —
        típico cheque en cartera cargado con el código equivocado. Si ya
        generó movimientos, anular+recargar.

    Bloqueado por stat:
      - stat ∈ {X, T, R, 3} → ValueError (terminales, no se editan).
      - stat ∈ {B, V, W, I, J, K, A} → fechad lockeado (sólo concepto/obs).

    Devuelve `{id_cheque, fechad_nueva, fechad_shifted_lunes}`.
    """
    asegurar_fecha_abierta(today_ec())

    # TMT 2026-05-26: la tabla scintela.cheque NO tiene columna `concepto`
    # (se confirmó contra prod). Antes el SELECT incluía `concepto` y rompía
    # con UndefinedColumn → 500 al editar. Si el usuario manda algo en el
    # campo concepto del form, lo guardamos como parte de la observación
    # con prefix [C], preservando la intención sin agregar una columna.
    ch = db.fetch_one(
        "SELECT id_cheque, no_cheque, stat, fechad, doc_banco, no_banco, "
        "codigo_cli, importe, fecha FROM scintela.cheque WHERE id_cheque = %s",
        (id_cheque,),
    )
    if not ch:
        raise ValueError(f"Cheque {id_cheque} no existe.")
    stat = (ch.get("stat") or "").upper()
    if stat in STATS_TERMINALES_EDIT:
        raise ValueError(
            f"Cheque en stat='{stat}' es terminal — no se edita. "
            "Para corregir, anular por error de carga y crear uno nuevo."
        )

    #: Monto nuevo que además tiene que arrastrar a las facturas aplicadas.
    ajuste_pendiente = None

    fechad_nueva = ch["fechad"]
    fechad_shifted = False
    if fechad is not None:
        # TMT 2026-05-27 dueña: 'dejame editar deposito de cheque'. Antes
        # cheques depositados (stat B/V/W/I/J/K/A) tenían fechad lockeada.
        # Permitido editar también cuando está depositado — necesario para
        # corregir la fecha de depósito y cuadrar con extracto banco.
        # (Las transiciones de stat siguen requiriendo flujo formal — esto
        # solo edita la FECHA del depósito ya hecho).
        fechad_lunes = _domingo_a_lunes(fechad)
        fechad_shifted = fechad_lunes != fechad
        fechad_nueva = fechad_lunes

    # Combinar concepto (si vino) + observación en un solo append a `observacion`.
    obs_partes: list[str] = []
    if concepto:
        obs_partes.append(f"[C] {concepto.strip()[:120]}")
    if observacion:
        obs_partes.append(f"[E] {observacion.strip()[:120]}")
    obs_marca = " · ".join(obs_partes) if obs_partes else None

    sql_set = ["fechad=%s", "usuario_modifica=%s", "fecha_modifica=CURRENT_TIMESTAMP"]
    params: list = [fechad_nueva, usuario]
    # TMT 2026-08-04: además de la traza `[C]` en observacion (que es una
    # bitácora append-only y no se puede leer como campo), el concepto ahora
    # va a su COLUMNA — es lo que imprime el resumen de cobranza del día. Así
    # un cobro ya cargado se puede explicar sin anularlo y recargarlo.
    _concepto_col = _concepto_cobro.limpiar(concepto)
    if _concepto_col:
        _concepto_cobro.bootstrap_columna()
        sql_set.append("concepto=%s")
        params.append(_concepto_col)
    # TMT 2026-08-06 (Alex): observación de texto libre EDITABLE (`nota_usuario`).
    # Va a columna propia — NO al append de `observacion`, que es una bitácora
    # de tags del sistema. Ver `modules/cheques/nota_usuario.py`. Si el form
    # manda "" (string vacío tras strip), se guarda como NULL (borrado).
    if nota_usuario is not None:
        _nota_usuario.bootstrap_columna()
        sql_set.append("nota_usuario=%s")
        params.append(_nota_usuario.limpiar(nota_usuario))
    if obs_marca:
        sql_set.append("observacion = COALESCE(observacion||' | ','')||%s")
        params.append(obs_marca)
    # TMT 2026-05-27 dueña: 'dejame editar valor de cheque!!'. Antes el
    # importe estaba lockeado y requería anular+reemitir. Permitido edit
    # directo.
    # TMT 2026-08-24 dueña: y desde hoy el monto nuevo ARRASTRA a las
    # facturas aplicadas (`ajustar_aplicaciones=True`, que pide la pantalla
    # de confirmación). Hasta ayer no las tocaba y la factura quedaba
    # diciendo que le habían pagado una plata que el cheque ya no valía.
    if importe is not None:
        from decimal import Decimal as _Dec
        # TMT 2026-08-03 (bug A): si el cheque está DENTRO de un depósito
        # bancario ('DE'), cambiarle el importe acá desincroniza el depósito —
        # el 'DE' sigue valiendo lo que el banco acreditó y el cheque pasa a
        # valer otra cosa. Después, "Volver a cartera" no puede distinguir ese
        # descuadre de un resto legítimo (el que dejan los cheques rebotados) y
        # termina inventando plata o borrándola. El importe se corrige con el
        # cheque FUERA del depósito; el camino por pantalla es "Volver a
        # cartera" (o desarmar el depósito en /bancos) y recién ahí editarlo.
        # ⚠️ El guard exige stat DEPOSITADO además del link: hay cheques que
        # conservan el link a un 'DE' viejo sin estar depositados (un rebote de
        # un depósito de UN cheque no desagrupa a propósito — ver
        # `transicionar_stat` rama '9' —, y `anular_por_error_de_carga` tampoco
        # borra el link). Esos no pueden ir por «Volver a cartera» (que exige
        # STATS_DEPOSITADO), así que bloquearlos los dejaría sin ninguna salida
        # por pantalla — y su 'DE' ya es historia compensada, editarles el
        # importe no descuadra nada.
        _dep = db.fetch_one(
            "SELECT tb.id_transaccion FROM scintela.chequextransaccion cxt "
            "  JOIN scintela.transacciones_bancarias tb "
            "    ON tb.id_transaccion = cxt.id_transaccion "
            " WHERE cxt.id_cheque = %s "
            "   AND UPPER(TRIM(COALESCE(tb.documento,''))) = 'DE' LIMIT 1",
            (id_cheque,),
        ) if (ch.get("stat") or "").upper() in STATS_DEPOSITADO else None
        if _dep:
            raise ValueError(
                f"Este cheque está dentro de un depósito bancario (mov "
                f"#{_dep['id_transaccion']}). Sacalo del depósito con «Volver a "
                "cartera» antes de cambiarle el importe, si no el depósito queda "
                "descuadrado contra el banco."
            )
        imp_dec = _Dec(str(importe))
        # TMT 2026-06-07: permitir NEGATIVO (notas de crédito/correcciones),
        # igual que crear. Solo bloqueamos el cero.
        if abs(imp_dec) < _Dec("0.005"):
            raise ValueError("El importe no puede ser cero.")
        # numeric(9,2) en DB — max 9_999_999.99. Validar para no tirar
        # NumericValueOutOfRange como 500.
        if imp_dec >= _Dec("10000000"):
            raise ValueError("Importe excede el máximo permitido (9.999.999,99).")
        # TMT 2026-08-24 (dueña): si el cheque YA está aplicado a facturas, el
        # monto nuevo tiene que arrastrar a la aplicación. Si no, la factura
        # queda diciendo que le pagaron una plata que el cheque ya no vale —
        # y la cuenta del cliente cambia sin que entre ni salga plata.
        _aplic = aplicaciones_vivas(id_cheque)
        if _aplic:
            _tot_ap = sum(_Dec(str(a.get("importe") or 0)) for a in _aplic)
            if _tot_ap != 0 and (_tot_ap > 0) != (imp_dec > 0):
                raise ValueError(
                    "Este cheque está aplicado a facturas y el monto nuevo "
                    "cambia de signo. Desaplicalo primero desde la ficha del "
                    "cheque y volvé a aplicarlo."
                )
            # Si el cheque ya tiene un anticipo por su sobrante y el monto
            # nuevo lo deja corto, achicar ese anticipo es cirugía aparte
            # (puede estar aplicado a otra factura). Se frena y se dice cómo.
            _ya = sum(
                abs(_Dec(str(e.get("importe") or 0)))
                for e in espejos_vivos(id_cheque)
            )
            if _ya > 0 and (imp_dec - _tot_ap) < _ya - _Dec("0.005"):
                raise ValueError(
                    "Este cheque ya tiene un anticipo por su sobrante. "
                    "Deshacé el anticipo desde la ficha del cheque antes de "
                    "bajarle el monto."
                )
            if not ajustar_aplicaciones:
                # Freno para cualquier camino que no pase por la pantalla de
                # confirmación: el ajuste se acepta mirando qué factura queda
                # con saldo, no a ciegas.
                raise ValueError(
                    "Este cheque ya está aplicado a facturas — el cambio de "
                    "monto se confirma desde la pantalla, que muestra qué "
                    "factura vuelve a quedar con saldo."
                )
            ajuste_pendiente = imp_dec
        sql_set.append("importe=%s")
        params.append(imp_dec)
    # TMT 2026-05-27 dueña: 'tambien se tiene que ver el numero de documento
    # y poder editar este'. Antes el no_cheque solo se podía cambiar via
    # anular+reemitir (era un overkill para "se cargó mal el número").
    # Permitido edit directo. Solo texto identificatorio, no usado en joins.
    # Validamos: max 10 chars (varchar(10)), no vacío si vino, distinto al
    # actual (evita escrituras inútiles).
    if no_cheque is not None:
        nc = (no_cheque or "").strip()
        if not nc:
            raise ValueError("N° de cheque no puede estar vacío.")
        if len(nc) > 10:
            raise ValueError(f"N° de cheque excede 10 caracteres ({len(nc)}).")
        actual_no = (ch.get("no_cheque") or "").strip()
        if nc != actual_no:
            sql_set.append("no_cheque=%s")
            params.append(nc)
    # TMT 2026-05-27 dueña: 'doc banco no es igual a cheque' — campo
    # separado para el N° de comprobante/depósito que da el banco.
    # varchar(40). Vacío = NULL en DB (la dueña puede dejarlo en blanco
    # si todavía no tiene el comprobante). El alta original ya lo permite
    # vacío así que el edit replica esa semántica.
    if doc_banco is not None:
        db_v = (doc_banco or "").strip()
        if len(db_v) > 40:
            raise ValueError(f"Doc. banco excede 40 caracteres ({len(db_v)}).")
        actual_db = (ch.get("doc_banco") or "").strip()
        if db_v != actual_db:
            sql_set.append("doc_banco=%s")
            params.append(db_v or None)  # vacío → NULL
    # TMT 2026-06-11 dueña: 'dejame en cheques editar banco emisor'.
    # Corrección del código de banco emisor cargado mal. Guard duro: si el
    # cheque ya generó movimientos (deposito → chequextransaccion, efectivo
    # → caja), cambiar el banco acá los dejaría desincronizados — para esos
    # el flujo sigue siendo anular por error de carga + recargar.
    if no_banco is not None and int(no_banco) != int(ch.get("no_banco") or 0):
        banco_row = db.fetch_one(
            "SELECT no_banco, COALESCE(nombre,'') AS nombre FROM scintela.banco WHERE no_banco = %s",
            (int(no_banco),),
        )
        if not banco_row:
            raise ValueError(f"Banco {no_banco} no existe.")
        viejo_nb = int(ch.get("no_banco") or 0)
        nuevo_nb = int(no_banco)
        tiene_mov = db.fetch_one(
            """
            SELECT 1 AS x FROM scintela.chequextransaccion WHERE id_cheque = %s
            UNION ALL
            SELECT 1 AS x FROM scintela.caja WHERE id_cheque = %s
            LIMIT 1
            """,
            (id_cheque, id_cheque),
        )
        if tiene_mov:
            # TMT 2026-06-11 dueña: 'este tendría que ser editable o no? era
            # un depósito' (cheque 99 EFECTIVO que en realidad fue DEP.PICH).
            # Entre códigos de depósito directo (90/91/99) SÍ se puede: la
            # migración compensa el movimiento viejo (caja S / banco ND) y
            # crea el nuevo (banco DE / caja E) en una sola tx. Para
            # cualquier otro caso con movimientos, sigue el flujo de anular.
            if (
                {viejo_nb, nuevo_nb} <= {90, 91, 99}
                and stat in ("B", "C")
                and float(ch.get("importe") or 0) > 0
            ):
                _migrar_deposito_directo(
                    ch=ch, nuevo_nb=nuevo_nb,
                    banco_nombre=(banco_row.get("nombre") or "")[:30],
                    usuario=usuario,
                )
                # El helper ya actualizó no_banco/banco/stat/obs del cheque.
            else:
                raise ValueError(
                    "Este cheque ya tiene movimientos de banco/caja linkeados — "
                    "el banco emisor no se puede cambiar acá. Usá 'Anular por "
                    "error de carga' y recargalo con el banco correcto."
                )
        else:
            sql_set.append("no_banco=%s")
            params.append(nuevo_nb)
            sql_set.append("banco=%s")
            params.append((banco_row.get("nombre") or "")[:30] or None)
            sql_set.append(
                "observacion = COALESCE(observacion||' | ','')||%s"
            )
            params.append(
                f"[E] banco emisor {viejo_nb or '—'} → {nuevo_nb}"
            )
    params.append(id_cheque)

    sql_update = f"UPDATE scintela.cheque SET {', '.join(sql_set)} WHERE id_cheque=%s"
    ajuste = None
    if ajuste_pendiente is not None:
        # El monto del cheque y el recorte de las facturas van en la MISMA
        # transacción: o quedan los dos, o no queda ninguno.
        with db.tx() as _conn:
            db.execute(sql_update, tuple(params), conn=_conn)
            ajuste = _ajustar_aplicaciones_al_importe(
                id_cheque=id_cheque,
                importe_nuevo=ajuste_pendiente,
                usuario=usuario,
                conn=_conn,
            )
    else:
        db.execute(sql_update, tuple(params))
    return {
        "id_cheque": id_cheque,
        "fechad_nueva": fechad_nueva,
        "fechad_shifted_lunes": fechad_shifted,
        "stat_actual": stat,
        "ajuste": ajuste,
    }


# Transiciones permitidas — origen → destino. Cada destino tiene una función
# que aplica los side-effects además del UPDATE del stat. Define la state
# machine completa de cheques (paridad MODIFICA.PRG + BANCOS.PRG).
#
# Codificación:
#   "C"           → cobrado en caja: side-effect = INSERT caja TIPO=E
#   "B"           → depositado (venía de cartera Z/P): INSERT tx_bancarias DOC=DE
#   "I"           → depositado en el otro banco (legacy Internacional): banco=2
#   "V"           → depositado (venía de un DEVUELTO 1/2/3): re-depósito de un
#                   cheque protestado. dBase (BANCOS.PRG DEPOBAN) usa esta letra
#                   para "protestado vuelto a depositar". NO es "internacional".
#   "9"           → rebotado: INSERT posdat banc=0 + cliente.stop=S
#   "X"           → anulado: sólo UPDATE
# NOTA TMT 2026-07-25 (dueña, alinear a dBase): 'V' = "re-depósito de devuelto"
# (Pichincha), es un estado VÁLIDO y usable — lo genera `depositar_lote` al
# depositar un cheque que estaba en 1/2/3. El comentario viejo lo trataba como
# "banco Internacional legacy no usar": era una etiqueta EQUIVOCADA.
# Estados SIN movimiento contable (sólo etiqueta): moverse entre los permitidos
# NO toca banco ni caja → consistente. PERO siguen valiendo reglas de negocio:
#   · Cartera (Z, P, D): "el cheque está en nuestras manos, sin resolver" →
#     intercambiables libremente entre sí.
#   · Devuelto (1→2→3): es una SECUENCIA. A "2" sólo se llega desde "1"; a "3"
#     sólo desde "2". No se puede saltar de cartera directo a "2"/"3"
#     (dueña 2026-07-11: "only 1 can go to 2, some rules still apply").
#   · Eliminado (X): se llega desde cualquiera; restaurar sólo a cartera.
STATS_NEUTROS = {"Z", "P", "D", "1", "2", "3", "X"}
_CARTERA = {"Z", "P", "D"}  # sin resolver — intercambiables

# TMT 2026-07-11 (dueña: "dejar pasar a cualquier estado, que la contabilidad
# quede consistente"). Regla general:
#   · Entrar a un estado CON movimiento (B/I depósito, C caja, 9 rebote) dispara
#     su efecto contable (lo hace transicionar_stat).
#   · Salir de un estado CON movimiento (B/I/A depositado) sólo por rebote (9) o
#     anulación (X), que compensan el banco — nunca por un cambio de etiqueta
#     pelado (dejaría el depósito colgado). Para volver a cartera se usa
#     "deshacer depósito".
# ⭐ TMT 2026-08-11 (dueña, sobre las 8 que el dBase dejaba y PC no): las
# aprobó todas. Cobrar EN EFECTIVO un devuelto (1/2/3→C), re-depositar un
# devuelto de 2ª o 3ª (2/3→V) y depositar marcando V desde cartera (Z/P/D→V).
# Las tres eran del dBase; volvieron con su motivo escrito en
# `estados.DIFERENCIAS_TRANSICIONES` (o sea: dejaron de ser una diferencia).
TRANSICIONES_VALIDAS = {
    # Cartera → dentro de cartera, marcar devuelto 1° (inicio de la secuencia),
    # eliminar, o entrar a estados con movimiento.
    "Z": {"P", "D", "1", "X"} | {"B", "C", "9", "I", "V"},
    "P": {"Z", "D", "1", "X"} | {"B", "C", "I", "V"},
    "D": {"Z", "P", "1", "X"} | {"B", "C", "I", "V"},
    # Devuelto 1°: escalar a 2°, volver a cartera, re-depositar (V), rebote, eliminar.
    "1": {"2", "Z", "P", "D", "V", "X"} | {"9", "C"},
    # TMT 2026-07-21 (dueña, casos CJE/NIF): un DEPOSITADO que el banco devolvió
    # DESPUÉS puede pasar a "1" (devuelto 1°) como cambio de etiqueta PLANO, con
    # nueva fecha de cobro. El ND del protesto NO se genera acá: llega por el
    # extracto/sync o se tipea en /bancos (como hace el dBase). El depósito
    # original queda en la historia del cheque. Para el rebote CON ND automático
    # sigue existiendo B→9. Desde "1" ya se puede volver a Z/P (cartera).
    # Devuelto 2°: escalar a 3°, volver a cartera, rebote, eliminar. (NO vuelve a 1°.)
    "2": {"3", "Z", "P", "D", "V", "X"} | {"9", "C"},
    # Devuelto 3° (segundo rechazo): volver a cartera para gestión, o eliminar.
    "3": {"Z", "P", "D", "V", "X"} | {"C"},
    # V = protestado vuelto a depositar (dueña 2026-06-30). Si el banco lo
    # protesta OTRA vez → vuelve a "1" (dueña 2026-07-20: "el cheque de CG3
    # necesito colocar en estado 1"). Cambio de etiqueta plano: la V nueva no
    # tiene mov de banco en la app (depósito/protesto reales llegan por el sync).
    "V": {"1"},
    # Eliminado: restaurar sólo a cartera (los movimientos ya se compensaron al anular).
    "X": {"Z", "P", "D"},
    # Estados CON movimiento: salida sólo por rebote/anulación (compensan banco).
    "B": {"9", "X", "1"},
    # I (INTERNACIONAL) se maneja igual que B — dueña 2026-08-11. Era el único
    # depósito al que no se le podía marcar el devuelto de una.
    "I": {"9", "X", "1"},
    "A": {"9", "X"},
}


# ── Comisión del banco por un cheque protestado ──────────────────────────────
# dBase MODIFICA.PRG L314-318: junto con la ND del cheque devuelto va un SEGUNDO
# renglón, "GS. cheq. <cliente>", por `GCR` = **2 en Pichincha / 5 en el resto**.
# Programa Core emitía sólo la ND, así que la comisión aparecía en el extracto y
# no en el libro: salía como diferencia de conciliación en cada protesto.
# TMT 2026-08-11 (dueña: "hacelo, no es un monto grande").
GS_PROTESTO_PICHINCHA = 2.0
GS_PROTESTO_OTRO_BANCO = 5.0
#: Con qué empieza el concepto del gasto. Es el discriminante para separarlo de
#: la ND del cheque: las dos son documento 'ND' y las dos llevan
#: `numreferencia = id_cheque`, así que contar "las ND del cheque" sin filtrar
#: por acá da 2 donde antes daba 1. Vive en el módulo —y no copiado en cada
#: test— porque ya rompió dos archivos de tests distintos. TMT 2026-08-11.
CONCEPTO_GS_PROTESTO = "GS. cheq."


def gs_protesto_de(nombre_banco: str | None) -> float:
    """Cuánto cobra el banco por protestar un cheque.

    Se decide por el NOMBRE, no por el número: los `no_banco` son de un
    catálogo legacy y no son estables (Pichincha es 10 en la data 2026, era 1
    en el dBase). Un número hardcodeado ya nos mandó un depósito a un banco
    inexistente (caso ch14778 BYG, 31/07).
    """
    return (
        GS_PROTESTO_PICHINCHA
        if "PICHINC" in (nombre_banco or "").upper()
        else GS_PROTESTO_OTRO_BANCO
    )


def _insertar_gs_protesto(
    conn,
    *,
    no_banco: int,
    codigo_cli: str | None,
    fecha: date,
    id_cheque: int,
    usuario: str = "web",
    registro: dict | None = None,
) -> float:
    """El renglón del gasto, al lado de la ND. Devuelve lo debitado.

    Va SIEMPRE pegado a una ND recién insertada (los dos caminos del protesto:
    el rebote real '9' y el cambio plano a 1/2/3), así que hereda la
    idempotencia de la ND — si la ND no se emitió porque ya estaba compensada,
    acá tampoco se llega.

    Dos cosas que el dBase hace y NO se replican, a propósito:

    · `STAT '*'`. En el dBase es una marca de control; en Programa Core '*' en
      `transacciones_bancarias` significa **conciliado** (lo escribe el matcher
      de /conciliacion y el sync de PICHINCH). Copiarlo daría por cruzado con el
      banco un movimiento que nadie matcheó. Queda en el default 'A'.

    · `FECHA WITH FD` (la fecha de depósito del cheque, o sea una fecha pasada).
      El gasto va con la MISMA fecha que su ND: insertar al medio deja el saldo
      running de todas las filas posteriores mal hasta correr
      `recompute_saldos_desde()`, y el banco cobra la comisión el día del
      protesto, no el día en que el cheque estaba fechado.
    """
    fila = db.fetch_one(
        "SELECT COALESCE(nombre,'') AS nombre FROM scintela.banco WHERE no_banco = %s",
        (int(no_banco),),
        conn=conn,
    )
    gasto = gs_protesto_de(fila.get("nombre") if fila else None)
    import bank_helpers

    _res = bank_helpers.insert_movimiento_bancario(
        conn,
        no_banco=int(no_banco),
        no_cta=None,
        fecha=fecha,
        documento="ND",
        importe=gasto,
        concepto=f"{CONCEPTO_GS_PROTESTO} {(codigo_cli or '').strip()}".strip()[:50],
        prov=codigo_cli,
        numreferencia=id_cheque,
        usuario=usuario,
    )
    if registro is not None:
        registro["id_gs"] = _res.get("id_transaccion")
        registro["importe_gs"] = gasto
    return gasto


def compensar_deposito_devuelto(
    conn,
    *,
    id_cheque: int,
    importe: float,
    codigo_cli: str | None,
    no_cheque: str | None,
    fecha: date,
    usuario: str = "web",
    registro: dict | None = None,
) -> float:
    """Descuenta del banco el importe de un cheque DEPOSITADO que fue devuelto.

    Cuando un cheque que estaba depositado (parte de un mov 'DE' — depósito
    individual o consolidado 'dep.N ch.') se marca devuelto/rebotado, vuelve a
    contar como CARTERA VIVA (stat 1/2/3, ver informes.queries línea "cheques
    son siempre cartera viva si stat IN (Z,1,2,3,P,D)"). Si NO se descuenta el
    depósito del banco, el importe queda contado DOBLE: en el banco (el DE sigue
    entero) y en cartera (el cheque devuelto). Este helper mete la nota de
    débito (ND) compensatoria y DESAGRUPA el cheque del depósito (borra el link
    chequextransaccion), dejando el mov 'DE' intacto — así el banco muestra el
    depósito completo y el débito por el cheque protestado, igual que el extracto.

    Idempotente y seguro:
      - Si el cheque NO está linkeado a ningún 'DE' vivo (nunca se depositó, o
        ya se desagrupó en un rebote anterior) → no hace nada. El link vivo ES
        el marcador atómico de "este depósito todavía no se compensó": la ND se
        crea y el link se borra en la MISMA transacción, así que un doble-click
        (2ª corrida) ya no encuentra link vivo y no duplica la ND.
      - Si YA existe una ND compensatoria POSTERIOR al depósito vivo (numref =
        id_cheque y su id_transaccion > el del 'DE' vivo) → no hace nada (evita
        doble compensación si se tipeó a mano en /bancos).

    OJO (bug 2026-07-26): antes bastaba con que existiera CUALQUIER ND para el
    cheque (numreferencia = id_cheque) para saltar la compensación. Eso rompía el
    2° rebote: cheque depositado→rebota (ND #1)→se RE-DEPOSITA (nuevo 'DE')→rebota
    otra vez, la ND #1 vieja hacía saltar la ND #2 y el 2° depósito quedaba
    contado doble en el banco. Ahora se compara contra el 'DE' vivo actual.

    Devuelve el importe compensado (0.0 si no hizo nada). Corre dentro de la
    transacción del caller (usa su `conn`).

    ⭐ `registro` (dict opcional) se llena con lo que ESTA corrida hizo: el id de
    la ND, el del gasto de protesto y los depósitos de los que se desagrupó. No
    cambia el valor de retorno (hay tres callers que lo usan como número), pero
    es lo que le permite a `deshacer_devuelto` revertir el protesto EXACTO en
    vez de salir a adivinar cuál ND era. TMT 2026-08-13: los seis protestos del
    12/08 se hicieron sin esto y hubo que reconocerlos por el concepto.
    """
    imp = float(importe or 0)
    # TMT 2026-08-24: `abs` — un cobro negativo (mudanza de plata entre
    # facturas, devolución) ya deja su 'DE' en negativo, así que reversarlo
    # tiene que descontarlo igual. Antes salía por acá sin compensar Y sin
    # desagrupar, dejando el cheque colgado del depósito.
    if abs(imp) <= 0.005:
        return 0.0
    # Links vivos a un depósito 'DE'. Si no hay, no hay nada que compensar
    # (nunca se depositó, o ya se compensó y se desagrupó) → idempotente.
    links = db.fetch_all(
        "SELECT DISTINCT tb.id_transaccion, tb.no_banco "
        "  FROM scintela.chequextransaccion cxt "
        "  JOIN scintela.transacciones_bancarias tb "
        "    ON tb.id_transaccion = cxt.id_transaccion "
        " WHERE cxt.id_cheque = %s "
        "   AND UPPER(TRIM(COALESCE(tb.documento,''))) = 'DE'",
        (id_cheque,),
        conn=conn,
    ) or []
    if not links:
        return 0.0
    # ¿Ya hay una ND POSTERIOR a este 'DE' vivo? (doble-click o ND manual). Se
    # compara por id_transaccion (serial) contra el depósito vivo más reciente:
    # una ND más nueva que el 'DE' ⇒ ese depósito ya se compensó. Una ND más
    # vieja (rebote anterior, ya re-depositado) NO bloquea el nuevo depósito.
    max_de = max(int(lk["id_transaccion"]) for lk in links)
    # TMT 2026-07-30 (dueña, caso CJE): el guard miraba SÓLO
    # `numreferencia = id_cheque`, y una ND tipeada a mano en /bancos nunca
    # lleva numreferencia (`crear_movimiento_simple` no la setea). Resultado:
    # la ND del protesto ya estaba cargada —y CONCILIADA contra el extracto—
    # y pasar el cheque a devuelto insertaba una SEGUNDA ND por el mismo
    # importe: Pichincha −3.384,32 por un solo cheque rebotado. Ahora también
    # cuenta como "ya compensado" una ND del MISMO cliente por el MISMO
    # importe posterior al depósito. Es angosto a propósito (banco + prov +
    # importe exacto + posterior al DE); si el importe no coincide, la ND se
    # crea igual.
    # El cliente puede venir en `prov` O en el concepto: la ND real del caso
    # CJE se cargó con prov='PROT' y concepto "ND ch. prot. CJE 1", así que
    # exigir prov=cliente no alcanzaba y la duplicaba igual.
    _cli = (codigo_cli or "").strip().upper()
    nd_post = db.fetch_one(
        "SELECT MAX(id_transaccion) AS m FROM scintela.transacciones_bancarias "
        " WHERE UPPER(TRIM(COALESCE(documento,''))) = 'ND' "
        "   AND ( numreferencia = %s "
        "         OR ( no_banco = %s "
        "              AND ABS(COALESCE(importe, 0) - %s) <= 0.01 "
        "              AND ( UPPER(TRIM(COALESCE(prov,''))) = %s "
        "                    OR UPPER(COALESCE(concepto,'')) LIKE %s ) ) )",
        (id_cheque, int(links[0]["no_banco"]), imp, _cli, f"%{_cli}%"),
        conn=conn,
    )
    if nd_post and nd_post["m"] is not None and int(nd_post["m"]) > max_de:
        # TMT 2026-08-03 (bug A): el depósito YA está compensado (la ND se
        # tipeó a mano en /bancos), así que no insertamos otra — pero hay que
        # DESAGRUPAR igual. Antes se salía dejando el link vivo, y el link vivo
        # significa "este cheque todavía está dentro de ese depósito": el cheque
        # quedaba en stat '1' (re-depositable) y al re-depositarlo terminaba
        # colgando de DOS 'DE' a la vez. Después, "Volver a cartera" resta su
        # importe a los dos → el banco perdía el importe del cheque una vez de
        # más. Desagrupar acá deja el mismo estado que el camino normal.
        for lk in links:
            db.execute(
                "DELETE FROM scintela.chequextransaccion "
                "WHERE id_cheque = %s AND id_transaccion = %s",
                (id_cheque, lk["id_transaccion"]),
                conn=conn,
            )
        if registro is not None:
            registro["links"] = [int(lk["id_transaccion"]) for lk in links]
            registro["nd_ya_existia"] = True
        return 0.0
    import bank_helpers

    banco_orig = int(links[0]["no_banco"])
    _res_nd = bank_helpers.insert_movimiento_bancario(
        conn,
        no_banco=banco_orig,
        no_cta=None,
        fecha=fecha,
        documento="ND",
        importe=imp,
        # El signo viaja con el importe: una 'ND' negativa SUBE el saldo, que
        # es justo lo que compensa un 'DE' negativo.
        permitir_signed=True,
        concepto=(
            f"ch.devuelto {no_cheque or id_cheque} {codigo_cli or ''}"
        ).strip()[:50],
        prov=codigo_cli,
        numreferencia=id_cheque,
        usuario=usuario,
    )
    if registro is not None:
        registro["id_nd"] = _res_nd.get("id_transaccion")
        registro["importe_nd"] = imp
        registro["no_banco"] = banco_orig
    # …y la comisión que el banco cobra por el protesto, como el dBase.
    # TMT 2026-08-24: sólo cuando había plata de verdad. Un cobro NEGATIVO no
    # es un cheque que rebota — no hay papel, no hay protesto y el banco no
    # cobra nada; cargarle la comisión sería inventar un gasto.
    if imp > 0:
        _insertar_gs_protesto(
            conn,
            no_banco=banco_orig,
            codigo_cli=codigo_cli,
            fecha=fecha,
            id_cheque=id_cheque,
            usuario=usuario,
            registro=registro,
        )
    # Desagrupar: borrar los links del cheque a su(s) depósito(s) 'DE'.
    for lk in links:
        db.execute(
            "DELETE FROM scintela.chequextransaccion "
            "WHERE id_cheque = %s AND id_transaccion = %s",
            (id_cheque, lk["id_transaccion"]),
            conn=conn,
        )
    if registro is not None:
        registro["links"] = [int(lk["id_transaccion"]) for lk in links]
    return imp


def cheques_devueltos_sin_nd() -> list[dict]:
    """Cheques DEVUELTOS (stat 1/2/3) que siguen linkeados a un depósito 'DE'
    vivo y NO tienen la nota de débito compensatoria → el banco los cuenta doble.

    Son los que quedaron mal por el bug del 21/07 (marcar devuelto un cheque
    depositado sin descontar el banco). `compensar_deposito_devuelto` los arregla
    de forma idempotente. Devuelve una fila por cheque con su importe y depósito.
    """
    return db.fetch_all(
        """
        SELECT c.id_cheque, c.no_cheque, c.stat, c.codigo_cli, c.importe,
               tb.id_transaccion, tb.no_banco,
               COALESCE(bk.nombre,'') AS banco_nombre,
               COALESCE(tb.concepto,'') AS deposito_concepto
          FROM scintela.cheque c
          JOIN scintela.chequextransaccion cxt ON cxt.id_cheque = c.id_cheque
          JOIN scintela.transacciones_bancarias tb
            ON tb.id_transaccion = cxt.id_transaccion
           AND UPPER(TRIM(COALESCE(tb.documento,''))) = 'DE'
          LEFT JOIN scintela.banco bk ON bk.no_banco = tb.no_banco
         WHERE c.stat IN ('1','2','3')
           AND NOT EXISTS (
                 SELECT 1 FROM scintela.transacciones_bancarias nd
                  WHERE nd.numreferencia = c.id_cheque
                    AND UPPER(TRIM(COALESCE(nd.documento,''))) = 'ND'
               )
         GROUP BY c.id_cheque, c.no_cheque, c.stat, c.codigo_cli, c.importe,
                  tb.id_transaccion, tb.no_banco, bk.nombre, tb.concepto
         ORDER BY c.importe DESC
        """
    ) or []


def _banco_operativo(needle: str, conn=None) -> int | None:
    """Devuelve el `no_banco` del banco cuyo nombre contiene `needle`.

    TMT 2026-07-31 (dueña): los números de banco NO son estables entre bases
    (el legacy usaba 1/2; la data 2026 usa Pichincha=10, Internacional=20).
    Cualquier side-effect que necesite "el banco Pichincha" tiene que
    resolverlo por NOMBRE, nunca hardcodear el número. Si no matchea nada
    devuelve None y el caller debe fallar con un mensaje claro — grabar en un
    banco inexistente deja el movimiento huérfano e invisible.
    """
    needle = (needle or "").upper()
    rows = db.fetch_all(
        "SELECT no_banco, COALESCE(nombre, '') AS nombre "
        "FROM scintela.banco ORDER BY no_banco",
        conn=conn,
    ) or []
    match = next((r for r in rows if needle in (r.get("nombre") or "").upper()), None)
    return int(match["no_banco"]) if match else None


#: Cómo se llama, en `mov_doble`, cada salto de la máquina de estados del
#: cheque. TMT 2026-08-07: *"hacé todas"*.
#:
#: 🚨 `transicionar_stat` mueve plata en el BANCO —deposita, compensa un
#: rebote, descuenta un devuelto— y no dejaba ninguna huella: esos movimientos
#: no salían en /historial, no se podían revertir con el ↺, y en la traza el
#: banco aparecía sin hecho que lo explicara. El depósito en LOTE sí la deja
#: (`cheque_depositado`), así que el mismo cheque depositado de dos maneras
#: distintas se contaba distinto.
TIPO_MD_TRANSICION = {
    "B": "cheque_depositado", "V": "cheque_depositado", "I": "cheque_depositado",
    "C": "cheque_efectivo_to_caja",
    "9": "cheque_rebotado",
    "1": "cheque_devuelto", "2": "cheque_devuelto", "3": "cheque_devuelto",
}

#: Cuando el salto no mueve plata (X, T, P, Z, D…) igual queda la huella del
#: cambio de estado: es lo que contesta "¿quién lo pasó a anulado y cuándo?".
TIPO_MD_TRANSICION_OTRO = "cheque_stat_cambio"


def transicionar_stat(
    id_cheque: int,
    *,
    stat_destino: str,
    no_banco: int | None = None,
    fecha: date | None = None,
    motivo: str = "",
    usuario: str = "web",
    nueva_fechad: date | None = None,
) -> dict:
    """Mueve un cheque de un stat a otro, aplicando los side-effects.

    Esta es la state machine completa de cheques (paridad
    MODIFICA.PRG + BANCOS.PRG). Cada transición tiene un side-effect fijo:

    | Destino | Side-effect en una sola tx                                    |
    |---------|---------------------------------------------------------------|
    | B/I/V   | INSERT tx_bancarias DOC='DE' con saldo running                |
    | C       | INSERT caja TIPO='E' con saldo running                        |
    | 9       | INSERT posdat (banc=0) + cliente.stop='S' (rebote real)       |
    | X       | sólo UPDATE — anulación administrativa                        |
    | P, D    | sólo UPDATE — postdat o Daniela                               |

    Para depositar un lote, usa `depositar_lote()` (más eficiente).

    Devuelve dict con `id_cheque, stat_previo, stat_nuevo, side_effect_id`.
    """
    fecha = fecha or today_ec()
    asegurar_fecha_abierta(fecha)

    stat_destino = (stat_destino or "").upper().strip()
    if not stat_destino:
        raise ValueError("stat_destino requerido.")
    # TMT 2026-07-25 (dueña): 'V' = "protestado vuelto a depositar" (re-depósito de
    # un cheque devuelto). Se ofrece desde el estado '1' y AHORA crea el movimiento
    # de banco (DE) igual que un depósito 'B' — ver la rama `stat_destino in
    # ("B","V","I")` abajo. Antes era etiqueta plana y esperaba el sync de
    # PICHINCH.DBF. Las 'V' históricas (banco Internacional legacy) se respetan.

    with db.tx() as conn:
        # TMT 2026-05-26: incluimos doc_banco — al depositar individual lo
        # propagamos a transaccion_bancaria.numreferencia para que el matcher
        # de conciliación lo use como Rule #1.
        ch = db.fetch_one(
            "SELECT id_cheque, no_cheque, stat, codigo_cli, importe, "
            "no_banco, banco, fechad, doc_banco "
            "FROM scintela.cheque WHERE id_cheque = %s",
            (id_cheque,),
            conn=conn,
        )
        if not ch:
            raise ValueError(f"Cheque {id_cheque} no existe.")
        stat_prev = (ch.get("stat") or "").upper()

        permitidos = TRANSICIONES_VALIDAS.get(stat_prev, set())
        if stat_destino not in permitidos:
            raise ValueError(
                f"Transición {stat_prev}→{stat_destino} no permitida. "
                f"Desde {stat_prev} sólo se puede ir a: {sorted(permitidos)}."
            )

        side_effect_id = None
        banco_destino = None
        importe = float(ch["importe"] or 0)
        # ⭐ Lo que este salto MUEVE, anotado mientras se hace: la ND del
        # protesto, su gasto, los depósitos de los que desagrupó y la fecha de
        # cobro que pisó. Va a la metadata del mov_doble y es lo que le permite
        # a `deshacer_devuelto` revertirlo EXACTO. Sin esto hay que reconocer la
        # ND por el texto del concepto, que es adivinar. TMT 2026-08-13.
        _comp_registro: dict = {}
        _fechad_previa = ch.get("fechad")
        _fechad_nueva = None

        # --- depositado: B (cartera→Pichincha), V (re-depósito de un devuelto→
        # Pichincha) o I (Internacional). TMT 2026-07-25 (dueña): 'V' ahora CREA
        # el movimiento de banco (DE) igual que 'B' — antes era etiqueta plana y
        # esperaba el sync de PICHINCH.DBF. dBase (BANCOS.PRG DEPOBAN) deposita
        # un devuelto igual que uno de cartera; solo cambia la letra. ---
        if stat_destino in DESTINOS_DEPOSITO:
            import bank_helpers

            # TMT 2026-07-31 (dueña, caso ch14778 BYG): el fallback era
            # `no_banco or (2 if I else 1)` — números HARDCODEADOS del legacy.
            # En la data 2026 Pichincha es no_banco=10 e Internacional 20, así
            # que un →V desde la lista (el form NO manda no_banco) grababa el
            # DE en un banco 1 INEXISTENTE: el movimiento quedaba huérfano,
            # invisible en /bancos y por lo tanto FUERA de la conciliación.
            # Ahora el banco se resuelve por NOMBRE (igual que hace la vista
            # para B/I) y, si no se puede, FALLA con un error claro en vez de
            # escribir en un banco fantasma.
            banco_destino = no_banco or _banco_operativo(
                "INTER" if stat_destino == "I" else "PICHINC", conn=conn
            )
            if not banco_destino:
                raise ValueError(
                    "No pude resolver el banco destino del depósito "
                    f"(stat {stat_destino}). Revisá los nombres en /bancos."
                )
            # TMT 2026-05-26 dueña: numreferencia = doc_banco si la dueña
            # cargó N° de comprobante; fallback id_cheque. Es la rule #1
            # del matcher de conciliación bancaria. Se lee del cheque row
            # (la dueña lo carga al ingresar el cheque o al depositar).
            num_ref = (ch.get("doc_banco") or "").strip() or str(id_cheque)
            # TMT 2026-08-14 (dueña, cheque 102251 de BED por −1.000): un
            # cheque de importe NEGATIVO —el espejo de una devolución o de un
            # saldo a favor— no se puede depositar. Depositar SUMA, y esto
            # resta: `insert_movimiento_bancario` lo rechazaba con "importe
            # debe ser positivo (abs)... el signo lo determina el documento
            # ('DE')" y el cheque quedaba trabado en cartera para siempre, sin
            # poder llegar nunca a la conciliación.
            #
            # Lo que el banco hace con una devolución es cargarla: es una NOTA
            # DE DÉBITO, no un depósito. Así que el signo lo lleva el
            # documento (ND resta, DE suma) y el importe va en magnitud, que
            # es la convención que pide bank_helpers. Del otro lado del
            # extracto esto se cruza con el "CHEQUE DEVUELTO" del banco.
            _imp = float(importe or 0)
            _es_debito = _imp < 0
            _doc = "ND" if _es_debito else "DE"
            _pref = "Devol. cheque" if _es_debito else "Dep cheque"
            res = bank_helpers.insert_movimiento_bancario(
                conn,
                no_banco=banco_destino,
                no_cta=None,
                fecha=fecha,
                documento=_doc,
                importe=abs(_imp),
                concepto=f"{_pref} {ch.get('no_cheque') or ''} {ch.get('codigo_cli') or ''}".strip(),
                prov=ch.get("codigo_cli"),
                numreferencia=num_ref,
                usuario=usuario,
            )
            side_effect_id = res["id_transaccion"]
            db.execute(
                # TMT 2026-08-05: la fecha de depósito va a `fechaout`, NO a
                # `fechaing`. Depositar es una SALIDA de cartera y las otras once
                # salidas (C, 9, X, E, T y sus deshacer) ya escriben `fechaout`;
                # el depósito era el único que no. En las filas del dBase
                # `fechaing` es FECHING = el día que el cheque ENTRÓ, así que
                # escribir ahí borraba ese dato y el cheque reaparecía como
                # cobranza del día del depósito (hoja del 04/08: 46 cheques
                # fantasma por $74.165,81).
                "UPDATE scintela.cheque "
                "SET stat=%s, fechaout=%s, no_banco=%s, "
                "    usuario_modifica=%s, fecha_modifica=CURRENT_TIMESTAMP "
                "WHERE id_cheque=%s",
                (stat_destino, fecha, banco_destino, usuario, id_cheque),
                conn=conn,
            )
            db.execute(
                """
                INSERT INTO scintela.chequextransaccion
                    (id_cheque, id_transaccion, fecha, stat_ch, usuario_crea)
                VALUES (%s, %s, %s, 'D', %s)
                """,
                (id_cheque, side_effect_id, fecha, usuario),
                conn=conn,
            )

        # --- cobrado en caja ---
        elif stat_destino == "C":
            import caja_helpers

            res = caja_helpers.insert_movimiento_caja(
                conn,
                fecha=fecha,
                tipo="E",
                importe=importe,
                concepto=f"Cobro cheque {ch.get('no_cheque') or ''} {ch.get('codigo_cli') or ''}".strip(),
                id_cheque=id_cheque,
                usuario=usuario,
            )
            side_effect_id = res["id_caja"]
            db.execute(
                "UPDATE scintela.cheque "
                "SET stat='C', fechaout=%s, "
                "    usuario_modifica=%s, fecha_modifica=CURRENT_TIMESTAMP "
                "WHERE id_cheque=%s",
                (fecha, usuario, id_cheque),
                conn=conn,
            )

        # --- rebotado por banco (rebote real) ---
        elif stat_destino == "9":
            # Si el cheque estaba depositado, compensar el depósito original
            # en el banco con ND (nota de débito) — el banco rechazó el cheque
            # y nos descuenta la plata. Antes esto NO compensaba banco y el
            # saldo bancario quedaba inflado por el importe del cheque rebotado.
            # TMT 2026-05-14.
            if stat_prev in STATS_DEPOSITADO:
                import bank_helpers

                banco_orig = ch.get("no_banco") or (1 if stat_prev == "B" else 2)
                bank_helpers.insert_movimiento_bancario(
                    conn,
                    no_banco=banco_orig,
                    no_cta=None,
                    fecha=fecha,
                    documento="ND",
                    importe=importe,
                    concepto=(
                        f"REBOTE ch{ch.get('no_cheque') or id_cheque} {ch.get('codigo_cli') or ''}"
                    ).strip()[:50],
                    prov=ch.get("codigo_cli"),
                    numreferencia=id_cheque,
                    usuario=usuario,
                )
                # …y la comisión del banco por el protesto (dBase MODIFICA.PRG
                # L314-318). El rebote real y el cambio plano a 1/2/3 son los
                # dos caminos del MISMO hecho, así que los dos la emiten.
                _insertar_gs_protesto(
                    conn,
                    no_banco=int(banco_orig),
                    codigo_cli=ch.get("codigo_cli"),
                    fecha=fecha,
                    id_cheque=id_cheque,
                    usuario=usuario,
                )

                # TMT 2026-06-29 (dueña: 'sacarlo del grupo'): si el cheque era
                # parte de un depósito CONSOLIDADO (dep.N ch. — varios cheques
                # al mismo mov DE), lo sacamos del grupo desvinculando SU link
                # chequextransaccion a ese mov. El mov consolidado y su saldo
                # quedan INTACTOS (el banco muestra el depósito completo; el
                # rebote ya se compensó con el ND de arriba, que matchea el
                # débito 'ch.prot.' del banco). En depósitos de 1 cheque NO se
                # desvincula (preserva la historia del depósito).
                try:
                    _shared = db.fetch_all(
                        """
                        SELECT cxt.id_transaccion
                          FROM scintela.chequextransaccion cxt
                          JOIN scintela.transacciones_bancarias tb
                            ON tb.id_transaccion = cxt.id_transaccion
                         WHERE cxt.id_cheque = %s
                           AND UPPER(COALESCE(tb.documento,'')) = 'DE'
                           AND (SELECT COUNT(*) FROM scintela.chequextransaccion c2
                                 WHERE c2.id_transaccion = cxt.id_transaccion) > 1
                        """,
                        (id_cheque,), conn=conn,
                    ) or []
                    for _sh in _shared:
                        db.execute(
                            "DELETE FROM scintela.chequextransaccion "
                            "WHERE id_cheque = %s AND id_transaccion = %s",
                            (id_cheque, _sh["id_transaccion"]), conn=conn,
                        )
                except Exception as _e_desagr:
                    # No abortar el rebote por la desagrupación (best-effort).
                    pass

            # INSERT posdat banc=0 (cheque protestado) + stop al cliente.
            db.execute(
                """
                INSERT INTO scintela.posdat
                    (fecha, fechad, prov, num, importe, concepto, banc, usuario_crea)
                VALUES (%s, %s, %s, %s, %s, %s, 0, %s)
                """,
                (
                    fecha,
                    fecha,
                    ch.get("codigo_cli"),
                    id_cheque,
                    importe,
                    f"ch.prot.{ch.get('no_cheque') or ''}".strip()[:50],
                    usuario,
                ),
                conn=conn,
            )
            db.execute(
                "UPDATE scintela.cheque "
                "SET stat='9', fechaout=%s, "
                "    usuario_modifica=%s, fecha_modifica=CURRENT_TIMESTAMP "
                "WHERE id_cheque=%s",
                (fecha, usuario, id_cheque),
                conn=conn,
            )
            if ch.get("codigo_cli"):
                marca = (
                    f"[S] CHEQUE {ch.get('no_cheque') or '#' + str(id_cheque)} REBOTADO {fecha.isoformat()}"
                )
                if motivo:
                    marca += f" — {motivo[:60]}"
                db.execute(
                    "UPDATE scintela.cliente "
                    "SET stop='S', "
                    "    observacion = RIGHT("
                    "        COALESCE(observacion || ' | ', '') || %s, %s), "
                    "    usuario_modifica=%s "
                    "WHERE codigo_cli=%s AND COALESCE(stop,'N') != 'S'",
                    (marca, _OBS_CAP, usuario, ch["codigo_cli"]),
                    conn=conn,
                )

        # --- anulado, postdat, daniela: sólo UPDATE ---
        else:
            # TMT 2026-07-23 (dueña): si se marca DEVUELTO (1/2/3) un cheque que
            # estaba depositado, descontar el importe del banco con una nota de
            # débito (sino queda contado doble: banco + cartera viva). Idempotente
            # (no hace nada si no hay depósito 'DE' vivo o ya se compensó).
            if stat_destino in ("1", "2", "3"):
                compensar_deposito_devuelto(
                    conn,
                    id_cheque=id_cheque,
                    importe=importe,
                    codigo_cli=ch.get("codigo_cli"),
                    no_cheque=ch.get("no_cheque"),
                    fecha=fecha,
                    usuario=usuario,
                    registro=_comp_registro,
                )
            # TMT 2026-07-20 (dueña): al pasar a 1 (protestado) se pregunta la
            # NUEVA fecha de cobro (hoy o futura). Se guarda en fechad (columna
            # POSTERGADA) preservando fechad_original (F.DEP = la original),
            # igual que postergar().
            set_fecha = ""
            params: list = [stat_destino]
            if stat_destino == "1" and nueva_fechad:
                if nueva_fechad < today_ec():
                    raise ValueError(
                        "La nueva fecha del protestado debe ser hoy o futura."
                    )
                set_fecha = (
                    ", fechad=%s, fecha_postergacion=CURRENT_DATE, "
                    "fechad_original=COALESCE(fechad_original, fechad)"
                )
                params.append(nueva_fechad)
                _fechad_nueva = nueva_fechad
            params += [usuario, id_cheque]
            db.execute(
                "UPDATE scintela.cheque "
                "SET stat=%s" + set_fecha + ", "
                "    usuario_modifica=%s, fecha_modifica=CURRENT_TIMESTAMP "
                "WHERE id_cheque=%s",
                tuple(params),
                conn=conn,
            )

        # La huella, UNA por transición y adentro de la misma tx: si el
        # cheque se movió, el hecho existe; si el registro falla, no se movió
        # nada. `no_banco` en la metadata es lo que le permite a la traza unir
        # este hecho con el renglón del banco.
        import mov_doble as _md

        _tipo_md = TIPO_MD_TRANSICION.get(stat_destino, TIPO_MD_TRANSICION_OTRO)
        _md.registrar(
            conn=conn,
            tipo=_tipo_md,
            origen_table="cheque",
            origen_id=int(id_cheque),
            destino_table=("transacciones_bancarias" if side_effect_id
                           and stat_destino in DESTINOS_DEPOSITO else "cheque"),
            destino_id=(int(side_effect_id) if side_effect_id
                        and stat_destino in DESTINOS_DEPOSITO else int(id_cheque)),
            importe=importe,
            fecha=fecha,
            concepto=(f"{stat_prev or '?'}→{stat_destino} cheque "
                      f"{ch.get('no_cheque') or '#' + str(id_cheque)} "
                      f"{ch.get('codigo_cli') or ''}").strip()[:200],
            usuario=usuario,
            metadata={k: v for k, v in {
                "id_cheque": int(id_cheque),
                "stat_prev": stat_prev,
                "stat_destino": stat_destino,
                "codigo_cli": (ch.get("codigo_cli") or "").strip() or None,
                "no_banco": int(banco_destino) if banco_destino else None,
                "id_transaccion": (int(side_effect_id)
                                   if side_effect_id
                                   and stat_destino in DESTINOS_DEPOSITO else None),
                "motivo": (motivo or "").strip() or None,
                "compensacion": _comp_registro or None,
                "fechad_previa": (_fechad_previa.isoformat()
                                  if _fechad_previa else None),
                "fechad_nueva": (_fechad_nueva.isoformat()
                                 if _fechad_nueva else None),
            }.items() if v is not None},
        )

    return {
        "id_cheque": id_cheque,
        "stat_previo": stat_prev,
        "stat_nuevo": stat_destino,
        "side_effect_id": side_effect_id,
        "motivo": motivo,
    }


def espejos_vivos_de(id_cheque: int, conn=None) -> list[int]:
    """Los cheques-ESPEJO NB=97/98 vivos que cuelgan de este cheque.

    El espejo negativo es la contrapartida del saldo a favor que deja un
    anticipo: si el padre se va, el espejo tiene que irse con él o la utilidad
    se mueve de una sola punta (caso HOM 19/08/2026).

    Filtra por `no_banco IN (97, 98)` a propósito: `id_cheque_padre` lo usa
    TAMBIÉN el cheque de REEMPLAZO (anular + recargar, `reemplazar()`), y ése
    no se toca — es el bueno.
    """
    return [
        int(r["id_cheque"])
        for r in (db.fetch_all(
            "SELECT id_cheque FROM scintela.cheque "
            " WHERE id_cheque_padre = %s AND no_banco IN (97, 98) "
            "   AND COALESCE(importe, 0) < 0 "
            "   AND TRIM(COALESCE(stat, '')) NOT IN ('X', 'T', 'R') "
            " ORDER BY id_cheque",
            (id_cheque,),
            conn=conn,
        ) or [])
    ]


def anular_por_error_de_carga(
    id_cheque: int,
    *,
    motivo: str,
    id_reemplazo: int | None = None,
    usuario: str = "web",
    sin_compensacion_bancaria: bool = False,
    _en_cascada: bool = False,
    conn=None,
) -> dict:
    """Anular un cheque mal cargado, con compensaciones automáticas.

    Decisión del dueño 2026-04-30 (addendum batch 22 §8): para corregir
    importe/cliente/banco mal cargados, se anula el cheque viejo y se crea
    uno nuevo. Más limpio que reversar→editar→re-depositar; mismo paper
    trail que la regla de facturas (anular y reemitir).

    DIFERENCIA vs `reversar()` (rebote real):
      - NO marca cliente.stop (es error administrativo, no rebote real).
      - Tag explícito `[X] error de carga` en observacion (vs `[REBOTE]`).
      - Side-effects compensatorios según stat actual:

        | stat actual          | side-effect compensatorio                  |
        |----------------------|---------------------------------------------|
        | Z, P, D              | sólo UPDATE — no había mov en banco/caja    |
        | B/V/W/I/J/K          | INSERT compensación ND en transacciones_bancarias |
        | C                    | INSERT TIPO='S' en caja                     |
        | con chequesxfact     | reverse de aplicaciones (factura.abono -=)  |
        | con posdat hermana   | DELETE posdat (banc=0, num=id_cheque)       |
        | con espejo NB=97/98  | se anula el espejo también (cascada)        |

    Después la persona usa "Nuevo cheque" para cargar el correcto.
    `id_reemplazo` (opcional) se appendea a la observacion para enlazar.

    `sin_compensacion_bancaria` (TMT 2026-08-14, dueña): NO insertar la ND.
    Lo usa /bancos cuando el depósito NO se compensa sino que se BORRA de
    verdad (el cheque nació como depósito directo y no existió nunca): ahí la
    ND movería el saldo dos veces y dejaría un renglón hablando de un depósito
    que ya no está. El freno de conciliado sigue corriendo igual — es el que
    impide anular algo que el banco ya explicó.

    ⭐ CASCADA AL ESPEJO (TMT 2026-08-19, caso HOM ch#102672). Un cheque de
    ANTICIPO no viene solo: `crear()` le cuelga un cheque ESPEJO negativo
    NB=98 (`id_cheque_padre` = el anticipo) que es la contrapartida del saldo
    a favor del cliente. Anular el padre sin anular el espejo deja ese
    negativo VIVO en cartera sin nada que lo compense: el cliente queda con un
    saldo a favor que nadie le debe y la utilidad baja por su importe, de una
    sola punta. Pasó el 19/08: Alex cargó $2.626,27 en vez de $2.626,67,
    anuló, recargó bien — y el espejo de la carga mala quedó solo.

    (El `continue` que saltea los `cheque_anticipo_espejo` al reversar un batch
    en /historial decía desde siempre "anular_por_error_de_carga ya cascadea".
    Recién ahora es verdad.)

    `_en_cascada` es privado: marca la llamada que anula al espejo, para que
    ésa no vuelva a buscar hijos.

    Todo en una sola transacción.
    """
    motivo = (motivo or "").strip()
    # TMT 2026-05-21 dueña: motivo opcional sin minlen.

    fecha = today_ec()
    asegurar_fecha_abierta(fecha)

    # TMT 2026-05-15: caller puede pasar `conn` (batch atómico).
    import contextlib as _ctx

    _tx = _ctx.nullcontext(conn) if conn is not None else db.tx()
    with _tx as conn:
        ch = db.fetch_one(
            "SELECT id_cheque, no_cheque, stat, codigo_cli, importe, "
            "no_banco, fechad "
            "FROM scintela.cheque WHERE id_cheque = %s",
            (id_cheque,),
            conn=conn,
        )
        if not ch:
            raise ValueError(f"Cheque {id_cheque} no existe.")
        # Antes de tocar nada: si un TOTALIZAR se llevó los vínculos, esta
        # anulación compensaría el banco y dejaría el abono puesto.
        _freno_si_el_totalizar_se_llevo_los_vinculos(id_cheque, conn=conn)
        stat_prev = (ch.get("stat") or "").upper()
        if stat_prev in ("X", "T", "R"):
            raise ValueError(
                f"Cheque ya cerrado (stat='{stat_prev}'). Anular por error de "
                "carga sólo aplica a cheques activos."
            )

        importe = float(ch["importe"] or 0)
        compensacion = None

        # --- El espejo NB=97/98 que cuelga de este cheque (ver docstring) ---
        ids_espejo: list[int] = [] if _en_cascada else espejos_vivos_de(
            id_cheque, conn=conn)

        # ── FRENO 2 (TMT 2026-08-05, caso MSS 1.100,93): si el movimiento
        # bancario de este cheque ya está CONCILIADO (contra el extracto o
        # interno), anular acá deja el match apuntando a un cheque anulado:
        # el crédito real del banco queda "explicado" por plata que según PC
        # no existe, y la ND compensatoria aparece después en la conciliación
        # como una mitad suelta que nadie sabe aparear. Primero se deshace la
        # conciliación (Conciliación → Deshacer conciliados), después se
        # anula. El match vivo es estado='matched' + deshecho_en IS NULL —
        # misma definición que usa banco_v2.
        _links_banco = db.fetch_all(
            """
            SELECT tb.id_transaccion, tb.no_banco, tb.fecha, tb.documento,
                   tb.concepto, tb.importe
              FROM scintela.chequextransaccion cxt
              JOIN scintela.transacciones_bancarias tb
                ON tb.id_transaccion = cxt.id_transaccion
             WHERE cxt.id_cheque = %s
            """,
            (id_cheque,),
            conn=conn,
        ) or []
        if _links_banco:
            _conc = db.fetch_all(
                """
                SELECT m.id_transaccion, m.metodo, m.real_fecha,
                       m.creado_en::date AS conciliado_el
                  FROM scintela.banco_conciliacion_match m
                 WHERE m.id_transaccion = ANY(%s)
                   AND m.estado = 'matched'
                   AND m.deshecho_en IS NULL
                """,
                ([int(x["id_transaccion"]) for x in _links_banco],),
                conn=conn,
            ) or []
            if _conc:
                _m0 = _conc[0]
                _es_interno = str(_m0.get("metodo") or "").startswith("interno")
                _f = _m0.get("conciliado_el")
                _f_txt = _f.strftime("%d/%m/%Y") if _f else "?"
                raise ValueError(
                    "El depósito de este cheque ya está CONCILIADO "
                    + ("(interno" if _es_interno else "con el extracto del banco")
                    + f", el {_f_txt}"
                    + (")" if _es_interno else "")
                    + ". Anularlo ahora dejaría esa conciliación apuntando a "
                    "un cheque anulado y una compensación suelta sin pareja. "
                    "Primero deshacé la conciliación en Conciliación → "
                    "Deshacer conciliados, y después anulá."
                )

        # --- Reverse de aplicaciones a facturas (igual que reversar()) ---
        aplic = db.fetch_all(
            "SELECT id_chequexfact, id_fact, importe FROM scintela.chequesxfact WHERE id_cheque = %s",
            (id_cheque,),
            conn=conn,
        )
        for ap in aplic:
            id_fact = ap["id_fact"]
            imp = float(ap["importe"] or 0)
            if not id_fact:
                continue
            f = db.fetch_one(
                "SELECT importe, abono, retencion FROM scintela.factura "
                "WHERE id_factura = %s",
                (id_fact,),
                conn=conn,
            )
            if not f:
                continue
            nuevo_abono = max(float(f["abono"] or 0) - imp, 0)
            nuevo_saldo = _fact_q.saldo_de(f["importe"], nuevo_abono, f["retencion"])
            # La regla del estado vive en UN lugar (ver _fact_q.stat_de).
            # Decía `nuevo_saldo <= 0.01 → T`, que mandaba un saldo a FAVOR a
            # cancelada y le hacía desaparecer el crédito al cliente.
            nuevo_stat_f = _fact_q.stat_de(nuevo_saldo, nuevo_abono, tol=0.01)
            db.execute(
                "UPDATE scintela.factura "
                "SET abono=%s, saldo=%s, stat=%s, usuario_modifica=%s "
                "WHERE id_factura=%s",
                (nuevo_abono, nuevo_saldo, nuevo_stat_f, usuario, id_fact),
                conn=conn,
            )

        # --- Borrar las chequesxfact del cheque anulado (paridad Bug G
        # de reversar(), TMT 2026-05-16): factura.abono es DERIVADA de
        # chequesxfact — ya restamos el abono arriba; si las filas quedan
        # vivas apuntando a un cheque stat='X', el detalle de la factura
        # muestra aplicaciones fantasma y el abono deja de cuadrar con la
        # tabla. BUG 2026-07-06 (caso EDU/alex). ---
        if aplic:
            db.execute(
                "DELETE FROM scintela.chequesxfact WHERE id_cheque=%s",
                (id_cheque,),
                conn=conn,
            )

        # --- Compensación bancaria/caja según stat actual ---
        if stat_prev in STATS_DEPOSITADO and sin_compensacion_bancaria:
            # El caller borra el movimiento del banco él mismo (ver
            # bancos.eliminar_error_carga). Compensar acá sería contarlo dos
            # veces. Los demás side-effects (facturas, posdat, observación,
            # stat X) YA corrieron arriba y siguen valiendo.
            compensacion = {"tipo": "banco", "id": None, "omitida": True}
        elif stat_prev in STATS_DEPOSITADO:
            import bank_helpers

            banco = ch.get("no_banco") or (10 if stat_prev == "B" else 32)
            # TMT 2026-06-25 (dueña: "no debería pasar nunca"): la compensación
            # NUNCA debe caer en un banco-concepto/espejo (DEP.PICH 90, DEP.INTER
            # 91, etc.) — esos no llevan asiento propio (depositar a 90 no crea
            # mov en 90), así que la ND quedaba como residuo (ej. -455,89 en
            # DEP.PICH). Resolvemos al banco REAL de destino del depósito.
            _CONCEPTO_A_REAL = {90: 10, 91: 32, 95: 10, 97: 10, 98: 10, 99: 10}
            try:
                if int(banco) >= 90:
                    banco = _CONCEPTO_A_REAL.get(int(banco), 10)
            except (TypeError, ValueError):
                banco = 10
            res = bank_helpers.insert_movimiento_bancario(
                conn,
                no_banco=banco,
                no_cta=None,
                fecha=fecha,
                documento="ND",  # nota de débito compensatoria
                importe=importe,
                # TMT 2026-08-24: desde hoy un cobro NEGATIVO también deja su
                # asiento ('DE' en negativo), así que anularlo tiene que poder
                # devolver la plata: una 'ND' en negativo SUBE el saldo. Sin
                # esto la anulación de un cobro negativo reventaba con
                # "importe debe ser positivo" y no se podía deshacer.
                permitir_signed=True,
                concepto=f"ANUL ch{ch.get('no_cheque') or id_cheque} err carga",
                prov=ch.get("codigo_cli"),
                numreferencia=id_cheque,
                usuario=usuario,
            )
            compensacion = {"tipo": "banco", "id": res["id_transaccion"]}

            # ── FRENO 3 (TMT 2026-08-05, casos CG3/ELF): la ND compensatoria
            # y el depósito original se cancelan entre sí — NINGUNO va a
            # aparecer jamás en el extracto, así que dejarlos sueltos siembra
            # dos mitades en la conciliación que semanas después nadie sabe
            # aparear. Si el depósito era de ESTE cheque solo (mismo banco,
            # mismo importe) y sigue sin conciliar (freno 2 ya lo garantizó),
            # quedan EMPAREJADOS como interno — auditable y reversible desde
            # 'Deshacer conciliados'. Depósitos consolidados (dep.N ch.)
            # quedan afuera: ahí la ND compensa una PARTE y el pareo es
            # manual.
            _dep_solo = [
                x for x in _links_banco
                if int(x.get("no_banco") or 0) == int(banco)
                and abs(float(x.get("importe") or 0) - abs(importe)) <= 0.01
                and str(x.get("documento") or "").strip().upper() == "DE"
            ]
            if len(_dep_solo) == 1:
                try:
                    from modules.conciliacion.queries import emparejar_interno

                    emparejar_interno(
                        [int(_dep_solo[0]["id_transaccion"]),
                         int(res["id_transaccion"])],
                        no_banco=int(banco),
                        motivo="anulacion",
                        usuario=usuario,
                        conn=conn,
                    )
                    compensacion["conciliado_interno"] = True
                except Exception:  # noqa: BLE001
                    # El pareo es un mimo, no un requisito: si falla (schema
                    # viejo, lo que sea), la anulación tiene que salir igual.
                    pass
        elif stat_prev == "C":
            import caja_helpers

            # TMT 2026-08-04 — ¿esta fila de caja la creó el cheque, o el
            # cheque la ADOPTÓ? (ver `caja_existente_id` en `crear`). Es la
            # misma pregunta que "¿de quién es la plata?": si el cheque la
            # creó, anularlo tiene que sacarla; si la adoptó, la plata entró
            # a la caja ANTES y por su cuenta — compensarla la borraría dos
            # veces y dejaría la caja corta por un cobro que sí ocurrió.
            #
            # El hecho que las distingue: una fila creada por el cheque nace
            # en la MISMA transacción, así que su `fecha_crea` es la del
            # cheque. Una adoptada es más vieja. No hace falta un marcador
            # que alguien pueda olvidar de escribir — la fecha ya lo dice.
            adoptada = db.fetch_one(
                """
                SELECT cj.id_caja
                  FROM scintela.caja cj
                  JOIN scintela.cheque c ON c.id_cheque = cj.id_cheque
                 WHERE cj.id_cheque = %s
                   AND cj.fecha_crea < c.fecha_crea
                 ORDER BY cj.id_caja
                 LIMIT 1
                """,
                (id_cheque,),
                conn=conn,
            )
            if adoptada:
                # Desenlazar, no compensar: la fila vuelve a quedar libre
                # (y `/caja` la vuelve a marcar "⚠ sin cobranza"), que es
                # exactamente el estado previo a la conversión.
                db.execute(
                    "UPDATE scintela.caja SET id_cheque = NULL "
                    "WHERE id_caja = %s AND id_cheque = %s",
                    (adoptada["id_caja"], id_cheque),
                    conn=conn,
                )
                compensacion = {"tipo": "caja_desenlazada", "id": adoptada["id_caja"]}
            else:
                # TMT 2026-07-30: si el efectivo era NEGATIVO (devolución al
                # cliente, caja tipo='S'), anularlo tiene que DEVOLVER la plata a
                # la caja — una 'E'. Con 'S' fijo la anulación restaba dos veces.
                res = caja_helpers.insert_movimiento_caja(
                    conn,
                    fecha=fecha,
                    tipo="E" if importe < 0 else "S",
                    importe=abs(importe),
                    concepto=f"ANUL ch{ch.get('no_cheque') or id_cheque} err carga",
                    id_cheque=id_cheque,
                    usuario=usuario,
                )
                compensacion = {"tipo": "caja", "id": res["id_caja"]}

        # --- DELETE posdat hermana si existía ---
        db.execute(
            "DELETE FROM scintela.posdat WHERE COALESCE(banc, 0) = 0 AND num=%s AND prov=%s",
            (id_cheque, ch.get("codigo_cli")),
            conn=conn,
        )

        # --- UPDATE cheque a stat='X' con tag explícito ---
        marca = f"[X] error de carga: {motivo[:60]}"
        if id_reemplazo:
            marca += f" (reemplaza por #{id_reemplazo})"
        db.execute(
            "UPDATE scintela.cheque "
            "SET stat='X', fechaout=%s, "
            "    observacion = RIGHT("
            "        COALESCE(observacion || ' | ', '') || %s, 200), "
            "    usuario_modifica=%s, fecha_modifica=CURRENT_TIMESTAMP "
            "WHERE id_cheque=%s",
            (fecha, marca, usuario, id_cheque),
            conn=conn,
        )

        # --- Historial unificado: registrar el reverso y marcar los
        # mov_doble originales como 'reversado'. BUG 2026-07-06 (dueña,
        # caso EDU/alex): este flujo anulaba el cheque y reabría las
        # facturas pero NO tocaba scintela.mov_doble → en /historial las
        # filas "Cheque: alta" y "Cheque → Factura aplicada" seguían
        # 'activo', con el botón "↺ reversar" ofrecido de nuevo (re-
        # reversar duplicaba el reverso). Mismo mecanismo que reversar():
        # el registrar() con id_original marca el alta como 'reversado'
        # + id_reverso; las aplicaciones se marcan con el UPDATE de abajo.
        import mov_doble as _md

        md_orig_cheque = db.fetch_one(
            """
            SELECT id_mov_doble FROM scintela.mov_doble
             WHERE origen_table='cheque' AND origen_id=%s
               AND tipo='cheque_creado' AND estado='activo'
             ORDER BY id_mov_doble DESC LIMIT 1
            """,
            (id_cheque,),
            conn=conn,
        )
        _id_reverso = _md.registrar(
            conn=conn,
            tipo="reverso_cheque_administrativo",
            origen_table="cheque",
            origen_id=id_cheque,
            destino_table="cheque",
            destino_id=id_cheque,
            # registrar() ignora importe 0 — mismo truco `or 1.0` que reversar().
            importe=importe or 1.0,
            fecha=fecha,
            concepto=(
                f"ANULADO error de carga ch {ch.get('no_cheque') or id_cheque} "
                f"{stat_prev}→X" + (f" — {motivo}" if motivo else "")
            )[:200],
            usuario=usuario,
            metadata={
                "id_cheque": id_cheque,
                "stat_previo": stat_prev,
                "id_reemplazo": id_reemplazo,
                "n_aplicaciones_reversadas": len(aplic),
                # TMT 2026-07-30: snapshot de las aplicaciones, no sólo el
                # conteo. Sin esto, deshacer la anulación devolvía el cheque a
                # cartera pero las facturas quedaban desabonadas y había que
                # re-aplicarlas de memoria. Mismo patrón que el snapshot de
                # posdat de `cancelar_por_anticipo` (29/07). Ausencia de la
                # clave = anulación vieja, sin snapshot posible.
                "aplicaciones_borradas": [
                    {"id_fact": int(a["id_fact"]),
                     "importe": float(a["importe"] or 0)}
                    for a in (aplic or [])
                ],
                "compensacion": compensacion,
                # TMT 2026-08-19: qué espejos se llevó la cascada. Sin esto,
                # deshacer la anulación revive el anticipo y deja el espejo
                # muerto — el mismo desbalance con el signo al revés.
                "espejos_anulados": ids_espejo,
                "motivo": motivo or "",
            },
            id_original=md_orig_cheque["id_mov_doble"] if md_orig_cheque else None,
        )
        # También marcar como reversadas las aplicaciones del cheque
        # (`cheque_aplicado_a_factura`) que seguían 'activo' — igual que
        # hace reversar().
        #
        # TMT 2026-07-29: se suma `cheque_efectivo_to_caja`. Un cheque
        # banco=99 (EFECTIVO) entra a caja con una fila 'E' y deja ese
        # mov_doble; anular por error de carga YA compensa esa caja con una
        # 'S' (tabla de arriba, stat 'C'), pero el mov quedaba 'activo': en
        # /historial la entrada de caja seguía ofreciendo un ↺ que después
        # rebotaba con "cheque ya cerrado". El dinero estaba bien, la
        # pantalla mentía.
        # TMT 2026-07-29 (dueña: "todas tienen que tener link a deshacer"):
        # los hijos se marcan en bloque, pero ahora APUNTAN al reverso del
        # cheque padre. Antes quedaban con id_reverso NULL y el chequeo de
        # salud los leía como link roto — un renglón tachado sin la
        # contrapartida al lado. El reverso es uno, pero ahora se sabe cuál.
        db.execute(
            """
            UPDATE scintela.mov_doble
               SET estado='reversado', id_reverso=%s
             WHERE origen_table='cheque' AND origen_id=%s
               AND tipo IN ('cheque_aplicado_a_factura',
                            'cheque_efectivo_to_caja',
                            'cheque_anticipo_espejo')
               AND estado='activo'
            """,
            (_id_reverso, id_cheque),
            conn=conn,
        )

        # --- CASCADA: el espejo se anula con el padre ---
        # Va al final, con el padre ya en 'X': el espejo se anula por este
        # mismo camino (reversa sus aplicaciones, borra su posdat, deja su
        # propio mov_doble), así que sigue siendo deshacible por separado.
        for _id_esp in ids_espejo:
            anular_por_error_de_carga(
                _id_esp,
                motivo=(
                    f"espejo del cheque {ch.get('no_cheque') or id_cheque} "
                    f"anulado por error de carga"
                ),
                usuario=usuario,
                conn=conn,
                _en_cascada=True,
            )

    return {
        "id_cheque": id_cheque,
        "stat_previo": stat_prev,
        "stat_nuevo": "X",
        "motivo": motivo,
        "id_reemplazo": id_reemplazo,
        "compensacion": compensacion,
        "aplicaciones_reversadas": len(aplic),
        "espejos_anulados": ids_espejo,
    }


def distribuir_espejos_anticipo(
    importes_anticipo: list[float], suma_cancelada: float
) -> list[float]:
    """Distribuye la suma de cheques cancelados contra los cheques-anticipo
    (97) del form, FIFO, y devuelve el importe de ESPEJO (= sobrante) que le
    corresponde a cada uno.

    TMT 2026-07-06 (dueña): "si el anticipo era 3000 y había 3 cheques de
    1000, tengo que cancelar todos esos cheques. Si me dio 10.000, cancelo
    los 3 de 1000 y además sumo una nota de crédito por 7000". El espejo
    NB=98 se crea SOLO por el sobrante (crear() lo saltea si < $1).

    Valida el tope: la suma cancelada NO puede superar el anticipo + $0.01.
    """
    total = round(sum(float(i or 0) for i in importes_anticipo), 2)
    suma = round(float(suma_cancelada or 0), 2)
    if suma < -0.005:
        raise ValueError("La suma de cheques a cancelar no puede ser negativa.")
    if suma > total + 0.01:
        raise ValueError(
            f"Los cheques a cancelar suman ${suma:,.2f} y el anticipo es de "
            f"${total:,.2f} — no se puede cancelar más que el anticipo. "
            f"Destildá algún cheque o subí el importe del anticipo."
        )
    espejos: list[float] = []
    restante = suma
    for imp in importes_anticipo:
        imp_f = float(imp or 0)
        consumo = min(restante, imp_f) if restante > 0 else 0.0
        restante = round(restante - consumo, 2)
        espejos.append(round(imp_f - consumo, 2))
    return espejos


def cancelar_por_anticipo(
    *,
    id_cheque: int,
    codigo_cli: str,
    id_cheque_anticipo: int | None = None,
    monto_anticipo: float = 0.0,
    usuario: str = "web",
    conn=None,
) -> dict:
    """Cancela (stat='X') un cheque VIVO del cliente, cubierto por un ANTICIPO.

    TMT 2026-07-06 (dueña): "esto va a ser un anticipo que se lo aplicamos a
    los cheques... tengo que cancelar (X) todos esos cheques". Flujo 97 de
    /cheques/nuevo: cada cheque tildado en el panel de cartera pasa a 'X' y
    el espejo NB=98 se crea SOLO por el sobrante (ver crear()).

    Validaciones (todas con error claro; ValueError → rollback total):
      - el cheque existe y es DEL cliente del anticipo;
      - está VIVO (stat en Z/1/2/3/P/D — grupo TOTC "Z123PD");
      - importe > 0 (los espejos NB=98 / NC no se cancelan por acá);
      - SIN aplicaciones a facturas (chequesxfact): cancelarlo reabriría las
        facturas sin compensación del anticipo → desaplicar primero desde la
        ficha del cheque y reintentar (automatizarlo queda fuera de alcance).

    Side-effects (mismo patrón que anular_por_error_de_carga, sin
    compensación bancaria — un cheque vivo no tiene mov de banco propio):
      - DELETE de la fila posdat hermana (cheques postdatados);
      - UPDATE stat='X' + fechaout + tag "[X] cancelado por anticipo ...";
      - mov_doble tipo='cheque_cancelado_por_anticipo' POR CHEQUE, sin
        batch_id → reversible individualmente (el reverso manual = volver a
        'Z' + ajustar el espejo; NO automatizado a propósito, queda el
        registro en /historial).

    Corre dentro de la tx del caller (conn) — si algo falla, rollback total.
    """
    fecha = today_ec()
    asegurar_fecha_abierta(fecha)
    codigo_cli = (codigo_cli or "").upper().strip()

    import contextlib as _ctx

    _tx = _ctx.nullcontext(conn) if conn is not None else db.tx()
    with _tx as conn:
        ch = db.fetch_one(
            "SELECT id_cheque, no_cheque, stat, codigo_cli, importe, fechad "
            "FROM scintela.cheque WHERE id_cheque = %s",
            (id_cheque,),
            conn=conn,
        )
        if not ch:
            raise ValueError(f"Cheque #{id_cheque} no existe.")
        if (ch.get("codigo_cli") or "").upper().strip() != codigo_cli:
            raise ValueError(
                f"El cheque #{id_cheque} es de "
                f"{(ch.get('codigo_cli') or '?').strip()}, no de {codigo_cli} "
                "— no se puede cancelar con este anticipo."
            )
        stat_prev = (ch.get("stat") or "").strip().upper()
        if stat_prev not in STATS_VIVOS:
            raise ValueError(
                f"El cheque #{id_cheque} está en stat='{stat_prev}' — sólo se "
                f"cancelan por anticipo cheques vivos "
                f"({'/'.join(STATS_VIVOS)})."
            )
        importe = float(ch.get("importe") or 0)
        if importe <= 0.005:
            raise ValueError(
                f"El cheque #{id_cheque} tiene importe {importe:.2f} — las "
                "notas de crédito / espejos no se cancelan por anticipo."
            )
        # ⭐ TMT 2026-08-12 — acá había un freno: si el cheque ya estaba aplicado
        # a facturas, no dejaba cancelarlo, "porque cancelarlo acá reabriría las
        # facturas sin compensación del anticipo".
        #
        # Ese peligro no existe: esta función NO toca `chequesxfact` ni los
        # saldos de las facturas. Sólo marca el cheque 'X', borra el posdatado
        # hermano y deja el mov_doble. El freno lo escribió el mismo commit que
        # creó la función (6429255e, 06/07) como precaución, sobre una premisa
        # que el código de al lado nunca cumplió.
        #
        # Y el caso que bloqueaba es el NORMAL (Alex, GLI, 12/08): el cliente
        # había dado cheques a fecha que ya dejaron sus facturas pagadas, trae
        # la plata y se lleva los cheques. La factura sigue pagada — lo que
        # cambia es qué la respalda: antes el cheque, ahora el anticipo.
        #
        # Medido de punta a punta antes de sacarlo (ver
        # tests/test_anticipo_cancela_cheque_aplicado_2026_08_12.py): la
        # posición del cliente —cartera + facturas abiertas + caja— queda
        # IDÉNTICA, la factura sigue en 'T' con saldo 0 y el cheque sale de
        # cartera.

        # posdat hermana (cheques postdatados viven también en el flujo posdat)
        #
        # TMT 2026-07-29: ANTES de borrarla, guardarla. El DELETE existía desde
        # el 06/07 sin snapshot, así que reversar la cancelación era imposible:
        # la fila del pasivo se perdía y no hay de dónde deducir su fecha ni su
        # concepto. Es el mismo patrón que ya usa `deshacer_neteo` (snapshot →
        # borrar → re-INSERT al deshacer). Barato: son 0 o 1 filas.
        posdat_snap = [
            {
                "fecha": (r.get("fecha").isoformat()
                          if hasattr(r.get("fecha"), "isoformat") else r.get("fecha")),
                "fechad": (r.get("fechad").isoformat()
                           if hasattr(r.get("fechad"), "isoformat") else r.get("fechad")),
                "prov": r.get("prov"),
                "num": r.get("num"),
                "importe": float(r.get("importe") or 0),
                "concepto": r.get("concepto") or "",
            }
            for r in (db.fetch_all(
                "SELECT fecha, fechad, prov, num, importe, concepto "
                "  FROM scintela.posdat "
                " WHERE COALESCE(banc, 0) = 0 AND num=%s AND prov=%s",
                (id_cheque, codigo_cli), conn=conn,
            ) or [])
        ]
        db.execute(
            "DELETE FROM scintela.posdat WHERE COALESCE(banc, 0) = 0 AND num=%s AND prov=%s",
            (id_cheque, codigo_cli),
            conn=conn,
        )

        marca = (
            "[X] cancelado por anticipo"
            + (f" #{id_cheque_anticipo}" if id_cheque_anticipo else "")
            + f" ${float(monto_anticipo or 0):,.2f}"
        )
        db.execute(
            "UPDATE scintela.cheque "
            "SET stat='X', fechaout=%s, "
            "    observacion = RIGHT("
            "        COALESCE(observacion || ' | ', '') || %s, 200), "
            "    usuario_modifica=%s, fecha_modifica=CURRENT_TIMESTAMP "
            "WHERE id_cheque=%s",
            (fecha, marca, usuario, id_cheque),
            conn=conn,
        )

        import mov_doble as _md

        _md.registrar(
            conn=conn,
            tipo="cheque_cancelado_por_anticipo",
            origen_table="cheque",
            origen_id=id_cheque,
            destino_table="cheque",
            destino_id=id_cheque_anticipo or id_cheque,
            importe=importe or 1.0,
            fecha=fecha,
            concepto=(
                "CANCELADO por anticipo"
                + (f" #{id_cheque_anticipo}" if id_cheque_anticipo else "")
                + f" ${float(monto_anticipo or 0):,.2f} — ch "
                + f"{(ch.get('no_cheque') or '').strip() or id_cheque} "
                + f"{codigo_cli} {stat_prev}→X"
            )[:200],
            usuario=usuario,
            metadata={
                "id_cheque": id_cheque,
                "stat_previo": stat_prev,
                "id_cheque_anticipo": id_cheque_anticipo,
                "monto_anticipo": float(monto_anticipo or 0),
                "codigo_cli": codigo_cli,
                # Snapshot para poder reversar (ver arriba). Lista vacía =
                # el cheque NO tenía posdat hermana; ausencia de la clave =
                # mov viejo, anterior al 29/07, sin snapshot posible.
                "posdat_borradas": posdat_snap,
            },
        )

    return {
        "id_cheque": id_cheque,
        "stat_previo": stat_prev,
        "stat_nuevo": "X",
        "importe": importe,
    }


def espejo_de_anticipo(id_cheque_anticipo: int | None, conn=None) -> dict | None:
    """El espejo NB=98 (saldo a favor) que cuelga de un cheque de anticipo.

    Se usa para PODER AVISAR — ver `reversar_cancelacion_por_anticipo`.
    """
    if not id_cheque_anticipo:
        return None
    return db.fetch_one(
        "SELECT id_cheque, no_cheque, importe, stat "
        "  FROM scintela.cheque "
        " WHERE id_cheque_padre = %s AND no_banco IN (97, 98) "
        "   AND TRIM(COALESCE(stat, '')) = 'Z' "
        " ORDER BY id_cheque DESC LIMIT 1",
        (int(id_cheque_anticipo),), conn=conn,
    )


def reversar_cancelacion_por_anticipo(
    id_mov_doble: int, *, usuario: str = "web", motivo: str = "",
) -> dict:
    """Devuelve a cartera un cheque que se había cancelado con un anticipo.

    TMT 2026-07-29. `cancelar_por_anticipo` decía en su docstring que el mov
    quedaba "reversible individualmente" y que el reverso era "manual… NO
    automatizado a propósito". Eran 21 movs activos que /historial mostraba
    con un ↺ que no llevaba a ninguna parte. Esto lo automatiza en lo que se
    puede automatizar sin adivinar, y AVISA de lo que no.

    Deshace las dos cosas que la cancelación hizo sobre el cheque:
      1. `stat='X'` → vuelve al stat que tenía (guardado en la metadata);
      2. el DELETE de la posdat hermana → se re-inserta desde el snapshot.

    Lo que NO toca, a propósito: **el espejo NB=98 (saldo a favor)**. El
    espejo se crea por el SOBRANTE del anticipo (anticipo − Σ cheques
    cancelados), así que al des-cancelar un cheque el sobrante debería subir
    por su importe. Ajustarlo automáticamente exige decidir algo contable que
    el código no puede decidir solo — si el anticipo quedó sobre-aplicado o
    si el cliente pasa a tener más saldo a favor. Devolvemos el espejo
    encontrado y su importe para que la pantalla lo diga con números y la
    dueña lo resuelva; mejor eso que mover el saldo a favor por adivinanza.

    Se niega (ValueError, sin escribir nada) si:
      - el mov no es una cancelación por anticipo, o ya está reversado;
      - el cheque ya no está en 'X' (alguien lo movió después: reponerlo
        pisaría ese cambio);
      - el mov es anterior al 29/07 y el cheque TENÍA posdat hermana pero no
        hay snapshot — en ese caso el pasivo no se puede reconstruir sin
        inventar fecha y concepto, y se explica qué cargar a mano.

    Devuelve `{id_cheque, no_cheque, stat_restaurado, posdat_restauradas,
    espejo, sin_snapshot}`.
    """
    import json as _json

    import mov_doble as _md

    mv = db.fetch_one(
        "SELECT id_mov_doble, tipo, origen_id, destino_id, estado, metadata, "
        "       importe, fecha_operacion "
        "  FROM scintela.mov_doble WHERE id_mov_doble = %s",
        (id_mov_doble,),
    )
    if not mv:
        raise ValueError("No encuentro ese movimiento.")
    if (mv.get("tipo") or "") != "cheque_cancelado_por_anticipo":
        raise ValueError(
            f"Ese movimiento es '{mv.get('tipo')}', no una cancelación por "
            "anticipo.")
    if (mv.get("estado") or "activo") != "activo":
        raise ValueError(
            f"Esa cancelación ya está {mv.get('estado')} — no se reversa dos veces.")

    meta = mv.get("metadata") or {}
    if isinstance(meta, str):
        try:
            meta = _json.loads(meta)
        except Exception:
            meta = {}
    id_cheque = int(mv.get("origen_id") or 0)
    stat_destino = (meta.get("stat_previo") or "Z").strip().upper() or "Z"
    if stat_destino not in STATS_VIVOS:
        raise ValueError(
            f"La metadata dice que el cheque venía de stat='{stat_destino}', "
            f"que no es un estado vivo ({'/'.join(STATS_VIVOS)}). No lo "
            "restauro a un estado inconsistente — revisalo a mano.")
    snap = meta.get("posdat_borradas")
    sin_snapshot = snap is None

    fecha = today_ec()
    asegurar_fecha_abierta(fecha)

    with db.tx() as conn:
        ch = db.fetch_one(
            "SELECT id_cheque, no_cheque, stat, codigo_cli, importe, fechad, fecha "
            "  FROM scintela.cheque WHERE id_cheque = %s FOR UPDATE",
            (id_cheque,), conn=conn,
        )
        if not ch:
            raise ValueError("El cheque de ese movimiento ya no existe.")
        stat_hoy = (ch.get("stat") or "").strip().upper()
        if stat_hoy != "X":
            raise ValueError(
                f"El cheque está en stat='{stat_hoy}', no en 'X': alguien lo "
                "movió después de la cancelación. Reversá primero ese cambio "
                "— si no, esto lo pisaría.")

        # Sin snapshot, ¿podía haber una posdat que reconstruir?
        #
        # TMT 2026-07-30 (dueña: "¿por qué no me deja? me debería dejar").
        # Tenía razón: el guard del 29/07 miraba si el cheque era POSTDATADO
        # (fechad > fecha) y bloqueaba. Eso es un falso positivo, porque un
        # cheque postdatado NO tiene fila de posdatados por ser postdatado.
        # El ÚNICO alta de `posdat banc=0` para un cheque en PC es el rebote
        # (`transicionar_stat` → stat '9', concepto "ch.prot.<n°>"), y el
        # DELETE de la cancelación aparea por `num = id_cheque`, que es un id
        # interno de PC — ninguna fila venida del dBase puede matchearlo.
        # Entonces: si el cheque vuelve a cartera/postergado/Daniela, nunca
        # pasó por el protesto, no hay nada que reconstruir y el reverso es
        # seguro.
        #
        # El caso testigo es el ch 99699 de CJE ($1.692,16): venía de 'Z' y el
        # dBase lo tiene VIVO en cartera — o sea que el bloqueado era el
        # arreglo que alinea PC con el dBase.
        #
        # Se sigue bloqueando si el cheque vuelve a un estado de REBOTE
        # (1/2/3/9): ésos sí pudieron pasar por el protesto y arrastrar su
        # fila de pasivo. Ahí la falta de snapshot es un problema real.
        if sin_snapshot and stat_destino in ("1", "2", "3", "9"):
            raise ValueError(
                f"Esta cancelación es anterior al 29/07, el cheque vuelve a "
                f"REBOTADO ('9') y no se guardó la fila de posdatados que se "
                f"borró: reconstruirla sería inventar su fecha y su concepto. "
                f"Volvé el cheque a '{stat_destino}' desde su ficha y cargá la "
                f"posdat a mano en /posdat (prov {ch.get('codigo_cli')}, "
                f"num {id_cheque}, $ {float(ch.get('importe') or 0):,.2f}).")

        db.execute(
            "UPDATE scintela.cheque "
            "   SET stat=%s, fechaout=NULL, "
            "       observacion = RIGHT("
            "           COALESCE(observacion || ' | ', '') || %s, 200), "
            "       usuario_modifica=%s, fecha_modifica=CURRENT_TIMESTAMP "
            " WHERE id_cheque=%s",
            (stat_destino,
             ("[R] cancelación por anticipo deshecha"
              + (f": {motivo[:60]}" if motivo else "")),
             usuario, id_cheque),
            conn=conn,
        )

        n_posdat = 0
        for p in (snap or []):
            db.execute(
                "INSERT INTO scintela.posdat "
                "  (fecha, fechad, prov, num, importe, concepto, banc, usuario_crea) "
                "VALUES (%s,%s,%s,%s,%s,%s,0,%s)",
                (p.get("fecha") or fecha, p.get("fechad"),
                 p.get("prov") or ch.get("codigo_cli"),
                 p.get("num") or id_cheque,
                 float(p.get("importe") or 0), (p.get("concepto") or "")[:50],
                 usuario),
                conn=conn,
            )
            n_posdat += 1

        espejo = espejo_de_anticipo(meta.get("id_cheque_anticipo"), conn=conn)

        _md.registrar(
            conn=conn,
            tipo="reverso_cheque_cancelado_por_anticipo",
            origen_table="cheque", origen_id=id_cheque,
            destino_table="cheque",
            destino_id=meta.get("id_cheque_anticipo") or id_cheque,
            importe=float(ch.get("importe") or 0) or 1.0,
            fecha=fecha,
            concepto=(
                f"DESHECHA cancelación por anticipo — ch "
                f"{(ch.get('no_cheque') or '').strip() or id_cheque} "
                f"{ch.get('codigo_cli')} X→{stat_destino}"
                + (f" ({motivo})" if motivo else "")
            )[:200],
            usuario=usuario,
            metadata={
                "stat_restaurado": stat_destino,
                "posdat_restauradas": n_posdat,
                "espejo_no_ajustado": (
                    {"id_cheque": espejo.get("id_cheque"),
                     "importe": float(espejo.get("importe") or 0)}
                    if espejo else None),
                "motivo": motivo or None,
            },
            id_original=id_mov_doble,
        )

    return {
        "id_cheque": id_cheque,
        "no_cheque": (ch.get("no_cheque") or "").strip() or f"#{id_cheque}",
        "importe": float(ch.get("importe") or 0),
        "stat_restaurado": stat_destino,
        "posdat_restauradas": n_posdat,
        "espejo": espejo,
        "sin_snapshot": bool(sin_snapshot),
    }


def reemplazar(
    *,
    id_cheque_viejo: int,
    nuevo_no_cheque: str,
    nuevo_importe=None,
    motivo: str = "",
    usuario: str = "web",
) -> dict:
    """Cheque XX reemplazo — replica BANCOS.PRG:266-305 (`PROV='XX'`).

    El cliente trae un cheque nuevo a reemplazar uno existente (típicamente
    porque el original venció, rebotó o se perdió, sin llegar a depositarse).
    Se marca el viejo con `stat='X'` (observación "reemplazado por #N") y
    se crea uno nuevo con el mismo cliente/banco/fecha del depósito, pero
    nuevo número de cheque + (opcionalmente) nuevo importe.

    Reglas:
      1. El viejo debe estar en stat ∈ {Z, P, D} (todavía vivo en cartera).
         Si está depositado (B/A), endosado (E) o eliminado (X/R/3), no se
         puede reemplazar — se trata como rebote o anulación + nuevo cheque.
      2. Si el viejo tiene aplicaciones a facturas vivas, se MIGRAN al
         nuevo: se deshacen del viejo y se aplican al nuevo. Atómico.
      3. El nuevo cheque hereda `id_cheque_padre = id_cheque_viejo` para
         trazabilidad.
      4. Si `nuevo_importe` es None, se hereda del viejo. Si difiere, se
         registra la diferencia en la observación.
      5. mov_doble `cheque_reemplazo` con id_original apuntando al
         mov_doble del alta del viejo (si existe).
      6. Atómico vía `db.tx()`.

    Devuelve `{id_cheque_viejo, id_cheque_nuevo, no_cheque_nuevo,
                aplicaciones_migradas, importe_viejo, importe_nuevo}`.
    """
    asegurar_fecha_abierta(today_ec())

    nuevo_no_cheque = (nuevo_no_cheque or "").strip()
    if not nuevo_no_cheque:
        raise ValueError("Número del cheque nuevo requerido.")

    with db.tx() as conn:
        # TMT 2026-05-15 (re-audit C2): FOR UPDATE para serializar dos
        # reemplazar concurrentes sobre el mismo cheque — sin esto, ambos
        # pasan el gate stat='Z', ambos crean cheque_nuevo, y ambos
        # migran chequesxfact → estado inconsistente.
        ch_viejo = db.fetch_one(
            "SELECT id_cheque, no_cheque, fecha, fechad, fecha_recibido, "
            "codigo_cli, importe, no_banco, banco, stat, prov, clave "
            "FROM scintela.cheque WHERE id_cheque = %s FOR UPDATE",
            (id_cheque_viejo,),
            conn=conn,
        )
        if not ch_viejo:
            raise ValueError(f"Cheque {id_cheque_viejo} no existe.")
        stat_prev = (ch_viejo.get("stat") or "").upper()
        # Sólo desde stat vivo (Z/P/D). BANCOS.PRG legacy lo hace antes de
        # depositar — el cheque viejo todavía no salió de cartera.
        if stat_prev not in STATS_APLICABLES:
            raise ValueError(
                f"Cheque {id_cheque_viejo} en stat='{stat_prev}' no se puede "
                f"reemplazar. Sólo desde {STATS_APLICABLES} (cartera/postergado/Daniela)."
            )

        importe_viejo = float(ch_viejo.get("importe") or 0)
        importe_nuevo = float(nuevo_importe) if nuevo_importe is not None else importe_viejo
        if importe_nuevo <= 0:
            raise ValueError("El importe del nuevo cheque debe ser positivo.")

        # 1) Crear el cheque nuevo — hereda fecha/codigo_cli/banco del viejo.
        # Usamos el INSERT directo (no `crear()`) para no disparar la lógica
        # de espejo de anticipo, y para poder setear id_cheque_padre.
        fecha_nuevo = today_ec()
        fechad_nuevo = ch_viejo.get("fechad") or fecha_nuevo
        row_nuevo = (
            db.execute_returning(
                """
            INSERT INTO scintela.cheque
                (no_cheque, fecha, fechad, fecha_recibido,
                 codigo_cli, importe, no_banco, banco,
                 stat, fechaing, prov, clave, usuario_crea, id_cheque_padre)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s,
                    'Z', CURRENT_DATE, %s, %s, %s, %s)
            RETURNING id_cheque
            """,
                (
                    nuevo_no_cheque[:10],
                    fecha_nuevo,
                    fechad_nuevo,
                    today_ec(),
                    ch_viejo.get("codigo_cli"),
                    importe_nuevo,
                    ch_viejo.get("no_banco"),
                    ch_viejo.get("banco"),
                    ch_viejo.get("prov"),
                    ch_viejo.get("clave"),
                    usuario[:50],
                    id_cheque_viejo,
                ),
                conn=conn,
            )
            or {}
        )
        id_cheque_nuevo = int(row_nuevo["id_cheque"])

        # 2) Migrar aplicaciones a facturas vivas: traer las que el viejo
        # tenía, deshacer su efecto sobre la factura, e insertar la nueva
        # aplicación apuntando al nuevo cheque. Las facturas ya canceladas
        # (T) se reabren por lo desaplicado y se vuelven a abonar.
        #
        # TMT 2026-05-15 (re-audit C1): cuando importe_nuevo != importe_viejo,
        # el código original NO actualizaba factura.abono (asumía -imp+imp=0),
        # pero al cambiar el importe del cheque las aplicaciones deben re-
        # escalarse o capparse. Si la suma de aplicaciones del viejo excede
        # el importe_nuevo, REHUSAMOS la operación — no podemos decidir auto-
        # máticamente cómo redistribuir; pedimos al usuario que primero
        # desaplique manualmente.
        aplicaciones = (
            db.fetch_all(
                "SELECT id_chequexfact, id_fact, importe FROM scintela.chequesxfact "
                "WHERE id_cheque = %s ORDER BY id_fact",  # orden estable → evita deadlocks
                (id_cheque_viejo,),
                conn=conn,
            )
            or []
        )
        # Sanity: el total de aplicaciones del viejo no puede exceder el
        # importe_nuevo. Si pasa, el usuario debe desaplicar primero.
        total_aplicado_viejo = sum(float(a.get("importe") or 0) for a in aplicaciones)
        if total_aplicado_viejo > importe_nuevo + 0.01:
            raise ValueError(
                f"El cheque viejo tiene aplicaciones por "
                f"$ {total_aplicado_viejo:,.2f} pero el nuevo es de "
                f"$ {importe_nuevo:,.2f}. Desaplicá algunas facturas antes "
                f"de reemplazar, o ingresá un importe mayor."
            )

        # TMT 2026-05-15 (re-audit M6): dedup por id_fact — antes el loop
        # hacía SELECT FOR UPDATE + UPDATE factura por CADA aplicación, y si
        # un cheque tenía N aplicaciones a la MISMA factura (data legacy con
        # abonos parciales) hacía N updates idénticos al mismo registro.
        # Ahora agrupamos: una vuelta por factura única, con DELETE/INSERT
        # batch para todas sus aplicaciones.
        aplicaciones_migradas = 0
        # Agrupar aplicaciones por id_fact preservando el orden estable.
        from collections import OrderedDict

        por_factura: OrderedDict[int, list[dict]] = OrderedDict()
        for ap in aplicaciones:
            id_fact = ap.get("id_fact")
            imp_ap = float(ap.get("importe") or 0)
            if not id_fact or imp_ap == 0:
                continue
            por_factura.setdefault(int(id_fact), []).append(ap)

        for id_fact, aps in por_factura.items():
            # FOR UPDATE para serializar contra aplicar/desaplicar concurrentes
            # sobre la misma factura. Orden estable (id_fact ASC) → no deadlock.
            f = db.fetch_one(
                "SELECT id_factura, numf, importe, abono, retencion "
                "  FROM scintela.factura WHERE id_factura = %s FOR UPDATE",
                (id_fact,),
                conn=conn,
            )
            if not f:
                continue
            sum(float(a.get("importe") or 0) for a in aps)
            # Borrar TODAS las aplicaciones del viejo a esta factura en
            # un solo statement (más limpio que N DELETEs por id).
            id_chequesxfact = [int(a["id_chequexfact"]) for a in aps]
            placeholder = ",".join(["%s"] * len(id_chequesxfact))
            db.execute(
                f"DELETE FROM scintela.chequesxfact WHERE id_chequexfact IN ({placeholder})",
                tuple(id_chequesxfact),
                conn=conn,
            )
            # Estado post-migración: el abono neto no cambia (-sum + sum = 0),
            # pero rehacemos los cálculos a partir del estado actual de la
            # factura para resistir cualquier drift.
            importe_f = float(f.get("importe") or 0)
            abono_actual = float(f.get("abono") or 0)
            # Tras DELETE, abono lógico = abono_actual - sum_imp; tras INSERT
            # de las nuevas filas, abono lógico = abono_actual (idempotente).
            nuevo_abono = abono_actual  # neto cero
            nuevo_saldo = _fact_q.saldo_de(
                importe_f, nuevo_abono, f.get("retencion")
            )
            # Criterio estricto: acá NO vale el "olvidar saldo" de $0,50 de
            # la cobranza; si la dueña quiere olvidar un residuo, usa el
            # toggle explícito del form. La regla del signo es la de siempre
            # (ver _fact_q.stat_de): un saldo a favor NO totaliza.
            nuevo_stat_f = _fact_q.stat_de(nuevo_saldo, nuevo_abono, tol=0.01)
            # INSERT por aplicación (preservamos granularidad histórica).
            for ap in aps:
                imp_ap = float(ap.get("importe") or 0)
                db.execute(
                    """
                    INSERT INTO scintela.chequesxfact
                        (id_cheque, id_fact, fechaing, codigo_cli, importe,
                         no_banco, abono_f, saldo_f, stat_f, usuario_crea)
                    VALUES (%s, %s, CURRENT_DATE, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        id_cheque_nuevo,
                        id_fact,
                        ch_viejo.get("codigo_cli"),
                        imp_ap,
                        ch_viejo.get("no_banco"),
                        nuevo_abono,
                        nuevo_saldo,
                        nuevo_stat_f,
                        usuario,
                    ),
                    conn=conn,
                )
                aplicaciones_migradas += 1
            # UN solo UPDATE por factura.
            db.execute(
                "UPDATE scintela.factura SET stat=%s, abono=%s, saldo=%s, "
                "    usuario_modifica=%s WHERE id_factura=%s",
                (nuevo_stat_f, nuevo_abono, nuevo_saldo, usuario, id_fact),
                conn=conn,
            )

        # 3) Marcar el viejo como reemplazado.
        # TMT 2026-05-15 (re-audit H1): NO zeroamos importe — necesitamos
        # la cara original del cheque para auditoría, mov_doble, validador
        # de saldos. Sólo cambiamos stat y observación.
        marca = (
            f"[X] reemplazado por nuevo cheque #{nuevo_no_cheque} "
            f"(id #{id_cheque_nuevo}) {today_ec().isoformat()}"
        )
        if motivo:
            marca += f" — {motivo[:80]}"
        db.execute(
            "UPDATE scintela.cheque "
            "SET stat='X', fechaout=CURRENT_DATE, "
            "    observacion = RIGHT(COALESCE(observacion || ' | ', '') || %s, %s), "
            "    usuario_modifica=%s, fecha_modifica=CURRENT_TIMESTAMP "
            "WHERE id_cheque=%s",
            (marca, _OBS_CAP, usuario, id_cheque_viejo),
            conn=conn,
        )

        # 4) Anular soft posdat hermana si existía. TMT 2026-05-15
        # (re-audit C2): el código anterior hacía HARD DELETE con la
        # condición `prov = codigo_cli`, lo cual es INCORRECTO — posdat.prov
        # es el código del PROVEEDOR (no cliente). El delete era no-op o,
        # peor, podía borrar posdats de proveedores ajenos si por casualidad
        # algún código colisionaba con un codigo_cli. Además, migration 0027
        # introdujo soft-delete: ahora se anula vía UPDATE anulada=TRUE.
        # En la práctica los cheques de clientes NO tienen posdat hermana
        # (los cheques propios emitidos sí — pero esos están en stat 'B'
        # o similar, no reemplazables). Por seguridad dejamos el statement
        # pero corregido: sólo posdats vinculadas al cheque viejo (num=id),
        # banc=0, sin filtro de prov (que era el bug).
        db.execute(
            "UPDATE scintela.posdat "
            "   SET anulada = TRUE, "
            "       motivo_anulacion = LEFT(%s, 200), "
            "       fecha_anulacion = CURRENT_TIMESTAMP, "
            "       usuario_modifica = %s "
            " WHERE COALESCE(banc, 0) = 0 AND num = %s "
            "   AND (anulada IS NOT TRUE OR anulada IS NULL)",
            (
                f"reemplazo cheque #{id_cheque_viejo}→#{id_cheque_nuevo}",
                usuario,
                id_cheque_viejo,
            ),
            conn=conn,
        )

        # 5) mov_doble del reemplazo. TMT 2026-05-15 (re-audit H1):
        # NO pasamos `id_original` apuntando al alta del cheque viejo —
        # eso marcaba el alta original como `estado='reversado'`,
        # confundiendo "alta deshecha" con "primer reemplazo aplicado".
        # cheque_reemplazo es su propio evento contable independiente:
        # ni reversa el alta (el cheque viejo sigue habiendo existido,
        # con stat='X' como marca), ni invalida el mov_doble del alta.
        # El link viejo→nuevo queda en metadata + cheque.id_cheque_padre.
        import mov_doble as _md

        _md.registrar(
            conn=conn,
            tipo="cheque_reemplazo",
            origen_table="cheque",
            origen_id=id_cheque_viejo,
            destino_table="cheque",
            destino_id=id_cheque_nuevo,
            importe=importe_nuevo,
            fecha=today_ec(),
            concepto=(
                f"REEMPLAZO cheque #{ch_viejo.get('no_cheque') or id_cheque_viejo} "
                f"→ #{nuevo_no_cheque}" + (f" — {motivo}" if motivo else "")
            )[:200],
            usuario=usuario,
            metadata={
                "id_cheque_viejo": id_cheque_viejo,
                "id_cheque_nuevo": id_cheque_nuevo,
                "no_cheque_viejo": ch_viejo.get("no_cheque"),
                "no_cheque_nuevo": nuevo_no_cheque,
                "importe_viejo": importe_viejo,
                "importe_nuevo": importe_nuevo,
                "aplicaciones_migradas": aplicaciones_migradas,
                "stat_previo": stat_prev,
                "motivo": motivo or "",
            },
            id_original=None,  # ver comentario arriba
        )

    return {
        "id_cheque_viejo": id_cheque_viejo,
        "id_cheque_nuevo": id_cheque_nuevo,
        "no_cheque_nuevo": nuevo_no_cheque,
        "no_cheque_viejo": ch_viejo.get("no_cheque"),
        "importe_viejo": importe_viejo,
        "importe_nuevo": importe_nuevo,
        "aplicaciones_migradas": aplicaciones_migradas,
        "motivo": motivo,
    }


def por_id(id_cheque: int) -> dict | None:
    """Cheque por id_cheque interno O por no_cheque visible.

    Tamara 2026-05-23: los links del historial/cheques usan el no_cheque
    real en la URL (ej. 1234) en lugar del id_cheque interno. Esta función
    acepta ambos — prioriza no_cheque si hay match y fallback a id_cheque.
    """
    # TMT 2026-08-06: `nota_usuario` es columna nueva y el deploy no corre
    # migraciones — bootstrap en caliente antes de SELECTearla.
    _nota_usuario.bootstrap_columna()
    return db.fetch_one(
        """
        SELECT c.id_cheque, c.no_cheque, c.fecha, c.fechad, c.fechaing, c.fechaout,
               c.fecha_recibido, c.fecha_crea, c.fecha_postergacion, c.fechad_original,
               __DIA_INGRESO__ AS dia_ingreso,
               c.codigo_cli, c.importe, c.stat, c.no_banco,
               c.banco AS banco_texto, c.prov, c.clave,
               c.numero_transaccion, c.id_cheque_padre, c.pasaconta,
               -- TMT 2026-05-27 dueña: doc_banco editable inline (separado
               -- del no_cheque). Card propio en detalle.
               c.doc_banco,
               c.nota_usuario,
               COALESCE(cli.nombre, '') AS cliente,
               cli.ruc, cli.telefono,
               -- TMT 2026-07-07: espejo de anticipo (NB=98 negativo o banco
               -- texto 'ANTICIPO') dice ANTICIPO, no el 'UKN' del catálogo.
               CASE
                 WHEN c.no_banco = 98
                      AND (UPPER(TRIM(COALESCE(c.banco, ''))) = 'ANTICIPO'
                           OR COALESCE(c.importe, 0) < 0)
                 THEN 'ANTICIPO'
                 ELSE COALESCE(bco.nombre, c.banco)
               END AS banco
          FROM scintela.cheque c
          LEFT JOIN scintela.cliente cli ON cli.codigo_cli = c.codigo_cli
          LEFT JOIN scintela.banco   bco ON bco.no_banco   = c.no_banco
         WHERE c.no_cheque::text = %s OR c.id_cheque = %s
         ORDER BY (c.no_cheque::text = %s) DESC, c.id_cheque ASC
         LIMIT 1
        """.replace("__DIA_INGRESO__", SQL_DIA_INGRESO),
        (str(id_cheque), id_cheque, str(id_cheque)),
    )


def hijos(id_cheque: int) -> list[dict]:
    """Cheques hijo (espejos de anticipo) creados desde este cheque.

    Un cheque puede tener un espejo (importe negativo) que se aplica a
    facturas futuras del mismo cliente. La FK es `id_cheque_padre`. Esta
    query devuelve la lista — vacía si no hubo anticipo. TMT 2026-05-14
    (#28).
    """
    return (
        db.fetch_all(
            """
        SELECT id_cheque, no_cheque, importe, stat, fecha, fechad
          FROM scintela.cheque
         WHERE id_cheque_padre = %s
         ORDER BY id_cheque
        """,
            (id_cheque,),
        )
        or []
    )


def depositar_lote(
    *,
    ids_cheques: list[int],
    no_banco: int,
    fecha_deposito: date | None = None,
    concepto: str | None = None,
    usuario: str = "web",
) -> dict:
    """Deposita varios cheques en un solo banco, en una sola transacción.

    Reemplaza el flujo `BANCOS.PRG > DEPOSITOS` del legacy: vas, marcás N
    cheques de cartera (stat='Z') y los enviás al banco. Para cada cheque:

      1. UPDATE cheque SET stat='D', fechaing=fecha_deposito
      2. INSERT en transacciones_bancarias (documento='DE')
      3. INSERT en chequextransaccion para enlazar

    Devuelve dict con `n_depositados`, `total`, `id_transacciones`.

    Falla en bloque: si un solo cheque no se puede depositar (ya está
    depositado, no existe, etc.), aborta toda la operación.
    """
    if not ids_cheques:
        raise ValueError("Debe seleccionar al menos un cheque.")
    if not no_banco:
        raise ValueError("Banco destino requerido.")
    fecha_deposito = fecha_deposito or today_ec()
    asegurar_fecha_abierta(fecha_deposito)

    # Validar el banco existe
    banco_row = db.fetch_one(
        "SELECT no_banco, COALESCE(nombre, '') AS nombre FROM scintela.banco WHERE no_banco = %s",
        (no_banco,),
    )
    if not banco_row:
        raise ValueError(f"Banco no_banco={no_banco} no existe.")
    banco_nombre = banco_row.get("nombre") or f"Banco {no_banco}"

    # Validar todos los cheques antes de tocar nada.
    # TMT 2026-05-26 — incluir doc_banco para propagarlo a numreferencia
    # del movimiento bancario (rule #1 del matcher de conciliación).
    placeholder = ",".join(["%s"] * len(ids_cheques))
    rows = (
        db.fetch_all(
            f"""
        SELECT id_cheque, no_cheque, codigo_cli, importe, stat, fechad, doc_banco
        FROM scintela.cheque
        WHERE id_cheque IN ({placeholder})
        ORDER BY id_cheque
        """,
            tuple(ids_cheques),
        )
        or []
    )
    if len(rows) != len(set(ids_cheques)):
        raise ValueError(f"Algunos cheques no existen ({len(rows)} de {len(set(ids_cheques))} encontrados).")
    no_depositables = [r for r in rows if (r.get("stat") or "").upper() not in STATS_DEPOSITABLES]
    if no_depositables:
        ejemplos = ", ".join(f"#{r['id_cheque']} (stat={r.get('stat')})" for r in no_depositables[:3])
        raise ValueError(
            f"{len(no_depositables)} cheque(s) no son depositables: {ejemplos}"
            f"{'…' if len(no_depositables) > 3 else ''}"
        )
    # TMT 2026-05-17: la validación que bloqueaba `fechad > fecha_deposito`
    # fue removida. La dueña depósita cuando quiere — el banco acepta
    # cheques con fechad posterior (algunos clientes aceptan, otros lo
    # rebotan, pero esa es decisión de campo, no nuestra). Si igual querés
    # ver cuáles eran post-fechados, quedan registrados en el cheque con
    # `fechad` original (que ahora se ve en la lista junto a "Postergada").
    # Antes el código lanzaba ValueError; ahora deja seguir.
    total = sum(float(r.get("importe") or 0) for r in rows)
    id_transacciones: list[int] = []

    # Importamos acá para evitar ciclo en bootstrap.
    import bank_helpers

    with db.tx() as conn, conn.cursor() as cur:
        # 1) UPDATE cheques en bloque. TMT 2026-07-25 (dueña, copiar dBase
        # BANCOS.PRG DEPOBAN): la letra del depósito RECUERDA de dónde venía el
        # cheque — cartera (Z/P) → 'B'; un DEVUELTO (1/2/3) que se re-deposita →
        # 'V' (protestado vuelto a depositar). El banco lo lleva `no_banco`, así
        # que no hacen falta las letras W/I/J/K del legacy (encodaban el otro
        # banco). El CASE lee el stat PREVIO (antes del UPDATE).
        cur.execute(
            f"""
            UPDATE scintela.cheque
               SET stat = CASE WHEN stat IN ('1','2','3') THEN 'V' ELSE 'B' END,
                   -- TMT 2026-08-05: la fecha de depósito va a `fechaout`
                   -- (salida de cartera), no a `fechaing` (ingreso). Ver
                   -- SQL_DIA_SALIDA arriba.
                   fechaout = %s,
                   no_banco = %s,
                   banco = %s,
                   usuario_modifica = %s,
                   fecha_modifica = CURRENT_TIMESTAMP
             WHERE id_cheque IN ({placeholder})
            """,
            (fecha_deposito, no_banco, banco_nombre[:30], usuario[:50], *ids_cheques),
        )

        # 2) INSERT transaccion bancaria + chequextransaccion por cada cheque.
        # Usamos bank_helpers.insert_movimiento_bancario en vez de un INSERT
        # raw porque calcula el `saldo` running de la tabla. Sin eso, la
        # columna saldo queda NULL y el balance lee 0 en bancos (bug TMT
        # 2026-05-11: "deposité un cheque y no se sumó a bancos"). dBase
        # paridad: el running saldo es la fuente de verdad del saldo banco.
        # 2) UN movimiento bancario CONSOLIDADO por el TOTAL del lote
        # (paridad dBase "dep.N ch.") + N links chequextransaccion al MISMO mov.
        # TMT 2026-06-29 (dueña): "cuando es un lote a depositar, agrupar en un
        # total" → conciliás contra un monto grande; el cruce N→1 contra las N
        # líneas que el banco genera ya lo soporta la conciliación. (Antes se
        # creaba un movimiento por cheque.) El rebote/anulado de un cheque del
        # grupo compensa por el importe del CHEQUE (no del mov), así que el
        # consolidado no se rompe.
        import mov_doble as _md

        positivos = [r for r in rows if float(r.get("importe") or 0) > 0]
        total_pos = round(sum(float(r.get("importe") or 0) for r in positivos), 2)
        if positivos:
            n_pos = len(positivos)
            # TMT 2026-07-22 (dueña): cuando el lote es UN solo cheque, incluir
            # N° de cheque + cliente en el concepto (como el path individual
            # transicionar_stat: "Dep cheque {no} {cli}") para que la
            # conciliación lo muestre legible. Mantiene el prefijo "dep.1 ch."
            # (paridad dBase); el display de conciliación resuelve el resto vía
            # chequextransaccion para lotes ya existentes.
            if concepto:
                concepto_dep = concepto.strip()[:50]
            elif n_pos == 1:
                _r0 = positivos[0]
                concepto_dep = (
                    f"dep.1 ch. {_r0.get('no_cheque') or ''} "
                    f"{_r0.get('codigo_cli') or ''}"
                ).strip()[:50]
            else:
                concepto_dep = f"dep.{n_pos} ch."
            mov = bank_helpers.insert_movimiento_bancario(
                conn,
                no_banco=no_banco,
                no_cta=None,
                fecha=fecha_deposito,
                documento="DE",
                importe=total_pos,
                concepto=concepto_dep,
                prov=None,
                numreferencia=None,
                stat="A",
                usuario=usuario,
            )
            id_t = mov.get("id_transaccion")
            if id_t:
                id_t = int(id_t)
                id_transacciones.append(id_t)
                for r in positivos:
                    imp = float(r.get("importe") or 0)
                    cur.execute(
                        """
                        INSERT INTO scintela.chequextransaccion
                            (id_cheque, id_transaccion, fecha, stat_ch, usuario_crea)
                        VALUES (%s, %s, %s, 'D', %s)
                        """,
                        (r["id_cheque"], id_t, fecha_deposito, usuario[:50]),
                    )
                    _md.registrar(
                        conn=conn,
                        tipo="cheque_depositado",
                        origen_table="cheque",
                        origen_id=int(r["id_cheque"]),
                        destino_table="transacciones_bancarias",
                        destino_id=id_t,
                        importe=imp,
                        fecha=fecha_deposito,
                        concepto=(
                            f"Dep. cheque {r.get('no_cheque') or '#' + str(r['id_cheque'])} "
                            f"{r.get('codigo_cli') or ''}"
                        ).strip()[:200],
                        usuario=usuario,
                        metadata={
                            "id_cheque": int(r["id_cheque"]),
                            "id_transaccion": id_t,
                            "no_banco": no_banco,
                            "banco_nombre": banco_nombre,
                            "consolidado": True,
                            "n_grupo": n_pos,
                        },
                    )

        # 3) Los NEGATIVOS. TMT 2026-08-14 (dueña: "¡cómo vas a ignorar
        # negativo en silencio!"). Hasta hoy este bloque no existía: el lote
        # filtraba `positivos` y a los de importe negativo —el espejo de una
        # devolución o de un saldo a favor— les cambiaba el estado a
        # depositado y NO les creaba el movimiento. El cheque quedaba marcado
        # como si hubiera entrado al banco, sin plata detrás y sin nada que
        # conciliar: peor que fallar, porque no falla, miente.
        #
        # No se consolidan como los positivos: un depósito agrupa varios
        # cheques en UN crédito, pero cada devolución es un cargo propio del
        # banco, con su propia línea en el extracto. Uno por cheque, así se
        # cruzan de a uno.
        negativos = [r for r in rows if float(r.get("importe") or 0) < 0]
        for r in negativos:
            imp = float(r.get("importe") or 0)
            mov_neg = bank_helpers.insert_movimiento_bancario(
                conn,
                no_banco=no_banco,
                no_cta=None,
                fecha=fecha_deposito,
                # ND: el banco CARGA una devolución, no la deposita. Mismo
                # criterio que `transicionar_stat`.
                documento="ND",
                importe=abs(imp),
                concepto=(
                    f"Devol. cheque {r.get('no_cheque') or ''} "
                    f"{r.get('codigo_cli') or ''}"
                ).strip()[:50],
                prov=r.get("codigo_cli"),
                numreferencia=(r.get("doc_banco") or "").strip()
                              or str(r["id_cheque"]),
                stat="A",
                usuario=usuario,
            )
            id_tn = mov_neg.get("id_transaccion")
            if not id_tn:
                continue
            id_tn = int(id_tn)
            id_transacciones.append(id_tn)
            cur.execute(
                """
                INSERT INTO scintela.chequextransaccion
                    (id_cheque, id_transaccion, fecha, stat_ch, usuario_crea)
                VALUES (%s, %s, %s, 'D', %s)
                """,
                (r["id_cheque"], id_tn, fecha_deposito, usuario[:50]),
            )
            _md.registrar(
                conn=conn,
                tipo="cheque_depositado",
                origen_table="cheque",
                origen_id=int(r["id_cheque"]),
                destino_table="transacciones_bancarias",
                destino_id=id_tn,
                importe=imp,
                fecha=fecha_deposito,
                concepto=(
                    f"Devol. cheque {r.get('no_cheque') or '#' + str(r['id_cheque'])} "
                    f"{r.get('codigo_cli') or ''}"
                ).strip()[:200],
                usuario=usuario,
                metadata={
                    "id_cheque": int(r["id_cheque"]),
                    "id_transaccion": id_tn,
                    "no_banco": no_banco,
                    "banco_nombre": banco_nombre,
                    "consolidado": False,
                    "negativo": True,
                },
            )

    return {
        "n_depositados": len(rows),
        "total": total,
        "no_banco": no_banco,
        "banco_nombre": banco_nombre,
        "id_transacciones": id_transacciones,
        "fecha_deposito": fecha_deposito,
        "ids_cheques": ids_cheques,
    }


def aplicaciones(id_cheque: int) -> list[dict]:
    """En qué facturas se aplicó este cheque."""
    return db.fetch_all(
        """
        SELECT cxf.id_chequexfact, cxf.id_fact, cxf.fechaing, cxf.tipo,
               cxf.importe AS aplicado, cxf.abono_f, cxf.saldo_f, cxf.stat_f,
               f.numf, f.numf_completo, f.fecha AS fact_fecha,
               f.importe AS fact_importe, f.saldo AS fact_saldo, f.stat AS fact_stat
        FROM scintela.chequesxfact cxf
        LEFT JOIN scintela.factura f ON f.id_factura = cxf.id_fact
        WHERE cxf.id_cheque = %s
        ORDER BY cxf.fechaing
        """,
        (id_cheque,),
    )


def boleta_deposito(
    *,
    fecha: date,
    no_banco: int,
) -> dict:
    """Reconstruye una boleta de depósito (BOLEPICH/BOLEIN, BANCOS.PRG:1250-1359).

    Agrupa los cheques que se depositaron a un banco en una fecha:
        - Se levantan las filas de `transacciones_bancarias` con
          `documento='DE'`, `no_banco=<destino>`, `fecha=<dia>`.
        - Cada DE referencia un cheque vía `chequextransaccion`.
        - Cuenta destino: leemos `scintela.banco` para el nombre; si no
          existe `no_cta`, fallback hardcoded (Pichincha=42000867-4,
          Internacional=60484-9).

    Devuelve dict con:
        - banco_nombre, no_banco, no_cuenta
        - fecha
        - cheques: lista de {no_cheque, banco_emisor, cliente, importe,
                              codigo_cli, id_cheque}
        - total
        - n_cheques
    """
    # Banco destino
    banco_row = db.fetch_one(
        "SELECT no_banco, COALESCE(nombre, '') AS nombre FROM scintela.banco WHERE no_banco = %s",
        (no_banco,),
    )
    if not banco_row:
        raise ValueError(f"Banco no_banco={no_banco} no existe.")
    banco_nombre = banco_row.get("nombre") or f"Banco {no_banco}"
    nombre_upper = banco_nombre.upper()
    # Cuenta destino — preferimos lo que hay en banco; fallback hardcoded
    # según paridad con BANCOS.PRG L1197 (cuenta "42000867-4"/"60484-9").
    no_cuenta = None
    # scintela.banco no tiene columna `cuenta` en el esquema actual — usamos
    # fallback siempre. Si en el futuro se agrega, leer aquí.
    if "PICHINC" in nombre_upper:
        no_cuenta = "42000867-4"
    elif "INTER" in nombre_upper:
        no_cuenta = "60484-9"

    # Cheques depositados ese día a ese banco vía chequextransaccion +
    # transacciones_bancarias.
    cheques = (
        db.fetch_all(
            """
        SELECT c.id_cheque, c.no_cheque,
               COALESCE(bco_e.nombre, c.banco, '') AS banco_emisor,
               c.no_banco AS banco_emisor_id,
               c.codigo_cli,
               COALESCE(cli.nombre, '') AS cliente,
               c.importe,
               t.id_transaccion,
               t.fecha AS fecha_deposito,
               t.documento
          FROM scintela.transacciones_bancarias t
          JOIN scintela.chequextransaccion cxt
            ON cxt.id_transaccion = t.id_transaccion
          JOIN scintela.cheque c ON c.id_cheque = cxt.id_cheque
          LEFT JOIN scintela.cliente cli ON cli.codigo_cli = c.codigo_cli
          LEFT JOIN scintela.banco   bco_e ON bco_e.no_banco = c.no_banco
         WHERE t.documento = 'DE'
           AND t.no_banco  = %s
           AND t.fecha     = %s
         ORDER BY c.importe DESC, c.id_cheque
        """,
            (no_banco, fecha),
        )
        or []
    )

    total = sum(float(c.get("importe") or 0) for c in cheques)
    return {
        "banco_nombre": banco_nombre,
        "no_banco": no_banco,
        "no_cuenta": no_cuenta,
        "fecha": fecha,
        "cheques": cheques,
        "total": total,
        "n_cheques": len(cheques),
    }


def depositos(id_cheque: int) -> list[dict]:
    """Depósitos de este cheque vía chequextransaccion."""
    return db.fetch_all(
        """
        SELECT cxt.id_chequextransacc, cxt.fecha, cxt.stat_ch,
               t.id_transaccion, t.documento, t.concepto, t.importe AS t_importe,
               t.no_banco, COALESCE(b.nombre, '') AS banco
        FROM scintela.chequextransaccion cxt
        LEFT JOIN scintela.transacciones_bancarias t ON t.id_transaccion = cxt.id_transaccion
        LEFT JOIN scintela.banco b ON b.no_banco = t.no_banco
        WHERE cxt.id_cheque = %s
        ORDER BY cxt.fecha
        """,
        (id_cheque,),
    )


# Mapping de filtro de estado (?estado= en la URL) → tuplas de cheque.stat.
# Vocabulario canónico (ver docstring del módulo). Las categorías son las que
# muestra el menú de filtros de /cheques.
#
# Compatibilidad con datos legacy:
#   - Filas con 'A' (acreditado) — históricas, ya no se generan. Se muestran
#     bajo "depositados" porque su semántica era "cheque cobrado en banco".
#   - Filas con 'R' (rebotado genérico) — se muestran bajo "devueltos".
#     Reversiones nuevas escriben '1' o '3' según el caso.
STATS = {
    # TMT 2026-07-22 (dueña): "Cartera" ahora agrupa los cheques que siguen
    # siendo un valor por cobrar y todavía se pueden depositar/re-depositar:
    # Z (ingresado sin movimiento) + P (postergado) + 1/2 (rebote 1°/2°, se
    # re-presentan al banco). Antes era solo ("Z",). Subconjunto coherente de
    # `cartera_total` (que además suma 3 y D).
    "cartera": ("Z", "P", "1", "2"),
    # TMT 2026-07-31 (dueña): V (protestado vuelto a depositar) TAMBIÉN es
    # un depósito — crea su DE en el banco. Sin esto no aparecía en NINGUNA
    # solapa (ni Depositados ni Cartera total): sólo en "Todos los estados".
    # ⭐ TMT 2026-08-11: se derivaba de una tupla escrita a mano y le faltaba la
    # `I` — un cheque depositado en el INTERNACIONAL no aparecía en esta solapa
    # (ni en ninguna otra: sólo en "Todos los estados"). Es el mismo agujero que
    # tuvo la V hasta el 31/07. Ahora sale de la tabla: todo lo que está en
    # banco, más el efectivo, que también salió de cartera.
    "depositados": STATS_DEPOSITADO + EN_CAJA,
    "devueltos": ("1", "2", "3", "R"),  # rebotes (3=segundo rebote)
    "daniela": ("D",),  # gestión Daniela
    "postergados": ("P",),  # postergados con fecha nueva
    "endosados": ("E",),  # endosados a proveedor (TMT 2026-05-12)
    "eliminados": ("X",),  # reversados / anulados
    "internacional": ("V",),  # legacy banco Inter — no usar
    # TMT 2026-05-19 v2 (pedido dueña): "Cartera total" = suma de los 4
    # buckets visibles en pantalla — Cartera Z + Postergados + Daniela
    # + Devueltos. NO incluye Depositados (B/A) — esos ya están "en el
    # banco" desde la perspectiva operativa.
    "cartera_total": ("Z", "P", "1", "2", "3", "D"),
}

# Subconjunto de stats que se consideran "vivos" para cartera/cobranza:
# son los que todavía nos representan algo a cobrar (incluye legacy A para
# compatibilidad — facturas viejas referencian estos cheques). 'E' (endosado)
# NO está vivo — ya salió de nuestra cartera.
# (Acá había un STATS_VIVOS con OTROS miembros: Python se queda con el último
# binding del módulo, así que el de más abajo —("Z","1","2","3","P","D"), el que
# quieren sus dos usos— era el que mandaba y éste sólo confundía al que leía.
# Borrado el 11/08/2026; ruff no avisa de una constante redefinida.)


# Transiciones legales por stat actual — TMT 2026-05-19, pedido Tamara.
# Cada entrada es un dict con:
#   stat_destino: char del stat al que va
#   label:        texto user-facing para el dropdown
#   kind:         "POST" (form submit a cheques.transicionar) o
#                 "WIZARD" (link GET al wizard correspondiente)
#   endpoint:     nombre de view Flask (solo si kind=WIZARD)
#   motivo:       True si el endpoint requiere motivo (POST con confirm)
#
# La regla canónica: si la transición requiere data extra (fecha nueva,
# proveedor endoso, motivo de rebote) → kind=WIZARD. Si es un cambio de
# stat seco → kind=POST.
TRANSICIONES_LEGALES: dict[str, list[dict]] = {
    # TMT 2026-05-19 v8 — pedido dueña:
    #   1. Postergar fecha: kind='POSTERGAR' para que el template muestre
    #      un date input inline en el popover en vez de redirigir al wizard.
    #   2. "Endosar a proveedor": removido del dropdown (no se usa más).
    #
    # Z = en cartera, recién cargado.
    # TMT 2026-05-20 — pedido dueña: agregar 1 y 2 al dropdown de
    # transiciones ("no veo 1 y 2 en el dropdown"). Permiten marcar el
    # cheque como devuelto directo sin pasar por deposito + reverso.
    "Z": [
        {"stat_destino": "V", "label": "Depositar marcando V (Pichincha, hoy)", "corto": "Depositar (V)", "kind": "POST", "endpoint": "cheques.transicionar"},
        {
            # TMT 2026-06-29 (dueña): el →B del dropdown debe DEPOSITAR el cheque
            # directo (Pichincha, hoy) con 1 confirmación, no mandar al wizard de
            # lote con 0 seleccionados (parecía que "no dejaba cambiar el estado").
            # El botón "Depositar lote" sigue para depósitos en lote con fecha.
            "stat_destino": "B",
            "label": "Depositar en Pichincha (hoy)",
            "kind": "POST",
            "endpoint": "cheques.transicionar",
        },
        {
            "stat_destino": "P",
            "label": "Postergar fecha",
            "kind": "POSTERGAR",
            "endpoint": "cheques.postergar",
        },
        {"stat_destino": "D", "label": "Pasar a Daniela", "kind": "POST", "endpoint": "cheques.transicionar"},
        {"stat_destino": "1", "label": "Devuelto", "kind": "POST", "endpoint": "cheques.transicionar"},
        {"stat_destino": "2", "label": "Devuelto (2°)", "kind": "POST", "endpoint": "cheques.transicionar"},
        {
            "stat_destino": "X",
            "label": "Anular (error carga)",
            "kind": "WIZARD",
            "endpoint": "cheques.anular_error_carga",
        },
    ],
    # B = depositado en banco. Volver a cartera (no se depositó) o marcar rebote.
    # TMT 2026-07-07 dueña: "si marcamos depositado y al final no lo depositamos".
    "B": [
        {
            "stat_destino": "Z",
            "label": "Volver a cartera (no se depositó)",
            "kind": "WIZARD",
            "endpoint": "cheques.deshacer_deposito",
        },
        # TMT 2026-07-15 (dueña): "que cheques de estado B se puedan pasar a P" y
        # "cuando pongo P me tiene que hacer seleccionar la fecha a la que lo
        # postergo". P en la app = POSTERGADO (tab Postergados), no "postdatado".
        # El cheque se marcó depositado por error → lo devolvemos a POSTERGADO,
        # pidiendo la nueva fecha de depósito. Va por el wizard deshacer_deposito
        # con destino=P: NO es un relabel plano (dejaría el depósito de banco
        # colgado), REVIERTE el movimiento de banco igual que "Volver a cartera"
        # y deja el cheque en P con la fechad elegida. `siempre` = mostrarlo en el
        # dropdown aunque P no esté en TRANSICIONES_VALIDAS['B'] (el wizard valida
        # por su cuenta; la transición plana B→P sigue bloqueada en transicionar_stat).
        {
            "stat_destino": "P",
            "label": "Volver a postergado (elegí la nueva fecha)",
            "corto": "A postergado",
            "kind": "WIZARD",
            "endpoint": "cheques.deshacer_deposito",
            "url_args": {"destino": "P"},
            "siempre": True,
        },
        {
            "stat_destino": "9",
            "label": "Marcar como rebotado",
            "kind": "WIZARD",
            "endpoint": "cheques.confirmar_reverso",
        },
    ],
    "A": [
        {
            "stat_destino": "Z",
            "label": "Volver a cartera (no se depositó)",
            "kind": "WIZARD",
            "endpoint": "cheques.deshacer_deposito",
        },
        {
            "stat_destino": "9",
            "label": "Marcar como rebotado",
            "kind": "WIZARD",
            "endpoint": "cheques.confirmar_reverso",
        },
    ],
    "V": [
        # TMT 2026-07-20 (dueña): el re-depósito se protestó otra vez → vuelve
        # a "1". Etiqueta plana (la V nueva no tiene mov de banco en la app).
        {"stat_destino": "1", "label": "Protestado de nuevo", "kind": "POST", "endpoint": "cheques.transicionar"},
        {
            "stat_destino": "Z",
            "label": "Volver a cartera (no se depositó)",
            "kind": "WIZARD",
            "endpoint": "cheques.deshacer_deposito",
        },
        {
            "stat_destino": "9",
            "label": "Marcar como rebotado",
            "kind": "WIZARD",
            "endpoint": "cheques.confirmar_reverso",
        },
    ],
    # 1 / 2 = rebote en gestión.
    "1": [
        {"stat_destino": "V", "label": "Protestado vuelto a depositar", "kind": "POST", "endpoint": "cheques.transicionar"},
        {
            "stat_destino": "P",
            "label": "Postergar fecha",
            "kind": "POSTERGAR",
            "endpoint": "cheques.postergar",
        },
        {"stat_destino": "D", "label": "Pasar a Daniela", "kind": "POST", "endpoint": "cheques.transicionar"},
        {
            "stat_destino": "X",
            "label": "Anular (incobrable)",
            "kind": "WIZARD",
            "endpoint": "cheques.anular_error_carga",
        },
    ],
    "2": [
        {"stat_destino": "V", "label": "Protestado vuelto a depositar", "corto": "Re-depositar", "kind": "POST", "endpoint": "cheques.transicionar"},
        {
            "stat_destino": "P",
            "label": "Postergar fecha",
            "kind": "POSTERGAR",
            "endpoint": "cheques.postergar",
        },
        {"stat_destino": "D", "label": "Pasar a Daniela", "kind": "POST", "endpoint": "cheques.transicionar"},
        {
            "stat_destino": "X",
            "label": "Anular (incobrable)",
            "kind": "WIZARD",
            "endpoint": "cheques.anular_error_carga",
        },
    ],
    # D = Daniela.
    # TMT 2026-07-09 (dueña): agregar →B (depositar) — TRANSICIONES_VALIDAS['D']
    # ya lo permite; faltaba en el dropdown (Daniela trajo el cheque, se deposita).
    "D": [
        {"stat_destino": "V", "label": "Depositar marcando V (Pichincha, hoy)", "corto": "Depositar (V)", "kind": "POST", "endpoint": "cheques.transicionar"},
        {"stat_destino": "B", "label": "Depositar en Pichincha (hoy)", "kind": "POST", "endpoint": "cheques.transicionar"},
        {
            "stat_destino": "P",
            "label": "Postergar fecha",
            "kind": "POSTERGAR",
            "endpoint": "cheques.postergar",
        },
    ],
    # P = postergado. Daniela, re-postergar, o marcar devuelto (dueña 2026-06-16).
    # TMT 2026-07-09 (dueña): "no me deja depositar cheques desde la pantalla".
    # Faltaba →B en el dropdown de P (postdatado que llegó su fecha → depositar
    # en Pichincha hoy). El backend (TRANSICIONES_VALIDAS['P']) ya lo permitía;
    # solo faltaba ofrecerlo en la UI. Va primero, con confirmación (como Z).
    "P": [
        {"stat_destino": "V", "label": "Depositar marcando V (Pichincha, hoy)", "corto": "Depositar (V)", "kind": "POST", "endpoint": "cheques.transicionar"},
        {"stat_destino": "B", "label": "Depositar en Pichincha (hoy)", "kind": "POST", "endpoint": "cheques.transicionar"},
        {"stat_destino": "D", "label": "Pasar a Daniela", "kind": "POST", "endpoint": "cheques.transicionar"},
        {
            "stat_destino": "P",
            "label": "Re-postergar (nueva fecha)",
            "kind": "POSTERGAR",
            "endpoint": "cheques.postergar",
        },
        {"stat_destino": "1", "label": "Devuelto", "kind": "POST", "endpoint": "cheques.transicionar"},
        {"stat_destino": "2", "label": "Devuelto (2°)", "kind": "POST", "endpoint": "cheques.transicionar"},
    ],
    # Estados terminales — sin transiciones disponibles.
    # TMT 2026-08-11 (dueña): el rechazo de 3ª deja de ser terminal — se puede
    # volver a depositar, como en el dBase.
    "3": [
        {"stat_destino": "V", "label": "Protestado vuelto a depositar", "corto": "Re-depositar", "kind": "POST", "endpoint": "cheques.transicionar"},
    ],
    "R": [],  # rebote terminal legacy
    "E": [],  # endosado — vive en /historial para reverso
    "X": [],  # eliminado/anulado
    "T": [],  # cobrado total
}


# (El mapa de etiquetas vive en `estados.py`, junto con el significado de cada
# estado y su familia — se importa arriba. Estaba escrito a mano en dos
# templates distintos hasta el 11/08/2026.)

# Etiquetas legibles por estado destino (para las opciones auto-generadas del
# dropdown). Los estados con movimiento tienen su propia entrada curada arriba.
_LABEL_ESTADO_DEST = {
    "Z": "En cartera",
    "P": "Postergado",
    "D": "En gestión Daniela",
    "1": "Devuelto",
    "2": "Devuelto (2°)",
    "3": "Segundo rechazo",
    "X": "Eliminar",
}


def es_rebote_real(stat_prev: str) -> bool:
    """¿Reversar un cheque en `stat_prev` es un rebote de verdad?

    Rebote real = el banco lo rechazó: vuelve a cartera como devuelto y el
    cliente queda con stop. Lo contrario es la reversa administrativa ("me
    confundí al cargarlo"), que lo elimina y no toca al cliente.

    La respuesta sale SIEMPRE de `_stat_destino_reversa`, que es la que
    ejecuta. Antes esto era una tupla aparte y se atrasó. TMT 2026-08-11.
    """
    try:
        return _stat_destino_reversa(stat_prev)[1]
    except ValueError:
        return False  # estado terminal: no se reversa, ni real ni administrativa


def transiciones_para(stat: str) -> list[dict]:
    """Transiciones que se ofrecen en el dropdown desde `stat`.

    Garantiza que el dropdown NUNCA ofrezca algo que el backend rechace: filtra
    las entradas curadas a las permitidas por TRANSICIONES_VALIDAS y auto-genera
    las de estados SIN movimiento que falten (respetando la secuencia 1→2→3).
    'Eliminar' (X) va SIEMPRE por el wizard de anulación, que reversa las
    aplicaciones a facturas (un cambio de etiqueta pelado las dejaría colgadas).
    TMT 2026-07-11 (dueña: "confirm every move makes sense, some rules apply").
    """
    s = (stat or "").upper().strip()
    permit = TRANSICIONES_VALIDAS.get(s, set())
    # 1) Entradas curadas (depósito, postergar, rebote, re-depositar…) que el
    #    backend efectivamente permite — descarta las obsoletas (ej. Z→2).
    #    Excepción: entradas con `siempre` (wizards que validan por su cuenta,
    #    ej. B→P por deshacer_deposito) se muestran aunque el destino no esté en
    #    TRANSICIONES_VALIDAS. TMT 2026-07-15.
    # ⭐ `dict(o)`: COPIA. Las entradas curadas son objetos del módulo, así que
    # escribirles encima (`destino_real`, `stat_destino`, `corto`) les dejaba el
    # dato pegado para la próxima llamada — y para todo el proceso. Se veía como
    # un test que pasa solo y falla en la suite. TMT 2026-08-11.
    base = [
        dict(o) for o in TRANSICIONES_LEGALES.get(s, [])
        if o.get("siempre") or o["stat_destino"] in permit
    ]
    ya = {o["stat_destino"] for o in base}
    # 2) Auto-generar los estados sin movimiento permitidos (menos X).
    for dest in sorted((permit & STATS_NEUTROS) - {"X"}):
        if dest in ya:
            continue
        base.append({
            "stat_destino": dest,
            "label": _LABEL_ESTADO_DEST.get(dest, dest),
            "kind": "POST",
            "endpoint": "cheques.transicionar",
        })
    # 2b) Cobrar en efectivo (C) → pasa el cheque a CAJA. NO es un estado "neutro"
    #     (mueve caja) ni tiene entrada curada, así que se agrega acá cuando el
    #     backend lo permite (cartera Z/P/D). dBase lo permite: MODIFICA.PRG
    #     `IF STI$CART .AND. STF$'9C'` → `USE CAJA` crea la entrada de caja. El
    #     template ya renderiza stat_destino='C' con su confirm ("Cobrar en caja").
    #     TMT 2026-07-26 (dueña: "si el dbase deja, dejalos").
    if "C" in permit and "C" not in ya:
        base.append({
            "stat_destino": "C",
            "label": "Cobrar en efectivo (caja)",
            "kind": "POST",
            "endpoint": "cheques.transicionar",
        })
    # 3) Rebote / sin fondos (9) → wizard de reverso (compensa banco si estaba
    #    depositado). Se ofrece siempre que el backend lo permita y no esté ya.
    if "9" in permit and "9" not in ya:
        base.append({
            "stat_destino": "9",
            "label": "Sin fondos (rebotó)",
            "kind": "WIZARD",
            "endpoint": "cheques.confirmar_reverso",
        })
    # 4) Eliminar (X) → siempre por el wizard de anulación (reversa aplicaciones).
    if "X" in permit and "X" not in ya:
        base.append({
            "stat_destino": "X",
            "label": "Eliminar",
            "kind": "WIZARD",
            "endpoint": "cheques.anular_error_carga",
        })
    # TMT 2026-07-14 (dueña "que pueda seleccionar 1"): el rebote (wizard de
    # reverso) muestra "→9" pero el estado RESULTANTE lo decide
    # _stat_destino_reversa (depositado B/A → 1 primer rebote; 1/2 → 3). La
    # dueña no entendía a dónde iba el "9", así que se lo hicimos MOSTRAR "1".
    #
    # 🚨 TMT 2026-08-11 — eso creaba DOS opciones idénticas. Desde un cheque
    # depositado (B) el menú ofrece el rebote (9, que muestra "1") Y el cambio
    # plano a "1": la lista dibujaba las dos como "→1", una al lado de la otra,
    # y la de arriba ni siquiera cambiaba el estado (es un link a un asistente).
    # `destino_real` sigue calculándose porque contesta la pregunta real de la
    # dueña —"¿en qué estado queda?"— pero va al TEXTO de la opción, no a la
    # letra: la letra es la ACCIÓN. Los dos templates lo rinden como
    # "9 Sin fondos (queda en 1)" vs "1 Devuelto".
    #
    # 🚨 Y `destino_real` se guardaba SÓLO cuando era rebote real. Desde Z/P/D/I
    # el mismo asistente hace una reversa ADMINISTRATIVA y el cheque termina en
    # 'X', pero el menú seguía diciendo "Sin fondos" y sin decir a dónde iba: la
    # opción prometía un estado al que no llegaba. La pantalla del asistente
    # distinguía bien los dos casos (_reverso_preview_cheque); el menú no.
    # Ahora el destino se guarda SIEMPRE y `es_rebote` decide cómo se llama.
    # ⭐ TMT 2026-08-11 (dueña): postergar desde un DEVUELTO (1/2) o desde
    # Daniela (D) NO cambia el estado — decisión suya del 16/06: mover la fecha
    # de cobro no borra que el cheque rebotó. Si pasara a 'P' se iría de la
    # solapa Devueltos y, peor, saldría de CHEQUES PROTESTADOS en el estado de
    # cuenta del cliente: la hoja lo mostraría como un cheque normal esperando
    # su fecha. Así que la opción muestra la letra en la que QUEDA, no una 'P'
    # a la que no va. La letra sigue siendo el idioma del menú.
    for o in base:
        if o.get("kind") == "POSTERGAR" and s not in ("Z", "P"):
            o["stat_destino"] = s
            o["corto"] = "Nueva fecha"
    for o in base:
        if o.get("endpoint") == "cheques.confirmar_reverso":
            try:
                _d, _es_reb = _stat_destino_reversa(s)
                o["destino_real"] = _d
                o["es_rebote"] = _es_reb
            except Exception:  # noqa: BLE001
                pass
    return base


def texto_opcion_estado(t: dict) -> str:
    """Cómo se LEE una opción del menú de cambiar estado.

    Existe para que la lista y la ficha no puedan divergir: eran dos armados
    distintos del mismo menú y por eso la lista terminó ofreciendo dos "→1"
    idénticos mientras la ficha mostraba bien "9 Sin fondos" y "1 Devuelto".
    Con una sola función, el invariante *ningún menú ofrece dos opciones que se
    leen igual* se puede PROBAR para todos los estados, no sólo para el que
    reportó la dueña. TMT 2026-08-11.
    """
    dest = t.get("stat_destino") or ""
    corto = (
        t.get("corto")
        or LABEL_CORTO_ESTADO.get(dest)
        or t.get("label")
        or dest
    )
    # El mismo asistente ('9') hace dos cosas distintas según de dónde venga:
    # rebote real (el banco lo rechazó) desde un depositado o un devuelto, o
    # reversa administrativa ("me confundí al cargarlo") desde cartera. Se
    # llaman distinto porque son distintas.
    if t.get("endpoint") == "cheques.confirmar_reverso" and not t.get("es_rebote", True):
        corto = "Reversar (me confundí)"
    # `destino_real` contesta "¿y en qué estado queda?" para el wizard de rebote
    # (9 → 1 si estaba depositado). Va en el TEXTO; la letra es la ACCIÓN.
    if t.get("destino_real"):
        corto = f"{corto} (queda en {t['destino_real']})"
    return corto


def texto_opcion_estado_completo(t: dict) -> str:
    """La opción como se lee entera en la lista: letra + qué hace."""
    return f"→{t.get('stat_destino') or ''} {texto_opcion_estado(t)}".strip()


def texto_opcion_estado_corto(t: dict) -> str:
    """La opción como se lee en la LISTA: sólo la letra a la que va.

    Pedido de la dueña (13/08/2026, segunda vez): en /cheques el menú de estado
    va con la letra pelada y el select angosto — la lista tiene 20 filas a la
    vista y cada renglón de texto ahí es ruido. La explicación no se pierde:
    viaja en el `title` de cada opción (`texto_opcion_estado_completo`) y la
    ficha del cheque sigue mostrando el menú largo.

    ⭐ Esto NO revive el bug del 11/08 (dos "→1" idénticos): ahí la letra que se
    dibujaba era `destino_real` —en qué estado QUEDA— y dos acciones distintas
    caían en el mismo estado. Acá la letra es siempre `stat_destino`, o sea la
    ACCIÓN, que es única por menú. El test lo prueba para TODOS los estados.
    """
    return f"→{t.get('stat_destino') or ''}".strip()


def transiciones_map() -> dict[str, list[dict]]:
    """{stat: transiciones_para(stat)} para todos los estados conocidos —
    para pasarle al template el mapa ya expandido."""
    estados = set(TRANSICIONES_VALIDAS) | set(TRANSICIONES_LEGALES)
    return {s: transiciones_para(s) for s in estados}


# Stats que pueden iniciar un depósito a banco. Z (cartera) es el flujo
# típico. P (postdatado/postergado) también es válido cuando llega la fecha
# de depósito — operacionalmente el cobranzador deposita directo sin pasar
# por Z. Cualquier otro stat origen es un bug en la UI.
STATS_DEPOSITABLES = ("Z", "P", "1", "2")  # TMT 2026-06-16 dueña: re-depositar cheques DEVUELTOS (1/2)

# Stats desde los que se puede postergar (Z, ver invariante 4 del addendum).
STATS_POSTERGABLES = ("Z",)

# ⭐ TMT 2026-08-11 — acá vivían DOS constantes escritas a mano que se habían
# quedado atrás de la realidad:
#
# · `STATS_REBOTE_REAL = ("B","1","2","A")` decidía el texto de la pantalla de
#   confirmación del reverso. Pero quién es un rebote real lo decide
#   `_stat_destino_reversa`, y ahí V/W/I/J/K TAMBIÉN lo son. La pantalla le
#   decía "reversión administrativa, no afecta al cliente" a un cheque de
#   Internacional que en realidad iba a rebotar de verdad y a ponerle stop al
#   cliente. Ahora se pregunta con `es_rebote_real()`, que llama a la función:
#   una lista que hay que mantener en paralelo se atrasa; una que se deriva, no.
#
# · `STATS_TERMINALES = ("B",)` decía que un depositado es terminal cuando
#   `TRANSICIONES_VALIDAS["B"]` ofrece tres salidas. No lo usaba nadie —era una
#   trampa esperando al próximo que la leyera de buena fe—, así que se va.


def _relabel_dep_concepto(concepto: str, n: int) -> str:
    """Reescribe el contador de un concepto de depósito consolidado
    'dep.N ch.' → 'dep.<n> ch.' cuando sacamos un cheque del lote. Si el
    concepto no matchea ese patrón, lo deja igual. TMT 2026-07-07.

    ⚠️ El FORMATO 'dep.N ch.' es contrato, no cosmética: lo parsean
    `matcher_banco._RE_AGRUPADO` / `_RE_CODIGO_INTERNO`,
    `hoja_queries._detectar_agrupado_simple` y la firma de match
    (`banco_v2_view._firma_expr`, LEFT(concepto,40)). Cambiarle el texto (p.ej.
    a 'dep. resto (…)') rompe la extracción del código de cliente, mueve el mov
    de bucket en la conciliación y, por los 17 chars extra, trunca el sufijo de
    los conceptos de lote con texto libre. Mantener el largo y la forma."""
    import re as _re
    c = concepto or ""
    return _re.sub(r"dep\.\s*\d+\s*ch\.", f"dep.{max(n, 0)} ch.", c, flags=_re.IGNORECASE)[:50]


def deshacer_deposito_cheque(
    *, id_cheque: int, usuario: str = "web", motivo: str = "", stat_destino: str = "Z",
    nueva_fechad: date | None = None,
) -> dict:
    """Saca UN cheque de su depósito y lo devuelve a cartera (Z) o postergado (P).

    TMT 2026-07-07 dueña: "si hay un cheque que marcamos depositado y al final
    no lo depositamos, cómo lo devolvemos". NO es rebote (no toca al cliente)
    ni anulación (el cheque sigue vivo) — simplemente deshace el depósito.

    TMT 2026-07-15 dueña: `stat_destino` permite devolverlo a cartera ('Z',
    default) o a POSTERGADO ('P') — mismo reverso de banco, distinto estado
    final. Cuando va a 'P' se pide `nueva_fechad` (la fecha a la que se posterga
    el depósito) y se registra la postergación igual que postergar() —
    fecha_postergacion + snapshot de fechad_original.

    Lado banco: el depósito consolidado 'dep.N ch.' baja su importe por el
    cheque y su contador N→N-1; el movimiento se elimina SÓLO si queda en ~0
    (TMT 2026-08-03: antes se borraba con sólo mirar que quedara UN link, y eso
    se llevaba puesto el resto que dejan los cheques REBOTADOS — ver el
    comentario del guard). Recalcula el saldo running del banco. Guard:
    NO toca un depósito ya conciliado (rompería la conciliación) — avisa que
    hay que desconciliar primero. Reproducible por pantalla, reversible
    (podés volver a depositar el cheque). Anda para cualquier usuario con
    cheques.transicionar (Alex, Andres, etc.), no solo la dueña.
    """
    import bank_helpers
    stat_destino = (stat_destino or "Z").upper().strip()
    if stat_destino not in ("Z", "P"):
        raise ValueError("Sólo se puede devolver a cartera (Z) o a postergado (P).")
    if stat_destino == "P" and nueva_fechad is None:
        raise ValueError("Para volver a postergado (P) hay que elegir la nueva fecha de depósito.")
    ch = db.fetch_one(
        "SELECT id_cheque, no_cheque, codigo_cli, importe, stat, no_banco "
        "FROM scintela.cheque WHERE id_cheque = %s",
        (id_cheque,),
    )
    if not ch:
        raise ValueError("El cheque no existe.")
    # 🚨 TMT 2026-08-24 — "Volver a cartera" no aplica a un depósito directo.
    # Un cheque vuelve a cartera porque el papel sigue en el cajón; un
    # DEP.PICH. no tiene papel: la plata YA está en Pichincha. Mandarlo a
    # cartera dejaba una fila esperando un depósito que no existe —y que
    # después alguien posterga para sacársela de encima— sumando a la cartera
    # del balance algo que ya está contado en el banco. Es el mismo estado
    # imposible que ahora `crear()` no deja nacer; cerrarlo en un solo lado
    # sería dejar la puerta de al lado abierta.
    if int(ch.get("no_banco") or 0) in (90, 91):
        raise ValueError(
            "Este cobro es un depósito directo: la plata ya está en el banco y "
            "no hay cheque que volver a cartera. Si el depósito no existió, "
            "anulalo por error de carga."
        )
    stat_prev = (ch.get("stat") or "").upper()
    if stat_prev not in STATS_DEPOSITADO:
        raise ValueError(
            f"El cheque no está depositado (estado {stat_prev}); no hay depósito que deshacer."
        )
    imp_ch = round(float(ch.get("importe") or 0), 2)
    bancos_recompute: set[int] = set()
    n_movs = 0
    with db.tx() as conn:
        links = db.fetch_all(
            "SELECT cxt.id_transaccion, tb.no_banco, tb.importe, tb.concepto "
            "  FROM scintela.chequextransaccion cxt "
            "  JOIN scintela.transacciones_bancarias tb "
            "    ON tb.id_transaccion = cxt.id_transaccion "
            " WHERE cxt.id_cheque = %s AND UPPER(COALESCE(tb.documento,'')) = 'DE'",
            (id_cheque,),
            conn=conn,
        ) or []
        for lk in links:
            id_t = int(lk["id_transaccion"])
            no_banco = int(lk["no_banco"])
            conc = db.fetch_one(
                "SELECT 1 FROM scintela.banco_conciliacion_match "
                "WHERE id_transaccion = %s AND deshecho_en IS NULL LIMIT 1",
                (id_t,),
                conn=conn,
            )
            if conc:
                raise ValueError(
                    f"El depósito de este cheque (mov #{id_t}) ya está conciliado con el "
                    "banco. Desconciliá primero desde la conciliación y volvé a intentar."
                )
            n = int((db.fetch_one(
                "SELECT COUNT(*) AS n FROM scintela.chequextransaccion WHERE id_transaccion = %s",
                (id_t,), conn=conn,
            ) or {}).get("n") or 0)
            conc_de = lk.get("concepto") or ""
            de_imp = round(float(lk["importe"] or 0), 2)
            nuevo_imp = round(de_imp - imp_ch, 2)
            # ── TMT 2026-08-03 (bug A, dueña) ────────────────────────────────
            # El guard era `if n <= 1 or nuevo_imp <= 0.005` → DELETE del mov.
            # Usaba el CONTEO DE LINKS como proxy de "este cheque era el único
            # del depósito", y ese proxy se rompe con el REBOTE:
            # `compensar_deposito_devuelto` le saca el link al cheque protestado
            # y DEJA el importe del 'DE' entero a propósito (el extracto muestra
            # el depósito completo + la ND del protesto, y así concilia). Un 'DE'
            # de 150 con 3 cheques del que dos rebotaron queda con UN link, y
            # "Volver a cartera" sobre el tercero (50) borraba los 150 enteros:
            # el banco perdía 100 reales.
            # Ahora se decide por IMPORTE: el mov se borra sólo si queda en ~0.
            # Que el resto sea siempre plata REAL depende de un invariante que
            # `editar()` rompía (dejaba cambiar el importe de un cheque ya
            # depositado sin tocar su 'DE', inventando un resto de la nada) —
            # por eso este fix viene junto con el guard de `editar()`.
            if nuevo_imp <= 0.005 and n > 1:
                # Quedaría en ~0 con cheques todavía agrupados: borrarlo los
                # dejaría marcados como depositados sin depósito. No adivinar.
                raise ValueError(
                    f"El depósito (mov #{id_t}) quedaría en cero pero todavía tiene "
                    f"{n - 1} cheque(s) agrupado(s). Borrarlo los dejaría marcados como "
                    "depositados sin depósito — revisá los importes antes de deshacerlo."
                )
            db.execute(
                "DELETE FROM scintela.chequextransaccion "
                "WHERE id_cheque = %s AND id_transaccion = %s",
                (id_cheque, id_t), conn=conn,
            )
            if nuevo_imp <= 0.005:
                db.execute(
                    "DELETE FROM scintela.transacciones_bancarias WHERE id_transaccion = %s",
                    (id_t,), conn=conn,
                )
            else:
                db.execute(
                    "UPDATE scintela.transacciones_bancarias "
                    "   SET importe = %s, concepto = %s "
                    " WHERE id_transaccion = %s",
                    (nuevo_imp, _relabel_dep_concepto(conc_de, n - 1), id_t),
                    conn=conn,
                )
            bancos_recompute.add(no_banco)
            n_movs += 1
        if stat_destino == "P":
            # Postergar: fija la nueva fechad y registra la postergación (igual
            # que postergar()). fechad_original = snapshot de la fechad previa.
            db.execute(
                # TMT 2026-08-05: se limpia `fechaout` (volvió a cartera), NO
                # `fechaing`: en las filas del dBase ese campo es el día que el
                # cheque ENTRÓ y borrarlo lo deja sin día de ingreso para
                # siempre. La columna "Depositado" ya no lo muestra porque el
                # template la gatea por `stat`.
                "UPDATE scintela.cheque "
                "   SET stat = 'P', fechaout = NULL, fechad = %s, "
                "       fecha_postergacion = CURRENT_DATE, "
                "       fechad_original = COALESCE(fechad_original, fechad), "
                "       usuario_modifica = %s, fecha_modifica = CURRENT_TIMESTAMP "
                " WHERE id_cheque = %s",
                (nueva_fechad, usuario, id_cheque), conn=conn,
            )
        else:
            db.execute(
                "UPDATE scintela.cheque "
                "   SET stat = %s, fechaout = NULL, "
                "       usuario_modifica = %s, fecha_modifica = CURRENT_TIMESTAMP "
                " WHERE id_cheque = %s",
                (stat_destino, usuario, id_cheque), conn=conn,
            )
        # TMT 2026-07-15 (dueña "me debería aparecer acá el movimiento que hice"):
        # dejar traza en /historial. Marca el mov_doble del depósito original
        # ('cheque_depositado') como reversado y crea la línea 'reverso_cheque_
        # depositado'. Si el depósito fue individual (sin mov_doble, aparece como
        # banco directo), igual registramos la línea de reverso (sin id_original)
        # para que la acción sea visible. Es audit-only: NO afecta flujo/balance.
        import mov_doble as _md
        _orig_md = db.fetch_one(
            "SELECT id_mov_doble FROM scintela.mov_doble "
            " WHERE tipo = 'cheque_depositado' AND origen_table = 'cheque' "
            "   AND origen_id = %s AND COALESCE(estado, 'activo') <> 'reversado' "
            " ORDER BY id_mov_doble DESC LIMIT 1",
            (id_cheque,), conn=conn,
        )
        _dest_txt = "postergado (P)" if stat_destino == "P" else "cartera (Z)"
        _md.registrar(
            conn=conn,
            tipo="reverso_cheque_depositado",
            origen_table="cheque",
            origen_id=id_cheque,
            destino_table="cheque",
            destino_id=id_cheque,
            importe=imp_ch,
            fecha=today_ec(),
            concepto=(
                f"Depósito deshecho — cheque "
                f"{ch.get('no_cheque') or '#' + str(id_cheque)} → {_dest_txt}"
            )[:200],
            usuario=usuario,
            id_original=(_orig_md.get("id_mov_doble") if _orig_md else None),
            metadata={
                "id_cheque": id_cheque,
                "stat_destino": stat_destino,
                "motivo": (motivo or "")[:200],
            },
        )
        for nb in bancos_recompute:
            anc = db.fetch_one(
                "SELECT id_transaccion AS ancla FROM scintela.transacciones_bancarias "
                "WHERE no_banco = %s ORDER BY fecha ASC, id_transaccion ASC OFFSET 1 LIMIT 1",
                (nb,), conn=conn,
            )
            if anc and anc.get("ancla"):
                bank_helpers.recompute_saldos_desde(
                    conn, no_banco=nb, no_cta=None, ancla_id=int(anc["ancla"]),
                )
    return {
        "id_cheque": id_cheque,
        "no_cheque": ch.get("no_cheque"),
        "stat_previo": stat_prev,
        "stat_nuevo": stat_destino,
        "importe": imp_ch,
        "movs_tocados": n_movs,
        "nueva_fechad": nueva_fechad if stat_destino == "P" else None,
    }




def crear(
    *,
    fecha: date,
    codigo_cli: str,
    no_cheque: str,
    importe,
    no_banco: int | None = None,
    banco_texto: str | None = None,
    fechad: date | None = None,
    fecha_recibido: date | None = None,
    stat: str | None = None,
    prov: str | None = None,
    clave: str | None = None,
    es_anticipo: bool = False,
    doc_banco: str | None = None,
    # TMT 2026-08-04 (Alex: "podemos colocar de forma manual un concepto
    # cuando sea anticipo … es más porque esto se entrega a contabilidad y no
    # van a saber qué hacer con el 'sin aplicar facturas'"). Texto corto que
    # explica un cobro que NO se aplica a facturas; se imprime en el resumen
    # de cobranza del día. Ver `modules/cheques/concepto_cobro.py`.
    concepto: str | None = None,
    usuario: str = "web",
    batch_id: str | None = None,
    # TMT 2026-07-06 (dueña): anticipo aplicado a cheques en cartera — el
    # espejo NB=98 se crea SOLO por el SOBRANTE (anticipo − Σ cancelados).
    # None = flujo clásico (espejo por el importe total del cheque).
    anticipo_espejo_importe: float | None = None,
    # TMT 2026-08-03 (dueña: "si las cargué yo están mal, ayudame a corregir").
    # Cobro en efectivo que YA está en la caja como entrada suelta
    # `CH.<cliente>`, cargada a mano por la pantalla de caja: entró la plata
    # pero nunca se generó la cobranza, así que la deuda del cliente no bajó
    # (a YHJ le sobraban $1.000 y a CHI $400, clavados contra el dBase) y no
    # contaba para la comisión del vendedor. Pasándole el `id_caja` existente,
    # el alta **ADOPTA esa fila** en lugar de insertar una nueva: el saldo de
    # caja no se mueve ni un centavo y ningún mes se toca.
    #
    # No alcanzaba con reversar y recargar: `caja.reversar` no borra, crea la
    # contrapartida CON FECHA DE HOY — reversar 5 cobros de julio y volver a
    # cargarlos dejaba julio +4.402,86 y agosto −4.402,86. Dos meses rotos
    # para arreglar uno.
    caja_existente_id: int | None = None,
    conn=None,
) -> dict:
    """Alta de cheque nuevo.

    Reglas (vocabulario canónico 2026-04-29):
      - Estado inicial siempre `Z` (cartera). Si `fechad > fecha` se usa `P`
        (postdatado/postergado) — cheque que el cliente nos dio con fecha
        futura. En ambos casos el cheque queda "vivo" y no movido al banco.
      - `stat='V'` está prohibido al alta (legacy banco Internacional).
      - `fecha_recibido`: cuándo lo recibimos físicamente. Default = HOY si
        no se pasa. Puede ser <= `fechad`. Es distinta de `fecha` (escrita
        en el papel del cheque) y de `fechad` (a depositar).
      - Si el cheque es postdatado, se crea ADEMÁS una fila en `posdat` con
        banc=0 para que aparezca en el flujo y el reporte de cheques futuros.
      - Si `es_anticipo=True` (legacy CONCEPTO=9999): el cliente está pagando
        adelantado, sin factura asociada. Se inserta el cheque normal + un
        cheque "espejo" con importe negativo (representa el anticipo aplicado
        contablemente). Cuando el cliente facture en el futuro, el cobrador
        aplica el espejo a esa factura nueva. Todo en la misma tx.

    Todo dentro de una sola transacción.

    Devuelve `{id_cheque, no_cheque, id_cheque_anticipo (si aplica)}`.
    """
    asegurar_fecha_abierta(fecha)
    fechad = fechad or fecha
    # Bug I fix (TMT 2026-05-16): si fechad cae domingo, shift a lunes
    # (paridad ALTAS.PRG L119). Solo en alta — la edición ya lo hacía
    # (línea 115). 3 cheques en cartera tenían fechad domingo por este bug.
    fechad = _domingo_a_lunes(fechad)
    fecha_recibido = fecha_recibido or today_ec()
    # Cheques nuevos SIEMPRE arrancan en cartera (Z), aunque fechad > fecha.
    # 'P' (postergado) sólo se aplica cuando la usuaria mueve un cheque YA
    # vencido hacia adelante — no es el estado inicial de un cheque recibido.
    # Antes: `stat = "P" if fechad > fecha else "Z"` → confundía postdatado
    # con postergado. Pedido TMT 2026-05-14.
    if stat is None:
        stat = "Z"
    # Validación: no aceptamos 'V' (legacy banco Internacional, deprecado).
    if (stat or "").upper() == "V":
        raise ValueError("stat='V' (banco Internacional) está deprecado. Usar 'B' al depositar.")

    # TMT 2026-05-19 v8 — códigos de banco "virtuales" (>=90) del legacy dBase.
    # Pedido literal dueña: "Asegurate que en cobranza funcionen las logicas
    # de seleccionar opciones de banco >90 ejemplo anticipos, efectivo etc".
    # Mapeo (confirmado por screenshot del dropdown):
    #   90 DEP. PICH   → cobro directo en Pichincha (sin papel) → stat='B'
    #   91 DEP. INTER  → cobro directo en Internacional        → stat='B'
    #   95 CANCELA ANTIC → marca contable: busca el espejo NB=98 del
    #                       anticipo y anula los dos ('X'). El 95 se aplica
    #                       igual a la factura que el anticipo paga — es esa
    #                       aplicación la que cierra la cuenta (ver la
    #                       excepción por no_banco=95 en aplicar_a_factura).
    #   97 ANTICIPO   → el view fuerza es_anticipo=True; queda en Z y
    #                    genera espejo negativo.
    #   98 UKN        → legacy unknown, sin side-effect.
    #   99 EFECTIVO   → cobro en efectivo: stat='B' + entrada en caja
    #                    (manejado abajo, post-INSERT, dentro de la tx).
    # Sin esto el cheque "virtual" quedaba en stat='Z' (cartera) y no
    # contaba en comisiones ni en el flujo real. BED (HECTOR BEDON) es el
    # caso testigo: $341k de débito, 0 cobrado en sistema porque "paga
    # mucho en efectivo" y entraba como banco=99 sin side-effect.
    #
    # TMT 2026-06-11 paridad ALTAS.PRG (pedido dueña: "fijate que hace el
    # dbase y hagamos lo mismo, con todos los codigos"):
    #   90/91 → stat 'B' + APPEND inmediato del movimiento bancario DOC='DE'
    #           en el banco REAL (PICHINCHA/INTER), concepto "1 ch.CLI",
    #           con numreferencia=doc_banco → conciliable por referencia.
    #           (ALTAS.PRG L170-186; antes PC solo flipeaba a B sin mov.)
    #   99    → stat 'C' (cobrado en caja, PASOCAJA L870-893) + entrada en
    #           CAJA "CH.CLI". Antes PC usaba 'B', que no es lo que tipea
    #           el dBase en CHEQUES.DBF (C) y los escondía de la vista.
    # TMT 2026-08-24 (dueña, mirando el −500 de RAR postergado al 30/08:
    # *"igual −500 también debería crear negativo en el banco, no?"*). SÍ: el
    # gate `> 0` era la mitad de un problema con dos caras. Un cobro negativo
    # (mudar plata de una factura a otra, devolverle al cliente) no generaba
    # movimiento de banco Y no salía de cartera, así que un −500 + un +500
    # se compensaban en la factura pero en Pichincha dejaban +500 —los $500
    # fantasma del 19/08— y el negativo quedaba colgado esperando un depósito
    # que no existe (lo postergaron dos veces). Ahora es simétrico: el
    # negativo emite su 'DE' en negativo (convención del dBase, ver
    # `permitir_signed` en bank_helpers) y queda depositado igual que el
    # positivo. El comentario viejo decía que marcarlo 'B' lo dejaba
    # "depositado SIN movimiento" — cierto entonces, porque el insert del
    # banco tenía el mismo gate `> 0`; se arreglan LOS DOS o ninguno.
    if no_banco in (90, 91) and (stat or "").upper() == "Z" and abs(float(importe or 0)) > 0.005:
        stat = "B"
    # TMT 2026-07-30 (dueña: "es una entrada en negativo a la caja que se
    # convierte en salida... necesito que se registre el abono en negativo en
    # la factura del cliente"). El efectivo NEGATIVO (devolución de plata al
    # cliente) es un cobro al revés, no un cheque en cartera: entra igual por
    # 'C' y abajo genera la fila de caja como SALIDA. Antes el gate `> 0` lo
    # dejaba en 'Z' SIN movimiento de caja — la plata salía de la lata y el
    # sistema no se enteraba.
    if (no_banco == 99 and (stat or "").upper() in ("Z", "B")
            and abs(float(importe or 0)) > 0.005):
        stat = "C"

    # TMT 2026-07-22 — GUARD anti-orphan (root cause del bug cheque 100410).
    # Un cheque NO puede nacer en un estado DEPOSITADO (STATS_DEPOSITADO) sin
    # que se genere su movimiento bancario. El único alta que crea el
    # movimiento (chequextransaccion + transacciones_bancarias) es el depósito
    # directo NB=90/91 con importe>0 (bloque más abajo, paridad ALTAS.PRG
    # L170-186). Si alguien elige stat='B' en el dropdown de /cheques/nuevo con
    # un banco EMISOR real (p.ej. Pichincha=10) —o con importe<=0— el cheque
    # quedaría "Depositado" SIN fila de movimiento → invisible en la
    # conciliación bancaria (exactamente lo que le pasó a 100410, cargado por
    # alex el 13/07). Lo bloqueamos y mandamos al flujo correcto: cargarlo en
    # cartera (Z) y depositarlo con "Depositar lote", o elegir banco 90/91
    # (DEP.PICH / DEP.INTER) para el depósito directo — ambos SÍ crean el mov.
    _stat_final = (stat or "").upper()
    # TMT 2026-08-24: `abs` — el negativo TAMBIÉN genera su movimiento ahora.
    _genera_mov_banco = (no_banco in (90, 91)) and abs(float(importe or 0)) > 0.005
    # 🚨 TMT 2026-08-24 — LA PUERTA POR DONDE ENTRÓ EL CHEQUE 102080.
    # El auto-flip de arriba pregunta por `stat == 'Z'`, así que alcanza con
    # elegir DEP.PICH. **y** "Postdatado" en el mismo renglón de Cobranza para
    # que el depósito NAZCA en cartera y SIN movimiento de banco. Así se cargó
    # el 07/08 el cobro de MTM por 536,30: la transferencia ya estaba en
    # Pichincha (la trajo la carga del 12/08, conciliada), pero esta fila
    # quedó viva, se postergó dos veces y cobró la factura por segunda vez.
    # El guard de abajo no lo veía porque mira los estados DEPOSITADOS, y
    # "postergado" no es uno — otra vez el error de preguntar por el camino y
    # no por el estado.
    # Un depósito directo no se puede postdatar: o la plata entró, o no es un
    # depósito. Se avisa en vez de corregir en silencio, porque las dos
    # lecturas posibles ("me equivoqué de medio" / "me equivoqué de estado")
    # las tiene que resolver quien está cargando.
    if (no_banco in (90, 91)) and _stat_final in STATS_EN_CARTERA:
        raise ValueError(
            "Un depósito directo (DEP.PICH. / DEP. INTER.) no puede quedar en "
            f"cartera ni postdatado — llegó en estado '{_stat_final}'. Si la "
            "plata ya está en el banco, dejá el estado en Depositado. Si "
            "todavía no entró, no es un depósito: elegí el banco del cheque."
        )
    if _stat_final in STATS_DEPOSITADO and not _genera_mov_banco:
        raise ValueError(
            f"No se puede crear un cheque directamente en estado '{_stat_final}' "
            "(depositado) sin generar el movimiento bancario. Cargalo en cartera "
            "(Z) y depositalo con 'Depositar lote', o elegí banco 90/91 "
            "(DEP.PICH / DEP.INTER) para el depósito directo."
        )

    importe_principal = float(importe or 0)

    # TMT 2026-05-15: caller puede pasar `conn` para compartir transacción
    # (multi-cheque atómico). Si no, abrimos tx propia.
    import contextlib as _ctx

    _tx = _ctx.nullcontext(conn) if conn is not None else db.tx()
    with _tx as conn:
        # Cheque principal — incluye fecha_recibido (columna agregada en
        # migración 0013).
        # Bug H fix (TMT 2026-05-16): fechaing antes se seteaba a CURRENT_DATE
        # por default. Pero la convención canónica dice fechaing=fecha de paso
        # por banco (solo aplica a stat B/A/1/2/3/R/D). Para cheques Z (cartera)
        # debe ser NULL. Los 985 cheques afectados son legacy + nuevos
        # creados con este bug. NULL = arrancamos limpios desde acá.
        # NOTA TMT 2026-05-17: NO seteamos fechad_original al alta. El
        # diseño canónico de la migración 0014 dice: fechad_original IS NULL
        # cuando el cheque NUNCA se postergó. La primera postergar() lo
        # snapshotea con COALESCE(fechad_original, fechad). Re-postergar no
        # lo toca (queda la 1ra fechad). Los displays usan NULL = "no
        # postergado".
        # TMT 2026-05-26: agregada col `doc_banco` (migration 0051) — N° de
        # comprobante/depósito/transferencia. Si no se pasa queda NULL.
        # TMT 2026-08-04: `concepto` es columna nueva y el deploy NO corre
        # migraciones → se bootstrapea en caliente antes del INSERT.
        _concepto_cobro.bootstrap_columna(conn=conn)
        row = (
            db.execute_returning(
                """
            INSERT INTO scintela.cheque
                (no_cheque, fecha, fechad, fecha_recibido,
                 codigo_cli, importe, no_banco,
                 banco, stat, fechaing, fechaout, prov, clave, doc_banco,
                 concepto, usuario_crea)
            VALUES (%s, %s, %s, %s,
                    %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s,
                    %s, %s)
            RETURNING id_cheque, no_cheque
            """,
                (
                    (no_cheque or "").strip()[:10],
                    fecha,
                    fechad,
                    fecha_recibido,
                    codigo_cli.upper().strip(),
                    importe_principal,
                    no_banco,
                    (banco_texto or None),
                    stat,
                    # TMT 2026-06-11 paridad dBase: depósito directo (90/91,
                    # stat B) lleva fechaing = fecha de paso por banco.
                    (fecha if (no_banco in (90, 91) and (stat or "").upper() == "B") else None),
                    # TMT 2026-08-10 — LA TERCERA RUTA DE DEPÓSITO.
                    # El 05/08 se arreglaron las dos rutas que SACAN un cheque
                    # de cartera (`depositar_lote`, `transicionar_stat`): la
                    # fecha del depósito pasó de `fechaing` a `fechaout`. Esta
                    # no saca nada de cartera —el cheque NACE afuera, ya
                    # depositado (90/91 → 'B') o ya cobrado en caja (99 →
                    # 'C')— así que no entraba en "las dos rutas de depósito"
                    # y quedó escribiendo sólo `fechaing`. 104 cheques por
                    # $116.459,12 entre el 05 y el 08/08 (más 13 efectivos sin
                    # NINGUNA de las dos fechas) los cazó
                    # /admin/health/deposito-sin-fechaout.
                    #
                    # Un cheque que nace afuera ENTRÓ Y SALIÓ EL MISMO DÍA:
                    # `fechaout` = `fecha`, igual que las otras once salidas.
                    # No mueve ningún número — SQL_DIA_SALIDA ya devolvía este
                    # mismo día por el COALESCE, y el balance as-of tiene la
                    # rama `fechaout > as_of` desde el 05/08. Lo que arregla es
                    # que el invariante "depositado ⇒ tiene fechaout" valga sin
                    # excepciones: una excepción acá es una puerta para la
                    # próxima ruta que nazca olvidándose.
                    #
                    # `fechaing` NO se toca: cambiarlo movería el resumen de
                    # cobranza del día, que agrupa por día de INGRESO y se
                    # imprime para contabilidad.
                    (fecha if (stat or "").upper() not in STATS_EN_CARTERA else None),
                    (prov or None),
                    (clave or None) and clave[:5],
                    (doc_banco or None),
                    _concepto_cobro.limpiar(concepto),
                    usuario,
                ),
                conn=conn,
            )
            or {}
        )
        # mov_doble del alta del cheque (paridad con factura_emitida).
        # TMT 2026-05-14: antes el alta del cheque quedaba invisible en
        # /historial — sólo veías la aplicación / depósito posterior.
        import mov_doble as _md

        if row.get("id_cheque") and importe_principal != 0:
            _md.registrar(
                conn=conn,
                tipo="cheque_creado",
                origen_table="cheque",
                origen_id=row["id_cheque"],
                destino_table="cheque",
                destino_id=row["id_cheque"],
                importe=importe_principal,
                fecha=fecha,
                # "Cheque 102345 de TNZ" o, cuando no hay número escrito
                # porque no es un cheque, "Dep. Pich. de TNZ" (TMT 2026-08-09).
                concepto=(f"{etiqueta_cobro({'no_cheque': no_cheque, 'no_banco': no_banco, 'banco_nombre': _nombre_banco(no_banco, conn=conn)}) or 'Cobro'}"
                          f" de {codigo_cli.upper().strip()}")[:200],
                usuario=usuario,
                metadata={
                    "codigo_cli": codigo_cli.upper().strip(),
                    "no_cheque": (no_cheque or "").strip(),
                    "no_banco": no_banco,
                    "stat_inicial": stat,
                    "es_anticipo": bool(es_anticipo),
                },
                batch_id=batch_id,
            )

        # TMT 2026-06-11 paridad ALTAS.PRG L170-186 — banco 90/91 (DEP.PICH /
        # DEP.INTER): el dBase appendea el movimiento bancario DOC='DE' en el
        # banco REAL al momento de la cobranza (no espera al deposito).
        # numreferencia = doc_banco (regla #1 del matcher de conciliacion) →
        # el deposito directo aparece en el panel Programa y matchea por
        # referencia. chequextransaccion linkea cheque ↔ movimiento.
        if (
            no_banco in (90, 91)
            and (stat or "").upper() == "B"
            and row.get("id_cheque")
            # TMT 2026-08-24: el cobro NEGATIVO también deja su asiento. Va
            # como 'DE' con el importe en negativo —la convención del dBase
            # (`permitir_signed`)— y no como una 'ND' aparte, para que todo lo
            # que busca el depósito de un cheque por `documento='DE'`
            # (compensar_deposito_devuelto, deshacer_deposito_cheque, el
            # matcher de conciliación) lo siga encontrando.
            and abs(importe_principal) > 0.005
        ):
            import bank_helpers

            banco_real = _banco_real_para_deposito(no_banco, conn=conn)
            # TMT 2026-06-12 audit: numreferencia es INTEGER en DB. Un doc
            # no-numerico ("TRF 123") reventaba TODA la cobranza con
            # InvalidTextRepresentation. Texto libre queda en cheque.doc_banco.
            _nr = (doc_banco or "").strip() or str(row["id_cheque"])
            num_ref = int(_nr) if _nr.isdigit() else None
            cli_u = codigo_cli.upper().strip()
            mov_dep = bank_helpers.insert_movimiento_bancario(
                conn,
                no_banco=banco_real,
                no_cta=None,
                fecha=fecha,
                documento="DE",
                # TMT 2026-06-12 hotfix: faltaba importe= (TypeError en prod
                # al cargar cobranza 90/91 — el stub del test lo tapaba).
                importe=importe_principal,
                permitir_signed=True,
                # Concepto paridad dBase: "1 ch.CLI" (el extractor de prov
                # de conciliacion ya parsea este formato).
                concepto=f"1 ch.{cli_u}"[:50],
                prov=cli_u[:5],
                numreferencia=num_ref,
                usuario=usuario,
            )
            if mov_dep.get("id_transaccion"):
                db.execute(
                    """
                    INSERT INTO scintela.chequextransaccion
                        (id_cheque, id_transaccion, fecha, stat_ch, usuario_crea)
                    VALUES (%s, %s, %s, 'D', %s)
                    """,
                    (row["id_cheque"], mov_dep["id_transaccion"], fecha, usuario),
                    conn=conn,
                )
                row["id_transaccion_deposito"] = mov_dep["id_transaccion"]

        # TMT 2026-06-11 paridad ALTAS.PRG NB=95 (CANCELA ANTIC.): el dBase
        # busca el espejo del anticipo (importe negativo, NB=98) del mismo
        # cliente y marca AMBOS con stat 'X' (anulados entre si). Si no lo
        # encuentra, el cheque queda 'Z' y se avisa (dBase: "NO SE ENCUENTRA
        # EL ANTICIPO") — la duena lo resuelve a mano.
        if no_banco == 95 and row.get("id_cheque") and importe_principal > 0:
            # TMT 2026-07-30 (dueña: "deberia mostrar postergados igual, no se
            # porque filtramos"). Antes exigía stat='Z' EXACTO y por eso el
            # primer caso real falló: el espejo de GL1 (−900) estaba POSTERGADO,
            # y postergarlo sólo movió la fecha de depósito — el crédito del
            # cliente sigue estando. Ahora acepta el mismo grupo vivo que la
            # cartera (Z/1/2/3/P/D), con Z primero para no cambiar el
            # comportamiento cuando hay uno en cartera. Los ya cancelados ('X')
            # o cobrados siguen afuera.
            espejo_95 = db.fetch_one(
                """
                SELECT id_cheque FROM scintela.cheque
                 WHERE codigo_cli = %s
                   AND importe = %s
                   AND no_banco IN (97, 98)
                   AND TRIM(COALESCE(stat, '')) IN ('Z','1','2','3','P','D')
                 ORDER BY (TRIM(COALESCE(stat, '')) = 'Z') DESC, id_cheque
                 LIMIT 1
                """,
                (codigo_cli.upper().strip(), -importe_principal),
                conn=conn,
            )
            if espejo_95:
                db.execute(
                    "UPDATE scintela.cheque "
                    "SET stat='X', fechaout=%s, fechad=%s, "
                    "    usuario_modifica=%s, fecha_modifica=CURRENT_TIMESTAMP "
                    "WHERE id_cheque IN (%s, %s)",
                    (fecha, fecha, usuario, row["id_cheque"], espejo_95["id_cheque"]),
                    conn=conn,
                )
                row["id_cheque_anticipo_cancelado"] = espejo_95["id_cheque"]
            else:
                row["warning"] = (
                    f"No se encontró el anticipo de {codigo_cli.upper().strip()} "
                    f"por -{importe_principal:.2f} — el cheque 95 quedó en cartera (Z)."
                )

        # TMT 2026-05-19 v8 — banco=99 EFECTIVO: insert en scintela.caja
        # (tipo='E') para que la plata entre realmente al saldo de caja.
        # NO usamos caja.queries.crear() acá para evitar la cascada de
        # side-effects basados en concepto_parser (riesgo de match falso):
        # bajamos un escalón, al helper compartido, que es la parte que sí
        # queremos compartir. Ver el comentario del INSERT. TMT 2026-08-14.
        if no_banco == 99 and row.get("id_cheque") and abs(importe_principal) > 0.005:
            # TMT 2026-07-30: el efectivo NEGATIVO es una SALIDA de caja. El
            # trigger `fn_caja_set_saldo` toma el signo del campo `tipo` y usa
            # ABS(importe) — meter un importe negativo con tipo='E' SUMABA a
            # la caja. Por eso el signo va en el tipo y el importe siempre
            # positivo, que es además la convención de todo el módulo caja
            # (`egresos = SUM(importe) WHERE tipo='S'`).
            _es_salida_caja = importe_principal < 0
            _tipo_caja = "S" if _es_salida_caja else "E"
            _importe_caja = abs(importe_principal)
            # Concepto paridad PASOCAJA: "CH."+cliente (antes "99 CLI ch N").
            # La salida se marca DEV. para que en la lista de caja se lea de
            # qué se trata sin abrir el cheque.
            _pre_caja = "DEV." if _es_salida_caja else "CH."
            concepto_caja = f"{_pre_caja}{codigo_cli.upper().strip()}"[:80]
            if caja_existente_id:
                # ADOPTAR la fila de caja que ya está cargada (ver el comentario
                # del parámetro). El UPDATE es la validación: sólo agarra si la
                # fila sigue siendo la misma que se le mostró al usuario en la
                # confirmación — mismo importe, mismo tipo, mismo cliente y
                # todavía sin cheque. Si alguien la reversó, la editó o la
                # convirtió en el medio, no matchea y abortamos la transacción
                # entera en vez de dejar el cheque colgado sin caja.
                _adoptadas = db.execute(
                    """
                    UPDATE scintela.caja
                       SET id_cheque = %s
                     WHERE id_caja   = %s
                       AND id_cheque IS NULL
                       AND tipo      = %s
                       AND ROUND(importe, 2) = ROUND(%s::numeric, 2)
                       AND UPPER(TRIM(concepto)) = UPPER(TRIM(%s))
                       AND fecha     = %s
                    """,
                    (
                        row["id_cheque"],
                        int(caja_existente_id),
                        _tipo_caja,
                        _importe_caja,
                        concepto_caja,
                        fecha,
                    ),
                    conn=conn,
                )
                if int(_adoptadas or 0) != 1:
                    raise ValueError(
                        f"La entrada de caja id={caja_existente_id} ya no coincide con "
                        f"este cobro ({concepto_caja} {_tipo_caja} {_importe_caja:.2f}) "
                        "o ya tiene un cheque asociado. Volvé a abrirla desde /caja."
                    )
                caja_row = {"id_caja": int(caja_existente_id)}
            else:
                # ⭐ TMT 2026-08-14 — LA FILA DE CAJA VA POR EL HELPER.
                #
                # Hasta hoy este INSERT mandaba `saldo = NULL` y dejaba que el
                # trigger `trg_caja_set_saldo` (mig 0022) lo estampara. El
                # trigger encadena bien la fila NUEVA, pero es un BEFORE INSERT
                # de una fila sola: no sabe que abajo pueden quedar filas cuyo
                # saldo se calculó sobre un estado que esta fila acaba de
                # cambiar, y no las re-encadena. Un cobro en efectivo cargado
                # con fecha ATRASADA —que es lo normal cuando Alex pone al día
                # la caja— partía la cadena en dos tramos, cada uno coherente
                # por su lado, que es la forma en que este bug no se ve. Es el
                # mismo que en bancos costó los 155.187,31 del 03/08
                # [[project_2026_08_03_utilidad_37k]] y el que se acaba de
                # cerrar en `/caja/nuevo` (`caja.queries.crear`).
                #
                # Se DELEGA, no se recalcula acá: `insert_movimiento_caja` ya
                # trae las tres piezas —saldo previo por (fecha, id_caja), que
                # es el orden en que lee la caja todo el resto del sistema;
                # re-encadenado de lo que queda debajo; y el candado
                # `assert_cadena_intacta`— y es por donde ya entran la
                # anulación por error de carga, el reverso y la migración de
                # depósito directo de este MISMO archivo. Escribir una segunda
                # cuenta del saldo sería repetir lo que ya pasó con el SIGNO de
                # la caja: cuatro definiciones conviviendo y una equivocada.
                # [[feedback_espejo_clasificador_compartido]]
                #
                # Va con el `conn` del `_tx` de arriba a propósito: la fila de
                # caja tiene que caer en la MISMA transacción que el cheque y
                # sus aplicaciones. Si el candado revienta, se rollbackea el
                # cobro entero — preferimos "no pude guardar esto" a un cheque
                # cobrado contra una caja que miente.
                #
                # El `tipo` y el importe NO se tocan: siguen saliendo de
                # `_tipo_caja` / `_importe_caja` (el signo vive en el tipo,
                # decisión del 2026-07-30 unas líneas más arriba).
                import caja_helpers

                caja_row = caja_helpers.insert_movimiento_caja(
                    conn,
                    fecha=fecha,
                    tipo=_tipo_caja,
                    importe=_importe_caja,
                    concepto=concepto_caja,
                    clave=(clave or None) and clave[:3],
                    id_cheque=row["id_cheque"],
                    usuario=usuario,
                )
            # mov_doble linkea cheque ↔ caja para que el reverso del
            # cheque pueda compensar la entrada de caja en automático.
            if caja_row.get("id_caja"):
                _md.registrar(
                    conn=conn,
                    tipo="cheque_efectivo_to_caja",
                    origen_table="cheque",
                    origen_id=row["id_cheque"],
                    destino_table="caja",
                    destino_id=caja_row["id_caja"],
                    importe=importe_principal,
                    fecha=fecha,
                    concepto=(
                        f"{'Devolución en efectivo a' if _es_salida_caja else 'Cobro efectivo ch' + (no_cheque or '').strip() + ' de'} "
                        f"{codigo_cli.upper().strip()} → caja"
                    )[:200],
                    usuario=usuario,
                    metadata={
                        "codigo_cli": codigo_cli.upper().strip(),
                        "no_banco": 99,
                        "id_cheque": row["id_cheque"],
                        "id_caja": caja_row["id_caja"],
                    },
                    batch_id=batch_id,
                )

        # Espejo de anticipo (importe negativo) — sólo si flag activo y >0.
        # TMT 2026-07-06 (dueña): si el anticipo se usó para CANCELAR cheques
        # en cartera (flujo 97 de /cheques/nuevo), el espejo/NC se crea SOLO
        # por el SOBRANTE = anticipo − Σ cheques cancelados
        # (`anticipo_espejo_importe`, lo calcula el view con
        # distribuir_espejos_anticipo). Sobrante < $1 = centavos → SIN espejo
        # (mismo umbral que el sobrante de cobranza). None = flujo clásico.
        _imp_espejo = (
            importe_principal
            if anticipo_espejo_importe is None
            else round(float(anticipo_espejo_importe), 2)
        )
        # El umbral $1 aplica SOLO al flujo con cancelados (override); el
        # flujo clásico (None) mantiene el comportamiento histórico.
        if es_anticipo and importe_principal > 0 and (
            anticipo_espejo_importe is None or _imp_espejo >= 1.00
        ):
            # TMT 2026-07-07: INSERT + mov_doble extraídos a
            # crear_espejo_anticipo() para reusar desde el view (anticipo
            # aplicado PARCIALMENTE a facturas → espejo por el resto).
            espejo = crear_espejo_anticipo(
                conn=conn,
                id_cheque_padre=row.get("id_cheque"),
                no_cheque=no_cheque,
                fecha=fecha,
                fechad=fechad,
                fecha_recibido=fecha_recibido,
                codigo_cli=codigo_cli,
                importe_espejo=_imp_espejo,
                prov=prov,
                clave=clave,
                usuario=usuario,
            )
            row["id_cheque_anticipo"] = espejo.get("id_cheque")
    return row


def crear_espejo_anticipo(
    *,
    conn,
    id_cheque_padre: int | None,
    no_cheque: str = "",
    fecha: date,
    fechad: date | None = None,
    fecha_recibido: date | None = None,
    codigo_cli: str,
    importe_espejo: float,
    prov: str | None = None,
    clave: str | None = None,
    usuario: str = "web",
) -> dict:
    """Crea el cheque ESPEJO de anticipo (NB=98, banco='ANTICIPO', negativo).

    Paridad ALTAS.PRG L156: FECHAD+30, stat 'Z', id_cheque_padre para
    auditoría. Registra mov_doble tipo='cheque_anticipo_espejo' (el
    historial ya lo conoce; el reverso existente no cambia).

    Usado por:
      - crear(es_anticipo=True): flujo clásico / sobrante de cancelados;
      - views.nuevo: anticipo (97) aplicado PARCIALMENTE a facturas —
        TMT 2026-07-07 (dueña, caso CLR): "si deselecciono, solo se tiene
        que ir a nota de crédito y ya" — lo NO aplicado del anticipo va a
        NC/espejo, aunque haya aplicaciones.

    Corre dentro de la tx del caller (conn obligatoria).
    """
    import mov_doble as _md

    _imp = round(float(importe_espejo or 0), 2)
    espejo = (
        db.execute_returning(
            """
        INSERT INTO scintela.cheque
            (no_cheque, fecha, fechad, fecha_recibido,
             codigo_cli, importe, no_banco,
             banco, stat, fechaing, prov, clave, usuario_crea,
             id_cheque_padre)
        VALUES (%s, %s, %s, %s,
                %s, %s, %s,
                %s, 'Z', CURRENT_DATE, %s, %s, %s, %s)
        RETURNING id_cheque
        """,
            (
                (no_cheque or "").strip()[:10],
                fecha,
                # TMT 2026-06-11 paridad ALTAS.PRG L156: espejo de
                # anticipo con FECHAD+30, NB=98 y BANCO='ANTICIPO'.
                (fechad or fecha) + timedelta(days=30),
                fecha_recibido,
                codigo_cli.upper().strip(),
                -_imp,  # espejo negativo (sobrante si hubo cancelados/aplicaciones)
                98,
                "ANTICIPO",
                (prov or None),
                (clave or None) and clave[:5],
                usuario,
                id_cheque_padre,  # apunta al cheque "padre" para auditoría
            ),
            conn=conn,
        )
        or {}
    )
    # mov_doble del espejo — link cheque normal → cheque espejo.
    # TMT 2026-05-14 (issue #25).
    if espejo.get("id_cheque") and id_cheque_padre:
        _md.registrar(
            conn=conn,
            tipo="cheque_anticipo_espejo",
            origen_table="cheque",
            origen_id=id_cheque_padre,
            destino_table="cheque",
            destino_id=espejo["id_cheque"],
            importe=-_imp,  # espejo es negativo
            fecha=fecha,
            concepto=(
                f"Espejo de anticipo ch{(no_cheque or '').strip()} de {codigo_cli.upper().strip()}"
            )[:200],
            usuario=usuario,
            metadata={
                "codigo_cli": codigo_cli.upper().strip(),
                "id_cheque_padre": id_cheque_padre,
                "id_cheque_espejo": espejo["id_cheque"],
            },
        )
    return espejo


def postergar(
    *,
    id_cheque: int,
    nueva_fechad: date,
    motivo: str = "",
    usuario: str = "web",
) -> dict:
    """Postergar un cheque — sólo desde stat='Z' (cartera).

    Cambia la `fechad` a una fecha futura y marca el cheque como `P`
    (postergado). El cliente nos pidió que esperemos, así que el depósito
    se mueve.

    Reglas (vocabulario canónico 2026-04-29):
      - Stat origen DEBE ser 'Z' o 'P' (postergaciones encadenadas — el
        cliente pide más tiempo otra vez). De B/D/1/2/3/V no se posterga.
      - `nueva_fechad` debe ser estrictamente posterior a la fechad actual.
      - Tracking (migración 0014):
          fecha_postergacion = CURRENT_DATE  (cuándo se decidió postergar)
          fechad_original    = COALESCE(prev, fechad)  (snapshot 1ra vez)
      - NO toca `scintela.posdat` (TMT 2026-07-15): postergar un cheque de
        CLIENTE no debe crear un Pasivo. El cheque futuro se ve desde
        scintela.cheque (stat 'P'); posdat es sólo para pagos a proveedores.
    """
    asegurar_fecha_abierta(today_ec())

    with db.tx() as conn:
        ch = db.fetch_one(
            "SELECT id_cheque, no_cheque, stat, codigo_cli, fechad, importe, no_banco "
            "FROM scintela.cheque WHERE id_cheque = %s",
            (id_cheque,),
            conn=conn,
        )
        if not ch:
            raise ValueError(f"Cheque {id_cheque} no existe.")
        # 🚨 TMT 2026-08-24 — postergar es "el cliente pide más tiempo antes de
        # que llevemos el papel al banco". Un depósito directo no tiene papel
        # ni fecha que correr: la plata entró el día que entró. Andrés
        # postergó dos de ellos (MTM 536,30 el 21 y el 24/08, RAR −500 el
        # 20/08) para sacarlos de la lista de "a depositar" donde no tenían
        # que estar — y eso fue lo que dejó a un cobro duplicado vivo cinco
        # días más y cobrando una factura de mayo por segunda vez.
        if int(ch.get("no_banco") or 0) in (90, 91):
            raise ValueError(
                "Este cobro es un depósito directo: la plata ya está en el "
                "banco, no hay fecha de depósito que postergar. Si el depósito "
                "no existió, anulalo por error de carga."
            )
        stat_prev = (ch["stat"] or "").upper()
        # Permitimos postergar desde Z (primer postergación) y desde P
        # (postergaciones encadenadas — "ya está postergado, pero el
        # cliente pide más tiempo de nuevo").
        # TMT 2026-06-16 dueña: también re-postergar cheques DEVUELTOS (1/2)
        # y de Daniela (D) — el cliente pide nueva fecha aunque el cheque haya
        # rebotado. Coincide con los estados que el template ya deja editar.
        if stat_prev not in (*STATS_POSTERGABLES, "P", "1", "2", "D"):
            raise ValueError(
                f"Sólo cheques en cartera (Z), postergados (P), devueltos (1/2) "
                f"o de Daniela (D) se pueden postergar. Está en stat='{stat_prev}'."
            )
        # TMT 2026-06-16 dueña: "quiero poner otra postergada" — permitir CUALQUIER
        # fecha >= hoy (antes exigía estrictamente > fechad actual, así que no dejaba
        # cambiar a una fecha futura ANTERIOR a la ya postergada). Solo bloqueamos el
        # pasado.
        # TMT 2026-06-16 dueña: "dejame postergar -3 días a hoy también". Permitir
        # hasta 3 días antes de hoy (gracia para back-date operativo); solo se
        # bloquea más atrás que eso.
        if not nueva_fechad or nueva_fechad < (today_ec() - timedelta(days=3)):
            raise ValueError(
                "La nueva fecha no puede ser más de 3 días anterior a hoy."
            )

        # TMT 2026-06-16 dueña: postergar un cheque DEVUELTO (1/2) o de Daniela (D)
        # debe cambiar SOLO la fecha, NO el estado (antes lo flipeaba a 'P').
        # 'Z' (cartera) sí pasa a 'P' (postdatado) — ese es el sentido de
        # postergar un cheque en cartera. 'P' queda 'P'.
        nuevo_stat = "P" if stat_prev == "Z" else stat_prev
        db.execute(
            "UPDATE scintela.cheque "
            "SET stat=%s, fechad=%s, "
            "    fecha_postergacion = CURRENT_DATE, "
            "    fechad_original = COALESCE(fechad_original, fechad), "
            "    usuario_modifica=%s, "
            "    fecha_modifica=CURRENT_TIMESTAMP "
            "WHERE id_cheque=%s",
            (nuevo_stat, nueva_fechad, usuario, id_cheque),
            conn=conn,
        )
        # TMT 2026-07-15 (dueña: "que no se sigan creando esos posdatados cuando
        # se posterga un cheque"). ANTES acá se hacía un upsert a scintela.posdat
        # con prov=código de CLIENTE y banc=0. Eso metía un cheque de cliente
        # (cuenta por COBRAR) dentro de Pasivos: TOTP = Σ posdat banc<>9 lo
        # contaba como PASIVO e inflaba la deuda con plata que en realidad nos
        # deben (divergía del dBase, que no pone cheques de cliente en POSDAT).
        # El flujo de cheques futuros / cartera lee de scintela.cheque (stat 'P'),
        # así que no se pierde nada al no crear el posdat hermano. Los hermanos
        # viejos se siguen limpiando en anular/reversar (DELETE banc=0 num=id_cheque).

    return {
        "id_cheque": id_cheque,
        "stat_previo": stat_prev,
        "stat_nuevo": nuevo_stat,
        "nueva_fechad": nueva_fechad,
        "motivo": motivo,
    }


def marcar_daniela(
    *,
    id_cheque: int,
    motivo: str = "",
    usuario: str = "web",
) -> dict:
    """Pasar un cheque a gestión de Daniela (stat='D').

    Sólo desde stat='Z' (los cheques en cartera son los que se pasan a
    cobranza con Daniela). No cambia ni la fecha ni el banco — sólo la
    flagging del estado.
    """
    asegurar_fecha_abierta(today_ec())

    with db.tx() as conn:
        ch = db.fetch_one(
            "SELECT id_cheque, stat FROM scintela.cheque WHERE id_cheque = %s",
            (id_cheque,),
            conn=conn,
        )
        if not ch:
            raise ValueError(f"Cheque {id_cheque} no existe.")
        stat_prev = (ch["stat"] or "").upper()
        if stat_prev != "Z":
            raise ValueError(f"Sólo desde cartera (Z) se puede pasar a Daniela. Stat actual: '{stat_prev}'.")
        db.execute(
            "UPDATE scintela.cheque "
            "SET stat='D', usuario_modifica=%s, fecha_modifica=CURRENT_TIMESTAMP "
            "WHERE id_cheque=%s",
            (usuario, id_cheque),
            conn=conn,
        )
    return {"id_cheque": id_cheque, "stat_previo": stat_prev, "stat_nuevo": "D", "motivo": motivo}


# Stats desde los que se puede endosar un cheque a proveedor: el cheque
# tiene que seguir "vivo en cartera". B (depositado) ya no se puede endosar
# — la plata está en el banco. 1/2/3 (rebotado) tampoco. X/R terminales.
STATS_ENDOSABLES = ("Z", "P", "D")

# Stats desde los que se puede aplicar el cheque a una factura.
# Z (cartera) — flujo típico.
# P (postergado) — cheque a futuro aplicado a anticipo.
# D (Daniela) — gestión de cobranza, todavía aplicable.
# Cualquier otro (B/A depositados, 1/2/3 rebotados, E endosado, X eliminado,
# R terminal) → ValueError. TMT 2026-05-14 (#26).
STATS_APLICABLES = ("Z", "P", "D")

# TMT 2026-07-06 (dueña): grupo "vivo" de cartera — fórmula canónica TOTC
# (PRG L24: STAT $ "Z123PD"). Son los cheques cancelables por un anticipo 97.
STATS_VIVOS = ("Z", "1", "2", "3", "P", "D")


def endosar(
    *,
    id_cheque: int,
    codigo_prov: str,
    concepto: str = "",
    tipo_compra: str = "C",
    fecha: date | None = None,
    usuario: str = "web",
) -> dict:
    """Endosar un cheque a un proveedor — usar el cheque del cliente como
    pago a un proveedor nuestro.

    Operación atómica:
      1. UPDATE cheque SET stat='E', prov=<codigo_prov>, fechaout=<fecha>,
         observacion+='[ENDOSO a <prov> <fecha>]'.
      2. INSERT en scintela.compra (cuenta_pagada='E' = pagada por endoso)
         con concepto = "ENDOSO ch <no_cheque> <cliente>" + texto libre,
         observacion = enlace al cheque por id.
      3. NO se reversan aplicaciones a facturas — el cliente ya pagó con
         ese cheque, su factura sigue abonada. Sólo cambia quién tiene el
         papel ahora.
      4. DELETE posdat hermana del cheque (banc=0, num=id_cheque) — el
         cheque ya no aparece como "futuro a depositar".

    Reglas:
      - Stat origen debe estar en STATS_ENDOSABLES (Z, P, D). Cualquier
        otro origen → ValueError (B ya depositado, 1/2/3 rebotado, etc).
      - codigo_prov tiene que existir en scintela.proveedor.

    Devuelve dict con id_cheque, id_compra, codigo_prov, stat_previo.
    """
    fecha = fecha or today_ec()
    asegurar_fecha_abierta(fecha)

    codigo_prov = (codigo_prov or "").strip().upper()
    if not codigo_prov:
        raise ValueError("Código de proveedor requerido.")

    tipo_norm = (tipo_compra or "C").upper().strip()[:1]
    if tipo_norm not in ("H", "K", "T", "Q", "C", "S"):
        # Tipos válidos en scintela.compra (ver compras/queries.py).
        tipo_norm = "C"

    with db.tx() as conn:
        # Cheque + cliente
        ch = db.fetch_one(
            "SELECT id_cheque, no_cheque, stat, codigo_cli, importe, fechad "
            "FROM scintela.cheque WHERE id_cheque = %s",
            (id_cheque,),
            conn=conn,
        )
        if not ch:
            raise ValueError(f"Cheque {id_cheque} no existe.")
        stat_prev = (ch.get("stat") or "").upper()
        if stat_prev not in STATS_ENDOSABLES:
            raise ValueError(
                f"Cheque en stat='{stat_prev}' no se puede endosar. "
                f"Sólo desde {STATS_ENDOSABLES} (cartera/postergado/Daniela)."
            )

        # Proveedor existe?
        prov_row = db.fetch_one(
            "SELECT id_proveedor, COALESCE(nombre,'') AS nombre "
            "FROM scintela.proveedor WHERE codigo_prov = %s",
            (codigo_prov,),
            conn=conn,
        )
        if not prov_row:
            raise ValueError(f"Proveedor {codigo_prov!r} no existe.")

        importe = float(ch["importe"] or 0)
        if importe < 0:
            # Espejo de anticipo (importe negativo) — no se puede endosar.
            # TMT 2026-05-14 (#21).
            raise ValueError("Este cheque es un espejo de anticipo (importe negativo). No se puede endosar.")
        if importe <= 0:
            raise ValueError(f"Cheque con importe inválido ($ {importe:.2f}) — no se puede endosar.")

        # Próximo número de compra (siguiente correlativo).
        row_n = db.fetch_one(
            "SELECT COALESCE(MAX(numero), 0) + 1 AS siguiente FROM scintela.compra",
            conn=conn,
        )
        numero_compra = int(row_n["siguiente"]) if row_n else 1

        # Concepto de compra: prefijo ENDOSO + texto del usuario.
        cli_txt = ch.get("codigo_cli") or ""
        concepto_compra = (
            f"ENDOSO ch{ch.get('no_cheque') or id_cheque} {cli_txt} {(concepto or '').strip()}"
        ).strip()[:50]

        # INSERT compra ya pagada con cuenta_pagada='E' (endoso).
        compra = (
            db.execute_returning(
                """
            INSERT INTO scintela.compra
                (fecha, id_proveedor, codigo_prov, tipo, comprobante,
                 importe, numero, fecha_ing, fechad, concepto,
                 clave, usuario_crea, cuenta_pagada, observacion)
            VALUES (%s, %s, %s, %s, %s,
                    %s, %s, CURRENT_DATE, %s, %s,
                    %s, %s, 'E', %s)
            RETURNING id_compra, numero
            """,
                (
                    fecha,
                    prov_row["id_proveedor"],
                    codigo_prov,
                    tipo_norm,
                    f"CH{ch.get('no_cheque') or id_cheque}"[:20],
                    importe,
                    numero_compra,
                    fecha,
                    concepto_compra,
                    (codigo_prov[:3] if codigo_prov else None),
                    usuario[:50],
                    f"Pagada por endoso del cheque #{id_cheque} "
                    f"(N° {ch.get('no_cheque') or ''}, cliente {cli_txt}).",
                ),
                conn=conn,
            )
            or {}
        )

        # UPDATE cheque: stat='E', prov, fechaout, traza en observacion.
        marca = (
            f"[ENDOSO a {codigo_prov} ({prov_row['nombre'][:20]}) "
            f"{fecha.isoformat()} → compra #{compra.get('numero')}]"
        )
        db.execute(
            "UPDATE scintela.cheque "
            "SET stat='E', prov=%s, fechaout=%s, "
            "    observacion = RIGHT("
            "        COALESCE(observacion || ' | ', '') || %s, %s), "
            "    usuario_modifica=%s, fecha_modifica=CURRENT_TIMESTAMP "
            "WHERE id_cheque=%s",
            (codigo_prov[:5], fecha, marca, _OBS_CAP, usuario, id_cheque),
            conn=conn,
        )

        # DELETE posdat hermana (si el cheque era postdatado/postergado).
        # Ya no figura como "futuro a depositar".
        db.execute(
            "DELETE FROM scintela.posdat WHERE COALESCE(banc, 0) = 0 AND num=%s AND prov=%s",
            (id_cheque, ch.get("codigo_cli")),
            conn=conn,
        )

        # Historial unificado.
        import mov_doble as _md

        id_mov_doble = _md.registrar(
            conn=conn,
            tipo="endoso_cheque_a_proveedor",
            origen_table="cheque",
            origen_id=id_cheque,
            destino_table="compra",
            destino_id=compra.get("id_compra"),
            importe=importe,
            fecha=fecha,
            concepto=(concepto or f"ENDOSO ch{ch.get('no_cheque') or ''} a {codigo_prov}")[:200],
            usuario=usuario,
            metadata={
                "codigo_cli": ch.get("codigo_cli"),
                "codigo_prov": codigo_prov,
                "numero_compra": compra.get("numero"),
            },
        )

    return {
        "id_cheque": id_cheque,
        "stat_previo": stat_prev,
        "stat_nuevo": "E",
        "codigo_prov": codigo_prov,
        "proveedor_nombre": prov_row.get("nombre", ""),
        "id_compra": compra.get("id_compra"),
        "numero_compra": compra.get("numero"),
        "importe": importe,
        "fecha": fecha,
        "id_mov_doble": id_mov_doble,
    }


def desaplicar_factura(
    *,
    id_cheque: int,
    id_factura: int,
    motivo: str = "",
    usuario: str = "web",
    conn=None,
) -> dict:
    """Deshace UNA aplicación específica cheque→factura (sin tocar el cheque).

    Útil cuando la dueña aplicó por error un cheque a una factura equivocada,
    pero el cheque sigue siendo válido (no rebotó). Atómico:

      1. Encuentra la(s) fila(s) chequesxfact con (id_cheque, id_fact).
      2. Recalcula factura.abono -= sum(importes) y el saldo con
         facturas.queries.saldo_de (importe − abono − retención),
         factura.stat según saldo.
      3. BORRA las filas chequesxfact (a diferencia del reverso del cheque
         entero, que las preserva).
      4. Registra mov_doble reverso linkeado al mov_doble de la aplicación
         original (tipo='cheque_aplicado_a_factura').

    Si el cheque no está en stat aplicable (Z/B/A...), levanta ValueError.
    Si no hay chequesxfact para el par (cheque, factura), idem.

    TMT 2026-05-13.
    """
    asegurar_fecha_abierta(today_ec())

    # TMT 2026-05-15: caller puede pasar `conn` (batch atómico).
    import contextlib as _ctx

    _tx = _ctx.nullcontext(conn) if conn is not None else db.tx()
    with _tx as conn:
        ch = db.fetch_one(
            "SELECT id_cheque, no_cheque, stat FROM scintela.cheque WHERE id_cheque = %s",
            (id_cheque,),
            conn=conn,
        )
        if not ch:
            raise ValueError(f"Cheque {id_cheque} no existe.")

        aplicaciones = (
            db.fetch_all(
                """
            SELECT id_chequexfact, importe FROM scintela.chequesxfact
             WHERE id_cheque = %s AND id_fact = %s
            """,
                (id_cheque, id_factura),
                conn=conn,
            )
            or []
        )
        if not aplicaciones:
            raise ValueError(f"No hay aplicaciones de cheque {id_cheque} a factura {id_factura}.")
        total_desaplicar = sum(float(a.get("importe") or 0) for a in aplicaciones)

        # Recomputar factura
        f = db.fetch_one(
            "SELECT id_factura, numf, importe, abono, retencion "
            "  FROM scintela.factura WHERE id_factura = %s",
            (id_factura,),
            conn=conn,
        )
        if not f:
            raise ValueError(f"Factura id={id_factura} no existe.")
        nuevo_abono = max(float(f.get("abono") or 0) - total_desaplicar, 0)
        nuevo_saldo = _fact_q.saldo_de(
            f.get("importe"), nuevo_abono, f.get("retencion")
        )
        nuevo_stat = _fact_q.stat_de(nuevo_saldo, nuevo_abono, tol=0.01)
        db.execute(
            "UPDATE scintela.factura "
            "SET abono=%s, saldo=%s, stat=%s, usuario_modifica=%s "
            "WHERE id_factura=%s",
            (nuevo_abono, nuevo_saldo, nuevo_stat, usuario, id_factura),
            conn=conn,
        )

        # Borrar las chequesxfact específicas (granular — preserva el resto)
        db.execute(
            """
            DELETE FROM scintela.chequesxfact
             WHERE id_cheque = %s AND id_fact = %s
            """,
            (id_cheque, id_factura),
            conn=conn,
        )

        # TMT 2026-05-21 dueña: el cheque después de desaplicar SIGUE
        # en Z (cartera), disponible para re-aplicar. Antes se marcaba
        # automáticamente como 'X' (Eliminado) cuando quedaba sin aplicaciones
        # vivas — esto generaba sorpresa ("¿por qué desapareció el cheque?").
        # Si la dueña quiere anular el cheque, lo hace manualmente desde la
        # pantalla del cheque.
        auto_anulado = False

        # Registrar mov_doble reverso linkeado al original.
        # SKILL.md "Lo que NO hacer": no `try/except: pass` silencioso
        # en mov_doble.registrar — si falla, debe burbujear. TMT 2026-05-14.
        import mov_doble as _md

        md_orig = db.fetch_one(
            """
            SELECT id_mov_doble, importe FROM scintela.mov_doble
             WHERE origen_table = 'cheque'
               AND origen_id    = %s
               AND destino_table = 'factura'
               AND destino_id    = %s
               AND tipo          = 'cheque_aplicado_a_factura'
               AND estado        = 'activo'
             ORDER BY id_mov_doble DESC LIMIT 1
            """,
            (id_cheque, id_factura),
            conn=conn,
        )
        _md.registrar(
            conn=conn,
            tipo="reverso_cheque_aplicacion",
            origen_table="cheque",
            origen_id=id_cheque,
            destino_table="factura",
            destino_id=id_factura,
            importe=total_desaplicar,
            fecha=today_ec(),
            concepto=(
                f"DESAPLICAR cheque #{id_cheque} de factura #{f.get('numf') or id_factura}"
                + (f" — {motivo}" if motivo else "")
            )[:200],
            usuario=usuario,
            metadata={
                "id_cheque": id_cheque,
                "id_factura": id_factura,
                "numf": f.get("numf"),
                "importe_desaplicado": total_desaplicar,
                "saldo_factura_post": nuevo_saldo,
                "stat_factura_post": nuevo_stat,
                "motivo": motivo or "",
            },
            id_original=md_orig["id_mov_doble"] if md_orig else None,
        )

    return {
        "id_cheque": id_cheque,
        "id_factura": id_factura,
        "importe_desaplicado": total_desaplicar,
        "saldo_factura_post": nuevo_saldo,
        "stat_factura_post": nuevo_stat,
        "cheque_auto_anulado": auto_anulado,
    }


def facturas_destino_para_mover(codigo_cli: str, id_fact_origen: int) -> list[dict]:
    """Facturas del cliente a las que se puede mudar un cobro.

    Vivas (les queda saldo) y distintas de la de origen. Ordenadas de la más
    vieja a la más nueva, que es como la dueña las nombra y el mismo criterio
    del FIFO de la cobranza.
    """
    return db.fetch_all(
        """
        SELECT id_factura, numf, numf_completo, fecha, importe, abono,
               COALESCE(retencion, 0) AS retencion, saldo, stat
          FROM scintela.factura
         WHERE codigo_cli = %s
           AND id_factura <> %s
           AND ABS(COALESCE(saldo, 0)) > 0.005
           AND UPPER(TRIM(COALESCE(stat, ''))) <> 'Y'
         ORDER BY fecha ASC, id_factura ASC
        """,
        ((codigo_cli or "").strip().upper(), int(id_fact_origen)),
    ) or []


def mover_aplicacion(
    *,
    id_cheque: int,
    id_fact_origen: int,
    id_fact_destino: int,
    motivo: str = "",
    usuario: str = "web",
) -> dict:
    """Mueve un cobro de una factura a otra, en UN paso y una transacción.

    🚨 POR QUÉ EXISTE (TMT 2026-08-24). Hasta hoy esto no se podía hacer: hay
    pantalla para DESAPLICAR, pero la de aplicar un cobro YA cargado se borró
    el 20/07 por huérfana. El único camino era volver a cargarlo por Cobranza
    — y toda carga con DEP.PICH. acuña un movimiento de banco nuevo.

    El 19/08 Alex necesitó mudar $500 de RAR de una factura a otra y lo hizo
    con un −500 y un +500. Las facturas quedaron bien, pero el negativo no
    dejaba asiento y el positivo sí: Pichincha quedó $500 arriba, y fueron los
    dos únicos movimientos que no conciliaron ese día. **No fue un descuido:
    era la única salida que le dejábamos.**

    Mover NO toca el banco ni el estado del cheque: la plata ya entró y sigue
    donde estaba. Lo único que cambia es a qué factura se le imputa.

    ⭐ Se apoya en `desaplicar_factura` + `aplicar_a_factura` compartiendo
    `conn`, en vez de reescribir la aritmética de la factura. Las dos ya saben
    recalcular abono, saldo y stat con `facturas.queries`; una tercera copia
    de esa cuenta es cómo se llega a dos pantallas que dicen números distintos
    de la misma factura.
    """
    cli_dest = db.fetch_one(
        "SELECT id_factura, numf, codigo_cli FROM scintela.factura WHERE id_factura = %s",
        (int(id_fact_destino),),
    )
    if not cli_dest:
        raise ValueError("La factura de destino no existe.")
    if int(id_fact_origen) == int(id_fact_destino):
        raise ValueError("La factura de destino es la misma que la de origen.")
    ch = db.fetch_one(
        "SELECT id_cheque, no_cheque, codigo_cli FROM scintela.cheque WHERE id_cheque = %s",
        (int(id_cheque),),
    )
    if not ch:
        raise ValueError(f"Cheque {id_cheque} no existe.")
    # 🚨 El cobro no cambia de dueño. Sin esto, un cobro de un cliente podría
    # terminar cancelando la factura de otro y el estado de cuenta de los dos
    # quedaría mal, cada uno por su lado y sin nada que los relacione.
    if (ch.get("codigo_cli") or "").strip().upper() != (
        cli_dest.get("codigo_cli") or ""
    ).strip().upper():
        raise ValueError(
            "La factura de destino es de otro cliente. Un cobro sólo se puede "
            "mover entre facturas del mismo cliente."
        )

    with db.tx() as conn:
        r_des = desaplicar_factura(
            id_cheque=int(id_cheque),
            id_factura=int(id_fact_origen),
            motivo=(f"Mover a factura #{cli_dest.get('numf') or id_fact_destino}"
                    + (f" — {motivo}" if motivo else ""))[:200],
            usuario=usuario,
            conn=conn,
        )
        importe = float(r_des.get("importe_desaplicado") or 0)
        if importe <= 0:
            # Una aplicación NEGATIVA es una corrección contable, no un cobro
            # que se pueda mudar: moverla dejaría a las dos facturas mal.
            raise ValueError(
                "Esta aplicación es negativa (una corrección), no un cobro. "
                "No se puede mover: desaplicala y volvé a cargar lo que "
                "corresponda."
            )
        # `permitir_depositado`: el cobro que se mueve casi siempre está
        # DEPOSITADO —es plata que ya entró—, y ése es justo el caso que hay
        # que resolver. El guard estricto sigue valiendo para el resto.
        r_apl = aplicar_a_factura(
            id_cheque=int(id_cheque),
            aplicaciones=[{"id_fact": int(id_fact_destino), "importe": importe}],
            usuario=usuario,
            conn=conn,
            permitir_depositado=True,
        )

    return {
        "id_cheque": int(id_cheque),
        "importe": importe,
        "id_fact_origen": int(id_fact_origen),
        "id_fact_destino": int(id_fact_destino),
        "numf_destino": cli_dest.get("numf"),
        "saldo_origen_post": r_des.get("saldo_factura_post"),
        "stat_origen_post": r_des.get("stat_factura_post"),
        "aplicado": r_apl,
    }


def reversar_endoso(
    *,
    id_cheque: int,
    motivo: str = "",
    usuario: str = "web",
) -> dict:
    """Reversa un endoso de cheque a proveedor.

    Deshace TODO lo que hizo `endosar()`, atómicamente:
      1. Encuentra la compra hermana (vía mov_doble de tipo
         'endoso_cheque_a_proveedor' con origen_id=id_cheque).
      2. Anula la compra (stat='Y', observación con motivo).
      3. Restaura el cheque a stat='Z' (cartera). Limpia prov, fechaout.
         Append observación con marca de reverso.
      4. Registra mov_doble del reverso con id_original apuntando al
         original — el INSERT automáticamente marca el original como
         estado='reversado' + id_reverso.

    Si el cheque NO está en stat='E', levanta ValueError.
    TMT 2026-05-13.
    """
    asegurar_fecha_abierta(today_ec())

    with db.tx() as conn:
        ch = db.fetch_one(
            "SELECT id_cheque, no_cheque, stat, codigo_cli, prov, importe "
            "FROM scintela.cheque WHERE id_cheque = %s",
            (id_cheque,),
            conn=conn,
        )
        if not ch:
            raise ValueError(f"Cheque {id_cheque} no existe.")
        if (ch.get("stat") or "").upper() != "E":
            raise ValueError(
                f"Cheque {id_cheque} no está endosado (stat='{ch.get('stat')}'). "
                "Sólo se puede reversar el endoso desde stat='E'."
            )

        # 1) Encontrar el mov_doble del endoso original.
        md_orig = db.fetch_one(
            """
            SELECT id_mov_doble, destino_table, destino_id, importe
              FROM scintela.mov_doble
             WHERE origen_table = 'cheque'
               AND origen_id    = %s
               AND tipo         = 'endoso_cheque_a_proveedor'
               AND estado       = 'activo'
             ORDER BY id_mov_doble DESC
             LIMIT 1
            """,
            (id_cheque,),
            conn=conn,
        )

        # 2) Compra hermana — del destino del mov_doble; fallback legacy
        # SÓLO si md_orig no existe (endoso pre-mov_doble). TMT 2026-05-14
        # (#47): antes el fallback corría aunque md_orig matcheara, y a
        # veces traía una compra distinta (otro cheque con mismo no_cheque
        # de otra época). Ahora confiamos en mov_doble cuando existe.
        id_compra = None
        if md_orig and md_orig.get("destino_table") == "compra":
            id_compra = md_orig.get("destino_id")
        elif md_orig is None:
            # Endoso legacy sin mov_doble: matchear por comprobante.
            row_c = db.fetch_one(
                """
                SELECT id_compra FROM scintela.compra
                 WHERE comprobante = %s
                   AND cuenta_pagada = 'E'
                   AND COALESCE(stat, '') != 'Y'
                 ORDER BY id_compra DESC LIMIT 1
                """,
                (f"CH{ch.get('no_cheque') or id_cheque}"[:20],),
                conn=conn,
            )
            id_compra = row_c.get("id_compra") if row_c else None

        # 3) Anular la compra hermana — si existe.
        if id_compra is not None:
            db.execute(
                """
                UPDATE scintela.compra
                   SET stat='Y',
                       observacion = COALESCE(observacion, '') ||
                                     E'\n[REVERSO endoso ch' || %s ||
                                     ' ' || CURRENT_DATE::text ||
                                     CASE WHEN %s != '' THEN E' — ' || %s ELSE '' END ||
                                     ']',
                       usuario_modifica=%s,
                       fecha_modifica=CURRENT_TIMESTAMP
                 WHERE id_compra=%s
                """,
                (id_cheque, motivo, motivo, usuario, id_compra),
                conn=conn,
            )

        # 4) Restaurar el cheque a cartera.
        stat_destino = "Z"
        marca = (
            f"[REVERSO_ENDOSO {today_ec().isoformat()} — antes a {ch.get('prov') or '?'}"
            + (f" — {motivo[:80]}" if motivo else "")
            + "]"
        )
        db.execute(
            "UPDATE scintela.cheque "
            "SET stat=%s, prov=NULL, fechaout=NULL, "
            "    observacion = RIGHT("
            "        COALESCE(observacion || ' | ', '') || %s, %s), "
            "    usuario_modifica=%s, fecha_modifica=CURRENT_TIMESTAMP "
            "WHERE id_cheque=%s",
            (stat_destino, marca, _OBS_CAP, usuario, id_cheque),
            conn=conn,
        )

        # 5) Registrar mov_doble del reverso linkeado al original.
        importe_reverso = float(md_orig.get("importe") or 0) if md_orig else float(ch.get("importe") or 0)
        import mov_doble as _md

        _md.registrar(
            conn=conn,
            tipo="reverso_endoso_cheque",
            origen_table="cheque",
            origen_id=id_cheque,
            destino_table="cheque",
            destino_id=id_cheque,
            importe=importe_reverso,
            fecha=today_ec(),
            concepto=(
                f"REVERSO endoso ch {ch.get('no_cheque') or id_cheque}" + (f" — {motivo}" if motivo else "")
            )[:200],
            usuario=usuario,
            metadata={
                "id_cheque_reversado": id_cheque,
                "id_compra_anulada": id_compra,
                "prov_anterior": ch.get("prov"),
                "stat_previo": "E",
                "stat_nuevo": stat_destino,
                "motivo": motivo or "",
            },
            id_original=md_orig["id_mov_doble"] if md_orig else None,
        )

    return {
        "id_cheque": id_cheque,
        "id_compra_anulada": id_compra,
        "stat_nuevo": stat_destino,
        "importe": importe_reverso,
        "motivo": motivo,
    }


def aplicar_a_factura(
    *,
    id_cheque: int,
    aplicaciones: list[dict],
    usuario: str = "web",
    batch_id: str | None = None,
    conn=None,
    permitir_depositado: bool = False,
) -> dict:
    """Aplicar un cheque a una o varias facturas.

    `aplicaciones` es [{id_fact, importe}, ...]. Cada fila:
      - inserta una `chequesxfact` con el importe y el abono_f/saldo_f calculados,
      - actualiza `factura.abono += importe` y el saldo con
        `facturas.queries.saldo_de` (importe − abono − retención),
      - cierra la factura (`stat='Z'`) si el saldo llega a 0.

    Todo en una sola transacción. Si alguna factura no existe o el importe
    excede el saldo pendiente, se revierte todo y se levanta ValueError.

    `factura.abono` es DERIVADA de chequesxfact — si algún día se "desaplica",
    hay que tocar las dos tablas en el mismo tx.

    `batch_id` (UUID) — si se pasa, todas las filas mov_doble generadas por
    esta llamada lo comparten. El caller (multi-cheque) genera un UUID al
    inicio del submit y lo pasa a TODAS las llamadas (crear + aplicar), así
    el reverso atómico de /historial las revierte juntas. TMT 2026-05-15.

    `conn` — opcional. Si se pasa, NO se abre tx propia (caller controla).
    Permite que multi-cheque haga crear() + aplicar() en la misma transacción
    para que el batch sea verdaderamente atómico (todo o nada).

    `permitir_depositado` — TMT 2026-06-10: Nueva Cobranza con banco de
    depósito (90/91/99) crea el cheque directamente en stat='B' (cobro
    directo, auto-flip en crear()). Ese cheque RECIÉN creado sí se puede
    aplicar a facturas en la misma transacción — es el flujo normal de un
    depósito/efectivo que cancela facturas. Sólo el flujo de creación pasa
    True; el guard estricto (#26) sigue vigente para cheques viejos ya
    depositados (ruta /cheques/<id>/aplicar y default False).
    """
    if not aplicaciones:
        raise ValueError("Sin facturas para aplicar.")

    total_aplicado = 0
    # TMT 2026-05-15: si el caller pasó `conn`, no abrimos tx propia —
    # él la maneja (multi-cheque atómico). Usamos contextlib.nullcontext
    # para mantener el bloque `with` igual en ambos paths.
    import contextlib as _ctx

    _tx = _ctx.nullcontext(conn) if conn is not None else db.tx()
    with _tx as conn:
        ch = db.fetch_one(
            "SELECT id_cheque, codigo_cli, no_banco, importe, stat, fecha "
            "FROM scintela.cheque WHERE id_cheque = %s",
            (id_cheque,),
            conn=conn,
        )
        if not ch:
            raise ValueError(f"Cheque {id_cheque} no existe.")
        # Validar stat aplicable. TMT 2026-05-14 (#26): antes esto
        # aceptaba B/A/E/etc, generando aplicaciones contra cheques ya
        # depositados/endosados/eliminados.
        stat_ch = (ch.get("stat") or "").upper()
        # TMT 2026-06-11: el flujo de creacion tambien aplica cheques 'C'
        # (efectivo 99, paridad dBase PASOCAJA) en la misma tx.
        stats_ok = STATS_APLICABLES + (("B", "C") if permitir_depositado else ())
        # TMT 2026-07-30 (dueña: "despues cancelalos") — banco 95 CANCELA
        # ANTICIPO. `crear()` ya anuló el 95 contra el espejo NB=98 del
        # anticipo (los dos a 'X', paridad ALTAS.PRG) DENTRO de esta misma
        # tx... y el 95 es justamente el cheque que tiene que cerrar la
        # factura que el anticipo paga. Sin esta excepción la cobranza moría
        # con "en stat='X' no se puede aplicar" y el saldo a favor no había
        # forma de usarlo.
        #
        # Por qué cierra la cuenta: la plata del anticipo ya entró en su día
        # (cheque real depositado); el espejo −900 es el crédito parqueado.
        # Al usarlo: espejo 'X' (−900 sale de cartera de cheques, +900) y la
        # factura baja 900 → cartera total IGUAL. El 95 no es plata nueva,
        # es la contrapartida contable, por eso también queda 'X' y no
        # infla la cartera.
        #
        # Alcance: SOLO el flujo de creación (permitir_depositado=True, el
        # cheque nació en esta tx) y SOLO no_banco=95. La ruta manual
        # /cheques/<id>/aplicar sigue sin poder tocar un cheque anulado.
        if permitir_depositado and int(ch.get("no_banco") or 0) == 95:
            stats_ok = stats_ok + ("X",)
        if stat_ch not in stats_ok:
            raise ValueError(
                f"Cheque {id_cheque} en stat='{stat_ch}' no se puede aplicar a "
                f"facturas. Sólo desde {STATS_APLICABLES} (cartera/postergado/Daniela)."
            )
        restante_cheque = float(ch["importe"] or 0)
        # Espejos de anticipo: cheque con importe NEGATIVO. Al aplicarlo a
        # una factura nueva, los importes vienen negativos también (resta
        # del abono). TMT 2026-05-14.
        es_espejo = restante_cheque < 0

        for a in aplicaciones:
            id_fact = int(a["id_fact"])
            imp = float(a["importe"])
            # Validar signo: debe matchear el del cheque (espejo de anticipo
            # = cheque con importe negativo → todas las aplicaciones también).
            # Cheques normales aceptan imp POSITIVO contra saldos positivos
            # (caso normal) o imp NEGATIVO contra saldos negativos (absorción
            # de crédito a favor del cliente — TMT 2026-05-15).
            if es_espejo:
                if imp >= 0:
                    raise ValueError(
                        f"Cheque {id_cheque} es espejo de anticipo (importe<0); "
                        f"el importe a aplicar a factura {id_fact} debe ser negativo."
                    )
            else:
                if abs(imp) < 0.005:
                    raise ValueError(f"Importe inválido para factura {id_fact}.")
            f = db.fetch_one(
                "SELECT id_factura, numf, importe, abono, retencion, saldo, stat "
                "FROM scintela.factura WHERE id_factura = %s",
                (id_fact,),
                conn=conn,
            )
            if not f:
                raise ValueError(f"Factura id={id_fact} no existe.")
            saldo_actual = float(f["saldo"] or 0)
            abono_actual = float(f["abono"] or 0)
            # Para espejos, |imp| no puede exceder el abono ya existente
            # (no podés revertir más abono del que hay). Para normales,
            # imp no puede exceder el saldo pendiente (signo a signo).
            if es_espejo:
                # TMT 2026-07-30 (dueña, caso ADI): el tope depende de qué
                # está haciendo el negativo, igual que en la rama de abajo.
                #   · factura con saldo NEGATIVO (nota de crédito / plata a
                #     favor del cliente): el negativo la SALDA — tope |saldo|.
                #     Es el caso de la devolución en efectivo: sale plata de
                #     caja y el crédito del cliente se cierra. Antes esto
                #     rebotaba con "excede el abono (0.00)" porque una NC
                #     nace con abono 0, así que no había forma de saldarla.
                #   · factura con saldo >= 0: el negativo REVIERTE un abono —
                #     tope el abono, no podés desabonar lo que no se abonó.
                # `permitir_sobre_saldo` = el monto lo TIPEÓ la dueña (espejo de
                # la sobre-aplicación positiva): devolvió más de lo que el
                # cliente tenía a favor y la factura queda debiendo. Es una
                # decisión, no un error de tipeo — no la topeamos.
                _sobre_ok = bool(a.get("permitir_sobre_saldo"))
                if saldo_actual < -0.005:
                    if not _sobre_ok and abs(imp) > abs(saldo_actual) + 0.01:
                        raise ValueError(
                            f"El negativo ({abs(imp):.2f}) excede el crédito a "
                            f"favor de la factura {f['numf']} "
                            f"({abs(saldo_actual):.2f})."
                        )
                elif not _sobre_ok and abs(imp) > abono_actual + 0.01:
                    raise ValueError(
                        f"El negativo ({abs(imp):.2f}) excede el abono de "
                        f"factura {f['numf']} ({abono_actual:.2f}) — no podés "
                        f"revertir más de lo abonado."
                    )
                nuevo_abono = abono_actual - abs(imp)
            else:
                # TMT 2026-05-15: para absorción de crédito (imp<0 contra
                # saldo<0), el signo debe matchear y |imp| <= |saldo|.
                if imp > 0:
                    # TMT 2026-06-30 (dueña, caso NOF / NC 10846): permitir aplicar
                    # un ABONO POSITIVO sobre una nota de crédito (saldo<0), IGUAL
                    # que el dBase — la NC acumula el abono y su saldo se hace más
                    # negativo (saldo = importe − abono). NO es "absorber": es un
                    # abono real en la línea. Sin tope de saldo (el saldo es
                    # negativo). Antes esto se rechazaba ("aplicá un importe
                    # NEGATIVO"), impidiendo repartir el cheque a una NC.
                    # TMT 2026-05-15: tolerancia de $50 — solo aplica a facturas
                    # con saldo POSITIVO. dBase legacy preguntaba "Faltan X, OK?".
                    # TMT 2026-07-01 (duena): si la duena eligio "dejar el
                    # sobrante como saldo a favor en ESTA factura", la sobre-
                    # aplicacion es intencional -> saltar el tope +$50.
                    _permitir_sobre = bool(a.get("permitir_sobre_saldo"))
                    if (not _permitir_sobre
                            and saldo_actual >= -0.005 and imp > saldo_actual + 50.00):
                        # TMT 2026-06-16: numf puede ser 0 (facturas asinfo) —
                        # usar el identificador real para que el mensaje sirva.
                        _ref = f.get("numf") or f"id {id_fact}"
                        raise ValueError(
                            f"Aplicación (${imp:,.2f}) a la factura {_ref} supera "
                            f"su saldo (${saldo_actual:,.2f}) por "
                            f"${imp - saldo_actual:,.2f}. Aplicá solo su saldo y "
                            f"dejá el resto como anticipo del cliente, o tildá otra "
                            f"factura para distribuir el resto."
                        )
                else:  # imp < 0 → dos casos distintos según el saldo:
                    # (a) saldo NEGATIVO (nota de crédito / sobrepago a favor
                    #     del cliente): el negativo ABSORBE el crédito. Tope =
                    #     |saldo| (no el abono — la NC arranca con abono 0).
                    #     TMT 2026-06-10: el fix del 06-06 trataba TODO
                    #     negativo como reversa y bloqueaba absorber la NC
                    #     ("excede el abono (0.00)") aunque el propio flujo
                    #     de arriba te manda a aplicar negativo contra
                    #     saldos negativos.
                    # (b) saldo >= 0: REVERSA de abono (abono mal cargado).
                    #     Tope = lo abonado. TMT 2026-06-06.
                    if saldo_actual < -0.005:
                        if abs(imp) > abs(saldo_actual) + 0.01:
                            raise ValueError(
                                f"El importe negativo ({abs(imp):.2f}) excede "
                                f"el crédito a favor de la factura {f['numf']} "
                                f"({abs(saldo_actual):.2f})."
                            )
                    elif abs(imp) > abono_actual + 0.01:
                        raise ValueError(
                            f"El importe negativo ({abs(imp):.2f}) excede el "
                            f"abono de la factura {f['numf']} "
                            f"({abono_actual:.2f}) — no podés revertir más de "
                            f"lo abonado."
                        )
                nuevo_abono = abono_actual + imp
            nuevo_saldo = _fact_q.saldo_de(
                f["importe"], nuevo_abono, f.get("retencion")
            )
            # Vocabulario canónico (2026-04-29, restaurado 2026-05-15):
            # El paso de confirmación puede pasar `forzar_stat='T'|'A'` por
            # aplicación. Si viene, ese gana sobre la lógica automática.
            # Sin override:
            #   saldo ≤ 0  → 'T' (cancelada — cubierto entero)
            #   |saldo| ≤ $0.50 → 'T' (centavos olvidados, auto)
            #   saldo > $0.50 con abono → 'A' (abonada parcial)
            #   abono = 0 → preserva el stat actual o 'Z'
            forzar_stat = (a.get("forzar_stat") or "").upper().strip()
            # TMT 2026-07-01 (duena): saldo NEGATIVO (over-pago = credito) queda
            # como 'A' (saldo a favor vivo), NO 'T'. Solo el saldo ~0 (|saldo|
            # <=$0.50) totaliza. Antes `nuevo_saldo <= 0.01` mandaba el credito
            # -42,08 a 'T' y desaparecia de cartera.
            if forzar_stat in ("T", "A"):
                nuevo_stat = forzar_stat
            elif abs(nuevo_saldo) <= 0.50:
                nuevo_stat = "T"
            elif nuevo_abono > 0.01:
                nuevo_stat = "A"
            else:
                nuevo_stat = f["stat"] or "Z"

            db.execute(
                """
                INSERT INTO scintela.chequesxfact
                    (id_cheque, id_fact, fechaing, codigo_cli, importe,
                     no_banco, abono_f, saldo_f, stat_f, usuario_crea)
                VALUES (%s, %s, CURRENT_DATE, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    id_cheque,
                    id_fact,
                    ch["codigo_cli"],
                    imp,
                    ch["no_banco"],
                    nuevo_abono,
                    nuevo_saldo,
                    nuevo_stat,
                    usuario,
                ),
                conn=conn,
            )
            db.execute(
                """
                UPDATE scintela.factura
                   SET abono = %s, saldo = %s, stat = %s,
                       usuario_modifica = %s
                 WHERE id_factura = %s
                """,
                (nuevo_abono, nuevo_saldo, nuevo_stat, usuario, id_fact),
                conn=conn,
            )
            total_aplicado += imp

            # Historial unificado: cada aplicación es un movimiento doble
            # cheque → factura. SKILL.md "Lo que NO hacer": no try/except:
            # pass silencioso en mov_doble.registrar — debe burbujear.
            # TMT 2026-05-14.
            import mov_doble as _md

            # Fallback de fecha: ch.fecha puede ser NULL para cheques
            # legacy importados sin fecha — usar HOY. Antes pasaba None y
            # mov_doble guardaba fecha NULL. TMT 2026-05-14 (#29).
            fecha_md = ch.get("fecha") or today_ec()
            _md.registrar(
                conn=conn,
                tipo="cheque_aplicado_a_factura",
                origen_table="cheque",
                origen_id=id_cheque,
                destino_table="factura",
                destino_id=id_fact,
                importe=imp,
                fecha=fecha_md,
                concepto=(f"Cheque #{id_cheque} → Factura #{f.get('numf') or id_fact} ({imp:.2f})")[:200],
                usuario=usuario,
                metadata={
                    "id_cheque": id_cheque,
                    "id_factura": id_fact,
                    "numf": f.get("numf"),
                    "saldo_factura_post": nuevo_saldo,
                    "stat_factura_post": nuevo_stat,
                },
                batch_id=batch_id,
            )

        # Para espejos (importe<0) comparamos en valor absoluto.
        if es_espejo:
            if abs(total_aplicado) > abs(restante_cheque) + 0.01:
                raise ValueError(
                    f"Total aplicado ({abs(total_aplicado):.2f}) excede el "
                    f"importe del espejo ({abs(restante_cheque):.2f}). "
                    f"Agregá otro cheque negativo para repartir el reverso, "
                    f"o ajustá los importes a aplicar."
                )
        else:
            if total_aplicado > restante_cheque + 0.01:
                raise ValueError(
                    f"Total aplicado ({total_aplicado:.2f}) excede el importe del cheque "
                    f"({restante_cheque:.2f}). Agregá otro cheque o revisá los importes aplicados."
                )

    return {"id_cheque": id_cheque, "total_aplicado": total_aplicado, "n": len(aplicaciones)}


def _freno_si_el_totalizar_se_llevo_los_vinculos(id_cheque: int, conn=None) -> None:
    """Frena la vuelta atrás de un cheque cuyos vínculos borró un TOTALIZAR.

    TMT 2026-08-20. `anular_por_error_de_carga` y la anulación administrativa
    de `reversar` arman qué desabonar leyendo `scintela.chequesxfact`. Y
    `totalizar_estado_cuenta_ejecutar` **borra esa tabla a propósito** para
    las facturas del cliente (decisión dueña #1: con el abono redistribuido
    el vínculo 1-a-1 dejó de ser cierto).

    Combinados: el `SELECT` vuelve vacío, el `for` no itera, **la factura se
    queda con el abono puesto** — y el cheque igual pasa a 'X' con su
    compensación en el banco. La plata sale de un lado y no del otro, sin que
    nada avise. Medido el 20/08: 114 cheques por $103.002,62 en 10 clientes
    están en ese estado, los 114 explicados por un totalizar del mismo
    cliente (cero falsos positivos).

    El detector es el mismo que ya usa `concepto_cobro`: aplicación ACTIVA en
    `mov_doble` y ni una fila en `chequesxfact`.

    ⚖️ NO frena el REBOTE REAL (B→1, V→2, 1/2→3): ahí la factura no se toca
    (decisión 2026-07-25, copiar el dBase), así que el vínculo borrado no
    cambia nada — y frenar un rebote sería absurdo, la plata ya se fue del
    banco. Sólo frena los dos caminos administrativos, que son errores de
    carga y por lo tanto se pueden posponer hasta deshacer el totalizar.
    """
    fila = db.fetch_one(
        """
        SELECT c.no_cheque, c.codigo_cli,
               (SELECT MAX(m2.fecha_creacion) FROM scintela.mov_doble m2
                 WHERE m2.tipo = 'totalizar_estado_cuenta' AND m2.estado = 'activo'
                   AND m2.metadata ->> 'codigo_cli' = c.codigo_cli) AS totalizado_el
          FROM scintela.cheque c
         WHERE c.id_cheque = %s
           AND EXISTS (SELECT 1 FROM scintela.mov_doble m
                        WHERE m.tipo = 'cheque_aplicado_a_factura'
                          AND m.origen_id = c.id_cheque AND m.estado = 'activo')
           AND NOT EXISTS (SELECT 1 FROM scintela.chequesxfact x
                            WHERE x.id_cheque = c.id_cheque)
        """,
        (id_cheque,), conn=conn,
    )
    if not fila:
        return
    cuando = fila.get("totalizado_el")
    cuando_txt = f" ({cuando.strftime('%d/%m/%Y')})" if cuando else ""
    raise ValueError(
        f"El cheque {str(fila.get('no_cheque') or '').strip()} se aplicó a "
        f"facturas de {fila.get('codigo_cli')}, y después se totalizó ese "
        f"estado de cuenta{cuando_txt}: el vínculo con las facturas ya no "
        "está. Anularlo ahora sacaría la plata del banco y dejaría el abono "
        "puesto en las facturas. Deshacé el totalizar desde el ↺ del "
        "Historial y volvé a intentar. Si ya no se puede deshacer, corregí "
        "el abono a mano desde la ficha de la factura."
    )


def _stat_destino_reversa(stat_prev: str) -> tuple[str, bool]:
    """Devuelve (stat_destino, es_rebote_real) según el vocabulario nuevo.

    Reglas (2026-04-29, V corregido 2026-07-26):
      - B → 1   (primer rebote del banco — REBOTE REAL)
      - V → 2   (rebote de un RE-DEPÓSITO de devuelto — REBOTE REAL, 2° rebote)
      - 1 → 3   (segundo rebote — REBOTE REAL)
      - 2 → 3   (segundo rebote desde alias 2 — REBOTE REAL)
      - A → 1   (legacy acreditado rebotado tardío — REBOTE REAL)
      - Z → X   (eliminado por error — administrativo)
      - D → X   (Daniela cancela, devuelve cheque — administrativo)
      - P → X   (postergado anulado — administrativo)
      - X, R, 3 → ValueError (terminal, no se puede reversar más)

    dBase (MODIFICA.PRG FIL3): un cheque DEPOSITADO (ENBANC='BVWIJK', incluye V)
    sólo puede rebotar a 1/2 creando una nota de débito (ND) en el banco — NUNCA
    a X. V es un re-depósito de un devuelto, así que su rebote es el 2° → '2'.
    Anular un V mal cargado va por `anular_por_error_de_carga`, no por acá.
    """
    s = (stat_prev or "").upper()
    # Depositado feliz: primer rebote → 1. B es el depósito de hoy; A/W/I/J/K
    # son los depositados legacy del DBF (I = INTERNACIONAL).
    #
    # ⭐ TMT 2026-08-11 (dueña: "internacional se tiene que manejar igual que
    # pichincha"). W/I/J/K caían en el default de abajo y volvían ("X", False):
    # un cheque depositado en Internacional que el banco devolvía terminaba
    # ELIMINADO en vez de quedar en cartera como devuelto. La nota de débito y
    # el gasto salían bien —el banco quedaba correcto—, pero el cheque
    # desaparecía y el cliente dejaba de deberlo. Contra el dBase, que trata a
    # los seis igual (`ENBANC='BVWIJK'`, MODIFICA.PRG FIL3: un depositado sólo
    # rebota a 1/2, NUNCA a X) y contra la propia docstring de acá arriba.
    if s in STATS_DEPOSITADO and s != "V":
        return "1", True
    # V = re-depósito de un devuelto (DEPOSITADO). Su rebote es REBOTE REAL: crea
    # ND y vuelve a cartera como devuelto (2° rebote). NO es anulación a X.
    if s == "V":
        return "2", True
    # Ya rebotado una vez (1 o 2): segundo rebote → 3.
    if s in ("1", "2"):
        return "3", True
    # Vivos no depositados (Z/D/P): eliminación administrativa.
    if s in ("Z", "D", "P"):
        return "X", False
    # Terminales (X eliminado, R legacy rebotado, 3 segundo rebote): no más.
    if s in ("X", "R", "3"):
        raise ValueError(f"Cheque en stat='{s}' es terminal — no se puede reversar.")
    # Sin stat o stat desconocido: tratar como Z (eliminación por error).
    return "X", False


def reversar(
    *,
    id_cheque: int,
    motivo: str = "",
    usuario: str = "web",
) -> dict:
    """Reversar un cheque.

    Máquina de estados (vocabulario canónico 2026-04-29, V corregido 2026-07-26):
        Z, D, P (cartera/Daniela/postergado)
                          → X (eliminado por error) — administrativo, sin stop
        B (depositado desde cartera)
                          → 1 (primer rebote) — REBOTE REAL, stop al cliente
        V (re-depósito de un devuelto)
                          → 2 (segundo rebote) — REBOTE REAL, stop al cliente
        1, 2 (devueltos)  → 3 (segundo rebote)  — REBOTE REAL, stop al cliente
        A (legacy acred.) → 1 (rebote tardío)   — REBOTE REAL, stop al cliente
        X, R, 3 (terminales) → ValueError

    Facturas (TMT 2026-07-25, alinear a dBase):
      - REBOTE REAL (B→1, V→2, 1/2→3): NO se toca la factura. dBase solo descuenta
        el banco (ND); el cheque vuelve a cartera como devuelto (por-cobrar) y la
        factura queda aplicada. Reabrirla es manual.
      - ANULACIÓN administrativa (Z/D/P→X): SÍ se revierte cada chequesxfact
        (resta abono, suma saldo, abre la factura) y se borran las aplicaciones,
        porque el cheque fue un error de carga.

    Side-effect: cuando el stat previo era B/1/2/A (rebote real del banco),
    el cliente queda en stop='S' con traza en observacion. Z/D/P/V → X es
    una anulación administrativa, no dispara stop. Idempotente: si ya
    estaba en stop, no pisa nada; rowcount=0 ⇒ stop_aplicado=False.

    Guardas:
      - `asegurar_fecha_abierta(today_ec())` — la reversión se asienta con
        fecha de hoy (fechaout=CURRENT_DATE), así que el período contable de
        hoy tiene que estar abierto, no el del cheque original.
      - El append a `cliente.observacion` va capado con RIGHT(..., 200) porque
        la columna es varchar(200) (SCHEMA.txt); clientes con varios rebotes
        desbordaban antes de este cap.

    Todo en una sola transacción.
    """
    # Guard de período: la reversión se escribe con fecha de hoy.
    asegurar_fecha_abierta(today_ec())

    with db.tx() as conn:
        ch = db.fetch_one(
            "SELECT id_cheque, no_cheque, stat, codigo_cli, importe FROM scintela.cheque WHERE id_cheque = %s",
            (id_cheque,),
            conn=conn,
        )
        if not ch:
            raise ValueError(f"Cheque {id_cheque} no existe.")
        stat_prev = (ch["stat"] or "").upper()
        # _stat_destino_reversa levanta si stat_prev es terminal (X/R/3).
        stat_nuevo, es_rebote_real = _stat_destino_reversa(stat_prev)
        # Sólo la anulación administrativa revierte las facturas; el rebote
        # real no las toca, así que el vínculo borrado no le cambia nada.
        if not es_rebote_real:
            _freno_si_el_totalizar_se_llevo_los_vinculos(id_cheque, conn=conn)

        # TMT 2026-07-23 (dueña): si el cheque estaba DEPOSITADO, el rebote debe
        # descontar el importe del banco (nota de débito) — sino queda contado
        # doble (banco + cartera viva del cheque devuelto). Idempotente: no hace
        # nada si no hay depósito 'DE' vivo o si ya se compensó. Ver
        # compensar_deposito_devuelto.
        compensar_deposito_devuelto(
            conn,
            id_cheque=id_cheque,
            importe=float(ch.get("importe") or 0),
            codigo_cli=ch.get("codigo_cli"),
            no_cheque=ch.get("no_cheque"),
            fecha=today_ec(),
            usuario=usuario,
        )

        # TMT 2026-07-25 (dueña, copiar dBase MODIFICA.PRG): en un REBOTE REAL
        # (B→1, 1/2→3) NO se revierte la factura ni se borran las aplicaciones.
        # dBase solo descuenta el banco (ND) — el cheque vuelve a cartera como
        # DEVUELTO (por-cobrar) y la factura queda aplicada; reabrirla es MANUAL.
        # Revertirla acá duplicaba el por-cobrar (factura reabierta + cheque
        # devuelto vivo). En una ANULACIÓN administrativa (Z/D/P/V→X) SÍ se
        # revierte, porque el cheque fue un error y no debe dejar rastro.
        # Traer aplicaciones para revertir (solo si NO es rebote real).
        aplic = db.fetch_all(
            "SELECT id_chequexfact, id_fact, importe FROM scintela.chequesxfact WHERE id_cheque = %s",
            (id_cheque,),
            conn=conn,
        )
        for ap in (aplic if not es_rebote_real else []):
            id_fact = ap["id_fact"]
            imp = float(ap["importe"] or 0)
            if not id_fact:
                continue
            f = db.fetch_one(
                "SELECT importe, abono, retencion FROM scintela.factura "
                "WHERE id_factura = %s",
                (id_fact,),
                conn=conn,
            )
            if not f:
                continue
            nuevo_abono = max(float(f["abono"] or 0) - imp, 0)
            nuevo_saldo = _fact_q.saldo_de(f["importe"], nuevo_abono, f.get("retencion"))
            # Vocabulario canónico (2026-04-29) — al reversar, restamos el
            # abono. El estado sale de la regla única (ver _fact_q.stat_de):
            #   |saldo| ≈ 0  → 'T'   ·   saldo a FAVOR → 'A' (viva)
            #   hay abono    → 'A'   ·   sin abono     → 'Z'
            nuevo_stat = _fact_q.stat_de(nuevo_saldo, nuevo_abono, tol=0.01)
            db.execute(
                "UPDATE scintela.factura "
                "SET abono=%s, saldo=%s, stat=%s, usuario_modifica=%s "
                "WHERE id_factura=%s",
                (nuevo_abono, nuevo_saldo, nuevo_stat, usuario, id_fact),
                conn=conn,
            )

        # Marcar el cheque con el stat destino calculado por
        # _stat_destino_reversa (X para administrativo, 1 o 3 para rebote real).
        db.execute(
            "UPDATE scintela.cheque "
            "SET stat=%s, fechaout=CURRENT_DATE, usuario_modifica=%s "
            "WHERE id_cheque=%s",
            (stat_nuevo, usuario, id_cheque),
            conn=conn,
        )

        # Bug G fix (TMT 2026-05-16): borrar las aplicaciones chequesxfact
        # del cheque reversado. Antes quedaban vivas apuntando a un cheque
        # con stat='X', lo que ensuciaba el detalle de factura y podía
        # bloquear futuras anulaciones de factura con falso "cheque vivo".
        # TMT 2026-07-25: SOLO en anulación administrativa. En un rebote real la
        # aplicación se MANTIENE (la factura sigue paga, como en dBase); borrarla
        # bajaría el abono derivado y reabriría la factura, que es lo que NO
        # queremos.
        if not es_rebote_real:
            db.execute(
                "DELETE FROM scintela.chequesxfact WHERE id_cheque=%s",
                (id_cheque,),
                conn=conn,
            )

        # --- El espejo se va con el padre (TMT 2026-08-19) ---
        # Z/D/P/V → X es la MISMA anulación administrativa que "anular por
        # error de carga" ("me confundí al cargarlo"), así que tiene que
        # llevarse el espejo NB=98 del anticipo igual que aquélla: si no, el
        # cliente queda con un saldo a favor que nadie le debe y la utilidad
        # baja de una sola punta. Un REBOTE real no: ahí el cheque sigue vivo
        # como devuelto y el par cheque/espejo se sigue neteando.
        espejos_anulados: list[int] = []
        if not es_rebote_real:
            for _id_esp in espejos_vivos_de(id_cheque, conn=conn):
                anular_por_error_de_carga(
                    _id_esp,
                    motivo=(
                        f"espejo del cheque {ch.get('no_cheque') or id_cheque} "
                        f"reversado"
                    ),
                    usuario=usuario,
                    conn=conn,
                    _en_cascada=True,
                )
                espejos_anulados.append(_id_esp)

        # TMT 2026-05-21 dueña: el STOP es SOLO MANUAL. No marcar
        # automáticamente al rebotar. La obs sí queda anotada para que
        # vea quién rebotó y decida si pone STOP manualmente.
        stop_aplicado = False
        es_rebote_real = es_rebote_real and bool(ch["codigo_cli"])
        if es_rebote_real:
            marca = f"[REBOTE] CHEQUE {ch['no_cheque'] or '#' + str(id_cheque)} {today_ec().isoformat()}"
            if motivo:
                marca += f" — {motivo[:80]}"
            # Anotar en observación SIN tocar el flag stop (lo decide la
            # dueña manualmente desde /clientes/<codigo>/stop).
            db.execute(
                "UPDATE scintela.cliente "
                "SET observacion = RIGHT("
                "        COALESCE(observacion || ' | ', '') || %s, %s), "
                "    usuario_modifica = %s "
                "WHERE codigo_cli = %s",
                (marca, _OBS_CAP, usuario, ch["codigo_cli"]),
                conn=conn,
            )

        # Si era postdatado, borrar su posdat
        db.execute(
            "DELETE FROM scintela.posdat WHERE COALESCE(banc, 0) = 0 AND num=%s AND prov=%s",
            (id_cheque, ch["codigo_cli"]),
            conn=conn,
        )

        # Historial unificado: registrar el reverso del cheque.
        # SKILL.md "Lo que NO hacer": no try/except: pass silencioso en
        # mov_doble.registrar — debe burbujear. TMT 2026-05-14.
        #
        # Bug A fix (TMT 2026-05-16): buscar el mov_doble original
        # (`cheque_creado` activo) y pasarlo como `id_original` para que
        # `mov_doble.registrar()` lo marque como `estado='reversado'` +
        # `id_reverso=<id_nuevo>`. Antes el original quedaba `activo` y
        # rompía la trazabilidad histórico→reverso (audit C 2026-05-16).
        import mov_doble as _md

        tipo_reverso = "reverso_cheque_rebote" if es_rebote_real else "reverso_cheque_administrativo"
        total_reversado = sum(float(a.get("importe") or 0) for a in aplic) or 1.0
        md_orig_cheque = db.fetch_one(
            """
            SELECT id_mov_doble FROM scintela.mov_doble
             WHERE origen_table='cheque' AND origen_id=%s
               AND tipo='cheque_creado' AND estado='activo'
             ORDER BY id_mov_doble DESC LIMIT 1
            """,
            (id_cheque,),
            conn=conn,
        )
        _id_reverso = _md.registrar(
            conn=conn,
            tipo=tipo_reverso,
            origen_table="cheque",
            origen_id=id_cheque,
            destino_table="cheque",
            destino_id=id_cheque,
            importe=total_reversado,
            fecha=today_ec(),
            concepto=(
                f"REVERSO cheque {ch.get('no_cheque') or id_cheque} "
                f"{stat_prev}→{stat_nuevo}" + (f" — {motivo}" if motivo else "")
            )[:200],
            usuario=usuario,
            metadata={
                "id_cheque": id_cheque,
                "stat_previo": stat_prev,
                "stat_nuevo": stat_nuevo,
                "es_rebote_real": es_rebote_real,
                "stop_aplicado": stop_aplicado,
                "n_aplicaciones_reversadas": len(aplic),
                "motivo": motivo or "",
            },
            id_original=md_orig_cheque["id_mov_doble"] if md_orig_cheque else None,
        )
        # También marcar como reversadas las aplicaciones del cheque
        # (`cheque_aplicado_a_factura`) que también seguían `activo`.
        # id_reverso apunta al reverso del cheque padre — ver la nota en
        # anular_por_error_de_carga. TMT 2026-07-29.
        db.execute(
            """
            UPDATE scintela.mov_doble
               SET estado='reversado', id_reverso=%s
             WHERE origen_table='cheque' AND origen_id=%s
               AND tipo='cheque_aplicado_a_factura' AND estado='activo'
            """,
            (_id_reverso, id_cheque),
            conn=conn,
        )

    return {
        "id_cheque": id_cheque,
        "reversadas": len(aplic),
        "motivo": motivo,
        "codigo_cli": ch["codigo_cli"],
        "stat_previo": stat_prev,
        "stat_nuevo": stat_nuevo,
        "es_rebote_real": es_rebote_real,
        "stop_aplicado": stop_aplicado,
        "espejos_anulados": espejos_anulados,
    }


def facturas_pendientes(codigo_cli: str, limite: int = 200) -> list[dict]:
    """Facturas con saldo distinto de cero de un cliente.

    Orden FIFO — la más vieja primero. NO cambiar: ver la nota del ORDER BY.

    Incluye las dos puntas para que la dueña pueda aplicar un cheque
    cancelando facturas vivas Y absorbiendo créditos a favor del cliente:

      - Saldo > 0: factura pendiente normal (stat válido o NULL).
      - Saldo < 0: devolución o sobre-aplicación — el cliente tiene
        crédito a favor. Sin importar el stat (suele quedar 'T' o 'A')
        porque la idea es netear contra una factura positiva existente.

    TMT 2026-05-15: antes filtraba `saldo > 0` y dejaba fuera todas las
    devoluciones/sobre-aplicaciones; al aplicar un cheque no se podían
    netear con facturas vivas → quedaba dinero sin imputar.
    """
    return db.fetch_all(
        """
        SELECT id_factura, numf, numf_completo, fecha, vencimiento,
               importe, abono, retencion, saldo, stat
        FROM scintela.factura
        WHERE codigo_cli = %s
          -- TMT 2026-06-17 (dueña, caso NJL/Bedon): excluir asinfo-backfill.
          -- Son facturas históricas de Asinfo que dBase ya no tiene abiertas
          -- (no están en el DBF) y NO cuentan en cartera/TOTF — pero se colaban
          -- en la lista de cobranza mostrándose como pendientes. Mismo criterio
          -- que informes (NO_BACKFILL_WHERE). Las cargadas con el botón
          -- (asinfo-carga) SÍ siguen apareciendo.
          AND COALESCE(usuario_crea, '') <> 'asinfo-backfill'
          AND COALESCE(saldo, 0) <> 0
          AND (
            -- vivas (positivo): stat válido
            (COALESCE(saldo, 0) > 0
             AND (stat IS NULL OR stat IN ('A','Z','',' ')))
            OR
            -- crédito a favor (negativo): SOLO si sigue ABIERTO. TMT 2026-06-30
            -- dueña: si el estado es T (cerrada/totalizada) se oculta igual que
            -- el dBase — una devolución/crédito ya consumido no debe reaparecer
            -- en la cobranza. Las abiertas (Z/A) sí se ven para netear.
            (COALESCE(saldo, 0) < 0
             AND (stat IS NULL OR stat IN ('A','Z','',' ')))
          )
        -- TMT 2026-05-15: orden cronológico puro (positivas y negativas
        -- mezcladas por fecha de emisión / vencimiento). La separación
        -- previa por signo confundía visualmente al aplicar.
        --
        -- NO CAMBIAR ESTE ORDEN. TMT 2026-07-30: se probó ponerlo DESC para
        -- imitar al dBase y la dueña lo revirtió en el acto — "estamos
        -- cambiando algo de la cobranza que ya teníamos hecho? eso no me
        -- gusta". La cobranza de PC funciona y FIFO es lo contablemente sano:
        -- el cliente paga lo más viejo que debe. Que el dBase haga otra cosa
        -- NO es razón para cambiarla.
        --
        -- Queda documentado igual, porque explica una divergencia que costó
        -- medio día entender. El FoxPro imputa al revés, y está en ALTAS.PRG,
        -- PROCEDURE REPLA:
        --
        --     GO BOTT
        --     REPLA ABONO WITH ABONO+D->IMPORTE, SALDO WITH SALDO-D->IMPORTE
        --
        -- `GO BOTT` va a la ÚLTIMA fila del cliente, que con el SORT ON
        -- FECHA, NUMF del pase diario es la más reciente. Y no hay ningún IF:
        -- suma el cheque entero a esa fila y deja el saldo irse a negativo.
        --
        -- Eso explica la divergencia: PC y el dBase imputan la misma plata a
        -- facturas distintas —extremos opuestos de la misma lista— y por eso
        -- `chequesxfact` de PC no suma el `abono` del DBF. No son links
        -- inventados y no hay nada que reparar: son dos criterios.
        --
        -- Consecuencia práctica: el chequeo NO puede exigir que coincidan. Lo
        -- que sí vale es el criterio del dBase sobre las notas de entrega y de
        -- crédito — no las distingue de una factura y deja el saldo irse a
        -- negativo sin chistar. Ver check_chequesxfact.
        ORDER BY fecha, vencimiento NULLS LAST, numf
        LIMIT %s
        """,
        (codigo_cli, limite),
    )


def cheques_vivos(codigo_cli: str, limite: int = 200) -> list[dict]:
    """Cheques VIVOS (stat en Z/1/2/3/P/D, importe > 0) de un cliente.

    TMT 2026-07-06 (dueña): alimenta el panel "anticipo (97) → cancelar
    cheques en cartera" de /cheques/nuevo. Mismo grupo que TOTC
    (STAT $ "Z123PD"). Los importes NEGATIVOS (espejos NB=98 / NC) quedan
    afuera — no son cheques físicos cancelables. Orden FIFO por fecha de
    depósito (el más próximo a depositarse se cancela primero).
    """
    return db.fetch_all(
        """
        SELECT id_cheque, no_cheque, fecha, fechad, importe, no_banco,
               COALESCE(banco, '') AS banco, stat
          FROM scintela.cheque
         WHERE codigo_cli = %s
           AND TRIM(COALESCE(stat, '')) IN ('Z','1','2','3','P','D')
           AND COALESCE(importe, 0) > 0
         ORDER BY fechad NULLS LAST, fecha, id_cheque
         LIMIT %s
        """,
        (codigo_cli, limite),
    )


def anticipos_vivos(codigo_cli: str, limite: int = 200) -> list[dict]:
    """Anticipos VIVOS del cliente — los espejos NB 97/98 en Z, importe < 0.

    TMT 2026-07-30 (dueña: *"cuando pongo 95 me tiene que mostrar cuales puedo
    aplicar"*). Es el espejo exacto de `cheques_vivos`, para el otro lado de la
    operación: el 97 CREA el anticipo (y ahí se eligen los cheques a cancelar),
    el **95 lo USA**, y hasta ahora había que acertar el importe de memoria —
    PC busca el espejo por importe EXACTO y, si no coincide al centavo, deja el
    cheque en cartera y avisa (paridad ALTAS.PRG: "NO SE ENCUENTRA EL
    ANTICIPO").

    Devuelve el importe en POSITIVO (`importe_abs`) además del original: es lo
    que hay que tipear en el cheque 95.

    TMT 2026-07-30, segunda vuelta: NO se filtra por stat='Z' aunque el match del
    95 exija exactamente eso. El primer caso real (GL1, espejo #99282 de −900)
    estaba **POSTERGADO**, y un panel que filtra por Z habría dicho "este cliente
    no tiene anticipos vivos" — mintiendo, porque el anticipo existe y la plata
    está. Se traen todos los espejos del grupo vivo (Z/1/2/3/P/D) con
    `aplicable` en True: desde el mismo día el MATCH del 95 acepta ese mismo
    grupo (dueña: *"deberia mostrar postergados igual, no se porque
    filtramos"*), así que todo lo que se muestra se puede usar. El flag queda
    por si algún día hay que volver a distinguir.
    """
    return db.fetch_all(
        """
        SELECT id_cheque, no_cheque, fecha, fechad, importe,
               ABS(COALESCE(importe, 0)) AS importe_abs,
               no_banco, COALESCE(banco, '') AS banco,
               TRIM(COALESCE(stat, '')) AS stat,
               TRUE AS aplicable
          FROM scintela.cheque
         WHERE codigo_cli = %s
           AND no_banco IN (97, 98)
           AND TRIM(COALESCE(stat, '')) IN ('Z', '1', '2', '3', 'P', 'D')
           AND COALESCE(importe, 0) < 0
         ORDER BY (TRIM(COALESCE(stat, '')) = 'Z') DESC, fecha, id_cheque
         LIMIT %s
        """,
        (codigo_cli, limite),
    )


def total_buscar(
    q: str = "",
    estado: str = "todos",
    desde: str | None = None,
    hasta: str | None = None,
    cliente: str | None = None,
    monto_min: float | None = None,
    monto_max: float | None = None,
    vendedor: str | None = None,
    medio: str = "",
) -> dict:
    """SUM(importe) + COUNT(*) sobre TODO el universo del filtro (sin LIMIT).

    Útil para mostrar "Total" en el listado: el total visible está limitado
    a `limite` filas, pero el total real del filtro lo sacamos en una query
    aparte con la misma cláusula WHERE.

    🚨 `medio` tiene que llegar con LO MISMO que recibió `buscar()`. Este total
    es el que se muestra arriba del listado: si acá entra un universo más
    grande que el de las filas, el número de arriba no cierra con la suma de
    abajo — que es exactamente la clase de diferencia que se lee como bug.
    """
    q = (q or "").strip()
    like = f"%{q}%" if q else None
    # Nombre de cliente / proveedor endosado: match por PALABRAS sueltas.
    _nom_cli, _p_cli = busqueda.condicion(q, ("cli.nombre",), prefijo="bqn")
    _nom_prv, _p_prv = busqueda.condicion(q, ("prv.nombre",), prefijo="bqp")
    stats = STATS.get(estado)
    # TMT 2026-05-19 v8 — bug detectado por dueña: hero cheques 1.851.871
    # vs balance 1.840.030 (diferencia ~$11.841 / 8 cheques). Root cause:
    # esta query usaba `LEFT JOIN scintela.cliente` y si un codigo_cli
    # tiene fanout > 1 fila en cliente, cada cheque se contaba múltiples
    # veces (el SUM y el COUNT inflaban). Solución: cliente entra vía
    # EXISTS subquery — el cheque queda en 1 fila siempre.
    row = db.fetch_one(
        """
        SELECT COUNT(*)                AS n,
               COALESCE(SUM(c.importe), 0) AS total
        FROM scintela.cheque c
        WHERE (
                %(q)s IS NULL
             OR UPPER(COALESCE(c.no_cheque, '')) LIKE UPPER(%(like)s)
             OR c.id_cheque::text LIKE %(like)s
             OR UPPER(COALESCE(c.codigo_cli, '')) LIKE UPPER(%(like)s)
             OR EXISTS (
                  SELECT 1 FROM scintela.cliente cli
                   WHERE cli.codigo_cli = c.codigo_cli
                     -- TMT 2026-08-04 (dueña "no funciona si solo busco
                     -- condor"): por PALABRAS y sin acentos. Ver
                     -- modules/_lib/busqueda.py.
                     AND (__NOMBRE_CLI__)
                )
          )
          -- Filtro por fecha de depósito (fechad) — es lo que importa
          -- operacionalmente: "qué cheques voy a depositar este día".
          -- TMT 2026-05-12: antes filtraba por c.fecha y los postdatados
          -- aparecían fuera de rango.
          AND (%(desde)s::date IS NULL OR COALESCE(c.fechad, c.fecha) >= %(desde)s::date)
          AND (%(hasta)s::date IS NULL OR COALESCE(c.fechad, c.fecha) <= %(hasta)s::date)
          AND (%(stats)s::text[] IS NULL OR c.stat = ANY(%(stats)s::text[]))
          __COND_MEDIO__
          -- TMT 2026-05-20 PASADA 6 Federico #8 — total_buscar ahora
          -- recibe cliente/monto_min/monto_max para que el hero KPI
          -- refleje el subset real cuando se filtra por cliente.
          AND (%(cliente)s::text IS NULL OR UPPER(COALESCE(c.codigo_cli, '')) = UPPER(%(cliente)s))
          -- Filtro por VENDEDOR — mismo criterio que buscar(). TMT 2026-07-15 dueña.
          AND (%(vendedor)s::text IS NULL OR EXISTS (
                  SELECT 1 FROM scintela.cliente cli_v
                   WHERE cli_v.codigo_cli = c.codigo_cli
                     AND UPPER(TRIM(COALESCE(cli_v.vend, ''))) = UPPER(%(vendedor)s)
                ))
          AND (%(monto_min)s::numeric IS NULL OR COALESCE(c.importe, 0) >= %(monto_min)s)
          AND (%(monto_max)s::numeric IS NULL OR COALESCE(c.importe, 0) <= %(monto_max)s)
          -- Excluir reversados del total. Pedido TMT 2026-05-14.
          AND COALESCE(c.stat, '') <> 'X'
        """.replace("__NOMBRE_CLI__", _nom_cli or "FALSE")
             .replace(
                 "__COND_MEDIO__",
                 (lambda _c: f"AND ({_c})" if _c else "")(
                     SQL_POR_MEDIO.get((medio or "").strip().lower())
                 ),
             ),
        {
            **_p_cli,
            "q": q or None,
            "like": like,
            "desde": desde or None,
            "hasta": hasta or None,
            "stats": list(stats) if stats else None,
            "cliente": (cliente or None),
            "vendedor": (vendedor or None),
            "monto_min": monto_min,
            "monto_max": monto_max,
        },
    )
    return {
        "n": int(row["n"] or 0) if row else 0,
        "total": float(row["total"] or 0) if row else 0.0,
    }


def vendedores_para_filtro() -> list[dict]:
    """Vendedores presentes en los clientes de los cheques — para el dropdown
    de filtro del listado. TMT 2026-07-15 (dueña: "un filtro por vendedor").

    Trae los códigos distintos de cliente.vend que tengan al menos un cheque
    vivo (stat != 'X'), con el nombre del vendedor (scintela.vendedor) si existe.
    Ordenados por nombre para que el dropdown sea legible.
    """
    return db.fetch_all(
        """
        SELECT sub.codigo,
               COALESCE(NULLIF(TRIM(v.nombre), ''), sub.codigo) AS nombre
          FROM (
            SELECT DISTINCT UPPER(TRIM(cli.vend)) AS codigo
              FROM scintela.cheque c
              JOIN scintela.cliente cli ON cli.codigo_cli = c.codigo_cli
             WHERE cli.vend IS NOT NULL AND TRIM(cli.vend) <> ''
               AND COALESCE(c.stat, '') <> 'X'
          ) sub
          LEFT JOIN scintela.vendedor v ON UPPER(TRIM(v.codigo)) = sub.codigo
         ORDER BY nombre, sub.codigo
        """
    ) or []


def buscar(
    q: str = "",
    estado: str = "todos",
    desde: str | None = None,
    hasta: str | None = None,
    limite: int = 500,
    cliente: str = "",
    monto_min: float | None = None,
    monto_max: float | None = None,
    ver_eliminados: bool = False,
    offset: int = 0,
    orden: str = "",
    vendedor: str = "",
    medio: str = "",
) -> list[dict]:
    """Filtros (mismas reglas que /facturas):
    cliente        — 3 chars alfanum → match EXACTO sobre codigo_cli.
                     Otra cantidad → LIKE fuzzy.
    monto_min      — importe >= N
    monto_max      — importe <= N
    desde/hasta    — fecha de depósito (fechad)
    q              — búsqueda libre: N° cheque, nombre cliente/prov endoso.
    ver_eliminados — si False (default), excluye stat='X' del listado
                     cuando estado='todos'. Tab "Eliminados" siempre los
                     muestra. Pedido TMT 2026-05-14 (#40 audit).
    medio          — 'cheques' | 'depositos' | 'efectivo'. Vacío = todos.
                     TMT 2026-08-24 (dueña, mirando la ficha de MMQ llena de
                     DEP.PICH.: *"cuando son depósitos debería ir directo al
                     banco, no pasar por cheques de clientes"*). La plata SÍ
                     va directo al banco —el depósito arma su movimiento en
                     Pichincha en el mismo momento—; lo que no era cierto era
                     el RÓTULO: 1.577 de las 4.584 filas de esta tabla no son
                     cheques. Ver `SQL_POR_MEDIO`.
    """
    # TMT 2026-08-06: `nota_usuario` es columna nueva y el deploy no corre
    # migraciones — bootstrap en caliente antes de SELECTearla en la lista.
    _nota_usuario.bootstrap_columna()
    q = (q or "").strip()
    like = f"%{q}%" if q else None
    # Nombre de cliente / proveedor endosado: match por PALABRAS sueltas.
    _nom_cli, _p_cli = busqueda.condicion(q, ("cli.nombre",), prefijo="bqn")
    _nom_prv, _p_prv = busqueda.condicion(q, ("prv.nombre",), prefijo="bqp")
    stats = STATS.get(estado)  # None = todos
    # Excluir stat='X' del listado por default cuando estado='todos'. Si la
    # usuaria pide `?ver_eliminados=1` o va al tab "eliminados", los muestra.
    excluir_eliminados = (stats is None) and (not ver_eliminados)
    cliente = (cliente or "").strip().upper()
    es_cli_codigo_exacto = bool(cliente) and len(cliente) == 3 and cliente.replace("_", "").isalnum()
    cliente_like = f"%{cliente}%" if cliente else None
    # TMT 2026-07-15 (dueña): filtro por VENDEDOR (código en cliente.vend). Match
    # exacto sobre el código del vendedor asignado al cliente del cheque.
    vendedor = (vendedor or "").strip().upper()
    # Qué columna de fecha aplica el filtro desde/hasta. Para los estados que
    # ya pasaron por el banco (depositados/devueltos/daniela), filtramos por
    # `fechaing` (cuándo se ingresó al banco / rebotó / pasó a Daniela). Para
    # cartera/postergados/eliminados/endosados/todos seguimos filtrando por
    # `fechad` (cuándo está agendado a depositar) — es lo operativo.
    # TMT 2026-05-16: "ver cheques del día" en tab Depositados antes daba 0
    # porque filtraba por fechad y los depósitos tienen fechaing≠fechad.
    # TMT 2026-08-03 (dueña: "quiero imprimir los cheques depositados hoy"):
    # va `fechaout` PRIMERO. En las filas que vienen del dBase la fecha de
    # depósito es FECHOUT (→ `fechaout`); `fechaing` es FECHING = el día de
    # INGRESO a cartera, y 697 de las 1.615 filas depositadas del DBF las
    # tienen distintas (mediana 41 días). Sin esto, "Depositados + hoy" se
    # comía cheques y traía otros. TMT 2026-08-05: desde hoy PC TAMBIÉN
    # escribe `fechaout` al depositar (antes escribía `fechaing`), así que el
    # fallback a `fechaing` queda sólo para las ~1.200 filas depositadas por PC
    # antes de esa fecha — ahí `fechaing` ES la fecha de depósito y el COALESCE
    # devuelve lo correcto sin migrar un solo dato.
    # Misma columna que muestra la pantalla (ver lista.html, "Depositado").
    # TMT 2026-08-05: se apoya en SQL_DIA_SALIDA (definición compartida) y
    # mantiene fechad/fecha como último recurso para las filas viejas sin
    # ninguna de las dos.
    _COL_DEPOSITO = f"COALESCE({SQL_DIA_SALIDA[len('COALESCE('):-1]}, c.fechad, c.fecha)"
    fecha_col_por_estado = {
        "depositados": _COL_DEPOSITO,
        "devueltos": _COL_DEPOSITO,
        "daniela": _COL_DEPOSITO,
    }
    fecha_col = fecha_col_por_estado.get(estado, "COALESCE(c.fechad, c.fecha)")
    # TMT 2026-05-19 v8 — refactor: cliente/banco/proveedor se traen vía
    # subqueries escalares (LIMIT 1) en lugar de LEFT JOIN, para que el
    # COUNT del listado coincida con totc() del balance. Antes, si cualquier
    # codigo_cli tenía fanout > 1 en scintela.cliente, los cheques se
    # duplicaban (1.851.871 mostrado vs 1.840.030 real, diff 8 cheques).
    sql_buscar_cheques = """
        WITH filtrados AS (
        SELECT c.id_cheque, c.no_cheque, c.fecha, c.fechad, c.fechaing, c.fechaout,
               c.fecha_recibido, c.fecha_crea,
               -- TMT 2026-08-03 (dueña: "cargado me muestra solo 12/07 sin
               -- importar como filtre"). La columna CARGADO usaba
               -- COALESCE(fecha_recibido, fecha_crea, fecha) y en las filas
               -- del dBase `fecha_recibido` es NULL → caía en `fecha_crea`,
               -- que es cuándo se INSERTÓ la fila en PC: el día del import
               -- masivo. Por eso TODOS los cheques del dBase decían el mismo
               -- día. El día real de carga es el de INGRESO (FECHING).
               __DIA_INGRESO__ AS dia_ingreso,
               -- TMT 2026-05-17: fechad_original NULL = no fue postergado;
               -- NOT NULL = la primera postergación snapshoteó la fechad
               -- previa acá. fecha_postergacion = cuándo se postergó (última).
               c.fechad_original, c.fecha_postergacion,
               c.codigo_cli,
               COALESCE(
                 (SELECT cli.nombre FROM scintela.cliente cli
                   WHERE cli.codigo_cli = c.codigo_cli LIMIT 1),
                 ''
               ) AS cliente,
               -- TMT 2026-07-15 (dueña): código del vendedor asignado, para
               -- mostrarlo en chico en la lista de cheques (cliente.vend).
               COALESCE(
                 (SELECT UPPER(TRIM(cli.vend)) FROM scintela.cliente cli
                   WHERE cli.codigo_cli = c.codigo_cli LIMIT 1),
                 ''
               ) AS vendedor,
               c.importe, c.stat,
               -- TMT 2026-05-27 dueña: doc_banco editable inline en lista.
               -- Es el N° de comprobante/depósito (varchar(40)) — separado
               -- del no_cheque, alimentado al alta y al inline edit.
               c.doc_banco,
               c.nota_usuario,
               c.no_banco, c.banco AS banco_nombre,
               -- TMT 2026-07-07 (dueña, caso CLR): los espejos de anticipo
               -- (NB=98 negativos / banco texto 'ANTICIPO') mostraban 'UKN'
               -- porque el catálogo tiene 98=UKN legacy y el COALESCE
               -- prefería el nombre del catálogo. Ahora dicen ANTICIPO;
               -- los 98 legacy positivos sin marca siguen como UKN.
               CASE
                 WHEN c.no_banco = 98
                      AND (UPPER(TRIM(COALESCE(c.banco, ''))) = 'ANTICIPO'
                           OR COALESCE(c.importe, 0) < 0)
                 THEN 'ANTICIPO'
                 ELSE COALESCE(
                   (SELECT bco.nombre FROM scintela.banco bco
                     WHERE bco.no_banco = c.no_banco LIMIT 1),
                   c.banco
                 )
               END AS banco,
               -- Para cheques endosados: a qué proveedor se le pasó.
               -- c.prov guarda el codigo_prov del destino. TMT 2026-05-13.
               c.prov AS endoso_prov,
               COALESCE(
                 (SELECT prv.nombre FROM scintela.proveedor prv
                   WHERE prv.codigo_prov = c.prov LIMIT 1),
                 ''
               ) AS endoso_proveedor
        FROM scintela.cheque c
        WHERE (
                %(q)s IS NULL
             OR UPPER(COALESCE(c.no_cheque, '')) LIKE UPPER(%(like)s)
             OR c.id_cheque::text LIKE %(like)s
             OR EXISTS (
                  SELECT 1 FROM scintela.cliente cli
                   WHERE cli.codigo_cli = c.codigo_cli
                     -- TMT 2026-08-04 (dueña "no funciona si solo busco
                     -- condor"): por PALABRAS y sin acentos. Ver
                     -- modules/_lib/busqueda.py.
                     AND (__NOMBRE_CLI__)
                )
             OR EXISTS (
                  SELECT 1 FROM scintela.proveedor prv
                   WHERE prv.codigo_prov = c.prov
                     AND (__NOMBRE_PRV__)
                )
          )
          -- Filtro explícito por cliente (3 chars = exacto, otro = fuzzy).
          AND (
                %(cliente)s IS NULL
             OR (%(cli_codigo_exacto)s
                 AND UPPER(TRIM(COALESCE(c.codigo_cli, ''))) = %(cliente)s)
             OR (NOT %(cli_codigo_exacto)s
                 AND UPPER(COALESCE(c.codigo_cli, '')) LIKE UPPER(%(cliente_like)s))
              )
          -- Filtro por VENDEDOR: clientes cuyo cliente.vend == código elegido.
          -- TMT 2026-07-15 (dueña): dropdown de vendedor arriba del listado.
          AND (
                %(vendedor)s IS NULL
             OR EXISTS (
                  SELECT 1 FROM scintela.cliente cli_v
                   WHERE cli_v.codigo_cli = c.codigo_cli
                     AND UPPER(TRIM(COALESCE(cli_v.vend, ''))) = %(vendedor)s
                )
              )
          -- Filtro por monto USD.
          AND (%(monto_min)s::numeric IS NULL OR COALESCE(c.importe, 0) >= %(monto_min)s::numeric)
          AND (%(monto_max)s::numeric IS NULL OR COALESCE(c.importe, 0) <= %(monto_max)s::numeric)
          -- Filtro por fecha — columna depende del estado:
          --   cartera/postergados/todos → fechad (cuándo se agendó a depositar).
          --   depositados/devueltos/daniela → fechaing (cuándo pasó por el banco).
          -- TMT 2026-05-12: antes filtraba por c.fecha y los postdatados aparecían fuera de rango.
          -- TMT 2026-05-16: split por estado para que "ver cheques del día" en
          --   Depositados muestre los de hoy (fechaing) y no 0 resultados.
          AND (%(desde)s::date IS NULL OR __FECHA_COL__ >= %(desde)s::date)
          AND (%(hasta)s::date IS NULL OR __FECHA_COL__ <= %(hasta)s::date)
          AND (%(stats)s::text[] IS NULL OR c.stat = ANY(%(stats)s::text[]))
          -- Filtro por MEDIO de cobro (cheque / depósito directo / efectivo).
          __COND_MEDIO__
          -- Excluir eliminados (stat='X') cuando el filtro es "todos".
          AND (NOT %(excluir_eliminados)s OR COALESCE(c.stat, '') <> 'X')
        )
        -- TMT 2026-08-05 (dueña: "a nadie le importa la pagina visible") — el
        -- ACUM sale de una window sobre el UNIVERSO filtrado, antes del
        -- LIMIT/OFFSET. Antes se acumulaba en Python sobre las filas de la
        -- pagina, asi que con 500/pag el corrido se reiniciaba en cada
        -- pagina. La window usa EXACTAMENTE el mismo orden que la pagina
        -- que la pagina, que es lo que hace que el corrido sea continuo
        -- al pasar de pagina: los dos ORDER BY tienen que ser el MISMO y
        -- ser total (todos terminan en id_cheque).
        SELECT fi.*,
               SUM(COALESCE(fi.importe, 0)) OVER (
                 ORDER BY __ORDER_EXPR__
                 ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
               ) AS saldo_acumulado
          FROM filtrados fi
        ORDER BY __ORDER_EXPR__
        LIMIT %(limite)s OFFSET %(offset)s
        """
    # TMT 2026-06-16 dueña: ordenar por IMPORTE de mayor a menor (server-side,
    # sobre TODO el universo, no solo la página visible). orden es un enum
    # controlado (no entra texto del usuario al SQL).
    # TMT 2026-08-03 (dueña: "los cargados de hoy tampoco se ven"). El sort de
    # los <th> es CLIENT-SIDE: reordena sólo la página cargada. Con 1.365
    # cheques en Cartera total, pedir "Cargado ↓" mostraba el máximo DE LA
    # PÁGINA (27/07) y los de hoy quedaban en otra página, invisibles. Por eso
    # CARGADO necesita orden server-side propio, igual que Importe.
    # TMT 2026-08-05 — la expresion de orden se escribe UNA sola vez y se usa
    # en los DOS lugares (la window del ACUM y el ORDER BY de la pagina).
    # Va sobre el alias del CTE (`fi.`), asi que `dia_ingreso` se referencia
    # por el nombre de la columna ya calculada, no repitiendo el CASE.
    # El orden por defecto es COALESCE(fechad, fecha) — la fecha que la
    # pantalla realmente muestra y por la que Python reordenaba despues de
    # paginar (antes el SQL paginaba por `fecha` y reordenaba por `fechad`
    # DENTRO de la pagina: un cheque podia verse fuera de orden entre dos
    # paginas).
    _orden = (orden or "").lower()
    if _orden == "importe_desc":
        _order_expr = "fi.importe DESC NULLS LAST, fi.id_cheque DESC"
    elif _orden == "importe_asc":
        _order_expr = "fi.importe ASC NULLS LAST, fi.id_cheque ASC"
    elif _orden == "cargado_desc":
        _order_expr = "fi.dia_ingreso DESC NULLS LAST, fi.id_cheque DESC"
    elif _orden == "cargado_asc":
        _order_expr = "fi.dia_ingreso ASC NULLS LAST, fi.id_cheque ASC"
    else:
        _order_expr = ("COALESCE(fi.fechad, fi.fecha) ASC NULLS FIRST, "
                       "fi.id_cheque ASC")
    sql_buscar_cheques = sql_buscar_cheques.replace("__ORDER_EXPR__", _order_expr)
    sql_buscar_cheques = sql_buscar_cheques.replace("__FECHA_COL__", fecha_col)
    sql_buscar_cheques = sql_buscar_cheques.replace(
        "__NOMBRE_CLI__", _nom_cli or "FALSE")
    sql_buscar_cheques = sql_buscar_cheques.replace(
        "__NOMBRE_PRV__", _nom_prv or "FALSE")
    sql_buscar_cheques = sql_buscar_cheques.replace("__DIA_INGRESO__", SQL_DIA_INGRESO)
    # El medio es un enum controlado: sale del diccionario o no se filtra. No
    # entra texto de la usuaria al SQL.
    _cond_medio = SQL_POR_MEDIO.get((medio or "").strip().lower())
    sql_buscar_cheques = sql_buscar_cheques.replace(
        "__COND_MEDIO__", f"AND ({_cond_medio})" if _cond_medio else "")
    rows = (
        db.fetch_all(
            sql_buscar_cheques,
            {
                **_p_cli, **_p_prv,
                "q": q or None,
                "like": like,
                "cliente": cliente or None,
                "cliente_like": cliente_like,
                "cli_codigo_exacto": es_cli_codigo_exacto,
                "vendedor": vendedor or None,
                "monto_min": monto_min,
                "monto_max": monto_max,
                "desde": desde or None,
                "hasta": hasta or None,
                "stats": list(stats) if stats else None,
                "excluir_eliminados": excluir_eliminados,
                "limite": limite,
                "offset": max(0, int(offset or 0)),
            },
        )
        or []
    )
    # TMT 2026-08-05 — el orden y el running total los hace el SQL (ver el
    # CTE de arriba). Antes se reordenaba y se acumulaba ACA, sobre las filas
    # que habian llegado: con paginacion de 500 eso reordenaba solo la pagina
    # y el corrido arrancaba de cero en cada una.
    rows_out = list(rows)
    for r in rows_out:
        r["saldo_acumulado"] = float(r.get("saldo_acumulado") or 0)
        # 🚨 TMT 2026-08-24 — cómo se llama esta fila cuando NO es un cheque.
        # La columna N° del listado mostraba el ID INTERNO como placeholder de
        # un campo editable ("99600", "100455"), o sea le ofrecía a la usuaria
        # escribir el número de un cheque que no existe: los 1.577 depósitos
        # directos y los 182 cobros en efectivo no tienen papel ni número.
        # Es el mismo arreglo que ya se hizo en el Historial el 09/08
        # (*"poné dep pich más que cheque #x"*); acá había quedado.
        # `etiqueta_cobro` es la ÚNICA dueña de la regla — se llama con el
        # número en blanco a propósito, porque en estos medios cualquier cosa
        # tipeada ahí es un error de carga, no un número de cheque.
        _medio = ""
        try:
            if int(r.get("no_banco") or 0) in MEDIOS_SIN_NUMERO:
                _medio = etiqueta_cobro({**r, "no_cheque": ""})
        except (TypeError, ValueError):
            _medio = ""
        r["medio"] = _medio
    return rows_out


def resumen_cobranza_dia(fecha) -> dict:
    """Resumen de la cobranza recibida en una fecha — réplica de FINAL (ALTAS.PRG).

    El dBase, al cerrar una sesión de cobranza, imprime: cuántos CHEQUES,
    DEPÓSITOS y EFECTIVO entraron, sus totales, y el detalle de cada uno con
    el cliente, medio y las facturas que cancela — con fecha, numf, importe,
    abonado acumulado y SALDO RESULTANTE (incluye 0.00 y negativos = saldo a
    favor, la dueña quiere verlos). Como PC no tiene "sesión", agrupamos por
    DÍA DE INGRESO del cobro.

    ⚠ TMT 2026-08-03 (dueña: "resumen cobranza del día está trayendo dbf
    imports, es erróneo … lo de KOR estaría bien, lo anterior no debería
    mostrar"). El filtro era `cheque.fecha = %s`, y eso NO es el día de
    ingreso para los cheques que vienen del dBase:
      · En PC, el alta de /cheques/nuevo COLAPSA `fecha` a `fecha_recibido`
        (views.py: `fecha = fecha_recibido`) → para lo cargado por la UI,
        `fecha` SÍ es el día de cobranza.
      · En el dBase NO: ALTAS.PRG estampa `FECHING WITH DD` (día de carga) y
        deja que el usuario tipee FECHA = la fecha DEL CHEQUE (posdatado).
        MODIFICA.PRG L674 filtra "ingresados hoy" por `FECHING=DD`, y
        BANCOS.PRG L441 pasa los ingresos del día por `FECHING=FFF`.
        FECHOUT (→ `fechaout`) es la salida/depósito, no FECHING.
      · El import mapea FECHA→`fecha` y FECHING→`fechaing`. Resultado: un
        cheque recibido el 09/06 y posdatado al 03/08 aparecía como cobranza
        DEL 03/08. El 03/08 el CHEQUES.DBF tenía 12 filas con FECHA=03/08 y
        CERO con FECHING=03/08 → los 10 "cheques" de la tirilla eran
        posdatados que sólo vencían ese día. No es un caso de borde: 2.047 de
        3.250 filas del DBF tienen FECHA ≠ FECHING.

    Por eso el día de ingreso es `fecha_recibido` y, sólo para las filas
    nacidas del dBase, `fechaing` (que ahí es FECHING = ingreso). `fecha`
    queda como último fallback.

    Buckets (paridad FINAL: CH = NB<90 ó NB=98; DE = NB 90/91; EF = NB=99):
      - cheques    → cheque real en cartera / depositado (banco emisor < 90)
      - depositos  → depósito directo (no_banco 90/91)
      - efectivo   → efectivo (no_banco 99)

    Devuelve dict con `ingresos` (lista plana en orden de carga, UNA entrada
    por cobro con sus aplicaciones + flag paga T/A), las 3 listas por bucket,
    sus totales y contadores + total general. Solo lectura.

    Por aplicación usamos el SNAPSHOT de chequesxfact (abono_f/saldo_f/stat_f
    al momento de aplicar) — igual que la tirilla del dBase, que imprime el
    saldo que quedó en ESE momento, no el saldo vivo de hoy.
    """
    # `concepto` es columna nueva (TMT 2026-08-04) y el deploy no corre
    # migraciones: asegurarla antes de SELECTearla. `nota_usuario` (TMT
    # 2026-08-06) va por el mismo carril — bootstrap en caliente antes del
    # SELECT porque el deploy no aplica 0170_cheque_nota_usuario.sql solo.
    _concepto_cobro.bootstrap_columna()
    _nota_usuario.bootstrap_columna()
    rows = (
        db.fetch_all(
            """
            SELECT c.id_cheque, c.no_cheque, c.importe, c.fecha, c.fechad,
                   c.no_banco, c.stat, c.doc_banco, c.concepto,
                   c.nota_usuario,
                   c.fecha_crea, c.usuario_crea, c.clave,
                   c.fecha_recibido, c.fechaing,
                   COALESCE(c.banco, '') AS banco_emisor,
                   c.codigo_cli,
                   COALESCE(cl.nombre, '') AS cliente
              FROM scintela.cheque c
              LEFT JOIN scintela.cliente cl ON cl.codigo_cli = c.codigo_cli
             WHERE __DIA_INGRESO__ = %s
               AND COALESCE(c.stat, '') NOT IN ('X', 'Y')
             ORDER BY c.id_cheque
            """.replace("__DIA_INGRESO__", SQL_DIA_INGRESO),
            (fecha,),
        )
        or []
    )

    # Facturas que cada cheque cancela/abona (una sola query para todos).
    ids = [r["id_cheque"] for r in rows]
    aplic_por_cheque: dict[int, list[dict]] = {}
    if ids:
        placeholders = ",".join(["%s"] * len(ids))
        aplics = (
            db.fetch_all(
                f"""
                SELECT cxf.id_cheque, cxf.importe AS aplicado, cxf.tipo,
                       cxf.abono_f, cxf.saldo_f, cxf.stat_f,
                       f.numf, f.numf_completo,
                       f.fecha AS fact_fecha,
                       f.importe AS fact_importe,
                       f.saldo AS fact_saldo
                  FROM scintela.chequesxfact cxf
                  LEFT JOIN scintela.factura f ON f.id_factura = cxf.id_fact
                 WHERE cxf.id_cheque IN ({placeholders})
                 ORDER BY cxf.id_chequexfact
                """,
                tuple(ids),
            )
            or []
        )
        for a in aplics:
            aplic_por_cheque.setdefault(a["id_cheque"], []).append(a)

    def _medio(nb: int) -> str:
        if nb == 90:
            return "DEP.PICH."
        if nb == 91:
            return "DEP.INTER."
        if nb == 99:
            return "EFECTIVO"
        if nb == 98:
            return "ANTICIPO"
        return "CHEQUE"

    # TMT 2026-08-04 (Alex: "en el caso de MTM los dos fueron abonos a
    # factura, pero refleja sin aplicar facturas"). Para los cobros que
    # quedaron SIN filas vivas en chequesxfact, el sistema igual sabe qué
    # fueron — está en mov_doble. El caso testigo: Alex aplicó los dos
    # depósitos de MTM a la factura 177617 y 16 minutos después corrió
    # TOTALIZAR del estado de cuenta, que BORRA los vínculos a propósito
    # (`n_links_borrados: 6`). La plata quedó; el rastro no. La dueña,
    # 04/08: "aunque se totalice la cuenta quiero que en cobranza quede
    # guardado qué factura se pagó".
    _sin_apps = [r["id_cheque"] for r in rows if not aplic_por_cheque.get(r["id_cheque"])]
    _expl = _concepto_cobro.explicaciones(
        _sin_apps, {r["id_cheque"]: r.get("codigo_cli") for r in rows})

    cheques: list[dict] = []
    depositos: list[dict] = []
    efectivo: list[dict] = []
    for r in rows:
        apps = aplic_por_cheque.get(r["id_cheque"], [])
        r["aplicaciones"] = apps
        r["explicacion"] = _expl.get(r["id_cheque"])
        nb = r.get("no_banco") or 0
        r["medio"] = _medio(nb)
        r["total_aplicado"] = round(sum(float(a.get("aplicado") or 0) for a in apps), 2)
        # Flag paga (paridad dBase): T si TODAS las facturas afectadas
        # quedaron totalizadas al aplicar (snapshot stat_f), A si abonó y
        # alguna quedó con saldo, '' si no cancela facturas.
        stats = {(a.get("stat_f") or "").strip().upper() for a in apps}
        r["paga"] = "T" if apps and stats == {"T"} else ("A" if apps else "")
        if nb == 99:
            efectivo.append(r)
        elif nb in (90, 91):
            depositos.append(r)
        else:  # NB<90 (banco real) o 98 (anticipo) o resto → bucket cheques
            cheques.append(r)

    def _tot(lst):
        return round(sum(float(x.get("importe") or 0) for x in lst), 2)

    tot_ch, tot_de, tot_ef = _tot(cheques), _tot(depositos), _tot(efectivo)
    return {
        "fecha": fecha,
        "ingresos": rows,  # lista plana en orden de carga (id_cheque asc)
        "cheques": cheques,
        "depositos": depositos,
        "efectivo": efectivo,
        "n_cheques": len(cheques),
        "n_depositos": len(depositos),
        "n_efectivo": len(efectivo),
        "total_cheques": tot_ch,
        "total_depositos": tot_de,
        "total_efectivo": tot_ef,
        "total_general": round(tot_ch + tot_de + tot_ef, 2),
    }


def cheques_ingresados_dia(fecha) -> dict:
    """Listado de cheques INGRESADOS en una fecha — réplica de CHEQUING (BANCOS.PRG).

    TMT 2026-08-03 (dueña, con la tirilla del FoxPro en la mano: "al parecer
    necesitamos imprimir esto" → *LISTADO DE CHEQUES INGRESADOS EN FECHA:
    31.07.26*). Es la opción 6 del menú de bancos del dBase,
    `PROCEDURE CHEQUING` (BANCOS.PRG L429-463):

        SORT ON IMPORTE/D TO PASAING FOR FECHING=FFF
        LIST ALL FECHAD, CLIENTE, IMPORTE, BANCO, STAT TO PRINT
        ? "      TOTAL: " + STR(IMPOR,10,2)

    O sea: filtra por **día de INGRESO** (no por la fecha del cheque), ordena
    por **IMPORTE DESCENDENTE**, y lista `FECHAD` (cuándo se deposita), no
    `FECHA`. El TOTAL es **neto**: los espejos de anticipo (NB=98) entran
    negativos y restan — en la tirilla del 31/07 los dos RTO de −3.754 y
    −7.000 llevan el total a −374,82.

    Se diferencia del *Resumen de cobranza del día* (`resumen_cobranza_dia`,
    réplica de FINAL en ALTAS.PRG) en que aquél agrupa por medio
    (cheques/depósitos/efectivo) y muestra las facturas que paga cada cobro;
    éste es la lista plana para llevar al banco. Los dos comparten
    `SQL_DIA_INGRESO` a propósito.

    ⚠ TMT 2026-08-03. Son DOS cortes, y hay que hacer los dos — se llegó acá
    después de equivocarse en las dos direcciones, con la dueña mirando la
    pantalla:

      1. **Por MEDIO** (`SQL_ES_CHEQUE`): fuera los DEP.PICH. (NB 90/91) y el
         efectivo (NB=99). Eso fue lo que le llamó la atención primero ("y
         solo estado Z no B") — no le molestaba el estado B, le molestaban
         los depósitos, que no son cheques.
      2. **Por ESTADO** (`stat='Z'`): sólo lo que sigue EN CARTERA. Al sacar
         este corte apareció un cheque de KOR depositado el mismo día y la
         dueña marcó "este falta"; después lo confirmó con Alex: *"está bien
         que falte"*. Esta lista se lleva al banco — lo ya depositado no va.

    Por eso `n` NO coincide con el bucket CHEQUES del resumen del mismo día:
    el resumen cuenta lo que ENTRÓ (esté donde esté) y esto lista lo que
    queda POR depositar. La diferencia se devuelve explícita en `n_fuera` /
    `total_fuera` y la pantalla la muestra: la dueña ya cruzó los dos números
    una vez ("cómo puede haber 15 ingresos y acá 16 cheques?? hay algo mal") y
    una diferencia sin explicar se lee como bug.

    Solo lectura. Devuelve {fecha, filas, total, n, n_fuera, total_fuera}.
    """
    # TMT 2026-08-06: `nota_usuario` es columna nueva y el deploy no corre
    # migraciones — bootstrap en caliente antes de SELECTearla.
    _nota_usuario.bootstrap_columna()
    sql = """
        SELECT c.id_cheque, c.no_cheque, c.fechad, c.fecha, c.importe,
               c.stat, c.no_banco, c.codigo_cli,
               c.nota_usuario,
               -- TMT 2026-08-03 (dueña: "banco es el banco del cheque, y
               -- estás seguro que esto está bien??"). `cheque.banco` es TEXTO
               -- y viene NULL en casi todo lo que carga PC (el banco real vive
               -- en `no_banco` contra el catálogo `scintela.banco`), así que
               -- leerlo solo dejaba la columna vacía. Misma resolución que
               -- usa buscar() para la lista, incluido el 98 = ANTICIPO (el
               -- catálogo tiene 98=UKN legacy y ganaba el nombre del catálogo).
               CASE
                 WHEN c.no_banco = 98
                      AND (UPPER(TRIM(COALESCE(c.banco, ''))) = 'ANTICIPO'
                           OR COALESCE(c.importe, 0) < 0)
                 THEN 'ANTICIPO'
                 ELSE COALESCE(
                   (SELECT bco.nombre FROM scintela.banco bco
                     WHERE bco.no_banco = c.no_banco LIMIT 1),
                   NULLIF(TRIM(COALESCE(c.banco, '')), ''),
                   ''
                 )
               END AS banco_emisor,
               COALESCE(cl.nombre, '') AS cliente
          FROM scintela.cheque c
          LEFT JOIN scintela.cliente cl ON cl.codigo_cli = c.codigo_cli
         WHERE __DIA_INGRESO__ = %(fecha)s
           AND COALESCE(c.stat, '') NOT IN ('X', 'Y')
           AND __ES_CHEQUE__
         ORDER BY c.importe DESC NULLS LAST, c.id_cheque ASC
    """.replace("__DIA_INGRESO__", SQL_DIA_INGRESO).replace(
        "__ES_CHEQUE__", SQL_ES_CHEQUE
    )
    todos = db.fetch_all(sql, {"fecha": fecha}) or []
    filas = [f for f in todos if (f.get("stat") or "").strip().upper() == "Z"]
    fuera = [f for f in todos if f not in filas]
    total = round(sum(float(f.get("importe") or 0) for f in filas), 2)
    return {
        "fecha": fecha,
        "filas": filas,
        "total": total,
        "n": len(filas),
        # Cheques que ingresaron ese día pero YA salieron de cartera. No se
        # listan, pero se dicen: es la diferencia contra el bucket CHEQUES del
        # resumen, y callarla la convierte en "hay algo mal".
        "n_fuera": len(fuera),
        "total_fuera": round(sum(float(f.get("importe") or 0) for f in fuera), 2),
    }


def netear_cheques_con_anticipos(
    *,
    codigo_cli: str,
    ids_cheques: list[int],
    ids_anticipos: list[int],
    usuario: str = "web",
    sobrante_a_anticipo: bool = True,
) -> dict:
    """NETEA (anula) cheque(s) vivo(s) contra anticipo(s) del mismo cliente.

    TMT 2026-07-09 (dueña): "cancelar cheques y anticipos (netearlos) desde el
    estado de cuenta — anular un/varios cheque con un/varios anticipo". Los dos
    lados se cancelan entre sí (stat='X'). TMT 2026-07-21 (dueña): ya no hace
    falta que sumen exactamente igual — si los ANTICIPOS suman más, el
    sobrante queda como saldo a favor nuevo (espejo NB=98 "RESTO"). Si los
    CHEQUES suman más se bloquea (un cheque no se anula en parte).

    - Si un cheque está aplicado a factura(s), se DESAPLICA primero (reusa
      `desaplicar_factura`, reversible) → la factura vuelve a quedar con saldo
      pendiente, igual que al anular un cheque por error/rebote. TMT 2026-07-09
      (dueña): "falta que se desaplique el cheque de la factura así completa el
      flujo".
    - Cheques: se reusa `cancelar_por_anticipo` (guard: vivos, importe>0,
      del cliente; ya sin aplicaciones tras desaplicar) → stat='X' + mov_doble
      reversible.
    - Anticipos (espejos NB=98, importe negativo): stat='X' + mov_doble
      'anticipo_neteado'. Todo en UNA tx: si algo falla, rollback total.

    TMT 2026-07-30 (dueña): el sobrante ya no se guarda siempre solo. Hasta
    TOLERANCIA_CENTAVOS_USD ($5) son monedas y se olvidan; por encima, la
    pantalla lo OFRECE (`sobrante_a_anticipo`, tildado por default) y si la
    dueña destilda, el sobrante se olvida igual que el "totalizar" de la
    cobranza.

    Devuelve {n_cheques, n_anticipos, total, cheques:[...], anticipos:[...],
    facturas_reabiertas:[{id_cheque, id_factura, numf}], id_residuo,
    sobrante_ofrecido}.
    """
    codigo_cli = (codigo_cli or "").strip().upper()
    ids_cheques = [int(i) for i in (ids_cheques or [])]
    ids_anticipos = [int(i) for i in (ids_anticipos or [])]
    if not ids_cheques:
        raise ValueError("Elegí al menos un cheque para netear.")
    if not ids_anticipos:
        raise ValueError("Elegí al menos un anticipo para netear.")

    fecha = today_ec()
    asegurar_fecha_abierta(fecha)
    with db.tx() as conn:
        # --- Cheques a anular (positivos, vivos, del cliente) ---
        cheques = db.fetch_all(
            "SELECT id_cheque, no_cheque, importe, stat, codigo_cli "
            "  FROM scintela.cheque "
            " WHERE id_cheque = ANY(%s) FOR UPDATE",
            (ids_cheques,),
            conn=conn,
        )
        if len(cheques) != len(set(ids_cheques)):
            raise ValueError("Algún cheque seleccionado no existe.")
        suma_cheques = 0.0
        for c in cheques:
            if (c.get("codigo_cli") or "").strip().upper() != codigo_cli:
                raise ValueError(
                    f"El cheque #{c['id_cheque']} no es de {codigo_cli}."
                )
            if float(c.get("importe") or 0) <= 0.005:
                raise ValueError(
                    f"El cheque #{c['id_cheque']} no es positivo — no se netea "
                    "por acá (los espejos/NC no son cheques a anular)."
                )
            suma_cheques += round(float(c["importe"] or 0), 2)
        suma_cheques = round(suma_cheques, 2)

        # --- Anticipos a anular (espejos NB=98, negativos, del cliente) ---
        anticipos = db.fetch_all(
            "SELECT id_cheque, no_cheque, importe, stat, codigo_cli, no_banco "
            "  FROM scintela.cheque "
            " WHERE id_cheque = ANY(%s) FOR UPDATE",
            (ids_anticipos,),
            conn=conn,
        )
        if len(anticipos) != len(set(ids_anticipos)):
            raise ValueError("Algún anticipo seleccionado no existe.")
        suma_anticipos = 0.0
        for a in anticipos:
            if (a.get("codigo_cli") or "").strip().upper() != codigo_cli:
                raise ValueError(
                    f"El anticipo #{a['id_cheque']} no es de {codigo_cli}."
                )
            if int(a.get("no_banco") or 0) != 98:
                raise ValueError(
                    f"#{a['id_cheque']} no es un anticipo (espejo NB=98)."
                )
            if (a.get("stat") or "").strip().upper() == "X":
                raise ValueError(
                    f"El anticipo #{a['id_cheque']} ya está anulado."
                )
            # importe del espejo es negativo → el saldo a favor es su abs().
            suma_anticipos += round(-float(a["importe"] or 0), 2)
        suma_anticipos = round(suma_anticipos, 2)

        # TMT 2026-07-21 (dueña): "permita netear con diferencia de valores".
        # Si los ANTICIPOS suman más que los cheques, el sobrante NO se pierde:
        # se anula todo y el resto queda como un espejo NB=98 NUEVO (saldo a
        # favor residual, reversible). Si los CHEQUES suman más se sigue
        # bloqueando: un cheque físico no se puede anular en parte.
        #
        # TMT 2026-07-30 (dueña, caso CEM): la banda era de UN CENTAVO y
        # frenaba neteos que sólo difieren en redondeo — CEM: cheques
        # $4.140,26 contra anticipos $4.140,00, o sea 26 centavos. Ahora la
        # banda es TOLERANCIA_CENTAVOS_USD ($5, ver arriba) y vale para los
        # dos lados:
        #   · faltan más de $5 de anticipos → BLOQUEA. Ahí falta plata de
        #     verdad y anular un cheque entero contra un anticipo más chico
        #     sería regalarla.
        #   · sobran hasta $5 de anticipos → se olvida, sin preguntar y sin
        #     fabricar un saldo a favor de monedas.
        #   · sobran más de $5 → es plata del cliente: se deja como saldo a
        #     favor si la dueña lo pidió (`sobrante_a_anticipo`, que la
        #     pantalla ofrece tildado). Si lo destilda, el sobrante se olvida
        #     — mismo criterio que el "totalizar" de la cobranza.
        residuo = round(suma_anticipos - suma_cheques, 2)
        if residuo < -TOLERANCIA_CENTAVOS_USD:
            raise ValueError(
                f"Los cheques suman ${suma_cheques:,.2f} y los anticipos "
                f"${suma_anticipos:,.2f}: faltan ${abs(residuo):,.2f} del "
                "lado de los anticipos y un cheque no se puede anular en "
                "parte. Sacá cheques o sumá anticipos — si los anticipos "
                "superan, el resto queda como saldo a favor."
            )
        sobrante_ofrecido = round(residuo, 2) if residuo > TOLERANCIA_CENTAVOS_USD else 0.0
        if residuo <= TOLERANCIA_CENTAVOS_USD or not sobrante_a_anticipo:
            # Monedas (o sobrante que la dueña eligió no guardar): los cheques
            # se anulan enteros y la diferencia se olvida. Sin espejo.
            residuo = 0.0

        # === SNAPSHOT para poder DESHACER el neteo (TMT 2026-07-21, dueña:
        # "se tiene que poder deshacer el neteo que se hace en un estado de
        # cuenta"). Capturamos el estado PREVIO antes de mutar: stat de cada
        # cheque/anticipo, la posdat hermana (que cancelar_por_anticipo BORRA)
        # y las aplicaciones a factura (que se DESAPLICAN). Todo JSON-safe →
        # va al metadata del evento resumen `neteo_estado_cuenta`, que después
        # `deshacer_neteo` usa para reconstruir todo tal cual estaba. ===
        _mov_hw = (db.fetch_one(
            "SELECT COALESCE(MAX(id_mov_doble), 0) AS m FROM scintela.mov_doble",
            conn=conn,
        ) or {}).get("m", 0)
        _snap_por_cheque: dict = {}
        for c in cheques:
            cid = int(c["id_cheque"])
            _pos = db.fetch_all(
                "SELECT num, prov, fecha, fechad, importe, concepto, "
                "       COALESCE(banc,0) AS banc "
                "  FROM scintela.posdat "
                " WHERE COALESCE(banc,0)=0 AND num=%s AND prov=%s",
                (cid, codigo_cli),
                conn=conn,
            ) or []
            _snap_por_cheque[cid] = {
                "id": cid,
                "stat_prev": (c.get("stat") or "").strip().upper(),
                "posdat": [
                    {
                        "num": int(p.get("num") or cid),
                        "prov": (p.get("prov") or codigo_cli),
                        "fecha": p["fecha"].isoformat() if p.get("fecha") else None,
                        "fechad": p["fechad"].isoformat() if p.get("fechad") else None,
                        "importe": round(float(p.get("importe") or 0), 2),
                        "concepto": (p.get("concepto") or "")[:50],
                    }
                    for p in _pos
                ],
                "aplicaciones": [],
            }
        _snap_anticipos = [
            {"id": int(a["id_cheque"]),
             "stat_prev": (a.get("stat") or "").strip().upper()}
            for a in anticipos
        ]

        # --- Desaplicar los cheques de sus facturas (si estaban aplicados) ---
        # TMT 2026-07-09 (dueña): "falta que se desaplique el cheque de la
        # factura así completa el flujo". Un cheque aplicado a factura(s) no
        # se puede anular directo (cancelar_por_anticipo lo bloquea). Al
        # netearlo contra un anticipo SÍ queremos reabrir la factura — mismo
        # criterio que anular un cheque por error de carga / rebote: la
        # desaplicamos primero (reversible desde el Historial) y después
        # anulamos el cheque. La factura vuelve a quedar con su saldo pendiente.
        facturas_reabiertas: list[dict] = []
        for c in cheques:
            aps = db.fetch_all(
                "SELECT cxf.id_fact, "
                "       COALESCE(f.numf::text, '') AS numf, "
                "       SUM(cxf.importe) AS imp "
                "  FROM scintela.chequesxfact cxf "
                "  LEFT JOIN scintela.factura f ON f.id_factura = cxf.id_fact "
                " WHERE cxf.id_cheque = %s AND cxf.id_fact IS NOT NULL "
                " GROUP BY cxf.id_fact, f.numf",
                (c["id_cheque"],),
                conn=conn,
            ) or []
            for ap in aps:
                desaplicar_factura(
                    id_cheque=c["id_cheque"],
                    id_factura=ap["id_fact"],
                    motivo=f"neteo cheque↔anticipo {codigo_cli}",
                    usuario=usuario,
                    conn=conn,
                )
                facturas_reabiertas.append(
                    {
                        "id_cheque": c["id_cheque"],
                        "id_factura": int(ap["id_fact"]),
                        "numf": (ap.get("numf") or "").strip() or None,
                    }
                )
                # snapshot de la aplicación (para re-aplicar al deshacer)
                _sc = _snap_por_cheque.get(int(c["id_cheque"]))
                if _sc is not None:
                    _sc["aplicaciones"].append({
                        "id_factura": int(ap["id_fact"]),
                        "importe": round(float(ap.get("imp") or 0), 2),
                        "numf": (ap.get("numf") or "").strip() or None,
                    })

        # --- Anular cheques (reusa el primitivo reversible) ---
        ref_ant = ids_anticipos[0]
        for c in cheques:
            cancelar_por_anticipo(
                id_cheque=c["id_cheque"],
                codigo_cli=codigo_cli,
                id_cheque_anticipo=ref_ant,
                monto_anticipo=round(float(c["importe"] or 0), 2),
                usuario=usuario,
                conn=conn,
            )

        # --- Anular espejos de anticipo ---
        import mov_doble as _md
        ref_ch = ids_cheques[0]
        for a in anticipos:
            db.execute(
                "UPDATE scintela.cheque "
                "SET stat='X', fechaout=%s, "
                "    observacion = RIGHT("
                "        COALESCE(observacion || ' | ', '') || %s, 200), "
                "    usuario_modifica=%s, fecha_modifica=CURRENT_TIMESTAMP "
                "WHERE id_cheque=%s",
                (fecha, f"[X] neteado con cheque(s) {codigo_cli}", usuario,
                 a["id_cheque"]),
                conn=conn,
            )
            _md.registrar(
                conn=conn,
                tipo="anticipo_neteado",
                origen_table="cheque", origen_id=a["id_cheque"],
                destino_table="cheque", destino_id=ref_ch,
                importe=-float(a["importe"] or 0) or 1.0,
                fecha=fecha,
                concepto=(
                    f"NETEADO anticipo #{a['id_cheque']} {codigo_cli} "
                    f"${-float(a['importe'] or 0):,.2f} → X"
                )[:200],
                usuario=usuario,
                metadata={
                    "id_cheque": a["id_cheque"], "codigo_cli": codigo_cli,
                    "importe": float(a["importe"] or 0),
                    "ids_cheques": ids_cheques,
                },
            )

        # --- Residuo: sobrante de anticipos → saldo a favor nuevo ---
        # TMT 2026-07-21 (dueña): el resto queda como espejo NB=98 fresco,
        # linkeado por mov_doble al anticipo de referencia (auditable y
        # reversible como cualquier espejo).
        id_residuo = None
        if residuo:
            espejo_residuo = crear_espejo_anticipo(
                conn=conn,
                id_cheque_padre=ids_anticipos[0],
                no_cheque="RESTO",
                fecha=fecha,
                codigo_cli=codigo_cli,
                importe_espejo=residuo,
                usuario=usuario,
            )
            id_residuo = (espejo_residuo or {}).get("id_cheque")

        # === EVENTO RESUMEN del neteo (TMT 2026-07-21) ===
        # Un batch_id agrupa TODOS los mov_doble que generó este neteo (los
        # de desaplicar/cancelar/anular espejo/residuo) → los marcamos con el
        # mismo batch_id para poder mostrarlos/reversarlos juntos. El evento
        # `neteo_estado_cuenta` guarda el snapshot completo para deshacer.
        import uuid as _uuid
        batch_id = str(_uuid.uuid4())
        db.execute(
            "UPDATE scintela.mov_doble SET batch_id=%s "
            " WHERE id_mov_doble > %s AND batch_id IS NULL",
            (batch_id, _mov_hw),
            conn=conn,
        )
        import mov_doble as _md2
        _snap = {
            "codigo_cli": codigo_cli,
            "ids_cheques": ids_cheques,
            "ids_anticipos": ids_anticipos,
            "id_residuo": id_residuo,
            "cheques": list(_snap_por_cheque.values()),
            "anticipos": _snap_anticipos,
        }
        _md2.registrar(
            conn=conn,
            tipo="neteo_estado_cuenta",
            origen_table="cheque", origen_id=ids_anticipos[0],
            destino_table="cheque", destino_id=ids_cheques[0],
            importe=suma_cheques or 1.0,
            fecha=fecha,
            concepto=(
                f"NETEO {codigo_cli}: {len(cheques)} cheque(s) "
                f"${suma_cheques:,.2f} ↔ {len(anticipos)} anticipo(s)"
                + (f" · resto ${residuo:,.2f}" if residuo else "")
            )[:200],
            usuario=usuario,
            batch_id=batch_id,
            metadata=_snap,
        )

    return {
        "n_cheques": len(cheques),
        "n_anticipos": len(anticipos),
        "total": suma_cheques,
        "residuo": residuo,
        "id_residuo": id_residuo,
        # Cuánto sobró por encima de la banda de monedas — se informa aunque
        # la dueña haya elegido NO guardarlo, para que el flash lo diga.
        "sobrante_ofrecido": sobrante_ofrecido,
        "batch_id": batch_id,
        "cheques": [c["id_cheque"] for c in cheques],
        "anticipos": [a["id_cheque"] for a in anticipos],
        "facturas_reabiertas": facturas_reabiertas,
    }


def neteos_activos_cliente(codigo_cli: str) -> list[dict]:
    """Lista los neteos DESHACIBLES (evento `neteo_estado_cuenta` activo) de un
    cliente, del más nuevo al más viejo. Alimenta el panel "Deshacer neteo" del
    estado de cuenta. TMT 2026-07-21 (dueña)."""
    import json as _json
    codigo_cli = (codigo_cli or "").strip().upper()
    # Filtro por cliente EN SQL (metadata JSONB) → no depende de una ventana de
    # "los N más nuevos globales"; un neteo viejo de este cliente igual aparece.
    rows = db.fetch_all(
        "SELECT id_mov_doble, fecha_operacion, concepto, importe, metadata "
        "  FROM scintela.mov_doble "
        " WHERE tipo='neteo_estado_cuenta' AND estado='activo' "
        "   AND UPPER(TRIM(metadata->>'codigo_cli')) = %s "
        " ORDER BY id_mov_doble DESC LIMIT 100",
        (codigo_cli,),
    ) or []
    out: list[dict] = []
    for r in rows:
        md = r.get("metadata") or {}
        if isinstance(md, str):
            try:
                md = _json.loads(md)
            except Exception:  # noqa: BLE001
                md = {}
        out.append({
            "id_evento": int(r["id_mov_doble"]),
            "fecha": r.get("fecha_operacion"),
            "concepto": r.get("concepto"),
            "importe": float(r.get("importe") or 0),
            "n_cheques": len(md.get("cheques") or []),
            "n_anticipos": len(md.get("anticipos") or []),
            "id_residuo": md.get("id_residuo"),
        })
    return out


def deshacer_neteo(id_evento: int, codigo_cli: str, usuario: str = "web") -> dict:
    """DESHACE un neteo (evento `neteo_estado_cuenta`) restaurando el estado
    PREVIO exacto, en una sola tx:

      1. Anula el saldo a favor residual que el neteo hubiera creado.
      2. Reactiva los anticipos (X → stat previo).
      3. Reactiva los cheques (X → stat previo) + recrea la posdat hermana que
         `cancelar_por_anticipo` había borrado.
      4. Re-aplica los cheques a las facturas de las que se desaplicaron
         (recomputa abono/saldo/stat de la factura).
      5. Marca el batch del neteo como reversado y registra el reverso del
         evento (→ el evento queda `reversado`, no vuelve a aparecer).

    Guard conservador (TMT 2026-07-21, dueña): si algo cambió DESPUÉS del neteo
    —algún cheque/anticipo ya no está en 'X', o el residuo ya se aplicó a una
    factura— aborta con un ValueError claro en vez de pisar los cambios
    posteriores. Todo por la UI (estado de cuenta), reproducible.
    """
    import json as _json
    codigo_cli = (codigo_cli or "").strip().upper()
    fecha = today_ec()
    asegurar_fecha_abierta(fecha)
    with db.tx() as conn:
        ev = db.fetch_one(
            "SELECT id_mov_doble, tipo, estado, importe, metadata, batch_id "
            "  FROM scintela.mov_doble WHERE id_mov_doble=%s FOR UPDATE",
            (id_evento,), conn=conn,
        )
        if not ev or ev.get("tipo") != "neteo_estado_cuenta":
            raise ValueError("Ese neteo no existe.")
        if (ev.get("estado") or "") != "activo":
            raise ValueError("Ese neteo ya fue deshecho.")
        md = ev.get("metadata") or {}
        if isinstance(md, str):
            try:
                md = _json.loads(md)
            except Exception:  # noqa: BLE001
                md = {}
        if (md.get("codigo_cli") or "").strip().upper() != codigo_cli:
            raise ValueError("Ese neteo es de otro cliente.")

        snap_cheques = md.get("cheques") or []
        snap_anticipos = md.get("anticipos") or []
        id_residuo = md.get("id_residuo")

        # --- Guards: nada tocado después del neteo ---
        for sc in snap_cheques:
            row = db.fetch_one(
                "SELECT stat FROM scintela.cheque WHERE id_cheque=%s FOR UPDATE",
                (sc["id"],), conn=conn)
            if not row:
                raise ValueError(f"El cheque #{sc['id']} ya no existe.")
            if (row.get("stat") or "").strip().upper() != "X":
                raise ValueError(
                    f"El cheque #{sc['id']} ya no está anulado "
                    f"(stat={row.get('stat')}). Deshacé los cambios posteriores "
                    "antes de deshacer el neteo.")
        for sa in snap_anticipos:
            row = db.fetch_one(
                "SELECT stat FROM scintela.cheque WHERE id_cheque=%s FOR UPDATE",
                (sa["id"],), conn=conn)
            if not row:
                raise ValueError(f"El anticipo #{sa['id']} ya no existe.")
            if (row.get("stat") or "").strip().upper() != "X":
                raise ValueError(
                    f"El anticipo #{sa['id']} ya no está anulado. Deshacé los "
                    "cambios posteriores primero.")
        if id_residuo:
            aps = db.fetch_all(
                "SELECT 1 FROM scintela.chequesxfact WHERE id_cheque=%s LIMIT 1",
                (id_residuo,), conn=conn) or []
            if aps:
                raise ValueError(
                    "El saldo a favor residual que dejó el neteo ya se aplicó a "
                    "una factura; no puedo deshacerlo automáticamente. Desaplicá "
                    "eso primero.")

        import mov_doble as _md

        # --- 1. Anular el espejo residuo (si sigue vivo) ---
        if id_residuo:
            db.execute(
                "UPDATE scintela.cheque SET stat='X', fechaout=%s, "
                "  observacion=RIGHT(COALESCE(observacion||' | ','')||%s,200), "
                "  usuario_modifica=%s, fecha_modifica=CURRENT_TIMESTAMP "
                " WHERE id_cheque=%s AND stat<>'X'",
                (fecha, "[X] deshecho neteo (saldo a favor residual)", usuario,
                 id_residuo), conn=conn)

        # --- 2. Reactivar anticipos (X → stat previo) ---
        for sa in snap_anticipos:
            db.execute(
                "UPDATE scintela.cheque SET stat=%s, fechaout=NULL, "
                "  observacion=RIGHT(COALESCE(observacion||' | ','')||%s,200), "
                "  usuario_modifica=%s, fecha_modifica=CURRENT_TIMESTAMP "
                " WHERE id_cheque=%s",
                ((sa.get("stat_prev") or "Z"),
                 "[deshacer neteo] anticipo reactivado", usuario, sa["id"]),
                conn=conn)

        # --- 3. Reactivar cheques + recrear posdat hermana ---
        for sc in snap_cheques:
            db.execute(
                "UPDATE scintela.cheque SET stat=%s, fechaout=NULL, "
                "  observacion=RIGHT(COALESCE(observacion||' | ','')||%s,200), "
                "  usuario_modifica=%s, fecha_modifica=CURRENT_TIMESTAMP "
                " WHERE id_cheque=%s",
                ((sc.get("stat_prev") or "Z"),
                 "[deshacer neteo] cheque reactivado", usuario, sc["id"]),
                conn=conn)
            for p in (sc.get("posdat") or []):
                existe = db.fetch_one(
                    "SELECT 1 FROM scintela.posdat "
                    " WHERE COALESCE(banc,0)=0 AND num=%s AND prov=%s LIMIT 1",
                    (p.get("num") or sc["id"], p.get("prov") or codigo_cli),
                    conn=conn)
                if existe:
                    continue
                db.execute(
                    "INSERT INTO scintela.posdat "
                    "  (fecha, fechad, prov, num, importe, concepto, banc, "
                    "   usuario_crea) "
                    "VALUES (%s,%s,%s,%s,%s,%s,0,%s)",
                    (p.get("fecha") or fecha.isoformat(), p.get("fechad"),
                     p.get("prov") or codigo_cli, p.get("num") or sc["id"],
                     p.get("importe") or 0, (p.get("concepto") or "")[:50],
                     usuario), conn=conn)

        # --- 4. Re-aplicar cheques a sus facturas ---
        for sc in snap_cheques:
            ch = db.fetch_one(
                "SELECT no_banco FROM scintela.cheque WHERE id_cheque=%s",
                (sc["id"],), conn=conn) or {}
            for ap in (sc.get("aplicaciones") or []):
                idf = int(ap["id_factura"])
                imp = round(float(ap.get("importe") or 0), 2)
                if imp <= 0:
                    continue
                ya = db.fetch_one(
                    "SELECT 1 FROM scintela.chequesxfact "
                    " WHERE id_cheque=%s AND id_fact=%s LIMIT 1",
                    (sc["id"], idf), conn=conn)
                if ya:
                    continue
                f = db.fetch_one(
                    "SELECT importe, abono, retencion FROM scintela.factura "
                    " WHERE id_factura=%s FOR UPDATE", (idf,), conn=conn)
                if not f:
                    continue
                nuevo_abono = round(float(f.get("abono") or 0) + imp, 2)
                nuevo_saldo = _fact_q.saldo_de(
                    f.get("importe"), nuevo_abono, f.get("retencion")
                )
                nuevo_stat = _fact_q.stat_de(nuevo_saldo, nuevo_abono, tol=0.01)
                db.execute(
                    "UPDATE scintela.factura SET abono=%s, saldo=%s, stat=%s, "
                    "  usuario_modifica=%s WHERE id_factura=%s",
                    (nuevo_abono, nuevo_saldo, nuevo_stat, usuario, idf),
                    conn=conn)
                db.execute(
                    "INSERT INTO scintela.chequesxfact "
                    "  (id_cheque,id_fact,fechaing,codigo_cli,importe,no_banco,"
                    "   abono_f,saldo_f,stat_f,usuario_crea) "
                    "VALUES (%s,%s,CURRENT_DATE,%s,%s,%s,%s,%s,%s,%s)",
                    (sc["id"], idf, codigo_cli, imp, int(ch.get("no_banco") or 0),
                     nuevo_abono, nuevo_saldo, nuevo_stat, usuario), conn=conn)
                _md.registrar(
                    conn=conn, tipo="cheque_aplicado_a_factura",
                    origen_table="cheque", origen_id=sc["id"],
                    destino_table="factura", destino_id=idf,
                    importe=imp, fecha=fecha,
                    concepto=(
                        f"RE-APLICADO (deshacer neteo) ch#{sc['id']} → "
                        f"factura #{ap.get('numf') or idf}")[:200],
                    usuario=usuario)

        # --- 5. Reverso del evento + linkear el batch ---
        # TMT 2026-07-29 (dueña: "todas tienen que tener link a deshacer"):
        # el reverso se registra ANTES de marcar el batch, así cada fila del
        # batch queda con id_reverso apuntando a él. Antes se marcaba primero
        # y las filas quedaban con id_reverso NULL: el link existía a nivel
        # batch pero ninguna fila lo decía, y el chequeo de salud las leía
        # como link roto. Los 27 movs de PUE del 15/05 son de acá.
        _ids_ant = md.get("ids_anticipos") or []
        _ids_ch = md.get("ids_cheques") or []
        _id_reverso = _md.registrar(
            conn=conn, tipo="neteo_deshecho",
            origen_table="cheque",
            origen_id=(_ids_ant[0] if _ids_ant else id_evento),
            destino_table="cheque",
            destino_id=(_ids_ch[0] if _ids_ch else id_evento),
            importe=float(ev.get("importe") or 0) or 1.0, fecha=fecha,
            concepto=f"DESHECHO neteo {codigo_cli} (evento #{id_evento})"[:200],
            usuario=usuario, id_original=id_evento)
        if ev.get("batch_id"):
            db.execute(
                "UPDATE scintela.mov_doble "
                "   SET estado='reversado', id_reverso=%s "
                " WHERE batch_id=%s AND estado='activo' AND id_mov_doble<>%s",
                (_id_reverso, ev["batch_id"], id_evento), conn=conn)

    return {
        "id_evento": id_evento,
        "n_cheques": len(snap_cheques),
        "n_anticipos": len(snap_anticipos),
        "id_residuo": id_residuo,
    }


# ── Residuos de retención (espejos NB=98 de monedas) ──────────────────────
# TMT 2026-07-30 (dueña): *"los residuos de retenciones deberían eliminarse"*.
#
# QUÉ SON. Cuando el cliente paga con un cheque que no cierra exacto contra
# sus facturas porque retuvo impuesto, la cobranza dejaba el sobrante como
# saldo a favor: un espejo NB=98 negativo de monedas. El dBase no los tiene —
# ahí esa diferencia simplemente se olvida. Resultado: PC arrastra decenas de
# "saldos a favor" de $2,86 que nadie va a usar nunca y que ensucian el estado
# de cuenta y el cuadre contra el dBase (−100,45 en el control del 29/07:
# DII −71,76 · WLL −8,96 · YAU −6,09 · CG3 −5,72 · HJV −5,24 · MVC −2,68).
#
# DE ACÁ EN MÁS YA NO SE CREAN: desde hoy el sobrante de hasta
# TOLERANCIA_CENTAVOS_USD se olvida solo y por encima se pregunta. Esto es la
# limpieza de los que quedaron.
#
# El tope por default ($100) es deliberadamente más alto que la banda de
# monedas: la idea es VER todo lo chico en una lista y que la dueña elija, no
# borrar por umbral.
TOPE_RESIDUO_RETENCION_USD = 100.0


def residuos_retencion(tope: float = TOPE_RESIDUO_RETENCION_USD) -> list[dict]:
    """Espejos NB=98 vivos por menos de `tope` — los saldos a favor de monedas.

    Solo lectura. Trae también el nombre del cliente y de dónde salió cada uno
    (el cheque padre) para poder decidir sin abrir ficha por ficha.
    """
    return db.fetch_all(
        """
        SELECT c.id_cheque, c.codigo_cli, c.importe, c.fecha, c.fechad,
               TRIM(COALESCE(c.stat, '')) AS stat,
               c.usuario_crea, c.id_cheque_padre,
               COALESCE(cl.nombre, '') AS nombre,
               p.importe AS importe_padre, p.fecha AS fecha_padre
          FROM scintela.cheque c
          LEFT JOIN scintela.cliente cl
                 ON UPPER(TRIM(cl.codigo_cli)) = UPPER(TRIM(c.codigo_cli))
          LEFT JOIN scintela.cheque p ON p.id_cheque = c.id_cheque_padre
         WHERE c.no_banco = 98
           AND COALESCE(c.importe, 0) < 0
           AND ABS(COALESCE(c.importe, 0)) <= %s
           AND TRIM(COALESCE(c.stat, '')) IN ('Z', '1', '2', '3', 'P', 'D')
         ORDER BY ABS(COALESCE(c.importe, 0)) DESC, c.codigo_cli
        """,
        (float(tope),),
    ) or []


def eliminar_residuos_retencion(
    ids: list[int], *, usuario: str = "web", tope: float = TOPE_RESIDUO_RETENCION_USD
) -> dict:
    """Anula los espejos de monedas elegidos. Devuelve {n, total, ids}.

    Reusa `anular_por_error_de_carga`, que para un espejo en cartera es un
    UPDATE a 'X' sin compensación de banco ni de caja (no hay movimiento que
    revertir) y deja su mov_doble — o sea que cada uno se puede deshacer
    individualmente desde /historial.

    GUARDA: sólo anula ids que la propia consulta considera residuo. Si
    alguien manda el id de un saldo a favor grande —a mano o por un form
    viejo— se ignora, no se anula de más.
    """
    validos = {int(r["id_cheque"]): float(r["importe"] or 0)
               for r in residuos_retencion(tope)}
    hechos, total = [], 0.0
    for i in [int(x) for x in (ids or [])]:
        if i not in validos:
            continue
        anular_por_error_de_carga(
            i,
            motivo="residuo de retencion (el dBase no lo tiene)",
            usuario=usuario,
        )
        hechos.append(i)
        total += abs(validos[i])
    return {"n": len(hechos), "total": round(total, 2), "ids": hechos}


def deshacer_anulacion_error_carga(
    id_mov_doble: int, *, usuario: str = "web", motivo: str = "", conn=None
) -> dict:
    """Devuelve a la vida un cheque anulado por "error de carga".

    TMT 2026-07-30 (dueña: *"tiene que tener pantalla"*). `anular_por_error_de_carga`
    era el único movimiento del módulo sin vuelta atrás: no estaba en el
    dispatcher de /historial y `TRANSICIONES_VALIDAS['X']` no ofrece →B, así
    que un cheque depositado anulado por error quedaba muerto y había que
    re-crearlo a mano. Eso fue exactamente lo que pasó con el anticipo de CJE
    (#100934) y obligó a recargar los 1.700 en vez de recuperarlos.

    Deshace las tres cosas que la anulación hizo, en una sola transacción:

      1. el cheque vuelve a `stat_previo` (de la metadata) y se le limpia
         `fechaout`;
      2. la compensación de banco/caja se COMPENSA con su opuesta (la ND se
         cancela con una NC, la salida de caja con una entrada) — no se borra
         la fila original: el paper trail queda entero, igual que en el resto
         de los reversos de la app;
      3. las aplicaciones a facturas se re-aplican desde el snapshot.

    Se niega, SIN escribir nada, si:
      - el mov no es una anulación por error de carga, o ya está reversado;
      - el cheque ya no está en 'X' (alguien lo movió después);
      - la metadata no dice de qué stat venía.

    Las anulaciones anteriores al 30/07 no guardaron el snapshot de
    aplicaciones: ésas se deshacen igual (el cheque vuelve y el banco se
    compensa) pero las facturas hay que re-aplicarlas a mano desde la ficha.
    `aplicaciones_pendientes` en el resultado lo dice con el número.

    ⭐ Y vuelve con su ESPEJO (TMT 2026-08-19). Si la anulación se llevó el
    espejo NB=98 del anticipo (`espejos_anulados` en la metadata), deshacerla
    tiene que devolverlo: un anticipo vivo sin su espejo es plata que entró
    sin la contrapartida del saldo a favor — la utilidad sube de una sola
    punta, que es el mismo desbalance que la cascada vino a arreglar, con el
    signo al revés. Si el espejo ya no está en 'X' (alguien lo movió), NO se
    deshace nada: mejor un error claro que medio cheque restaurado.
    """
    import json as _json

    import mov_doble as _md

    mv = db.fetch_one(
        "SELECT id_mov_doble, tipo, origen_id, estado, metadata, importe "
        "  FROM scintela.mov_doble WHERE id_mov_doble = %s",
        (id_mov_doble,),
    )
    if not mv:
        raise ValueError("No encuentro ese movimiento.")
    if (mv.get("tipo") or "") != "reverso_cheque_administrativo":
        raise ValueError(
            f"Ese movimiento es '{mv.get('tipo')}', no una anulación por error "
            "de carga.")
    if (mv.get("estado") or "") == "reversado":
        raise ValueError("Esa anulación ya se deshizo — no se deshace dos veces.")

    meta = mv.get("metadata") or {}
    if isinstance(meta, str):
        try:
            meta = _json.loads(meta)
        except Exception:  # noqa: BLE001
            meta = {}
    id_cheque = int(mv.get("origen_id") or 0)
    stat_destino = (meta.get("stat_previo") or "").strip().upper()
    if not stat_destino:
        raise ValueError(
            "La metadata no dice de qué estado venía el cheque. No lo restauro "
            "a ciegas — revisalo a mano desde su ficha.")

    snap_aplic = meta.get("aplicaciones_borradas")
    sin_snapshot_aplic = snap_aplic is None and int(
        meta.get("n_aplicaciones_reversadas") or 0) > 0

    fecha = today_ec()
    asegurar_fecha_abierta(fecha)

    import contextlib as _ctx

    _tx = _ctx.nullcontext(conn) if conn is not None else db.tx()
    with _tx as conn:
        ch = db.fetch_one(
            "SELECT id_cheque, no_cheque, stat, codigo_cli, importe, no_banco "
            "  FROM scintela.cheque WHERE id_cheque = %s FOR UPDATE",
            (id_cheque,), conn=conn,
        )
        if not ch:
            raise ValueError("El cheque de ese movimiento ya no existe.")
        stat_hoy = (ch.get("stat") or "").strip().upper()
        if stat_hoy != "X":
            raise ValueError(
                f"El cheque está en stat='{stat_hoy}', no en 'X': alguien lo "
                "movió después de la anulación. Reversá primero ese cambio.")

        importe = float(ch.get("importe") or 0)

        # 1. el cheque vuelve
        db.execute(
            "UPDATE scintela.cheque "
            "   SET stat=%s, fechaout=NULL, "
            "       observacion = RIGHT("
            "           COALESCE(observacion || ' | ', '') || %s, 200), "
            "       usuario_modifica=%s, fecha_modifica=CURRENT_TIMESTAMP "
            " WHERE id_cheque=%s",
            (stat_destino,
             ("[R] anulación por error de carga deshecha"
              + (f": {motivo[:60]}" if motivo else "")),
             usuario, id_cheque),
            conn=conn,
        )

        # 2. la compensación, al revés
        comp = meta.get("compensacion") or {}
        compensacion_nueva = None
        if comp.get("tipo") == "banco" and importe:
            import bank_helpers
            _banco = int(ch.get("no_banco") or 10)
            _CONCEPTO_A_REAL = {90: 10, 91: 32, 95: 10, 97: 10, 98: 10, 99: 10}
            if _banco >= 90:
                _banco = _CONCEPTO_A_REAL.get(_banco, 10)
            _res = bank_helpers.insert_movimiento_bancario(
                conn,
                no_banco=_banco,
                no_cta=None,
                fecha=fecha,
                documento="NC",  # opuesta a la ND que puso la anulación
                importe=abs(importe),
                concepto=(f"REV ANUL ch{ch.get('no_cheque') or id_cheque} "
                          f"err carga")[:80],
                prov=ch.get("codigo_cli"),
                numreferencia=id_cheque,
                usuario=usuario,
            )
            compensacion_nueva = {"tipo": "banco", "id": _res.get("id_transaccion")}
        elif comp.get("tipo") == "caja" and importe:
            import caja_helpers
            _res = caja_helpers.insert_movimiento_caja(
                conn,
                fecha=fecha,
                # La anulación de un efectivo POSITIVO sacó plata ('S'); la de
                # uno negativo la puso ('E'). Deshacer es el espejo de eso.
                tipo="S" if importe < 0 else "E",
                importe=abs(importe),
                concepto=(f"REV ANUL ch{ch.get('no_cheque') or id_cheque} "
                          f"err carga")[:80],
                id_cheque=id_cheque,
                usuario=usuario,
            )
            compensacion_nueva = {"tipo": "caja", "id": _res.get("id_caja")}

        # 3. las facturas, re-aplicadas
        n_aplic = 0
        if snap_aplic:
            aplicar_a_factura(
                id_cheque=id_cheque,
                aplicaciones=[{"id_fact": int(a["id_fact"]),
                               "importe": float(a["importe"] or 0)}
                              for a in snap_aplic],
                usuario=usuario,
                conn=conn,
                # El cheque acaba de volver a su stat original en ESTA tx; si
                # era un depósito directo, ese stat es 'B'.
                permitir_depositado=True,
            )
            n_aplic = len(snap_aplic)

        # 4. el espejo vuelve con el padre (ver docstring)
        espejos_revividos: list[int] = []
        for _id_esp in [int(x) for x in (meta.get("espejos_anulados") or [])]:
            # TMT 2026-08-24: pedía `estado='activo'` y así se saltaba más de
            # la mitad de los casos. Una anulación por error de carga nace
            # 'activo' o 'reverso' según haya encontrado el `cheque_creado`
            # del cheque para linkearlo (`id_original=… if md_orig_cheque else
            # None`, más arriba en `anular_por_error_carga`): en producción hay
            # 34 'activo' y 47 'reverso'. Con el filtro viejo, deshacer la
            # anulación del padre revivía el anticipo y dejaba el espejo NB=98
            # muerto en 47 de cada 81 casos — el desbalance que el arreglo del
            # 19/08 vino justamente a tapar, con el signo al revés, y el
            # `continue` de abajo lo hacía en silencio. Lo que importa acá es
            # que la anulación del espejo NO esté ya deshecha.
            _mv_esp = db.fetch_one(
                """
                SELECT id_mov_doble FROM scintela.mov_doble
                 WHERE tipo='reverso_cheque_administrativo'
                   AND origen_table='cheque' AND origen_id=%s
                   AND estado <> 'reversado'
                 ORDER BY id_mov_doble DESC LIMIT 1
                """,
                (_id_esp,), conn=conn,
            )
            if not _mv_esp:
                # No debería pasar: el espejo se anuló en cascada con este
                # mismo cheque, así que su anulación tiene que estar. Si no
                # está, el espejo NO vuelve y el cliente queda con un saldo a
                # favor que no existe — que eso pase sin dejar rastro es cómo
                # se pierden días hasta que alguien mira el número.
                _LOG.warning(
                    "deshacer_anulacion_error_carga: el espejo %s no vuelve — "
                    "no encontré su anulación sin deshacer (cheque padre %s)",
                    _id_esp, id_cheque,
                )
                continue
            try:
                deshacer_anulacion_error_carga(
                    int(_mv_esp["id_mov_doble"]),
                    usuario=usuario,
                    motivo=f"vuelve con el cheque {ch.get('no_cheque') or id_cheque}",
                    conn=conn,
                )
            except ValueError as e:
                raise ValueError(
                    f"No deshago la anulación: el espejo de anticipo #{_id_esp} "
                    f"no se puede devolver ({e}). Resolvelo primero desde su "
                    "ficha — el anticipo sin su espejo deja al cliente con un "
                    "saldo a favor que no existe."
                ) from e
            espejos_revividos.append(_id_esp)

        # y su renglón del historial vuelve a estar vivo
        if espejos_revividos:
            db.execute(
                "UPDATE scintela.mov_doble SET estado='activo', id_reverso=NULL "
                " WHERE tipo='cheque_anticipo_espejo' AND origen_table='cheque' "
                "   AND origen_id=%s AND id_reverso=%s",
                (id_cheque, id_mov_doble),
                conn=conn,
            )

        _md.registrar(
            conn=conn,
            tipo="reverso_anulacion_error_carga",
            origen_table="cheque", origen_id=id_cheque,
            destino_table="cheque", destino_id=id_cheque,
            importe=importe or 1.0,
            fecha=fecha,
            concepto=(
                f"DESHECHA anulación por error de carga — ch "
                f"{(ch.get('no_cheque') or '').strip() or id_cheque} "
                f"{ch.get('codigo_cli')} X→{stat_destino}"
                + (f" ({motivo})" if motivo else "")
            )[:200],
            usuario=usuario,
            metadata={
                "id_mov_anulacion": id_mov_doble,
                "stat_restaurado": stat_destino,
                "aplicaciones_reaplicadas": n_aplic,
                "aplicaciones_pendientes": (
                    int(meta.get("n_aplicaciones_reversadas") or 0)
                    if sin_snapshot_aplic else 0),
                "compensacion": compensacion_nueva,
                "espejos_revividos": espejos_revividos,
                "motivo": motivo or "",
            },
            id_original=id_mov_doble,
        )

    return {
        "id_cheque": id_cheque,
        "no_cheque": (ch.get("no_cheque") or "").strip(),
        "stat_restaurado": stat_destino,
        "espejos_revividos": espejos_revividos,
        "aplicaciones_reaplicadas": n_aplic,
        "aplicaciones_pendientes": (
            int(meta.get("n_aplicaciones_reversadas") or 0)
            if sin_snapshot_aplic else 0),
        "compensacion": compensacion_nueva,
    }

# ═══════════════════════════════════════════════════════════════════════════
# DESHACER UN PROTESTO — TMT 2026-08-13 (dueña: *"protesté por confusión y
# sigue protestado"*)
# ═══════════════════════════════════════════════════════════════════════════
# Marcar un cheque devuelto (B→1) era un camino de ida: `TRANSICIONES_VALIDAS`
# no ofrece 1→B —y no debería: volver a "depositado" NO es una transición del
# negocio, es deshacer un error— y el ↺ de /historial contestaba "Tipo
# 'cheque_devuelto' aún no tiene reverso automatizado". El cheque quedaba en
# cartera como deuda viva de un cliente que ya había pagado, y en el banco con
# la ND del protesto restando plata que nunca se fue.
#
# ⭐ Lo que se deshace NO se borra: la ND se cancela con una NC y el gasto de
# protesto con la suya, igual que en el resto de los reversos de la app. El
# extracto del banco no cambia; el saldo vuelve a donde estaba.
_CONCEPTO_REV_PROTESTO = "REV PROT"


def _compensacion_del_protesto(mv: dict, meta: dict, id_cheque: int) -> dict:
    """Qué movimientos de banco dejó ESTE protesto, para poder revertirlos.

    Dos fuentes, en este orden:

    1. `meta['compensacion']` — lo que anotó el propio protesto. Es exacto:
       trae el id de la ND, el del gasto y de qué depósitos se desagrupó.
    2. Reconocerlos por el concepto. Sólo para los protestos anteriores al
       13/08/2026, que se hicieron sin anotar nada (los seis del 12/08). Se
       buscan las ND del cheque emitidas desde el día del protesto con los dos
       conceptos que escribe `compensar_deposito_devuelto`.

    En los dos casos se descartan las que YA se compensaron (si existe una NC
    'REV PROT' de este cheque posterior a la ND), así que deshacer dos veces no
    duplica la plata. La pantalla de confirmación muestra esta lista con los
    números ANTES de tocar nada.
    """
    comp = meta.get("compensacion") or {}
    ids: list[int] = []
    if comp.get("id_nd"):
        ids.append(int(comp["id_nd"]))
    if comp.get("id_gs"):
        ids.append(int(comp["id_gs"]))
    exacto = bool(ids)
    if not exacto:
        desde = mv.get("fecha_operacion") or mv.get("fecha_creacion")
        if hasattr(desde, "date"):
            desde = desde.date()
        filas = db.fetch_all(
            "SELECT id_transaccion FROM scintela.transacciones_bancarias "
            " WHERE numreferencia = %s "
            "   AND UPPER(TRIM(COALESCE(documento,''))) = 'ND' "
            "   AND ( COALESCE(concepto,'') ILIKE 'ch.devuelto%%' "
            "         OR COALESCE(concepto,'') ILIKE %s ) "
            "   AND fecha >= %s "
            " ORDER BY id_transaccion",
            (id_cheque, f"{CONCEPTO_GS_PROTESTO}%", desde),
        ) or []
        ids = [int(f["id_transaccion"]) for f in filas]
    if not ids:
        return {"exacto": exacto, "movimientos": [], "links": [
            int(x) for x in (comp.get("links") or [])]}
    filas = db.fetch_all(
        "SELECT tb.id_transaccion, tb.no_banco, tb.fecha, tb.importe, "
        "       COALESCE(tb.concepto,'') AS concepto, "
        "       COALESCE(bk.nombre,'') AS banco_nombre, "
        "       EXISTS (SELECT 1 FROM scintela.transacciones_bancarias nc "
        "                WHERE nc.numreferencia = tb.numreferencia "
        "                  AND UPPER(TRIM(COALESCE(nc.documento,''))) = 'NC' "
        "                  AND COALESCE(nc.concepto,'') ILIKE %s "
        "                  AND nc.id_transaccion > tb.id_transaccion) AS ya_revertido "
        "  FROM scintela.transacciones_bancarias tb "
        "  LEFT JOIN scintela.banco bk ON bk.no_banco = tb.no_banco "
        " WHERE tb.id_transaccion = ANY(%s) "
        " ORDER BY tb.id_transaccion",
        (f"{_CONCEPTO_REV_PROTESTO}%", ids),
    ) or []
    return {
        "exacto": exacto,
        "movimientos": [f for f in filas if not f.get("ya_revertido")],
        "ya_revertidos": [f for f in filas if f.get("ya_revertido")],
        "links": [int(x) for x in (comp.get("links") or [])],
    }


def protesto_deshacible(id_cheque: int, stat_hoy: str | None = None) -> int | None:
    """El protesto que se puede deshacer de este cheque, si hay alguno.

    Existe para que el botón viva en la FICHA del cheque y no sólo en el ↺ de
    /historial: cuando la dueña se da cuenta del error está mirando el cheque,
    no el listado de movimientos. Devuelve el id del último `cheque_devuelto`
    activo que dejó al cheque en el estado en el que está hoy — si alguien lo
    movió después, no hay nada que deshacer desde acá.
    """
    stat = (stat_hoy or "").strip().upper()
    if not stat:
        fila = db.fetch_one(
            "SELECT stat FROM scintela.cheque WHERE id_cheque = %s",
            (int(id_cheque),),
        )
        stat = ((fila or {}).get("stat") or "").strip().upper()
    if stat not in ("1", "2", "3"):
        return None
    fila = db.fetch_one(
        "SELECT id_mov_doble FROM scintela.mov_doble "
        " WHERE tipo = 'cheque_devuelto' AND estado = 'activo' "
        "   AND origen_table = 'cheque' AND origen_id = %s "
        "   AND COALESCE(metadata->>'stat_destino', '1') = %s "
        " ORDER BY id_mov_doble DESC LIMIT 1",
        (int(id_cheque), stat),
    )
    return int(fila["id_mov_doble"]) if fila else None


def plan_deshacer_devuelto(id_mov_doble: int) -> dict:
    """Qué va a pasar si se deshace este protesto. NO escribe nada.

    La pantalla de confirmación se arma con esto: el estado al que vuelve el
    cheque, la fecha de cobro que se restaura y los movimientos de banco que se
    van a compensar, con importe y banco. *"Igualar no es explicar"* (31/07):
    el que confirma tiene que poder ver la plata antes de tocar el botón.
    """
    import json as _json

    mv = db.fetch_one(
        "SELECT id_mov_doble, tipo, estado, origen_id, importe, metadata, "
        "       fecha_creacion, fecha_operacion "
        "  FROM scintela.mov_doble WHERE id_mov_doble = %s",
        (id_mov_doble,),
    )
    if not mv:
        raise ValueError("No encuentro ese movimiento.")
    if (mv.get("tipo") or "") != "cheque_devuelto":
        raise ValueError(
            f"Ese movimiento es '{mv.get('tipo')}', no un cheque marcado como "
            "devuelto.")
    if (mv.get("estado") or "") == "reversado":
        raise ValueError("Ese protesto ya se deshizo — no se deshace dos veces.")
    meta = mv.get("metadata") or {}
    if isinstance(meta, str):
        try:
            meta = _json.loads(meta)
        except Exception:  # noqa: BLE001
            meta = {}
    id_cheque = int(mv.get("origen_id") or 0)
    ch = db.fetch_one(
        "SELECT id_cheque, no_cheque, stat, codigo_cli, importe, banco, "
        "       no_banco, fecha, fechad, fechad_original, fecha_postergacion "
        "  FROM scintela.cheque WHERE id_cheque = %s",
        (id_cheque,),
    )
    if not ch:
        raise ValueError("El cheque de ese movimiento ya no existe.")
    stat_prev = (meta.get("stat_prev") or "").strip().upper()
    if not stat_prev:
        raise ValueError(
            "La metadata no dice de qué estado venía el cheque. No lo restauro "
            "a ciegas — cambialo a mano desde su ficha.")
    stat_marcado = (meta.get("stat_destino") or "1").strip().upper()
    stat_hoy = (ch.get("stat") or "").strip().upper()
    comp = _compensacion_del_protesto(mv, meta, id_cheque)
    return {
        "id_mov_doble": int(id_mov_doble),
        "id_cheque": id_cheque,
        "cheque": ch,
        "stat_hoy": stat_hoy,
        "stat_prev": stat_prev,
        "stat_marcado": stat_marcado,
        "puede": stat_hoy == stat_marcado,
        "fechad_restaurada": _fechad_a_restaurar(ch, stat_prev),
        "compensacion": comp,
        "fecha_protesto": (mv.get("fecha_operacion")
                           or mv.get("fecha_creacion")),
    }


def _fechad_a_restaurar(ch: dict, stat_prev: str):
    """La fecha de cobro que hay que devolverle al cheque, o None.

    Un cheque que vuelve al banco (`STATS_DEPOSITADO` = B/V/I/A) ya se depositó: su "a depositar" es
    el día en que se depositó, no la fecha futura que se le puso mientras
    estuvo protestado. `fechad_original` la guarda. Si el cheque vuelve a
    cartera (Z/P/D) la fecha futura SÍ es la que vale —es cuándo se va a
    cobrar— y no se toca.
    """
    if stat_prev not in STATS_DEPOSITADO:
        return None
    orig = ch.get("fechad_original")
    if not orig or orig == ch.get("fechad"):
        return None
    return orig


def deshacer_devuelto(
    id_mov_doble: int, *, usuario: str = "web", motivo: str = ""
) -> dict:
    """Deshace un protesto: el cheque vuelve al estado del que salió.

    Todo en UNA transacción:

      1. el cheque vuelve a `stat_prev` (de la metadata del protesto) y, si
         venía de estar depositado, recupera su fecha de cobro original;
      2. la ND del protesto y su gasto se compensan con NC por el mismo
         importe — las filas originales NO se borran;
      3. si el protesto lo había sacado de un depósito agrupado, se lo vuelve
         a enganchar (sólo cuando el protesto anotó de cuál: los anteriores al
         13/08/2026 no lo anotaron y ahí el cheque queda sin el link, que es
         historia, no plata).

    Se niega, SIN escribir nada, si el cheque ya no está en el estado que dejó
    el protesto: alguien lo movió después y restaurarlo a ciegas pisaría ese
    cambio.
    """
    import mov_doble as _md

    plan = plan_deshacer_devuelto(id_mov_doble)
    if not plan["puede"]:
        raise ValueError(
            f"El cheque está en estado '{plan['stat_hoy']}', no en "
            f"'{plan['stat_marcado']}': alguien lo movió después del protesto. "
            "Deshacé primero ese cambio.")

    id_cheque = plan["id_cheque"]
    ch = plan["cheque"]
    stat_prev = plan["stat_prev"]
    fechad_nueva = plan["fechad_restaurada"]
    fecha = today_ec()
    asegurar_fecha_abierta(fecha)

    with db.tx() as conn:
        # Releer con lock: entre el plan y el UPDATE alguien pudo moverlo.
        actual = db.fetch_one(
            "SELECT stat FROM scintela.cheque WHERE id_cheque = %s FOR UPDATE",
            (id_cheque,), conn=conn,
        )
        if (actual or {}).get("stat", "").strip().upper() != plan["stat_marcado"]:
            raise ValueError(
                "El cheque cambió de estado mientras confirmabas. Volvé a "
                "abrir la pantalla.")

        set_fecha = ""
        params: list = [stat_prev]
        if fechad_nueva:
            set_fecha = (", fechad=%s, fechad_original=NULL, "
                         "fecha_postergacion=NULL")
            params.append(fechad_nueva)
        params += [
            ("[R] protesto deshecho" + (f": {motivo[:60]}" if motivo else "")),
            usuario, id_cheque,
        ]
        db.execute(
            "UPDATE scintela.cheque "
            "   SET stat=%s" + set_fecha + ", "
            "       observacion = RIGHT("
            "           COALESCE(observacion || ' | ', '') || %s, 200), "
            "       usuario_modifica=%s, fecha_modifica=CURRENT_TIMESTAMP "
            " WHERE id_cheque=%s",
            tuple(params),
            conn=conn,
        )

        # 2. las notas de débito del protesto, canceladas con su opuesta.
        compensados = []
        if plan["compensacion"]["movimientos"]:
            import bank_helpers
            for m in plan["compensacion"]["movimientos"]:
                res = bank_helpers.insert_movimiento_bancario(
                    conn,
                    no_banco=int(m["no_banco"]),
                    no_cta=None,
                    fecha=fecha,
                    documento="NC",
                    importe=abs(float(m["importe"] or 0)),
                    concepto=(
                        f"{_CONCEPTO_REV_PROTESTO} ch"
                        f"{(ch.get('no_cheque') or '').strip() or id_cheque} "
                        f"{(ch.get('codigo_cli') or '').strip()}"
                    ).strip()[:50],
                    prov=ch.get("codigo_cli"),
                    numreferencia=id_cheque,
                    usuario=usuario,
                )
                compensados.append({
                    "id_nd": int(m["id_transaccion"]),
                    "id_nc": res.get("id_transaccion"),
                    "importe": float(m["importe"] or 0),
                })

        # 3. de vuelta adentro del depósito del que lo sacamos.
        relinkeados = 0
        for id_tx in plan["compensacion"].get("links") or []:
            ya = db.fetch_one(
                "SELECT 1 AS x FROM scintela.chequextransaccion "
                " WHERE id_cheque=%s AND id_transaccion=%s",
                (id_cheque, int(id_tx)), conn=conn,
            )
            if ya:
                continue
            db.execute(
                "INSERT INTO scintela.chequextransaccion "
                "    (id_cheque, id_transaccion, fecha, stat_ch, usuario_crea) "
                "VALUES (%s, %s, %s, 'D', %s)",
                (id_cheque, int(id_tx), fecha, usuario), conn=conn,
            )
            relinkeados += 1

        _md.registrar(
            conn=conn,
            tipo="reverso_cheque_devuelto",
            origen_table="cheque", origen_id=id_cheque,
            destino_table="cheque", destino_id=id_cheque,
            importe=float(ch.get("importe") or 0) or 1.0,
            fecha=fecha,
            concepto=(
                f"DESHECHO el protesto — ch "
                f"{(ch.get('no_cheque') or '').strip() or '#' + str(id_cheque)} "
                f"{(ch.get('codigo_cli') or '').strip()} "
                f"{plan['stat_marcado']}→{stat_prev}"
                + (f" ({motivo})" if motivo else "")
            )[:200],
            usuario=usuario,
            metadata={
                "id_mov_protesto": int(id_mov_doble),
                "stat_restaurado": stat_prev,
                "fechad_restaurada": (fechad_nueva.isoformat()
                                      if fechad_nueva else None),
                "compensados": compensados,
                "relinkeados": relinkeados,
                "motivo": motivo or "",
            },
            id_original=id_mov_doble,
        )

    return {
        "id_cheque": id_cheque,
        "no_cheque": (ch.get("no_cheque") or "").strip(),
        "stat_restaurado": stat_prev,
        "fechad_restaurada": fechad_nueva,
        "compensados": compensados,
        "relinkeados": relinkeados,
    }

