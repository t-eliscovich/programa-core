"""Caja (libro de caja en efectivo).

scintela.caja: id_caja, fecha, tipo(3), importe, concepto, saldo, clave, id_cheque

CONVENCIÓN DE SIGNO (legacy dBase, NO cambiar — los 722 rows históricos
están así y `resumen()` también):
- `importe` se almacena SIEMPRE POSITIVO (valor absoluto).
- El signo viene del campo `tipo`:
    'E'  = ENTRADA (ingreso) → suma al saldo
    'S'  = SALIDA  (egreso)  → resta del saldo
    'CB' = cobro con cheque  (informativo, requiere id_cheque)
- Antes de 2026-04-30 esta función usaba I/E con importe negativo, lo
  cual contaminaba la data legacy y rompía `resumen()`. Si encontrás
  filas con tipo='I' o importe<0, son del período Apr 17 → Apr 30 —
  corregir con un script de migración (`UPDATE caja SET tipo='E',
  importe=ABS(importe) WHERE tipo='I'` y simétrico para egresos).

`saldo` es el running balance histórico — se deriva en `resumen()` como
`opening + Σ E − Σ S`.
"""
from datetime import date

import db
from periodo_guard import asegurar_fecha_abierta


def movimientos(
    desde: str | None = None,
    hasta: str | None = None,
    q: str = "",
    limite: int = 500,
) -> list[dict]:
    q = (q or "").strip()
    like = f"%{q}%" if q else None
    return db.fetch_all(
        """
        SELECT c.id_caja, c.fecha, c.tipo, c.importe, c.concepto, c.saldo,
               c.clave, c.id_cheque,
               ch.no_cheque, ch.codigo_cli,
               COALESCE(cli.nombre, '') AS cliente
        FROM scintela.caja c
        LEFT JOIN scintela.cheque  ch  ON ch.id_cheque = c.id_cheque
        LEFT JOIN scintela.cliente cli ON cli.codigo_cli = ch.codigo_cli
        WHERE (%(desde)s::date IS NULL OR c.fecha >= %(desde)s::date)
          AND (%(hasta)s::date IS NULL OR c.fecha <= %(hasta)s::date)
          AND (%(q)s IS NULL
               OR UPPER(COALESCE(c.concepto,'')) LIKE UPPER(%(like)s)
               OR UPPER(COALESCE(c.tipo,'')) LIKE UPPER(%(like)s))
        ORDER BY c.fecha DESC, c.id_caja DESC
        LIMIT %(limite)s
        """,
        {
            "desde": desde or None, "hasta": hasta or None,
            "q": q or None, "like": like,
            "limite": limite,
        },
    )


def saldo_actual() -> float:
    """Saldo en caja AHORA = opening + Σ entradas − Σ salidas.

    El campo `importe` está en valor absoluto y el signo viene de `tipo`
    (E=+, S=−). `SUM(importe)` plano da basura. Para incluir caja con
    saldo histórico antes del primer movimiento cargado, sumamos también
    el opening (saldo de la primera fila menos su movimiento firmado).
    """
    row = db.fetch_one(
        """
        SELECT
            COALESCE(SUM(CASE WHEN tipo='E' THEN importe
                              WHEN tipo='S' THEN -importe
                              ELSE importe END), 0)
          + COALESCE(
              (SELECT saldo - CASE WHEN tipo='E' THEN importe
                                   WHEN tipo='S' THEN -importe
                                   ELSE importe END
                 FROM scintela.caja
                 WHERE saldo IS NOT NULL
                 ORDER BY fecha ASC, id_caja ASC LIMIT 1), 0
            ) AS saldo
        FROM scintela.caja
        """
    )
    return float(row["saldo"]) if row else 0.0


