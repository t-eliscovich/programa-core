"""Consultas de bancos / movimientos.

Siguiendo la regla de la skill: el saldo acumulado en transacciones_bancarias
está históricamente desconfiable. Mostramos el saldo stored y entre paréntesis
el saldo derivado (suma corrida). Durante la migración sirve como sanity check.
"""
from datetime import date as _date

import db
from periodo_guard import asegurar_fecha_abierta


def lista_bancos() -> list[dict]:
    """Saldos por banco con opening balance.

    - `saldo_stored`: running balance de la última fila (= dBase legacy).
    - `saldo_derivado`: Σ entradas − Σ salidas desde 0 (no incluye opening).
    - `opening`: saldo de la PRIMERA fila menos su movimiento firmado.
      Si la chequera arrancó con un saldo previo (caja existente antes de
      la primera transacción cargada), opening > 0.

    Invariante: saldo_stored == opening + saldo_derivado. Si hay drift
    real (fila editada sin recomputar las posteriores) el template
    muestra el warning.
    """
    # Convención de documentos (TMT 2026-05-12): 'TR' (transferencia recibida)
    # se agregó cuando armamos /bancos/transferir — antes este CASE lo
    # ignoraba (caía en ELSE 0) y daba un drift falso entre saldo_stored y
    # saldo_derivado por cada transferencia banco→banco.
    return db.fetch_all(
        """
        SELECT b.no_banco, b.nombre,
               COALESCE((
                 SELECT t.saldo FROM scintela.transacciones_bancarias t
                 WHERE t.no_banco = b.no_banco
                 ORDER BY t.fecha DESC, t.id_transaccion DESC
                 LIMIT 1
               ), 0) AS saldo_stored,
               COALESCE((
                 SELECT SUM(CASE WHEN documento IN ('DE','AC','NC','TR') THEN importe
                                 WHEN documento IN ('CH','ND','DB')      THEN -importe
                                 ELSE 0 END)
                 FROM scintela.transacciones_bancarias
                 WHERE no_banco = b.no_banco
               ), 0) AS saldo_derivado,
               COALESCE((
                 SELECT t.saldo - CASE
                          WHEN t.documento IN ('DE','AC','NC','TR') THEN t.importe
                          WHEN t.documento IN ('CH','ND','DB')      THEN -t.importe
                          ELSE 0
                        END
                 FROM scintela.transacciones_bancarias t
                 WHERE t.no_banco = b.no_banco AND t.saldo IS NOT NULL
                 ORDER BY t.fecha ASC, t.id_transaccion ASC
                 LIMIT 1
               ), 0) AS opening
        FROM scintela.banco b
        ORDER BY b.no_banco
        """
    )


def bancos_operativos() -> list[dict]:
    """Subset de scintela.banco que sí se usa en operaciones diarias.

    Filtra el ruido del legacy dBase: muchos códigos de banco son meros
    rubros contables (DEP.PICH, CANCELA ANTICIPO, UKN, EFECTIVO, etc.) que
    no son cuentas bancarias reales. Esta función devuelve sólo las que
    tienen sentido para transferir / depositar / etc.

    Criterios (en orden):
      1. Match por nombre — los que la usuaria llama "Pichincha" o "Internacional".
      2. Tienen saldo distinto de cero, O movimientos en los últimos 6 meses.

    Si querés ver TODOS los bancos (incluyendo legacy/contables), usá `lista_bancos()`.
    """
    return db.fetch_all(
        """
        SELECT b.no_banco, b.nombre,
               COALESCE((
                 SELECT t.saldo FROM scintela.transacciones_bancarias t
                 WHERE t.no_banco = b.no_banco
                 ORDER BY t.fecha DESC, t.id_transaccion DESC
                 LIMIT 1
               ), 0) AS saldo
          FROM scintela.banco b
         WHERE
             -- Match por nombre: estos son los que la usuaria opera.
             -- Nota: los signos porcentaje viven duplicados aca abajo
             -- porque psycopg2 los procesa como placeholders incluso
             -- dentro de comentarios SQL. Ver docstring de la function.
             UPPER(COALESCE(b.nombre,'')) LIKE '%%PICHINC%%'
             OR UPPER(COALESCE(b.nombre,'')) = 'INTERNACI'
             OR UPPER(COALESCE(b.nombre,'')) = 'INTERNACIONAL'
             OR (
                 -- O bien tienen movimientos en los últimos 6 meses
                 -- (excluyendo bancos contables que tienen "DEP." o "ANTIC" en el nombre)
                 UPPER(COALESCE(b.nombre,'')) NOT LIKE 'DEP%%'
                 AND UPPER(COALESCE(b.nombre,'')) NOT LIKE '%%ANTIC%%'
                 AND UPPER(COALESCE(b.nombre,'')) NOT IN ('UKN', 'EFECTIVO', 'CANCELA ANTICIPO')
                 AND EXISTS (
                     SELECT 1 FROM scintela.transacciones_bancarias t
                      WHERE t.no_banco = b.no_banco
                        AND t.fecha >= CURRENT_DATE - INTERVAL '6 months'
                 )
             )
         ORDER BY b.no_banco
        """
    ) or []


