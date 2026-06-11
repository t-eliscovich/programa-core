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
from periodo_guard import asegurar_fecha_abierta

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


def editar(
    id_cheque: int,
    *,
    concepto: str | None = None,
    observacion: str | None = None,
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
        "SELECT id_cheque, no_cheque, stat, fechad, doc_banco, no_banco FROM scintela.cheque WHERE id_cheque = %s",
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
            raise ValueError(
                "Este cheque ya tiene movimientos de banco/caja linkeados — "
                "el banco emisor no se puede cambiar acá. Usá 'Anular por "
                "error de carga' y recargalo con el banco correcto."
            )
        sql_set.append("no_banco=%s")
        params.append(int(no_banco))
        sql_set.append("banco=%s")
        params.append((banco_row.get("nombre") or "")[:30] or None)
        sql_set.append(
            "observacion = COALESCE(observacion||' | ','')||%s"
        )
        params.append(
            f"[E] banco emisor {ch.get('no_banco') or '—'} → {no_banco}"
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
#   "B"           → depositado Pichincha: INSERT tx_bancarias DOC=DE banco=1
#   "I" o "V"     → depositado Internacional: INSERT tx_bancarias DOC=DE banco=2
#   "9"           → rebotado: INSERT posdat banc=0 + cliente.stop=S
#   "X"           → anulado: sólo UPDATE
# NOTA TMT 2026-05-14 (#17): 'V' (banco Internacional legacy) está
# DEPRECADO como destino. No aparece en ninguna lista — intentarlo
# levanta ValueError abajo. Filas históricas con stat='V' se respetan,
# pero no se generan nuevas.
TRANSICIONES_VALIDAS = {
    "Z": {"B", "C", "9", "X", "P", "D", "I"},
    # TMT 2026-05-19 v4 audit — agregadas D/P como salida desde
    # rebotados (1/2) y desde D/P entre sí. El UI dropdown
    # (TRANSICIONES_LEGALES) ya las ofrecía pero el backend rechazaba.
    # Casos operativos:
    #   1 → D: el cheque rebotó y pasamos a Daniela a cobrar.
    #   1 → P: el cheque rebotó y postergamos a otra fecha.
    #   D → P: Daniela trajo el cheque, queremos posdatarlo.
    #   P → D: el postergado lo manda a Daniela.
    "P": {"B", "C", "X", "I", "D"},
    "D": {"B", "C", "X", "I", "P"},
    "B": {"9", "X"},
    "I": {"9", "X"},
    "1": {"9", "X", "P", "D"},
    "2": {"9", "X", "P", "D"},
    "A": {"9", "X"},
}


def transicionar_stat(
    id_cheque: int,
    *,
    stat_destino: str,
    no_banco: int | None = None,
    fecha: date | None = None,
    motivo: str = "",
    usuario: str = "web",
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
    # 'V' (banco Internacional legacy) deprecado como destino. TMT 2026-05-14 (#17).
    if stat_destino == "V":
        raise ValueError(
            "stat='V' (banco Internacional legacy) está deprecado. Usá 'B' (Pichincha) o 'I' al depositar."
        )

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

        # --- depositado: B (Pichincha) o I (Internacional) ---
        # 'V' está bloqueado arriba con ValueError. TMT 2026-05-14 (#17).
        if stat_destino in ("B", "I"):
            import bank_helpers

            banco_destino = no_banco or (1 if stat_destino == "B" else 2)
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
                "UPDATE scintela.cheque "
                "SET stat=%s, fechaing=%s, no_banco=%s, "
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
            db.execute(
                "UPDATE scintela.cheque "
                "SET stat=%s, "
                "    usuario_modifica=%s, fecha_modifica=CURRENT_TIMESTAMP "
                "WHERE id_cheque=%s",
                (stat_destino, usuario, id_cheque),
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
                "SELECT importe, abono FROM scintela.factura WHERE id_factura = %s",
                (id_fact,),
                conn=conn,
            )
            if not f:
                continue
            nuevo_abono = max(float(f["abono"] or 0) - imp, 0)
            nuevo_saldo = float(f["importe"] or 0) - nuevo_abono
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

        # --- Compensación bancaria/caja según stat actual ---
        if stat_prev in ("B", "V", "W", "I", "J", "K", "A"):
            import bank_helpers

            banco = ch.get("no_banco") or (1 if stat_prev == "B" else 2)
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
        elif stat_prev == "C":
            import caja_helpers

            res = caja_helpers.insert_movimiento_caja(
                conn,
                fecha=fecha,
                tipo="S",
                importe=importe,
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

    return {
        "id_cheque": id_cheque,
        "stat_previo": stat_prev,
        "stat_nuevo": "X",
        "motivo": motivo,
        "id_reemplazo": id_reemplazo,
        "compensacion": compensacion,
        "aplicaciones_reversadas": len(aplic),
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
                "SELECT id_factura, numf, importe, abono FROM scintela.factura "
                "WHERE id_factura = %s FOR UPDATE",
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
            nuevo_saldo = importe_f - nuevo_abono
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
    return db.fetch_one(
        """
        SELECT c.id_cheque, c.no_cheque, c.fecha, c.fechad, c.fechaing, c.fechaout,
               c.fecha_recibido, c.fecha_crea, c.fecha_postergacion, c.fechad_original,
               c.codigo_cli, c.importe, c.stat, c.no_banco,
               c.banco AS banco_texto, c.prov, c.clave,
               c.numero_transaccion, c.id_cheque_padre, c.pasaconta,
               -- TMT 2026-05-27 dueña: doc_banco editable inline (separado
               -- del no_cheque). Card propio en detalle.
               c.doc_banco,
               COALESCE(cli.nombre, '') AS cliente,
               cli.ruc, cli.telefono,
               COALESCE(bco.nombre, c.banco) AS banco
          FROM scintela.cheque c
          LEFT JOIN scintela.cliente cli ON cli.codigo_cli = c.codigo_cli
          LEFT JOIN scintela.banco   bco ON bco.no_banco   = c.no_banco
         WHERE c.no_cheque::text = %s OR c.id_cheque = %s
         ORDER BY (c.no_cheque::text = %s) DESC, c.id_cheque ASC
         LIMIT 1
        """,
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
        # 1) UPDATE cheques en bloque — stat='B' (depositado, terminal feliz).
        # En el vocabulario nuevo (2026-04-29), todo depósito a cualquier
        # banco va a 'B'. La distinción de banco vive en `no_banco` — el
        # stat sólo trackea la fase del cheque (cartera / depositado /
        # rebotado / etc), no la cuenta destino.
        cur.execute(
            f"""
            UPDATE scintela.cheque
               SET stat = 'B',
                   fechaing = %s,
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
        for r in rows:
            imp = float(r.get("importe") or 0)
            if imp <= 0:
                # Cheque con importe 0 o negativo — no genera movimiento
                # bancario (en cartera puede ser un cheque rebotado mal cargado).
                continue
            # TMT 2026-05-26 dueña: numreferencia = doc_banco (N° comprobante
            # del banco que la dueña carga al ingresar el cheque) si existe.
            # Es la rule #1 del matcher de conciliación. Fallback: id_cheque.
            num_ref = (r.get("doc_banco") or "").strip() or str(r.get("id_cheque") or "")
            mov = bank_helpers.insert_movimiento_bancario(
                conn,
                no_banco=no_banco,
                no_cta=None,
                fecha=fecha_deposito,
                documento="DE",
                importe=imp,
                concepto=(
                    concepto or f"Dep. cheque {r.get('no_cheque') or ''} {r.get('codigo_cli') or ''}"
                ).strip()[:50],
                prov=(r.get("codigo_cli") or "")[:5],
                numreferencia=num_ref,
                stat="A",
                usuario=usuario,
            )
            id_t = mov.get("id_transaccion")
            if id_t:
                id_transacciones.append(int(id_t))
                # stat_ch en chequextransaccion histórico era 'D' (depositado).
                # Mantenemos el código histórico 'D' — esta tabla traza el
                # evento del depósito, no el estado del cheque. El cheque
                # mismo está en stat='B'.
                cur.execute(
                    """
                    INSERT INTO scintela.chequextransaccion
                        (id_cheque, id_transaccion, fecha, stat_ch, usuario_crea)
                    VALUES (%s, %s, %s, 'D', %s)
                    """,
                    (r["id_cheque"], id_t, fecha_deposito, usuario[:50]),
                )
                # mov_doble — para que /historial muestre el depósito como
                # cheque → banco (no "banco mov #X → banco mov #X"). TMT
                # 2026-05-16. El tipo `cheque_depositado` ya está mapeado en
                # _REVERSO_DISPATCH para reversar Z→B con compensación banco.
                import mov_doble as _md

                _md.registrar(
                    conn=conn,
                    tipo="cheque_depositado",
                    origen_table="cheque",
                    origen_id=int(r["id_cheque"]),
                    destino_table="transacciones_bancarias",
                    destino_id=int(id_t),
                    importe=imp,
                    fecha=fecha_deposito,
                    concepto=(
                        f"Dep. cheque {r.get('no_cheque') or '#' + str(r['id_cheque'])} "
                        f"{r.get('codigo_cli') or ''}"
                    ).strip()[:200],
                    usuario=usuario,
                    metadata={
                        "id_cheque": int(r["id_cheque"]),
                        "id_transaccion": int(id_t),
                        "no_banco": no_banco,
                        "banco_nombre": banco_nombre,
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
    "cartera": ("Z",),  # ingresado, sin movimiento
    "depositados": ("B", "A", "C"),  # B nuevo + A legacy + C efectivo (99, paridad dBase)
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
            "stat_destino": "B",
            "label": "Depositar (al banco)",
            "kind": "WIZARD",
            "endpoint": "cheques.depositar_lote",
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
    # B = depositado en banco. Sólo se puede marcar rebote.
    "B": [
        {
            "stat_destino": "9",
            "label": "Marcar como rebotado",
            "kind": "WIZARD",
            "endpoint": "cheques.confirmar_reverso",
        },
    ],
    "A": [
        {
            "stat_destino": "9",
            "label": "Marcar como rebotado",
            "kind": "WIZARD",
            "endpoint": "cheques.confirmar_reverso",
        },
    ],
    "V": [
        {
            "stat_destino": "9",
            "label": "Marcar como rebotado",
            "kind": "WIZARD",
            "endpoint": "cheques.confirmar_reverso",
        },
    ],
    # 1 / 2 = rebote en gestión.
    "1": [
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
    "D": [
        {
            "stat_destino": "P",
            "label": "Postergar fecha",
            "kind": "POSTERGAR",
            "endpoint": "cheques.postergar",
        },
    ],
    # P = postergado. Volver a cartera (Z), Daniela, o re-postergar.
    "P": [
        {"stat_destino": "D", "label": "Pasar a Daniela", "kind": "POST", "endpoint": "cheques.transicionar"},
        {
            "stat_destino": "P",
            "label": "Re-postergar (nueva fecha)",
            "kind": "POSTERGAR",
            "endpoint": "cheques.postergar",
        },
    ],
    # Estados terminales — sin transiciones disponibles.
    "3": [],  # 2do rebote terminal
    "R": [],  # rebote terminal legacy
    "E": [],  # endosado — vive en /historial para reverso
    "X": [],  # eliminado/anulado
    "T": [],  # cobrado total
}


def transiciones_para(stat: str) -> list[dict]:
    """Devuelve la lista de transiciones legales desde un stat actual."""
    s = (stat or "").upper().strip()
    return TRANSICIONES_LEGALES.get(s, [])


# Stats que pueden iniciar un depósito a banco. Z (cartera) es el flujo
# típico. P (postdatado/postergado) también es válido cuando llega la fecha
# de depósito — operacionalmente el cobranzador deposita directo sin pasar
# por Z. Cualquier otro stat origen es un bug en la UI.
STATS_DEPOSITABLES = ("Z", "P")

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
    usuario: str = "web",
    batch_id: str | None = None,
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
    #   95 CANCELA ANTIC → marca contable; queda en cartera para que la
    #                       dueña lo aplique manualmente.
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
    if no_banco == 99 and (stat or "").upper() in ("Z", "B") and float(importe or 0) > 0:
        stat = "C"

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
        row = (
            db.execute_returning(
                """
            INSERT INTO scintela.cheque
                (no_cheque, fecha, fechad, fecha_recibido,
                 codigo_cli, importe, no_banco,
                 banco, stat, fechaing, prov, clave, doc_banco, usuario_crea)
            VALUES (%s, %s, %s, %s,
                    %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s)
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
            num_ref = (doc_banco or "").strip() or str(row["id_cheque"])
            cli_u = codigo_cli.upper().strip()
            mov_dep = bank_helpers.insert_movimiento_bancario(
                conn,
                no_banco=banco_real,
                no_cta=None,
                fecha=fecha,
                documento="DE",
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
            espejo_95 = db.fetch_one(
                """
                SELECT id_cheque FROM scintela.cheque
                 WHERE codigo_cli = %s
                   AND importe = %s
                   AND no_banco IN (97, 98)
                   AND TRIM(COALESCE(stat, '')) = 'Z'
                 ORDER BY id_cheque
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
        if no_banco == 99 and row.get("id_cheque") and importe_principal > 0:
            # Concepto paridad PASOCAJA: "CH."+cliente (antes "99 CLI ch N").
            concepto_caja = f"CH.{codigo_cli.upper().strip()}"[:80]
            caja_row = (
                db.execute_returning(
                    """
                INSERT INTO scintela.caja
                    (fecha, tipo, importe, concepto, saldo, clave,
                     id_cheque, usuario_crea)
                VALUES (%s, 'E', %s, %s, NULL, %s, %s, %s)
                RETURNING id_caja
                """,
                    (
                        fecha,
                        importe_principal,
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
                        f"Cobro efectivo ch{(no_cheque or '').strip()} de {codigo_cli.upper().strip()} → caja"
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

        # Espejo de anticipo (importe negativo) — sólo si flag activo y >0
        if es_anticipo and importe_principal > 0:
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
                        -importe_principal,  # espejo negativo
                        98,
                        "ANTICIPO",
                        (prov or None),
                        (clave or None) and clave[:5],
                        usuario,
                        row.get("id_cheque"),  # apunta al cheque "padre" para auditoría
                    ),
                    conn=conn,
                )
                or {}
            )
            row["id_cheque_anticipo"] = espejo.get("id_cheque")
            # mov_doble del espejo — link cheque normal → cheque espejo.
            # TMT 2026-05-14 (issue #25).
            if espejo.get("id_cheque") and row.get("id_cheque"):
                _md.registrar(
                    conn=conn,
                    tipo="cheque_anticipo_espejo",
                    origen_table="cheque",
                    origen_id=row["id_cheque"],
                    destino_table="cheque",
                    destino_id=espejo["id_cheque"],
                    importe=-importe_principal,  # espejo es negativo
                    fecha=fecha,
                    concepto=(
                        f"Espejo de anticipo ch{(no_cheque or '').strip()} de {codigo_cli.upper().strip()}"
                    )[:200],
                    usuario=usuario,
                    metadata={
                        "codigo_cli": codigo_cli.upper().strip(),
                        "id_cheque_padre": row["id_cheque"],
                        "id_cheque_espejo": espejo["id_cheque"],
                    },
                )
    return row


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
      - Append al `posdat` ya existente: actualiza la fecha si había una
        fila banc=0; si no había, la crea (cheques que originalmente eran
        Z ahora pasan a vivir en el flujo de "cheques futuros").
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
        if stat_prev not in (*STATS_POSTERGABLES, "P"):
            raise ValueError(
                f"Sólo cheques en cartera (Z) o ya postergados (P) se pueden "
                f"postergar. Este está en stat='{stat_prev}'."
            )
        if not nueva_fechad or nueva_fechad <= (ch["fechad"] or today_ec()):
            raise ValueError("La nueva fecha debe ser posterior a la fecha actual del cheque.")

        db.execute(
            "UPDATE scintela.cheque "
            "SET stat='P', fechad=%s, "
            "    fecha_postergacion = CURRENT_DATE, "
            "    fechad_original = COALESCE(fechad_original, fechad), "
            "    usuario_modifica=%s, "
            "    fecha_modifica=CURRENT_TIMESTAMP "
            "WHERE id_cheque=%s",
            (nueva_fechad, usuario, id_cheque),
            conn=conn,
        )
        # Sincroniza posdat: upsert manual.
        db.execute(
            """
            INSERT INTO scintela.posdat (fecha, prov, num, importe, banc, usuario_crea)
            SELECT %s, %s, %s, %s, 0, %s
            WHERE NOT EXISTS (
                SELECT 1 FROM scintela.posdat
                 WHERE COALESCE(banc, 0) = 0 AND num = %s AND prov = %s
            )
            """,
            (
                nueva_fechad,
                ch["codigo_cli"],
                id_cheque,
                float(ch["importe"] or 0),
                usuario,
                id_cheque,
                ch["codigo_cli"],
            ),
            conn=conn,
        )
        db.execute(
            "UPDATE scintela.posdat SET fecha=%s, usuario_modifica=%s WHERE COALESCE(banc, 0) = 0 AND num=%s AND prov=%s",
            (nueva_fechad, usuario, id_cheque, ch["codigo_cli"]),
            conn=conn,
        )

    return {
        "id_cheque": id_cheque,
        "stat_previo": stat_prev,
        "stat_nuevo": "P",
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
      2. Recalcula factura.abono -= sum(importes), factura.saldo = importe - abono,
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
            "SELECT id_factura, numf, importe, abono FROM scintela.factura WHERE id_factura = %s",
            (id_factura,),
            conn=conn,
        )
        if not f:
            raise ValueError(f"Factura id={id_factura} no existe.")
        nuevo_abono = max(float(f.get("abono") or 0) - total_desaplicar, 0)
        nuevo_saldo = float(f.get("importe") or 0) - nuevo_abono
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
      - actualiza `factura.abono += importe`, `factura.saldo = importe - abono`,
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
                "SELECT id_factura, numf, importe, abono, saldo, stat "
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
                if abs(imp) > abono_actual + 0.01:
                    raise ValueError(
                        f"Espejo ({abs(imp):.2f}) excede el abono de factura "
                        f"{f['numf']} ({abono_actual:.2f})."
                    )
                nuevo_abono = abono_actual - abs(imp)
            else:
                # TMT 2026-05-15: para absorción de crédito (imp<0 contra
                # saldo<0), el signo debe matchear y |imp| <= |saldo|.
                if imp > 0:
                    if saldo_actual < -0.005:
                        raise ValueError(
                            f"Factura {f['numf']} tiene saldo NEGATIVO "
                            f"({saldo_actual:.2f}) — aplicá un importe NEGATIVO "
                            f"para absorber el crédito, o sacá esta factura de "
                            f"la lista."
                        )
                    # TMT 2026-05-15: tolerancia de $50 — el JS pregunta al
                    # submit para diferencias chicas. dBase legacy preguntaba
                    # "Faltan X dólares, OK?".
                    if imp > saldo_actual + 50.00:
                        raise ValueError(
                            f"Aplicación ({imp:.2f}) excede el saldo de "
                            f"factura {f['numf']} ({saldo_actual:.2f}) "
                            f"por más de $50."
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
            nuevo_saldo = float(f["importe"] or 0) - nuevo_abono
            # Vocabulario canónico (2026-04-29, restaurado 2026-05-15):
            # El paso de confirmación puede pasar `forzar_stat='T'|'A'` por
            # aplicación. Si viene, ese gana sobre la lógica automática.
            # Sin override:
            #   saldo ≤ 0  → 'T' (cancelada — cubierto entero)
            #   |saldo| ≤ $0.50 → 'T' (centavos olvidados, auto)
            #   saldo > $0.50 con abono → 'A' (abonada parcial)
            #   abono = 0 → preserva el stat actual o 'Z'
            forzar_stat = (a.get("forzar_stat") or "").upper().strip()
            if forzar_stat in ("T", "A"):
                nuevo_stat = forzar_stat
            elif nuevo_saldo <= 0.01 or abs(nuevo_saldo) <= 0.50:
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
                    f"importe del espejo ({abs(restante_cheque):.2f})."
                )
        else:
            if total_aplicado > restante_cheque + 0.01:
                raise ValueError(
                    f"Total aplicado ({total_aplicado:.2f}) excede el importe del cheque ({restante_cheque:.2f})."
                )

    return {"id_cheque": id_cheque, "total_aplicado": total_aplicado, "n": len(aplicaciones)}


def _stat_destino_reversa(stat_prev: str) -> tuple[str, bool]:
    """Devuelve (stat_destino, es_rebote_real) según el vocabulario nuevo.

    Reglas (2026-04-29):
      - B → 1   (primer rebote del banco — REBOTE REAL)
      - 1 → 3   (segundo rebote — REBOTE REAL)
      - 2 → 3   (segundo rebote desde alias 2 — REBOTE REAL)
      - A → 1   (legacy acreditado rebotado tardío — REBOTE REAL)
      - Z → X   (eliminado por error — administrativo)
      - D → X   (Daniela cancela, devuelve cheque — administrativo)
      - P → X   (postergado anulado — administrativo)
      - V → X   (legacy Internacional cancelado — administrativo)
      - X, R, 3 → ValueError (terminal, no se puede reversar más)
    """
    s = (stat_prev or "").upper()
    # Depositado feliz (B nuevo o A legacy): primer rebote → 1.
    if s in ("B", "A"):
        return "1", True
    # Ya rebotado una vez (1 o 2): segundo rebote → 3.
    if s in ("1", "2"):
        return "3", True
    # Vivos no depositados (Z/D/P) o legacy V: eliminación administrativa.
    if s in ("Z", "D", "P", "V"):
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

    Máquina de estados (vocabulario canónico 2026-04-29):
        Z, D, P, V (cartera/Daniela/postergado/legacy)
                          → X (eliminado por error) — administrativo, sin stop
        B (depositado Pichincha)
                          → 1 (primer rebote) — REBOTE REAL, stop al cliente
        1, 2 (devueltos)  → 3 (segundo rebote)  — REBOTE REAL, stop al cliente
        A (legacy acred.) → 1 (rebote tardío)   — REBOTE REAL, stop al cliente
        X, R, 3 (terminales) → ValueError

    Para cada chequesxfact del cheque:
      - resta el importe de factura.abono,
      - suma al saldo,
      - si el saldo > 0, abre la factura (stat='A').

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
            "SELECT id_cheque, no_cheque, stat, codigo_cli FROM scintela.cheque WHERE id_cheque = %s",
            (id_cheque,),
            conn=conn,
        )
        if not ch:
            raise ValueError(f"Cheque {id_cheque} no existe.")
        stat_prev = (ch["stat"] or "").upper()
        # _stat_destino_reversa levanta si stat_prev es terminal (X/R/3).
        stat_nuevo, es_rebote_real = _stat_destino_reversa(stat_prev)

        # Traer aplicaciones para revertir
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
                "SELECT importe, abono FROM scintela.factura WHERE id_factura = %s",
                (id_fact,),
                conn=conn,
            )
            if not f:
                continue
            nuevo_abono = max(float(f["abono"] or 0) - imp, 0)
            nuevo_saldo = float(f["importe"] or 0) - nuevo_abono
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
        # con stat='X', lo que ensuciaba el detalle de factura (mostraba
        # "Cheque XXX aplicado $21" aunque ya estuviera anulado) y podía
        # bloquear futuras anulaciones de factura con falso "cheque vivo".
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
        _md.registrar(
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
        db.execute(
            """
            UPDATE scintela.mov_doble
               SET estado='reversado'
             WHERE origen_table='cheque' AND origen_id=%s
               AND tipo='cheque_aplicado_a_factura' AND estado='activo'
            """,
            (id_cheque,),
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
               importe, abono, saldo, stat
        FROM scintela.factura
        WHERE codigo_cli = %s
          AND COALESCE(saldo, 0) <> 0
          AND (
            -- vivas (positivo): stat válido
            (COALESCE(saldo, 0) > 0
             AND (stat IS NULL OR stat IN ('A','Z','',' ')))
            OR
            -- crédito a favor (negativo): cualquier stat, queremos verlas
            -- aunque la factura esté formalmente cerrada (T).
            COALESCE(saldo, 0) < 0
          )
        -- TMT 2026-05-15: orden cronológico puro (positivas y negativas
        -- mezcladas por fecha de emisión / vencimiento). La separación
        -- previa por signo confundía visualmente al aplicar.
        ORDER BY fecha, vencimiento NULLS LAST, numf
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
) -> dict:
    """SUM(importe) + COUNT(*) sobre TODO el universo del filtro (sin LIMIT).

    Útil para mostrar "Total" en el listado: el total visible está limitado
    a `limite` filas, pero el total real del filtro lo sacamos en una query
    aparte con la misma cláusula WHERE.
    """
    q = (q or "").strip()
    like = f"%{q}%" if q else None
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
                     AND UPPER(cli.nombre) LIKE UPPER(%(like)s)
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
          AND (%(monto_min)s::numeric IS NULL OR COALESCE(c.importe, 0) >= %(monto_min)s)
          AND (%(monto_max)s::numeric IS NULL OR COALESCE(c.importe, 0) <= %(monto_max)s)
          -- Excluir reversados del total. Pedido TMT 2026-05-14.
          AND COALESCE(c.stat, '') <> 'X'
        """,
        {
            "q": q or None,
            "like": like,
            "desde": desde or None,
            "hasta": hasta or None,
            "stats": list(stats) if stats else None,
            "cliente": (cliente or None),
            "monto_min": monto_min,
            "monto_max": monto_max,
        },
    )
    return {
        "n": int(row["n"] or 0) if row else 0,
        "total": float(row["total"] or 0) if row else 0.0,
    }


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
    q = (q or "").strip()
    like = f"%{q}%" if q else None
    stats = STATS.get(estado)  # None = todos
    # Excluir stat='X' del listado por default cuando estado='todos'. Si la
    # usuaria pide `?ver_eliminados=1` o va al tab "eliminados", los muestra.
    excluir_eliminados = (stats is None) and (not ver_eliminados)
    cliente = (cliente or "").strip().upper()
    es_cli_codigo_exacto = bool(cliente) and len(cliente) == 3 and cliente.replace("_", "").isalnum()
    cliente_like = f"%{cliente}%" if cliente else None
    # Qué columna de fecha aplica el filtro desde/hasta. Para los estados que
    # ya pasaron por el banco (depositados/devueltos/daniela), filtramos por
    # `fechaing` (cuándo se ingresó al banco / rebotó / pasó a Daniela). Para
    # cartera/postergados/eliminados/endosados/todos seguimos filtrando por
    # `fechad` (cuándo está agendado a depositar) — es lo operativo.
    # TMT 2026-05-16: "ver cheques del día" en tab Depositados antes daba 0
    # porque filtraba por fechad y los depósitos tienen fechaing≠fechad.
    fecha_col_por_estado = {
        "depositados": "COALESCE(c.fechaing, c.fechad, c.fecha)",
        "devueltos": "COALESCE(c.fechaing, c.fechad, c.fecha)",
        "daniela": "COALESCE(c.fechaing, c.fechad, c.fecha)",
    }
    fecha_col = fecha_col_por_estado.get(estado, "COALESCE(c.fechad, c.fecha)")
    # TMT 2026-05-19 v8 — refactor: cliente/banco/proveedor se traen vía
    # subqueries escalares (LIMIT 1) en lugar de LEFT JOIN, para que el
    # COUNT del listado coincida con totc() del balance. Antes, si cualquier
    # codigo_cli tenía fanout > 1 en scintela.cliente, los cheques se
    # duplicaban (1.851.871 mostrado vs 1.840.030 real, diff 8 cheques).
    sql_buscar_cheques = """
        SELECT c.id_cheque, c.no_cheque, c.fecha, c.fechad, c.fechaing, c.fechaout,
               c.fecha_recibido, c.fecha_crea,
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
               c.importe, c.stat,
               -- TMT 2026-05-27 dueña: doc_banco editable inline en lista.
               -- Es el N° de comprobante/depósito (varchar(40)) — separado
               -- del no_cheque, alimentado al alta y al inline edit.
               c.doc_banco,
               c.no_banco, c.banco AS banco_nombre,
               COALESCE(
                 (SELECT bco.nombre FROM scintela.banco bco
                   WHERE bco.no_banco = c.no_banco LIMIT 1),
                 c.banco
               ) AS banco,
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
                     AND UPPER(cli.nombre) LIKE UPPER(%(like)s)
                )
             OR EXISTS (
                  SELECT 1 FROM scintela.proveedor prv
                   WHERE prv.codigo_prov = c.prov
                     AND UPPER(prv.nombre) LIKE UPPER(%(like)s)
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
        ORDER BY c.fecha ASC, c.id_cheque ASC
        LIMIT %(limite)s OFFSET %(offset)s
        """
    sql_buscar_cheques = sql_buscar_cheques.replace("__FECHA_COL__", fecha_col)
    rows = (
        db.fetch_all(
            sql_buscar_cheques,
            {
                "q": q or None,
                "like": like,
                "cliente": cliente or None,
                "cliente_like": cliente_like,
                "cli_codigo_exacto": es_cli_codigo_exacto,
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
    # Running total cronológico. Listado en orden ASC.
    from datetime import date as _date

    rows_asc = sorted(
        rows, key=lambda r: (r.get("fechad") or r.get("fecha") or _date.min, r.get("id_cheque") or 0)
    )
    acum = 0.0
    for r in rows_asc:
        acum += float(r.get("importe") or 0)
        r["saldo_acumulado"] = acum
    return rows_asc