def crear(
    *,
    fecha: date,
    tipo: str,
    importe,
    concepto: str,
    clave: str | None = None,
    id_cheque: int | None = None,
    usuario: str = "web",
    xgast_num: int | None = None,
) -> dict:
    """Registrar un movimiento en caja.

    Convención (legacy dBase, ver docstring del módulo):
        tipo 'E'  = ENTRADA (ingreso) → importe siempre positivo
        tipo 'S'  = SALIDA  (egreso)  → importe siempre positivo
        tipo 'CB' = cobro con cheque  (informativo, requiere id_cheque)

    El SIGNO viene del `tipo`, NUNCA de `importe`. Si el caller pasa un
    importe negativo, esta función toma el ABS y deja `tipo` como vino.

    Actualiza el saldo running: saldo_prev + (importe firmado por tipo).
    """
    if not tipo:
        raise ValueError("Tipo requerido.")
    tipo = tipo.strip().upper()[:3]
    if tipo not in ("E", "S", "CB"):
        raise ValueError(f"Tipo inválido: {tipo!r}. Usá 'E' (entrada), 'S' (salida) o 'CB'.")
    if importe is None:
        raise ValueError("Importe requerido.")
    asegurar_fecha_abierta(fecha)

    # Importe SIEMPRE positivo en la columna; el signo lo lleva tipo.
    importe = abs(float(importe))
    if importe == 0:
        raise ValueError("Importe debe ser distinto de cero.")

    # Signo aritmético sólo para chequear que el saldo no quede negativo
    # y para actualizar el running saldo. NUNCA se persiste negativo.
    #
    # #34 (TMT 2026-05-14): interacción Python vs trigger DB.
    # `scintela.caja` tiene un BEFORE INSERT trigger
    # (migration 0022_auto_saldo_trigger_caja.sql) que setea `saldo`
    # SI viene NULL. Este Python computa `saldo_nuevo` y lo INCLUYE en
    # el INSERT — por lo tanto el trigger ve `NEW.saldo IS NOT NULL` y
    # respeta el valor (early-return en la función). No hay double-update.
    # La validación de "no negativo" se hace acá en Python (saldo_actual
    # + delta). Si en el futuro alguien deja `saldo=NULL`, el trigger lo
    # llena pero esta validación se saltea — por eso siempre pasamos saldo.
    importe_firmado = importe if tipo == "E" else (-importe if tipo == "S" else importe)
    # TMT 2026-06-03 audit fix: el cálculo de saldo_prev/saldo_nuevo se
    # mueve DENTRO del with db.tx() (línea 178+) con un advisory lock para
    # serializar inserts concurrentes. Antes leíamos saldo_actual() acá
    # afuera — dos requests simultáneas computaban el mismo saldo y
    # ambas INSERTaban con saldo igual, corrompiendo el running balance.

    # Side effects automáticos basados en el concepto (TMT 2026-05-12).
    # "Concept-driven double entries": si el concepto matchea un patrón
    # legacy (PICH/INTER/RR/IN.XX/PROV), se crea la fila secundaria en la
    # tabla destino, todo en la misma transacción.
    import concepto_parser
    import side_effects as _se

    # Contexto: provs válidos + bancos por nombre, para que el parser
    # pueda distinguir "AQ pago" (prov) de "ALQUILER" (no prov).
    provs_validos = {
        (r.get("codigo_prov") or "").strip().upper()
        for r in (db.fetch_all(
            "SELECT codigo_prov FROM scintela.proveedor"
        ) or [])
    }
    bancos_map: dict = {}
    for b in db.fetch_all(
        "SELECT no_banco, COALESCE(nombre, '') AS nombre FROM scintela.banco"
    ) or []:
        n = (b.get("nombre") or "").upper().strip()
        if "PICHINC" in n:
            bancos_map.setdefault("PICHINCHA", int(b["no_banco"]))
        if "INTER" in n:
            bancos_map.setdefault("INTERNACIONAL", int(b["no_banco"]))

    parsed = concepto_parser.parse_concepto(
        concepto or "",
        {"provs_validos": provs_validos, "bancos": bancos_map},
    )

    # Toda la operación (caja + side effect) en una sola tx — si el
    # side effect falla, no queda la caja huérfana.
    side_effect_result = None
    with db.tx() as conn:
        # 0) Advisory lock — serializa inserts concurrentes contra esta
        # caja. Sin esto, dos crear() en paralelo computan el mismo
        # saldo_prev y corrompen el running.
        db.execute(
            "SELECT pg_advisory_xact_lock(hashtext('scintela.caja.running'))",
            (), conn=conn,
        )
        # 1) Computar saldo_prev DENTRO de la tx + del lock.
        sp_row = db.fetch_one(
            """
            SELECT COALESCE(saldo, 0) AS s
              FROM scintela.caja
             ORDER BY id_caja DESC
             LIMIT 1
            """,
            (), conn=conn,
        ) or {}
        saldo_prev = float(sp_row.get("s") or 0)
        saldo_nuevo = saldo_prev + importe_firmado
        if saldo_nuevo < -0.01:
            raise ValueError(
                f"El movimiento dejaría la caja en negativo ({saldo_nuevo:.2f})."
            )
        # 2) INSERT en caja con saldo running calculado.
        row = db.execute_returning(
            """
            INSERT INTO scintela.caja
                (fecha, tipo, importe, concepto, saldo, clave, id_cheque, usuario_crea)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id_caja
            """,
            (
                fecha, tipo, importe, (concepto or "")[:80],
                saldo_nuevo, (clave or None) and clave[:3],
                id_cheque, usuario,
            ),
            conn=conn,
        ) or {}

        # 2) Side effect: si caja egresa (S), la plata entra al destino.
        #    Si caja ingresa (E), la plata sale del destino.
        origen = ("caja_egreso" if tipo == "S"
                  else "caja_ingreso" if tipo == "E"
                  else "caja_egreso")  # CB lo trato como egreso a banco
        try:
            side_effect_result = _se.aplicar_side_effect(
                parsed=parsed,
                importe=importe,
                fecha=fecha,
                origen=origen,
                usuario=usuario,
                conn=conn,
            )
        except Exception as e:
            # Re-raise para que la tx haga rollback de TODO (caja + lo que sea
            # que falló del side effect). Esto evita "caja bajó pero banco no
            # subió" — el bug original que TMT reportó.
            raise ValueError(
                f"Side effect del concepto {concepto!r} falló: {e}. "
                "El movimiento de caja NO se aplicó."
            ) from e

        # 3) Registrar en mov_doble (historial unificado, Fase H/I 2026-05-12).
        # TMT 2026-05-12 follow-up: SIEMPRE registrar — con o sin side effect.
        # Los simples apuntan a sí mismos (caja→caja) y se tipan
        # "caja_simple_<tipo>" para que aparezcan en el historial general.
        id_mov_doble = None
        if row.get("id_caja"):
            import mov_doble as _md
            if side_effect_result:
                destino_table, destino_id = _destino_table_id(side_effect_result)
                tipo_md = f"caja_{tipo.lower()}_to_{side_effect_result.get('tipo')}"
            else:
                destino_table, destino_id = "caja", row["id_caja"]
                tipo_md = f"caja_{tipo.lower()}_simple"
            if destino_table and destino_id:
                id_mov_doble = _md.registrar(
                    conn=conn,
                    tipo=tipo_md,
                    origen_table="caja",
                    origen_id=row["id_caja"],
                    destino_table=destino_table,
                    destino_id=destino_id,
                    importe=importe,
                    fecha=fecha,
                    concepto=concepto or "",
                    usuario=usuario,
                    metadata={"parsed_tipo": parsed.get("tipo"),
                              "tipo_caja": tipo,
                              "tiene_side_effect": bool(side_effect_result)},
                )

        # 4) TMT 2026-05-19 v4 audit — clasificación atómica como gasto
        # V1..V9 si se pidió. DENTRO de la misma tx: si la clasif falla,
        # rollback total (caja + side_effect + xgast). Antes esto se hacía
        # en una tx separada en views.py y dejaba caja huérfana on failure.
        clasif_result = None
        if (xgast_num and tipo == "S" and row.get("id_caja")
                and 1 <= int(xgast_num) <= 9):
            # Defensa: la lógica del helper rechaza la clasif si la caja
            # ya tiene un side-effect bancario activo (sería doble conta-
            # bilización). Lo dejamos burbujear para que la tx haga rollback.
            from modules.gastos import queries as _gq
            clasif_result = _gq._clasificar_caja_dentro_tx(
                conn=conn,
                id_caja=int(row["id_caja"]),
                num=int(xgast_num),
                usuario=usuario,
                atomico_caja_xgast=True,
            )

    return {
        "id_caja": row.get("id_caja"),
        "saldo_nuevo": saldo_nuevo,
        "side_effect": side_effect_result,
        "id_mov_doble": id_mov_doble,
        "clasif_gasto": clasif_result,
    }