def movimientos(
    no_banco: int,
    desde: str | None = None,
    hasta: str | None = None,
    limite: int = 500,
) -> list[dict]:
    """Lista movimientos del banco. Incluye linkage de reversos via
    mov_doble: si la fila tiene un mov_doble asociado (sea como origen o
    destino), trae estado ('activo'|'reversado'|'reverso'), usuario que
    hizo la operación, concepto largo del mov, y los punteros id_original
    (si esta fila es un reverso) / id_reverso (si fue reversada). El
    template usa esto para mostrar quién hizo la op y un badge claro de
    "reverso de #N" o "reversado por #N". TMT 2026-05-13.
    """
    rows = db.fetch_all(
        """
        SELECT
            t.id_transaccion, t.fecha, t.documento, t.concepto, t.fechad,
            t.importe, t.saldo, t.stat, t.no_banco, t.no_cta, t.prov,
            t.numreferencia, t.usuario_crea,
            md.id_mov_doble        AS mov_doble_id,
            md.estado              AS mov_estado,
            md.usuario             AS mov_usuario,
            md.concepto            AS mov_concepto,
            md.id_original         AS mov_id_original,
            md.id_reverso          AS mov_id_reverso
        FROM scintela.transacciones_bancarias t
        LEFT JOIN scintela.mov_doble md
               ON (md.origen_table  = 'transacciones_bancarias'
                   AND md.origen_id  = t.id_transaccion)
               OR (md.destino_table = 'transacciones_bancarias'
                   AND md.destino_id = t.id_transaccion)
        WHERE t.no_banco = %(no_banco)s
          AND (%(desde)s::date IS NULL OR t.fecha >= %(desde)s::date)
          AND (%(hasta)s::date IS NULL OR t.fecha <= %(hasta)s::date)
        ORDER BY t.fecha DESC, t.id_transaccion DESC
        LIMIT %(limite)s
        """,
        {"no_banco": no_banco, "desde": desde or None, "hasta": hasta or None, "limite": limite},
    )
    # Enriquecer con info de conciliación bancaria (defensivo: si la tabla
    # no existe o falla la query, seguimos con rows sin flag).
    try:
        ids = [r["id_transaccion"] for r in rows if r.get("id_transaccion")]
        if ids:
            conc = db.fetch_all(
                """
                SELECT id_transaccion, id AS conciliacion_id, creado_en, usuario,
                       real_fecha, real_documento, estado
                  FROM scintela.banco_conciliacion_match
                 WHERE id_transaccion = ANY(%s)
                   AND (deshecho_en IS NULL OR deshecho_en IS NULL)
                """,
                (ids,),
            ) or []
            conc_by_id = {c["id_transaccion"]: c for c in conc}
            for r in rows:
                c = conc_by_id.get(r.get("id_transaccion"))
                if c:
                    r["conciliacion_id"] = c.get("conciliacion_id")
                    r["conciliado_en"] = c.get("creado_en")
                    r["conciliado_por"] = c.get("usuario")
                    r["conciliado_real_fecha"] = c.get("real_fecha")
                    r["conciliado_real_doc"] = c.get("real_documento")
                    r["conciliado_estado"] = c.get("estado")
    except Exception:
        pass  # fail-graceful: la vista funciona sin el badge si la tabla no está
    return rows


def banco_info(no_banco: int) -> dict | None:
    return db.fetch_one(
        "SELECT no_banco, nombre FROM scintela.banco WHERE no_banco = %s",
        (no_banco,),
    )


# =====================================================================
# Emisión de cheque propio (chequera) — replica BANCOS.PRG::CHEQUERA
# =====================================================================
# El legacy disparaba cascadas mágicas según proveedor + concepto. En el
# nuevo app pedimos el destino EXPLÍCITO al usuario y aplicamos el side-effect
# correspondiente. Tipos válidos:
#
#   - "proveedor": pagás una posdat existente al proveedor X. La posdat
#     se marca pagada (banc=no_banco). Si no hay posdat o querés generar
#     una compra nueva, usá `compras.nueva` y luego volvé a esto.
#   - "retiro": el dueño retira plata. INSERT en `retiros` con doc='CH'.
#   - "caja": transferís plata del banco a la caja física. INSERT en `caja`
#     con tipo='E' (entrada de caja).
#   - "gasto": pagás un gasto general (luz, contadora, etc). INSERT en
#     `xgast` con saldo=0 (ya pagado).
#   - "otro": sólo se registra el movimiento bancario, sin side-effect en
#     otra tabla. Útil para casos atípicos (impuestos, transferencias entre
#     cuentas propias).
#
# TODOS comparten: INSERT en transacciones_bancarias con documento='CH'.

TIPOS_CHEQUE_EMITIDO = ("proveedor", "retiro", "caja", "gasto", "anticipo_usd", "otro")


