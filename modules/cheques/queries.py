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

# Qué cuenta como CHEQUE — la misma partición por medio que usa el resumen de
# cobranza (réplica de FINAL, ALTAS.PRG): NB 90/91 = depósito directo, NB 99 =
# efectivo, todo lo demás (banco emisor real, y el 98 = espejo de anticipo) es
# cheque. Compartida para que el listado de ingresados y el bucket CHEQUES del
# resumen NO puedan dar números distintos para el mismo día.
SQL_ES_CHEQUE = "COALESCE(c.no_banco, 0) NOT IN (90, 91, 99)"

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


# Stats donde el cheque está depositado en banco (lockean campos duros).
STATS_DEPOSITADO = ("B", "V", "W", "I", "J", "K", "A")

# Stats terminales — no se puede editar nada. 'E' = endosado (cheque ya
# salió de nuestra cartera, no nos pertenece más).
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
    # directo. Heads up: chequesxfact y otras tablas relacionadas NO se
    # ajustan automáticamente — esto solo modifica el importe del cheque.
    # Para corregir las relacionadas anular+reemitir sigue siendo el flow.
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

    db.execute(
        f"UPDATE scintela.cheque SET {', '.join(sql_set)} WHERE id_cheque=%s",
        tuple(params),
    )
    return {
        "id_cheque": id_cheque,
        "fechad_nueva": fechad_nueva,
        "fechad_shifted_lunes": fechad_shifted,
        "stat_actual": stat,
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
TRANSICIONES_VALIDAS = {
    # Cartera → dentro de cartera, marcar devuelto 1° (inicio de la secuencia),
    # eliminar, o entrar a estados con movimiento.
    "Z": {"P", "D", "1", "X"} | {"B", "C", "9", "I"},
    "P": {"Z", "D", "1", "X"} | {"B", "C", "I"},
    "D": {"Z", "P", "1", "X"} | {"B", "C", "I"},
    # Devuelto 1°: escalar a 2°, volver a cartera, re-depositar (V), rebote, eliminar.
    "1": {"2", "Z", "P", "D", "V", "X"} | {"9"},
    # TMT 2026-07-21 (dueña, casos CJE/NIF): un DEPOSITADO que el banco devolvió
    # DESPUÉS puede pasar a "1" (devuelto 1°) como cambio de etiqueta PLANO, con
    # nueva fecha de cobro. El ND del protesto NO se genera acá: llega por el
    # extracto/sync o se tipea en /bancos (como hace el dBase). El depósito
    # original queda en la historia del cheque. Para el rebote CON ND automático
    # sigue existiendo B→9. Desde "1" ya se puede volver a Z/P (cartera).
    # Devuelto 2°: escalar a 3°, volver a cartera, rebote, eliminar. (NO vuelve a 1°.)
    "2": {"3", "Z", "P", "D", "X"} | {"9"},
    # Devuelto 3° (segundo rechazo): volver a cartera para gestión, o eliminar.
    "3": {"Z", "P", "D", "X"},
    # V = protestado vuelto a depositar (dueña 2026-06-30). Si el banco lo
    # protesta OTRA vez → vuelve a "1" (dueña 2026-07-20: "el cheque de CG3
    # necesito colocar en estado 1"). Cambio de etiqueta plano: la V nueva no
    # tiene mov de banco en la app (depósito/protesto reales llegan por el sync).
    "V": {"1"},
    # Eliminado: restaurar sólo a cartera (los movimientos ya se compensaron al anular).
    "X": {"Z", "P", "D"},
    # Estados CON movimiento: salida sólo por rebote/anulación (compensan banco).
    "B": {"9", "X", "1"},
    "I": {"9", "X"},
    "A": {"9", "X"},
}


def compensar_deposito_devuelto(
    conn,
    *,
    id_cheque: int,
    importe: float,
    codigo_cli: str | None,
    no_cheque: str | None,
    fecha: date,
    usuario: str = "web",
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
    """
    imp = float(importe or 0)
    if imp <= 0:
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
        return 0.0
    import bank_helpers

    banco_orig = int(links[0]["no_banco"])
    bank_helpers.insert_movimiento_bancario(
        conn,
        no_banco=banco_orig,
        no_cta=None,
        fecha=fecha,
        documento="ND",
        importe=imp,
        concepto=(
            f"ch.devuelto {no_cheque or id_cheque} {codigo_cli or ''}"
        ).strip()[:50],
        prov=codigo_cli,
        numreferencia=id_cheque,
        usuario=usuario,
    )
    # Desagrupar: borrar los links del cheque a su(s) depósito(s) 'DE'.
    for lk in links:
        db.execute(
            "DELETE FROM scintela.chequextransaccion "
            "WHERE id_cheque = %s AND id_transaccion = %s",
            (id_cheque, lk["id_transaccion"]),
            conn=conn,
        )
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
        importe = float(ch["importe"] or 0)

        # --- depositado: B (cartera→Pichincha), V (re-depósito de un devuelto→
        # Pichincha) o I (Internacional). TMT 2026-07-25 (dueña): 'V' ahora CREA
        # el movimiento de banco (DE) igual que 'B' — antes era etiqueta plana y
        # esperaba el sync de PICHINCH.DBF. dBase (BANCOS.PRG DEPOBAN) deposita
        # un devuelto igual que uno de cartera; solo cambia la letra. ---
        if stat_destino in ("B", "V", "I"):
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
            res = bank_helpers.insert_movimiento_bancario(
                conn,
                no_banco=banco_destino,
                no_cta=None,
                fecha=fecha,
                documento="DE",
                importe=importe,
                concepto=f"Dep cheque {ch.get('no_cheque') or ''} {ch.get('codigo_cli') or ''}".strip(),
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
            params += [usuario, id_cheque]
            db.execute(
                "UPDATE scintela.cheque "
                "SET stat=%s" + set_fecha + ", "
                "    usuario_modifica=%s, fecha_modifica=CURRENT_TIMESTAMP "
                "WHERE id_cheque=%s",
                tuple(params),
                conn=conn,
            )

    return {
        "id_cheque": id_cheque,
        "stat_previo": stat_prev,
        "stat_nuevo": stat_destino,
        "side_effect_id": side_effect_id,
        "motivo": motivo,
    }


def anular_por_error_de_carga(
    id_cheque: int,
    *,
    motivo: str,
    id_reemplazo: int | None = None,
    usuario: str = "web",
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

    Después la persona usa "Nuevo cheque" para cargar el correcto.
    `id_reemplazo` (opcional) se appendea a la observacion para enlazar.

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
        stat_prev = (ch.get("stat") or "").upper()
        if stat_prev in ("X", "T", "R"):
            raise ValueError(
                f"Cheque ya cerrado (stat='{stat_prev}'). Anular por error de "
                "carga sólo aplica a cheques activos."
            )

        importe = float(ch["importe"] or 0)
        compensacion = None

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
            if nuevo_abono <= 0.01:
                nuevo_stat_f = "Z"
            elif nuevo_saldo <= 0.01:
                nuevo_stat_f = "T"
            else:
                nuevo_stat_f = "A"
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
        if stat_prev in ("B", "V", "W", "I", "J", "K", "A"):
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
                            'cheque_efectivo_to_caja')
               AND estado='activo'
            """,
            (_id_reverso, id_cheque),
            conn=conn,
        )

    return {
        "id_cheque": id_cheque,
        "stat_previo": stat_prev,
        "stat_nuevo": "X",
        "motivo": motivo,
        "id_reemplazo": id_reemplazo,
        "compensacion": compensacion,
        "aplicaciones_reversadas": len(aplic),
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
        aplic = db.fetch_all(
            "SELECT id_chequexfact FROM scintela.chequesxfact WHERE id_cheque = %s",
            (id_cheque,),
            conn=conn,
        ) or []
        if aplic:
            raise ValueError(
                f"El cheque #{id_cheque} ya está aplicado a factura(s) — "
                "desaplicalo desde su ficha y volvé a intentar (cancelarlo "
                "acá reabriría las facturas sin compensación del anticipo)."
            )

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
            # Criterio estricto: T sólo si saldo ≤ 0. Si la dueña quiere
            # "olvidar" un saldo residual de la aplicación, usa el toggle
            # explícito "olvidar saldo" en el form.
            if nuevo_saldo <= 0.01:
                nuevo_stat_f = "T"
            elif nuevo_abono > 0.01:
                nuevo_stat_f = "A"
            else:
                nuevo_stat_f = "Z"
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
    "depositados": ("B", "A", "C", "V"),  # B nuevo + A legacy + C efectivo + V re-depósito
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
STATS_VIVOS = ("Z", "B", "1", "2", "3", "D", "P", "A")


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
    "3": [],  # 2do rebote terminal
    "R": [],  # rebote terminal legacy
    "E": [],  # endosado — vive en /historial para reverso
    "X": [],  # eliminado/anulado
    "T": [],  # cobrado total
}


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
    base = [
        o for o in TRANSICIONES_LEGALES.get(s, [])
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
    # _stat_destino_reversa (depositado B/A → 1 primer rebote; 1/2 → 3). Mostramos
    # el destino REAL en el dropdown en vez del confuso "9". Solo para rebote real
    # (B/A/1/2); Z/D/P/V es reversa administrativa (→X) y queda como está.
    for o in base:
        if o.get("endpoint") == "cheques.confirmar_reverso":
            try:
                _d, _es_reb = _stat_destino_reversa(s)
                if _es_reb:
                    # `destino_real` es SOLO para el display del dropdown; el
                    # stat_destino (9) se mantiene = la ACCIÓN válida del backend
                    # (el wizard de reverso decide el estado final vía reversar).
                    o["destino_real"] = _d
            except Exception:  # noqa: BLE001
                pass
    return base


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

# Stats desde los que un reversar() representa un REBOTE REAL (banco lo
# rechazó), no una anulación administrativa. Dispara stop automático del
# cliente. Incluye:
#   - 'B' = depositado en Pichincha (rebote de primera vez)
#   - '1', '2' = ya rebotado una vez (un segundo intento de cobro que rebota)
#   - 'A' = legacy acreditado (rebote tardío en datos viejos)
STATS_REBOTE_REAL = ("B", "1", "2", "A")

# Stats considerados terminales — no admiten transiciones salvo reversa.
# 'B' = depositado feliz; 'T' no aplica a cheques (es factura).
STATS_TERMINALES = ("B",)


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
        "SELECT id_cheque, no_cheque, codigo_cli, importe, stat "
        "FROM scintela.cheque WHERE id_cheque = %s",
        (id_cheque,),
    )
    if not ch:
        raise ValueError("El cheque no existe.")
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
    # TMT 2026-06-07: SOLO auto-flip a 'B' (depositado) para importes
    # POSITIVOS. Un importe NEGATIVO (reversa/crédito) que se marcaba 'B'
    # quedaba "depositado" SIN movimiento de banco/caja (el insert de banco
    # y el de caja tienen gate importe>0) → se tragaba el negativo en
    # silencio y el saldo bancario/caja quedaba inflado. Dejándolo en 'Z'
    # (cartera) el negativo queda VISIBLE como nota de crédito y consistente.
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
    if no_banco in (90, 91) and (stat or "").upper() == "Z" and float(importe or 0) > 0:
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
    _genera_mov_banco = (no_banco in (90, 91)) and float(importe or 0) > 0
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
                 banco, stat, fechaing, prov, clave, doc_banco, concepto,
                 usuario_crea)
            VALUES (%s, %s, %s, %s,
                    %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s,
                    %s)
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
                concepto=(f"Cheque #{(no_cheque or '').strip()} de {codigo_cli.upper().strip()}")[:200],
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
            and importe_principal > 0
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
        # Saldo running: pasamos NULL, el trigger
        # `trg_caja_set_saldo` (mig 0022) lo computa BEFORE INSERT.
        # NO usamos caja.queries.crear() acá para evitar la cascada de
        # side-effects basados en concepto_parser (riesgo de match falso).
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
                caja_row = (
                    db.execute_returning(
                        """
                INSERT INTO scintela.caja
                    (fecha, tipo, importe, concepto, saldo, clave,
                     id_cheque, usuario_crea)
                VALUES (%s, %s, %s, %s, NULL, %s, %s, %s)
                RETURNING id_caja
                """,
                        (
                            fecha,
                            _tipo_caja,
                            _importe_caja,
                            concepto_caja,
                            (clave or None) and clave[:3],
                            row["id_cheque"],
                            usuario,
                        ),
                        conn=conn,
                    )
                    or {}
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
            "SELECT id_cheque, no_cheque, stat, codigo_cli, fechad, importe "
            "FROM scintela.cheque WHERE id_cheque = %s",
            (id_cheque,),
            conn=conn,
        )
        if not ch:
            raise ValueError(f"Cheque {id_cheque} no existe.")
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
        if nuevo_abono <= 0.01:
            nuevo_stat = "Z"
        elif nuevo_saldo <= 0.01:
            nuevo_stat = "T"
        else:
            nuevo_stat = "A"
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
    # Depositado feliz (B nuevo o A legacy): primer rebote → 1.
    if s in ("B", "A"):
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
            # abono. El stat se recalcula:
            #   abono = 0    → 'Z' (factura back to emitida)
            #   abono > 0    → 'A' (abonada parcial)
            #   saldo = 0    → 'T' (no debería pasar reduciendo abono;
            #                       sólo si ya estaba cancelada y queda así)
            if nuevo_abono <= 0.01:
                nuevo_stat = "Z"
            elif nuevo_saldo <= 0.01:
                nuevo_stat = "T"
            else:
                nuevo_stat = "A"
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
) -> dict:
    """SUM(importe) + COUNT(*) sobre TODO el universo del filtro (sin LIMIT).

    Útil para mostrar "Total" en el listado: el total visible está limitado
    a `limite` filas, pero el total real del filtro lo sacamos en una query
    aparte con la misma cláusula WHERE.
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
        """.replace("__NOMBRE_CLI__", _nom_cli or "FALSE"),
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
                nuevo_stat = "T" if nuevo_saldo <= 0.01 else "A"
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
    id_mov_doble: int, *, usuario: str = "web", motivo: str = ""
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

    with db.tx() as conn:
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
                "motivo": motivo or "",
            },
            id_original=id_mov_doble,
        )

    return {
        "id_cheque": id_cheque,
        "no_cheque": (ch.get("no_cheque") or "").strip(),
        "stat_restaurado": stat_destino,
        "aplicaciones_reaplicadas": n_aplic,
        "aplicaciones_pendientes": (
            int(meta.get("n_aplicaciones_reversadas") or 0)
            if sin_snapshot_aplic else 0),
        "compensacion": compensacion_nueva,
    }