def _destino_table_id(side_effect: dict) -> tuple[str | None, int | None]:
    """Mapeo side_effect dict → (tabla_destino, id_destino) para mov_doble.

    Centralizado acá porque los reverses lo van a usar también.
    """
    t = side_effect.get("tipo")
    if t == "transfer_banco":
        return "transacciones_bancarias", side_effect.get("id_transaccion")
    if t == "retiro_socio":
        return "retiros", side_effect.get("id_retiro")
    if t == "dolares":
        return "dolares", side_effect.get("id_dolares")
    if t == "compra_proveedor":
        return "compra", side_effect.get("id_compra")
    if t == "gasto":
        return "xgast", side_effect.get("id_xgast")
    return None, None


# OLD: el INSERT plano se mantuvo abajo por compatibilidad con cualquier
# código que pueda haberlo llamado directamente. Lo bloqueamos para
# detectar callers viejos durante este refactor.
def _crear_legacy_solo_caja_no_usar(
    *,
    fecha: date, tipo: str, importe, concepto: str,
    clave: str | None = None, id_cheque: int | None = None,
    usuario: str = "web",
) -> dict:
    """DEPRECATED — solo dejé el shell por si algo lo llama. La versión
    correcta es `crear` arriba, con side effects automáticos. Si ves esta
    función llamada en otro lado, eso es un bug."""
    raise RuntimeError(
        "_crear_legacy_solo_caja_no_usar fue llamada — debe ser crear()"
    )
    # Código original (referencia, no se ejecuta):
    return db.execute_returning(
        """
        INSERT INTO scintela.caja
            (fecha, tipo, importe, concepto, saldo, clave, id_cheque, usuario_crea)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id_caja
        """,
        (
            fecha, tipo, importe, (concepto or "")[:80],
            None, (clave or None) and clave[:3],
            id_cheque, usuario,
        ),
    ) or {}