def emitir_cheque(
    *,
    tipo: str,
    no_banco: int,
    importe,
    fecha,
    no_cheque: str = "",
    beneficiario: str = "",
    concepto: str = "",
    # Específico por tipo:
    id_posdat: int | None = None,        # tipo='proveedor': cierra esta posdat
    de_socio: str | None = None,         # tipo='retiro': código de socio (ej "TM")
    es_postdatado: bool = False,         # tipo='proveedor' o 'gasto': dejarlo en posdat futuro
    fechad=None,                         # fecha de cobro si postdatado
    usuario: str = "web",
    xgast_num: int | None = None,        # TMT 2026-05-19 v4 audit: categoría V1..V9
                                          # cuando tipo='gasto'. Sin esto el xgast quedaba
                                          # con num=NULL → invisible en /informes/gastos.
) -> dict:
    """Emite un cheque propio en el banco `no_banco`.

    Devuelve `{id_transaccion, side_effect: <descripción>}`.

    Lanza ValueError si los datos son inválidos para el tipo elegido.
    """
    if tipo not in TIPOS_CHEQUE_EMITIDO:
        raise ValueError(f"Tipo inválido: {tipo!r}. Usá: {', '.join(TIPOS_CHEQUE_EMITIDO)}")
    if not no_banco:
        raise ValueError("Banco origen requerido.")
    importe_f = float(importe or 0)
    if importe_f <= 0:
        raise ValueError("Importe debe ser mayor a cero.")
    asegurar_fecha_abierta(fecha)

    banco_row = db.fetch_one(
        "SELECT no_banco, COALESCE(nombre, '') AS nombre FROM scintela.banco WHERE no_banco = %s",
        (no_banco,),
    )
    if not banco_row:
        raise ValueError(f"Banco no_banco={no_banco} no existe.")

    side_effect = "ninguno"
    extras: dict = {}

    with db.tx() as conn, conn.cursor() as cur:
        # 1) Registro común — usar bank_helpers para que compute saldo
        # consistentemente (mismo path que transferir / reversar). El raw
        # INSERT antiguo dependía del trigger y dejaba saldo=0 cuando éste
        # no estaba aplicado en la DB. TMT 2026-05-13.
        import bank_helpers
        bh_row = bank_helpers.insert_movimiento_bancario(
            conn,
            no_banco=no_banco,
            no_cta=None,
            fecha=fecha,
            documento="CH",
            importe=importe_f,  # bank_helpers espera ABS, el signo lo aplica internamente
            concepto=(concepto or beneficiario or f"Cheque {no_cheque}").strip()[:50],
            prov=(beneficiario or "")[:5].upper() if beneficiario else None,
            numreferencia=(int(no_cheque) if (no_cheque or "").strip().isdigit() else None),
            usuario=usuario[:50],
            fechad=(fechad if (es_postdatado and fechad) else None),
            stat="A",
        )
        id_transaccion = bh_row["id_transaccion"]

        # 2) Side effect específico por tipo
        if tipo == "proveedor":
            if id_posdat:
                # Cerrar la posdat: banc = no_banco (la pagamos con este banco)
                cur.execute(
                    """
                    UPDATE scintela.posdat
                       SET banc = %s,
                           fecha_modifica = CURRENT_TIMESTAMP,
                           usuario_modifica = %s
                     WHERE id_posdat = %s
                    """,
                    (no_banco, usuario[:50], id_posdat),
                )
                side_effect = f"Posdat #{id_posdat} cerrada (banc={no_banco})"
                extras["id_posdat"] = id_posdat
            else:
                side_effect = "Sin posdat asociada — sólo movimiento bancario"

        elif tipo == "retiro":
            cur.execute(
                """
                INSERT INTO scintela.retiros
                    (fecha, nb, ret, de, concepto, clave, usuario_crea, id_transaccion_bancaria)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id_retiro
                """,
                (
                    fecha, no_banco, importe_f, (de_socio or "")[:5],
                    (concepto or "")[:100], (de_socio or "")[:5],
                    usuario[:50], id_transaccion,
                ),
            )
            ret = cur.fetchone()
            id_retiro = ret[0] if isinstance(ret, list | tuple) else (ret.get("id_retiro") if ret else None)
            side_effect = f"Retiro #{id_retiro} registrado"
            extras["id_retiro"] = id_retiro

        elif tipo == "caja":
            # Usar caja_helpers para que compute saldo running. El raw
            # INSERT anterior dejaba saldo NULL. TMT 2026-05-13.
            import caja_helpers
            ch_row = caja_helpers.insert_movimiento_caja(
                conn,
                fecha=fecha,
                tipo="E",
                importe=importe_f,
                concepto=(concepto or "Transferencia banco→caja")[:100],
                clave="BCO",
                usuario=usuario[:50],
            )
            id_caja = ch_row["id_caja"]
            side_effect = f"Caja #{id_caja} (entrada de banco)"
            extras["id_caja"] = id_caja

        elif tipo == "gasto":
            # Crear el gasto YA PAGADO (saldo=0) o pendiente si postdatado.
            # TMT 2026-05-19 v4 audit — incluir `num` para que aparezca en
            # /informes/gastos V1..V9. Antes el num quedaba NULL → fila
            # invisible en el matriz (bug clase $220K).
            saldo_xgast = importe_f if es_postdatado else 0.0
            stat_xgast = "P" if es_postdatado else "C"  # P=pendiente, C=cancelado
            num_xgast = None
            if xgast_num is not None:
                try:
                    n = int(xgast_num)
                    if 1 <= n <= 9:
                        num_xgast = n
                except (TypeError, ValueError):
                    num_xgast = None
            # Fallback: si no vino xgast_num explícito, intentar inferir
            # del concepto vía el matcher de gastos. Si tampoco matchea,
            # queda NULL (legacy — visible solo en /gastos).
            if num_xgast is None and concepto:
                try:
                    from modules.gastos.queries import sugerir_categoria as _sug
                    num_xgast = _sug(concepto)
                except Exception:
                    num_xgast = None
            cur.execute(
                """
                INSERT INTO scintela.xgast
                    (fecha, doc, prov, concepto, importe, saldo, stat,
                     fechad, clave, usuario_crea, num)
                VALUES (%s, 'CH', %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id_xgast
                """,
                (
                    fecha,
                    (beneficiario or "")[:5].upper() if beneficiario else None,
                    (concepto or "Gasto pagado con cheque")[:100],
                    importe_f, saldo_xgast, stat_xgast,
                    fechad if es_postdatado else fecha,
                    (beneficiario or "")[:3] if beneficiario else None,
                    usuario[:50],
                    num_xgast,
                ),
            )
            gx = cur.fetchone()
            id_xgast = gx[0] if isinstance(gx, list | tuple) else (gx.get("id_xgast") if gx else None)
            side_effect = (
                f"Gasto #{id_xgast} registrado"
                + (" (pendiente — posdatado)" if es_postdatado else " (pagado)")
                + (f" V{num_xgast}" if num_xgast else " (sin categoría — clasificar después en /gastos)")
            )
            extras["id_xgast"] = id_xgast
            extras["num_xgast"] = num_xgast

        elif tipo == "anticipo_usd":
            # TMT 2026-05-17: paridad dBase BANCOS.PRG > CHEQUERA con concepto
            # `IN.<CT>` o `IN <CT>` — emite un cheque a un proveedor en cuenta
            # dólares. La fila en `scintela.dolares` representa el anticipo
            # entregado (cuando llegue la factura del proveedor, se aplica vía
            # BAP). `beneficiario` es el código de cuenta USD (2 letras, ej. MP).
            cta_usd = (beneficiario or "")[:5].upper()
            if not cta_usd:
                raise ValueError(
                    "Anticipo USD requiere código de cuenta dólares (2 letras, "
                    "ej. MP). Usá el campo beneficiario o tipeá IN.<CT> en concepto."
                )
            cur.execute(
                """
                INSERT INTO scintela.dolares
                    (fecha, cta, importe, concepto, usuario_crea)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id_dolares
                """,
                (
                    fecha, cta_usd, importe_f,
                    (concepto or f"Anticipo USD cta {cta_usd}")[:50],
                    usuario[:50],
                ),
            )
            dr = cur.fetchone()
            id_dolares = dr[0] if isinstance(dr, list | tuple) else (dr.get("id_dolares") if dr else None)
            side_effect = f"Anticipo USD #{id_dolares} (cuenta {cta_usd})"
            extras["id_dolares"] = id_dolares

        # tipo == "otro" → no side-effect (el INSERT en transacciones_bancarias ya alcanza)

        # Registrar movimiento doble si tuvo side effect (TMT 2026-05-12).
        id_mov_doble = None
        if id_transaccion:
            destino_table, destino_id = None, None
            if tipo == "proveedor" and extras.get("id_posdat"):
                destino_table, destino_id = "posdat", extras["id_posdat"]
            elif tipo == "retiro" and extras.get("id_retiro"):
                destino_table, destino_id = "retiros", extras["id_retiro"]
            elif tipo == "caja" and extras.get("id_caja"):
                destino_table, destino_id = "caja", extras["id_caja"]
            elif tipo == "gasto" and extras.get("id_xgast"):
                destino_table, destino_id = "xgast", extras["id_xgast"]
            elif tipo == "anticipo_usd" and extras.get("id_dolares"):
                destino_table, destino_id = "dolares", extras["id_dolares"]
            if destino_table:
                import mov_doble as _md
                id_mov_doble = _md.registrar(
                    conn=conn,
                    tipo=f"cheque_emitido_{tipo}",
                    origen_table="transacciones_bancarias",
                    origen_id=id_transaccion,
                    destino_table=destino_table,
                    destino_id=destino_id,
                    importe=importe_f,
                    fecha=fecha,
                    concepto=(concepto or beneficiario or "")[:200],
                    usuario=usuario,
                    metadata={"no_banco": no_banco,
                              "no_cheque": no_cheque,
                              "beneficiario": beneficiario},
                )

    return {
        "id_transaccion":  id_transaccion,
        "no_banco":        no_banco,
        "banco_nombre":    banco_row.get("nombre") or "",
        "tipo":            tipo,
        "importe":         importe_f,
        "side_effect":     side_effect,
        "id_mov_doble":    id_mov_doble,
        **extras,
    }


def posdat_abiertas_de(prov: str | None = None) -> list[dict]:
    """Posdats abiertas para el wizard de emitir cheque.

    Filtros:
      - `banc = 0`: deuda viva SIN cheque emitido. Excluye banc=9 (legacy
        ya con cheque), banc=10/32 (modernos con cheque PC) — sino el
        wizard ofrecería posdats ya pagadas, abriendo doble-pago (bug #R2
        audit 2026-05-14).
      - `anulada IS NOT TRUE`: soft-delete (migration 0027) excluido.
      - opcional `prov`: filtrar por proveedor específico.
    """
    return db.fetch_all(
        """
        SELECT p.id_posdat, p.fecha, p.fechad, p.prov, p.importe,
               p.concepto, p.num, p.clave,
               COALESCE(pr.nombre, '') AS proveedor
        FROM scintela.posdat p
        LEFT JOIN scintela.proveedor pr ON pr.codigo_prov = p.prov
        WHERE COALESCE(p.banc, 0) = 0
          AND (p.anulada IS NOT TRUE OR p.anulada IS NULL)
          AND (%(prov)s IS NULL OR UPPER(p.prov) = UPPER(%(prov)s))
        ORDER BY p.fechad ASC, p.id_posdat ASC
        LIMIT 200
        """,
        {"prov": prov or None},
    ) or []


def conceptos_frecuentes_egresos(limite: int = 50) -> list[dict]:
    """Top conceptos usados en cheques propios (egresos del banco).

    Para autocomplete del form emitir-cheque. Filtra documentos de SALIDA
    (CH/ND/etc.) — los DE/TR/IN son entradas y no aplican.
    """
    return db.fetch_all(
        """
        SELECT TRIM(concepto) AS concepto,
               COUNT(*)        AS usos
          FROM scintela.transacciones_bancarias
         WHERE UPPER(TRIM(COALESCE(documento, ''))) NOT IN
               ('DE','TR','XX','NC','IN')
           AND COALESCE(concepto, '') <> ''
         GROUP BY TRIM(concepto)
         ORDER BY usos DESC, concepto
         LIMIT %s
        """,
        (limite,),
    ) or []


def proveedores_activos(limite: int = 500) -> list[dict]:
    """Lista de proveedores activos para autocomplete (cheque a proveedor).

    Devuelve `codigo_prov`, `nombre`. Ordenado alfabético.
    """
    return db.fetch_all(
        """
        SELECT codigo_prov, COALESCE(nombre, '') AS nombre
          FROM scintela.proveedor
         WHERE COALESCE(activo, '1') NOT IN ('0', 'N')
         ORDER BY codigo_prov
         LIMIT %s
        """,
        (limite,),
    ) or []