def resumen() -> dict:
    """Totales de ingresos/egresos + último saldo registrado.

    Notas:
    - En `scintela.caja` el campo `importe` está en VALOR ABSOLUTO; el
      signo viene del campo `tipo` ('E' = entrada/+, 'S' = salida/-).
      Por eso `ingresos = SUM(importe) WHERE tipo='E'` y
      `egresos = SUM(importe) WHERE tipo='S'`. Sumar plano `SUM(importe)`
      es BUG — da un número sin sentido.
    - `saldo_ultimo` es el running balance de la última fila (= dBase
      "saldo en caja").
    - `saldo_derivado` reconstruye desde 0: `Σ(E) − Σ(S)`. Si el log
      arrancó con un opening balance (caja existente antes del primer
      movimiento), `saldo_derivado` será MENOR que `saldo_ultimo`. La
      diferencia es el opening — NO es bug, es por construcción. Por eso
      la UI muestra ambos sin alarma cuando hay opening, y SÓLO alerta
      cuando hay un drift sin explicación (ej. fila editada sin
      recalcular saldo).
    - `opening_estimado` = primer saldo running − signo×primer importe.
      Si saldo_ultimo ≈ opening_estimado + saldo_derivado, todo cuadra.
    """
    row = db.fetch_one(
        """
        SELECT
            COALESCE(SUM(CASE WHEN tipo = 'E' THEN importe ELSE 0 END), 0) AS ingresos,
            COALESCE(SUM(CASE WHEN tipo = 'S' THEN importe ELSE 0 END), 0) AS egresos,
            COALESCE(SUM(CASE WHEN tipo = 'E' THEN importe
                              WHEN tipo = 'S' THEN -importe
                              ELSE importe END), 0) AS saldo_derivado,
            (SELECT saldo FROM scintela.caja
                WHERE saldo IS NOT NULL
                ORDER BY fecha DESC, id_caja DESC LIMIT 1) AS saldo_ultimo,
            (SELECT saldo - CASE WHEN tipo='E' THEN importe
                                 WHEN tipo='S' THEN -importe
                                 ELSE importe END
                FROM scintela.caja
                WHERE saldo IS NOT NULL
                ORDER BY fecha ASC, id_caja ASC LIMIT 1) AS opening,
            MAX(fecha) AS ultima_fecha,
            COUNT(*) AS n_movimientos
        FROM scintela.caja
        """,
    )
    return row or {}