def crear_movimiento_simple(
    *,
    no_banco: int,
    documento: str,
    importe: float,
    fecha,
    concepto: str = "",
    prov: str | None = None,
    usuario: str = "web",
) -> dict:
    """Crea un movimiento bancario "simple" (DE / NC / ND).

    Pedido Tamara 2026-05-19: la pantalla de Bancos ahora tiene 4 acciones
    (Emitir cheque + Depositar + NC + ND). Las 3 últimas usan este helper.

    Argumentos:
        documento: 'DE' (depósito), 'NC' (nota de crédito), 'ND' (nota de débito).
        importe:   positivo siempre — el signo lo aplica bank_helpers.
        prov:      opcional, código de proveedor relacionado (informativo).

    Signos (`bank_helpers.signo_documento`):
        DE → +1 (suma al saldo)
        NC → +1 (suma al saldo)
        ND → −1 (resta del saldo)

    Atómico: insert + mov_doble en la misma tx. Devuelve dict con
    `id_transaccion`, `saldo_nuevo`, `id_mov_doble`.
    """
    import bank_helpers
    import mov_doble as _md

    documento = (documento or "").upper().strip()
    if documento not in ("DE", "NC", "ND"):
        raise ValueError(
            f"documento debe ser DE, NC o ND (recibido: {documento!r})"
        )
    if not no_banco:
        raise ValueError("no_banco requerido")
    importe_f = abs(float(importe or 0))
    if importe_f <= 0:
        raise ValueError("Importe debe ser > 0.")
    if not fecha:
        raise ValueError("fecha requerida")
    asegurar_fecha_abierta(fecha)

    # Tipo de mov_doble: 1 a 1 con documento para que el dispatcher de
    # /historial sepa cómo reversarlo. Convención:
    #   DE → "deposito"
    #   NC → "nota_credito"
    #   ND → "nota_debito"
    tipo_md = {
        "DE": "deposito",
        "NC": "nota_credito",
        "ND": "nota_debito",
    }[documento]

    concepto_clean = (concepto or "").strip()[:50]

    with db.tx() as conn:
        mov = bank_helpers.insert_movimiento_bancario(
            conn,
            no_banco=no_banco,
            no_cta=None,
            fecha=fecha,
            documento=documento,
            importe=importe_f,
            concepto=concepto_clean,
            prov=(prov or None),
            usuario=usuario,
        )
        # Auto-link: origen=destino = la fila bancaria misma (no hay
        # contraparte tipo factura/posdat). Esto permite reversarlo
        # individualmente desde /historial usando id_mov_doble.
        id_md = _md.registrar(
            conn=conn,
            tipo=tipo_md,
            origen_table="transacciones_bancarias",
            origen_id=mov.get("id_transaccion"),
            destino_table="transacciones_bancarias",
            destino_id=mov.get("id_transaccion"),
            importe=importe_f,
            fecha=fecha,
            concepto=f"{documento} {concepto_clean}".strip()[:200],
            usuario=usuario,
            metadata={
                "no_banco": no_banco,
                "documento": documento,
                "prov": prov or "",
            },
        )

    return {
        "id_transaccion": mov.get("id_transaccion"),
        "saldo_nuevo":    mov.get("saldo_nuevo"),
        "id_mov_doble":   id_md,
        "documento":      documento,
        "importe":        importe_f,
        "no_banco":       no_banco,
    }


def reversar_movimiento_simple(
    *,
    id_mov_doble: int,
    motivo: str = "",
    usuario: str = "web",
) -> dict:
    """Reversa un movimiento simple (deposito/nota_credito/nota_debito).

    Lee el mov_doble original, mira su documento y compensa con el
    documento de signo opuesto:
      DE(+1) → reversado con CH(-1)
      NC(+1) → reversado con CH(-1)
      ND(-1) → reversado con NC(+1)

    Marca el mov_doble original como `estado='reversado'` + `id_reverso`.
    Atómico.
    """
    import bank_helpers
    import mov_doble as _md

    if not id_mov_doble:
        raise ValueError("id_mov_doble requerido.")

    md_orig = db.fetch_one(
        """
        SELECT id_mov_doble, tipo, origen_id, destino_id, importe,
               fecha, concepto, metadata, estado
          FROM scintela.mov_doble
         WHERE id_mov_doble = %s
        """,
        (id_mov_doble,),
    )
    if not md_orig:
        raise ValueError(f"mov_doble #{id_mov_doble} no existe.")
    if md_orig.get("estado") != "activo":
        raise ValueError(
            f"mov_doble #{id_mov_doble} no está activo "
            f"(estado={md_orig.get('estado')!r})."
        )
    tipo_orig = (md_orig.get("tipo") or "").strip()
    if tipo_orig not in ("deposito", "nota_credito", "nota_debito"):
        raise ValueError(
            f"mov_doble #{id_mov_doble} no es un movimiento simple "
            f"(tipo={tipo_orig!r})."
        )

    # Leer el documento original desde la transacción bancaria linkeada.
    tx_orig = db.fetch_one(
        """
        SELECT id_transaccion, no_banco, documento, importe AS importe_orig, fecha
          FROM scintela.transacciones_bancarias
         WHERE id_transaccion = %s
        """,
        (md_orig.get("origen_id"),),
    )
    if not tx_orig:
        raise ValueError(
            f"Transacción origen #{md_orig.get('origen_id')} no existe."
        )

    doc_orig = (tx_orig.get("documento") or "").upper().strip()
    # Documento de reverso (signo opuesto).
    doc_reverso = {"DE": "CH", "NC": "CH", "ND": "NC"}.get(doc_orig)
    if not doc_reverso:
        raise ValueError(
            f"No sé cómo reversar documento {doc_orig!r} (esperaba DE/NC/ND)."
        )

    importe_f = abs(float(md_orig.get("importe") or 0))
    fecha_rev = _date.today()
    asegurar_fecha_abierta(fecha_rev)

    motivo_clean = (motivo or "").strip()
    concepto_rev = (
        f"REVERSO {tipo_orig} #{id_mov_doble}"
        + (f" — {motivo_clean}" if motivo_clean else "")
    )[:50]

    with db.tx() as conn:
        mov_rev = bank_helpers.insert_movimiento_bancario(
            conn,
            no_banco=int(tx_orig["no_banco"]),
            no_cta=None,
            fecha=fecha_rev,
            documento=doc_reverso,
            importe=importe_f,
            concepto=concepto_rev,
            usuario=usuario,
        )

        # mov_doble del reverso, linkeado al original via id_original.
        id_md_rev = _md.registrar(
            conn=conn,
            tipo=f"reverso_{tipo_orig}",
            origen_table="transacciones_bancarias",
            origen_id=mov_rev.get("id_transaccion"),
            destino_table="transacciones_bancarias",
            destino_id=mov_rev.get("id_transaccion"),
            importe=importe_f,
            fecha=fecha_rev,
            concepto=concepto_rev,
            usuario=usuario,
            metadata={
                "motivo": motivo_clean,
                "doc_orig": doc_orig,
                "doc_reverso": doc_reverso,
                "no_banco": int(tx_orig["no_banco"]),
            },
            id_original=id_mov_doble,
        )

    return {
        "id_transaccion_reverso": mov_rev.get("id_transaccion"),
        "saldo_nuevo":            mov_rev.get("saldo_nuevo"),
        "id_mov_doble_reverso":   id_md_rev,
        "doc_orig":               doc_orig,
        "doc_reverso":            doc_reverso,
    }


def transferir_entre_bancos(
    *,
    no_banco_origen: int,
    no_banco_destino: int,
    importe: float,
    fecha,
    concepto: str = "",
    usuario: str = "web",
) -> dict:
    """Mueve plata de un banco al otro. Atómico.

    Inserta:
      1) CH (egreso) en el banco origen
      2) DE (ingreso) en el banco destino
    Mismo importe, misma fecha, conceptos vinculados ("TR a/de banco X").
    Saldos auto-actualizados por el trigger.
    """
    import bank_helpers
    if not no_banco_origen or not no_banco_destino:
        raise ValueError("Banco origen y destino requeridos.")
    if no_banco_origen == no_banco_destino:
        raise ValueError("Origen y destino son el mismo banco.")
    importe_f = abs(float(importe or 0))
    if importe_f <= 0:
        raise ValueError("Importe debe ser > 0.")

    bancos = {
        int(b["no_banco"]): (b.get("nombre") or "")
        for b in (db.fetch_all(
            "SELECT no_banco, COALESCE(nombre, '') AS nombre FROM scintela.banco"
        ) or [])
    }
    nombre_origen  = bancos.get(no_banco_origen)
    nombre_destino = bancos.get(no_banco_destino)
    if not nombre_origen or not nombre_destino:
        raise ValueError(f"Banco no encontrado: {no_banco_origen}/{no_banco_destino}")

    concepto_base = (concepto or "").strip()
    concepto_origen  = (f"TR a {nombre_destino}" +
                        (f" — {concepto_base}" if concepto_base else ""))[:50]
    concepto_destino = (f"TR de {nombre_origen}" +
                        (f" — {concepto_base}" if concepto_base else ""))[:50]

    id_mov_doble = None
    with db.tx() as conn:
        mov_origen = bank_helpers.insert_movimiento_bancario(
            conn,
            no_banco=no_banco_origen,
            no_cta=None,
            fecha=fecha,
            documento="CH",
            importe=importe_f,
            concepto=concepto_origen,
            usuario=usuario,
        )
        mov_destino = bank_helpers.insert_movimiento_bancario(
            conn,
            no_banco=no_banco_destino,
            no_cta=None,
            fecha=fecha,
            documento="TR",  # transferencia recibida (entrada)
            importe=importe_f,
            concepto=concepto_destino,
            usuario=usuario,
        )

        # Registrar en historial unificado.
        import mov_doble as _md
        id_mov_doble = _md.registrar(
            conn=conn,
            tipo="transfer_banco_banco",
            origen_table="transacciones_bancarias",
            origen_id=mov_origen.get("id_transaccion"),
            destino_table="transacciones_bancarias",
            destino_id=mov_destino.get("id_transaccion"),
            importe=importe_f,
            fecha=fecha,
            concepto=f"{nombre_origen} → {nombre_destino} {concepto_base}".strip(),
            usuario=usuario,
            metadata={"no_banco_origen": no_banco_origen,
                      "no_banco_destino": no_banco_destino},
        )

    return {
        "origen": {"no_banco": no_banco_origen, "nombre": nombre_origen,
                   "id_transaccion": mov_origen["id_transaccion"],
                   "saldo_nuevo": mov_origen["saldo_nuevo"]},
        "destino": {"no_banco": no_banco_destino, "nombre": nombre_destino,
                    "id_transaccion": mov_destino["id_transaccion"],
                    "saldo_nuevo": mov_destino["saldo_nuevo"]},
        "importe": importe_f,
        "id_mov_doble": id_mov_doble,
    }