def conceptos_frecuentes(limite: int = 50) -> list[dict]:
    """Top conceptos usados históricamente en `scintela.caja`, agrupados.

    Para el autocomplete del form "Nuevo movimiento de caja" — la usuaria
    pidió poder seleccionar de una lista en vez de tipear de cero.
    El legacy dBase usa CONCEPTO como código estructurado (PROV de 2 letras,
    PICH/INTER/RR/INHB/IN.XX). Esta query trae los más usados, ordenados por
    frecuencia, para que aparezcan primero.
    """
    return db.fetch_all(
        """
        SELECT TRIM(concepto) AS concepto,
               COUNT(*)        AS usos
          FROM scintela.caja
         WHERE COALESCE(concepto, '') <> ''
         GROUP BY TRIM(concepto)
         ORDER BY usos DESC, concepto
         LIMIT %s
        """,
        (limite,),
    ) or []


def por_id(id_caja: int) -> dict | None:
    return db.fetch_one(
        """
        SELECT id_caja, fecha, tipo, importe, concepto, saldo,
               clave, id_cheque, usuario_crea
          FROM scintela.caja
         WHERE id_caja = %s
        """,
        (id_caja,),
    )


def reversar(id_caja: int, motivo: str = "", usuario: str = "web") -> dict:
    """Crea un movimiento opuesto que compensa al id_caja indicado.

    No borra la fila original — conserva audit trail. Inserta una nueva
    con tipo invertido (E↔S), mismo importe, concepto "REVERSO id N — <motivo>".
    El trigger del saldo auto-actualiza.

    Si la fila original ya fue reversada (existe otra con concepto
    "REVERSO id N"), levanta error para evitar doble-reverso.

    Side effects (TMT 2026-05-12 Fase B): si el movimiento original tenía
    un side effect detectable por el concepto (PICH/INTER/RR/IN.XX/PR...),
    el side effect también se reversa en la misma transacción. Esto
    arregla el bug donde "reversar una caja a PICH" dejaba el banco subido.
    """
    orig = por_id(id_caja)
    if not orig:
        raise ValueError(f"Movimiento de caja id={id_caja} no existe.")

    # TMT 2026-06-03 audit fix: usar zona EC en lugar de date.today() (UTC).
    # Tarde noche EC, UTC ya está en el día siguiente → reverso queda con
    # fecha futura. Igual criterio que posdat._hoy_ec().
    from datetime import datetime as _dt_, timedelta as _td_
    _hoy_ec = (_dt_.utcnow() - _td_(hours=5)).date()
    asegurar_fecha_abierta(_hoy_ec)

    tipo_orig = (orig.get("tipo") or "").strip().upper()
    if tipo_orig not in ("E", "S", "CB"):
        raise ValueError(f"Tipo inválido en el movimiento original: {tipo_orig!r}.")
    tipo_nuevo = "S" if tipo_orig == "E" else "E"  # CB → E (tratamos como entrada inversa)

    # #30 (TMT 2026-05-14): detectar doble reverso via mov_doble.estado,
    # no por LIKE en el concepto. El LIKE viejo era frágil (un concepto
    # editado o un patrón diferente lo evadía) y además matcheaba mal el
    # prefijo: "REVERSO id 12%" matchea tanto id=12 como id=120, 121, etc.
    # — falso positivo. mov_doble lleva el estado canónico.
    ya_reversado = db.fetch_one(
        """
        SELECT id_mov_doble FROM scintela.mov_doble
         WHERE origen_table = 'caja'
           AND origen_id    = %s
           AND estado       = 'reversado'
         LIMIT 1
        """,
        (id_caja,),
    )
    if ya_reversado:
        raise ValueError(
            f"El movimiento de caja id={id_caja} ya fue reversado "
            f"(mov_doble.estado='reversado'). Revisá /historial para "
            f"ver el reverso linkeado."
        )

    importe = abs(float(orig.get("importe") or 0))
    if importe == 0:
        raise ValueError("Importe original = 0, no hay nada que reversar.")

    concepto_orig = orig.get("concepto") or ""
    concepto_nuevo = f"REVERSO id {id_caja} — {(motivo or 'sin motivo')[:40]}"
    fecha_rev = _hoy_ec  # TMT 2026-06-03: zona EC, no UTC.
    # Chequear que el saldo nuevo no caiga negativo si es un egreso
    importe_firmado = importe if tipo_nuevo == "E" else -importe
    saldo_prev = saldo_actual()
    if saldo_prev + importe_firmado < -0.01:
        raise ValueError(
            f"El reverso dejaría caja en negativo ({saldo_prev + importe_firmado:.2f})."
        )

    # Parse del concepto original para detectar side effects a deshacer.
    import concepto_parser
    import side_effects as _se
    provs_validos = {
        (r.get("codigo_prov") or "").strip().upper()
        for r in (db.fetch_all(
            "SELECT codigo_prov FROM scintela.proveedor"
        ) or [])
    }
    bancos_map: dict = {}
    for b in db.fetch_all(
        "SELECT no_banco, COALESCE(nombre, '') AS nombre FROM scintela.banco"
    ) or []:
        n = (b.get("nombre") or "").upper().strip()
        if "PICHINC" in n:
            bancos_map.setdefault("PICHINCHA", int(b["no_banco"]))
        if "INTER" in n:
            bancos_map.setdefault("INTERNACIONAL", int(b["no_banco"]))
    parsed = concepto_parser.parse_concepto(
        concepto_orig,
        {"provs_validos": provs_validos, "bancos": bancos_map},
    )

    side_effect_rev = None
    id_mov_doble_rev = None
    id_cheque_link = orig.get("id_cheque")  # Por si era 'CB'
    with db.tx() as conn:
        # 1) INSERT caja del reverso usando caja_helpers para que el
        # saldo running se compute en Python (sino quedaba NULL).
        # TMT 2026-05-13.
        import caja_helpers
        row = caja_helpers.insert_movimiento_caja(
            conn,
            fecha=fecha_rev,
            tipo=tipo_nuevo,
            importe=float(importe),
            concepto=concepto_nuevo[:80],
            clave=(usuario or "")[:3],
            usuario=usuario,
        )

        # 1b) Si era CB (cobro con cheque), agregar nota al cheque para
        # que la dueña sepa que se reversó el cobro. NO cambiamos stat
        # porque no sabemos el stat previo al cobro — eso queda a su
        # criterio. TMT 2026-05-13.
        if tipo_orig == "CB" and id_cheque_link:
            try:
                db.execute(
                    """
                    UPDATE scintela.cheque
                       SET observacion = RIGHT(
                             COALESCE(observacion || ' | ', '') ||
                             '[REVERSO cobro caja id ' || %s || ' — ' || %s ||
                             ' — chequeá stat]', 500),
                           usuario_modifica = %s,
                           fecha_modifica = CURRENT_TIMESTAMP
                     WHERE id_cheque = %s
                    """,
                    (id_caja, (motivo or "sin motivo")[:40],
                     usuario[:50], id_cheque_link),
                    conn=conn,
                )
            except Exception:
                pass  # observación legacy — no bloquear

        # 2) Si el original tenía side effect, deshacerlo (inverso=True).
        # Para compra_proveedor, además pasamos el id_compra original
        # (vía mov_doble.destino_id) para que se anule en vez de insertar
        # una compensación negativa — UX más limpia. TMT 2026-05-13.
        import mov_doble as _md
        if parsed.get("tipo") not in (None, "none"):
            origen_orig = ("caja_egreso" if tipo_orig == "S"
                           else "caja_ingreso" if tipo_orig == "E"
                           else "caja_egreso")
            # Buscar id_compra original si es compra_proveedor.
            id_destino_original = None
            if parsed.get("tipo") == "compra_proveedor":
                md_orig_compra = _md.buscar_por_origen(
                    origen_table="caja", origen_id=id_caja, conn=conn,
                )
                if md_orig_compra and md_orig_compra.get("destino_table") == "compra":
                    id_destino_original = md_orig_compra.get("destino_id")
            try:
                side_effect_rev = _se.aplicar_side_effect(
                    parsed=parsed,
                    importe=importe,
                    fecha=fecha_rev,
                    origen=origen_orig,
                    usuario=usuario,
                    conn=conn,
                    inverso=True,
                    id_destino_original=id_destino_original,
                )
            except Exception as e:
                raise ValueError(
                    f"No pude deshacer el side effect "
                    f"({parsed.get('tipo')}) del concepto original: {e}. "
                    "El reverso de caja NO se aplicó."
                ) from e

        # 3) Registrar SIEMPRE el reverso en el historial — con o sin side
        #    effect. Si hubo side effect, apunta a la fila secundaria; si
        #    no, apunta al propio movimiento original (caja → caja). Así
        #    todos los reversos quedan en /historial (TMT 2026-05-12 follow-up).
        if row.get("id_caja"):
            orig_md = _md.buscar_por_origen(
                origen_table="caja", origen_id=id_caja, conn=conn,
            )
            if side_effect_rev:
                # Reverso con side effect → enlaza a la tabla del side effect.
                destino_table, destino_id = _destino_table_id(side_effect_rev)
                tipo_md = (f"reverso_caja_{tipo_orig.lower()}_"
                           f"to_{side_effect_rev.get('tipo')}")
            else:
                # Reverso simple → apunta al movimiento original mismo.
                destino_table, destino_id = "caja", id_caja
                tipo_md = "reverso_caja_simple"

            if destino_table and destino_id:
                id_mov_doble_rev = _md.registrar(
                    conn=conn,
                    tipo=tipo_md,
                    origen_table="caja",
                    origen_id=row["id_caja"],
                    destino_table=destino_table,
                    destino_id=destino_id,
                    importe=importe,
                    fecha=fecha_rev,
                    concepto=concepto_nuevo,
                    usuario=usuario,
                    id_original=(orig_md or {}).get("id_mov_doble"),
                    metadata={"id_caja_original": id_caja,
                              "motivo": motivo,
                              "tenia_side_effect": bool(side_effect_rev)},
                )

    return {
        "id_caja_nuevo": row.get("id_caja"),
        "id_caja_original": id_caja,
        "tipo_nuevo": tipo_nuevo,
        "importe": importe,
        "side_effect_reversado": side_effect_rev,
        "id_mov_doble_reverso": id_mov_doble_rev,
    }