def reversar_transferencia(
    *,
    id_mov_doble: int,
    motivo: str = "",
    usuario: str = "web",
) -> dict:
    """Reversa una transferencia banco↔banco previamente registrada.

    Toma el `id_mov_doble` de un movimiento tipo='transfer_banco_banco' activo
    y compensa atómicamente ambos lados:
      - En el banco origen (que tenía CH egreso): inserta NC (ingreso).
      - En el banco destino (que tenía TR ingreso): inserta CH (egreso).
    Marca el mov_doble original como 'reversado' y registra el reverso
    linkeado con `id_original`.

    Regla de signos:
      CH(-1) compensado con NC(+1) → neto cero.
      TR(+1) compensado con CH(-1) → neto cero.

    TMT 2026-05-13.
    """
    import bank_helpers
    import mov_doble as _md

    motivo = (motivo or "").strip()
    fecha_rev = _date.today()
    asegurar_fecha_abierta(fecha_rev)

    md = db.fetch_one(
        """
        SELECT id_mov_doble, tipo, origen_table, origen_id,
               destino_table, destino_id, importe, estado, metadata
          FROM scintela.mov_doble
         WHERE id_mov_doble = %s
        """,
        (id_mov_doble,),
    )
    if not md:
        raise ValueError(f"mov_doble {id_mov_doble} no existe.")
    if md.get("tipo") != "transfer_banco_banco":
        raise ValueError(
            f"mov_doble #{id_mov_doble} no es una transferencia banco↔banco "
            f"(tipo={md.get('tipo')!r})."
        )
    if md.get("estado") != "activo":
        raise ValueError(
            f"La transferencia #{id_mov_doble} ya está en estado "
            f"{md.get('estado')!r} — no se puede reversar otra vez."
        )

    # Origen es la tx de egreso (CH); destino la de ingreso (TR/DE).
    tx_orig = db.fetch_one(
        "SELECT id_transaccion, no_banco, importe, documento FROM "
        "scintela.transacciones_bancarias WHERE id_transaccion = %s",
        (md["origen_id"],),
    )
    tx_dest = db.fetch_one(
        "SELECT id_transaccion, no_banco, importe, documento FROM "
        "scintela.transacciones_bancarias WHERE id_transaccion = %s",
        (md["destino_id"],),
    )
    if not tx_orig or not tx_dest:
        raise ValueError(
            "No encuentro las transacciones origen/destino — datos rotos."
        )

    importe_abs = abs(float(md.get("importe") or tx_orig.get("importe") or 0))
    if importe_abs <= 0:
        raise ValueError("Importe original = 0, nada que reversar.")

    with db.tx() as conn:
        # 1) Compensación en el banco ORIGEN: NC (ingreso) que cancela el CH.
        comp_origen = bank_helpers.insert_movimiento_bancario(
            conn,
            no_banco=tx_orig["no_banco"],
            no_cta=None,
            fecha=fecha_rev,
            documento="NC",
            importe=importe_abs,
            concepto=(f"REVERSO transfer #{id_mov_doble}"
                      + (f" — {motivo}" if motivo else ""))[:50],
            usuario=usuario,
        )
        # 2) Compensación en el banco DESTINO: CH (egreso) que cancela el TR.
        comp_destino = bank_helpers.insert_movimiento_bancario(
            conn,
            no_banco=tx_dest["no_banco"],
            no_cta=None,
            fecha=fecha_rev,
            documento="CH",
            importe=importe_abs,
            concepto=(f"REVERSO transfer #{id_mov_doble}"
                      + (f" — {motivo}" if motivo else ""))[:50],
            usuario=usuario,
        )
        # 3) Registrar reverso linkeado al original.
        id_md_rev = _md.registrar(
            conn=conn,
            tipo="reverso_transfer_banco_banco",
            origen_table="transacciones_bancarias",
            origen_id=comp_origen["id_transaccion"],
            destino_table="transacciones_bancarias",
            destino_id=comp_destino["id_transaccion"],
            importe=importe_abs,
            fecha=fecha_rev,
            concepto=("REVERSO transferencia banco→banco"
                      + (f" — {motivo}" if motivo else ""))[:200],
            usuario=usuario,
            metadata={"motivo": motivo or "",
                      "id_mov_doble_original": id_mov_doble},
            id_original=id_mov_doble,
        )

    return {
        "id_mov_doble_original": id_mov_doble,
        "id_mov_doble_reverso":  id_md_rev,
        "compensacion_origen":   comp_origen["id_transaccion"],
        "compensacion_destino":  comp_destino["id_transaccion"],
        "importe":               importe_abs,
        "no_banco_origen":       tx_orig["no_banco"],
        "no_banco_destino":      tx_dest["no_banco"],
    }


def reversar_cheque_emitido(
    *,
    id_transaccion: int,
    motivo: str = "",
    usuario: str = "web",
) -> dict:
    """Reversa un cheque emitido (de chequera) con todos sus side effects.

    Operación atómica (TMT 2026-05-12 Fase K):
      1. Lee la transacción original (documento='CH'). Si ya fue reversada
         (existe una ND apuntando), error.
      2. INSERT compensación bancaria documento='ND' (nota de débito) por
         el mismo importe en signo OPUESTO.
      3. Según el tipo de side effect (lookup via mov_doble):
         - proveedor con id_posdat → reabre la posdat (banc=0).
         - retiro → INSERT scintela.retiros con importe NEGATIVO.
         - caja   → INSERT scintela.caja TIPO='S' (cancela la entrada).
         - gasto  → marca xgast con stat='Y' (anulado).
         - otro / sin side effect → sólo compensación bancaria.
      4. Registra reverso en mov_doble enlazado al original.

    Devuelve dict con info de lo reversado.
    """
    import bank_helpers
    import mov_doble as _md

    motivo = (motivo or "").strip()
    if not motivo:
        raise ValueError("Motivo requerido para reversar el cheque.")
    fecha_rev = _date.today()
    asegurar_fecha_abierta(fecha_rev)

    tx = db.fetch_one(
        """
        SELECT id_transaccion, no_banco, documento, importe, concepto,
               prov, numreferencia, fecha, stat
          FROM scintela.transacciones_bancarias
         WHERE id_transaccion = %s
        """,
        (id_transaccion,),
    )
    if not tx:
        raise ValueError(f"Transacción {id_transaccion} no existe.")
    doc = (tx.get("documento") or "").strip().upper()
    if doc != "CH":
        raise ValueError(
            f"Esta transacción no es un cheque emitido (documento={doc!r}). "
            "Sólo se puede reversar con esta operación cheques de chequera."
        )
    # Detectar doble reverso: si ya hay una mov_doble del tipo reverso_emitido
    # que apunta al original, abortar.
    ya = db.fetch_one(
        """
        SELECT id_mov_doble FROM scintela.mov_doble
         WHERE origen_table = 'transacciones_bancarias'
           AND origen_id = %s
           AND estado = 'reversado'
         LIMIT 1
        """,
        (id_transaccion,),
    )
    if ya:
        raise ValueError(
            f"El cheque emitido (tx #{id_transaccion}) ya fue reversado."
        )

    # Buscar el mov_doble original para saber qué side effect deshacer.
    md_orig = _md.buscar_por_origen(
        origen_table="transacciones_bancarias",
        origen_id=id_transaccion,
    )
    importe_orig = float(tx.get("importe") or 0)
    importe_abs = abs(importe_orig)
    if importe_abs <= 0:
        raise ValueError("Importe original = 0, no hay nada que reversar.")

    side_revertido = None
    with db.tx() as conn:
        # 1) Compensación bancaria — NC (Nota de Crédito) ingresa la plata
        # de vuelta al banco. ANTES usaba 'ND' (Nota de Débito) que es un
        # documento de EGRESO → restaba el saldo otra vez en lugar de
        # devolverlo. signo_documento('ND')=-1, signo_documento('NC')=+1.
        # TMT 2026-05-13.
        mov_comp = bank_helpers.insert_movimiento_bancario(
            conn,
            no_banco=tx["no_banco"],
            no_cta=None,
            fecha=fecha_rev,
            documento="NC",
            importe=importe_abs,
            concepto=(f"REVERSO ch tx#{id_transaccion} — {motivo}")[:50],
            prov=tx.get("prov"),
            numreferencia=tx.get("numreferencia"),
            usuario=usuario,
        )
        id_compensacion = mov_comp.get("id_transaccion")

        # 2) Side effect inverso según mov_doble original.
        if md_orig:
            tipo_orig = md_orig.get("tipo") or ""
            dest_table = md_orig.get("destino_table")
            dest_id = md_orig.get("destino_id")

            if tipo_orig == "cheque_emitido_proveedor" and dest_table == "posdat":
                # Reabrir la posdat — pasar banc de no_banco a 0.
                db.execute(
                    "UPDATE scintela.posdat SET banc=0, "
                    "    fecha_modifica=CURRENT_TIMESTAMP, usuario_modifica=%s "
                    "WHERE id_posdat=%s",
                    (usuario[:50], dest_id),
                    conn=conn,
                )
                side_revertido = {"tipo": "posdat_reabierta", "id_posdat": dest_id}

            elif tipo_orig == "cheque_emitido_retiro" and dest_table == "retiros":
                # INSERT retiro con importe negativo (compensación contable).
                # El campo `nb` lo dejamos NULL — no toca un banco real (el
                # reverso bancario ya se hizo arriba).
                rev_row = db.execute_returning(
                    """
                    INSERT INTO scintela.retiros
                        (fecha, ret, de, concepto, clave, usuario_crea)
                    SELECT %s, -ret, de,
                           ('REVERSO id ' || id_retiro || ' — ' || %s)::varchar,
                           clave, %s
                      FROM scintela.retiros
                     WHERE id_retiro = %s
                    RETURNING id_retiro
                    """,
                    (fecha_rev, motivo[:60], usuario[:50], dest_id),
                    conn=conn,
                ) or {}
                side_revertido = {"tipo": "retiro_compensado",
                                  "id_retiro_compensacion": rev_row.get("id_retiro"),
                                  "id_retiro_original": dest_id}

            elif tipo_orig == "cheque_emitido_caja" and dest_table == "caja":
                # INSERT caja salida (S) que cancela la entrada original.
                # Usar caja_helpers para que saldo running se compute.
                # TMT 2026-05-13.
                import caja_helpers
                rev_row = caja_helpers.insert_movimiento_caja(
                    conn,
                    fecha=fecha_rev,
                    tipo="S",
                    importe=importe_abs,
                    concepto=(f"REVERSO caja#{dest_id} — {motivo}")[:80],
                    clave="REV",
                    usuario=usuario[:50],
                )
                side_revertido = {"tipo": "caja_compensada",
                                  "id_caja_compensacion": rev_row.get("id_caja"),
                                  "id_caja_original": dest_id}

            elif tipo_orig == "cheque_emitido_gasto" and dest_table == "xgast":
                # Marcar el xgast como anulado (stat='Y') — no inserta nuevo.
                db.execute(
                    "UPDATE scintela.xgast SET stat='Y', "
                    "    usuario_modifica=%s "
                    "WHERE id_xgast=%s",
                    (usuario[:50], dest_id),
                    conn=conn,
                )
                side_revertido = {"tipo": "xgast_anulado", "id_xgast": dest_id}

        # 3) Registrar reverso en mov_doble (siempre, aunque no haya side effect).
        id_md_rev = _md.registrar(
            conn=conn,
            tipo="reverso_cheque_emitido",
            origen_table="transacciones_bancarias",
            origen_id=id_compensacion,
            destino_table=(md_orig or {}).get("destino_table", "transacciones_bancarias"),
            destino_id=(md_orig or {}).get("destino_id", id_transaccion),
            importe=importe_abs,
            fecha=fecha_rev,
            concepto=f"REVERSO ch tx#{id_transaccion} — {motivo}",
            usuario=usuario,
            id_original=(md_orig or {}).get("id_mov_doble"),
            metadata={"id_transaccion_original": id_transaccion,
                      "motivo": motivo,
                      "side_effect_revertido": side_revertido},
        )

    return {
        "id_transaccion_original":    id_transaccion,
        "id_transaccion_compensacion": id_compensacion,
        "no_banco":                   tx["no_banco"],
        "importe":                    importe_abs,
        "side_effect_revertido":      side_revertido,
        "id_mov_doble_reverso":       id_md_rev,
    }
