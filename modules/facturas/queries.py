"""Consultas de facturas de venta.

Vocabulario canónico de stats (2026-04-29 — ver docs/SKILL_ADDENDUM_BATCH_18.md):

    Z = emitida (sin abono todavía)        -- estado inicial
    A = abonada parcialmente (saldo > 0)
    T = cancelada por el total (saldo = 0) -- terminal feliz
    X = eliminada por error                -- anulación administrativa

Cartera de facturas = Z + A (las que tienen saldo vivo).

TMT 2026-05-19 v8 — La stat 'Y' fue retirada del universo de facturas
(la dueña confirmó: "factura Y no existe, borremoslo de todos lados").
Anulada = 'X'. Si por algún motivo aparece una fila legacy con stat='Y',
queda fuera de cualquier vista (cartera, canceladas, eliminadas, estado).
"""
from datetime import date, timedelta

import db
from periodo_guard import asegurar_fecha_abierta

# Stats que cuentan como "vivos" (cartera viva). Excluye T (cancelada) y X (anulada).
STATS_VIVOS = ("Z", "A")
# Stats que cuentan como "anulado / eliminado" — para excluir en cartera.
# TMT 2026-05-19 v8 — dueña: "factura Y no existe, borremoslo de todos lados".
# Antes había STATS_ANULADAS = ("X","Y") como compat legacy, pero Y nunca
# se usó en la base operativa. Se elimina del universo conocido.
STATS_ANULADAS = ("X",)


def _stat_desde_saldo(importe: float, abono: float) -> str:
    """Devuelve el stat correspondiente según el flujo de cobranza.

    - importe = saldo (abono=0)  → 'Z' (emitida, sin abono)
    - 0 < abono < importe        → 'A' (abonada parcial)
    - abono >= importe           → 'T' (cancelada total)
    """
    abono = float(abono or 0)
    importe = float(importe or 0)
    if abono <= 0.01:
        return "Z"
    if abono >= importe - 0.01:
        return "T"
    return "A"


def proximo_numf() -> int:
    """Siguiente número de factura (MAX+1). Fallback a 1 si no hay."""
    row = db.fetch_one("SELECT COALESCE(MAX(numf), 0) + 1 AS siguiente FROM scintela.factura")
    return int(row["siguiente"]) if row else 1


def crear(
    *,
    fecha: date,
    codigo_cli: str,
    kg,
    importe,
    numf: int | None = None,
    vencimiento: date | None = None,
    condic: str | None = None,
    tipo: str | None = None,
    numf_completo: str | None = None,
    clave: str | None = None,
    usuario: str = "web",
) -> dict:
    """Insert de una factura nueva.

    Reglas (vocabulario canónico 2026-04-29):
        - saldo inicial = importe (abono = 0, saldo = importe)
        - stat inicial  = 'Z' (emitida, sin abono)
        - Si no llega vencimiento y el cliente tiene c.pago (días), se usa.
        - Si no llega numf, se asigna MAX+1.

    Notas:
        - El stat 'A' se asigna automáticamente cuando se aplica un cheque
          parcial (ver `cheques.queries.aplicar_a_factura`).
        - El stat 'T' se asigna cuando saldo = 0.
        - La anulación va por `anular()` que setea 'X'.
    """
    asegurar_fecha_abierta(fecha)

    # Vencimiento por defecto = fecha + pago_del_cliente días (si hay)
    if vencimiento is None:
        row = db.fetch_one(
            "SELECT pago FROM scintela.cliente WHERE codigo_cli = %s",
            (codigo_cli,),
        )
        # Bug D fix (TMT 2026-05-16): 3545 clientes legacy tienen pago='C'
        # (contado), 'X', '+', '.', '', etc. — strings que int() no parsea.
        # Si no es numérico, fallback a 30 días.
        _pago_raw = (row.get("pago") if row else None) or ""
        _pago_str = str(_pago_raw).strip()
        dias = int(_pago_str) if _pago_str.isdigit() else 30
        vencimiento = fecha + timedelta(days=dias)

    with db.tx() as conn:
        # TMT 2026-05-20 PASADA 3 — race-condition fix.
        # Antes: numf = MAX(numf)+1 fuera de tx. Dos requests concurrentes
        # podían asignar el mismo numf. Con advisory lock por la tabla
        # entera (clave 4242 = "factura.numf"), solo una transacción a la
        # vez recalcula el siguiente. El lock se libera al COMMIT/ROLLBACK.
        if numf is None:
            with conn.cursor() as _cur:
                _cur.execute("SELECT pg_advisory_xact_lock(4242)")
                _cur.execute(
                    "SELECT COALESCE(MAX(numf), 0) + 1 FROM scintela.factura"
                )
                numf = int(_cur.fetchone()[0])

        row = db.execute_returning(
            """
            INSERT INTO scintela.factura
                (numf, fecha, codigo_cli, kg, importe, abono, saldo,
                 stat, condic, tipo, vencimiento, numf_completo, clave, usuario_crea)
            VALUES (%s, %s, %s, %s, %s, 0, %s,
                    'Z', %s, %s, %s, %s, %s, %s)
            RETURNING id_factura, numf
            """,
            (
                numf, fecha, codigo_cli.upper().strip(),
                kg, importe, importe,
                (condic or None), (tipo or None),
                vencimiento, (numf_completo or None),
                (clave or None)[:2] if clave else None,
                usuario,
            ),
            conn=conn,
        )
        # Historial unificado: toda factura emitida queda registrada en
        # mov_doble como auto-referencia (factura→factura) para que
        # aparezca en /historial. Si el registro falla, rollback total
        # (vale más perder la factura que tener una sin huella). El
        # importe se preserva con signo (devoluciones < 0) — el historial
        # ya distingue tipo via metadata.devolucion. TMT 2026-05-13.
        if row and row.get("id_factura"):
            import mov_doble as _md
            es_devolucion = float(importe or 0) < 0
            _md.registrar(
                conn=conn,
                tipo=("factura_devolucion" if es_devolucion else "factura_emitida"),
                origen_table="factura",
                origen_id=row["id_factura"],
                destino_table="factura",
                destino_id=row["id_factura"],
                importe=float(importe or 0),
                fecha=fecha,
                concepto=(("DEVOLUCION " if es_devolucion else "")
                           + f"Factura #{numf} {codigo_cli.upper().strip()}")[:200],
                usuario=usuario,
                metadata={"codigo_cli": codigo_cli.upper().strip(),
                          "numf": numf,
                          "kg": float(kg or 0),
                          "devolucion": es_devolucion},
            )
    return row or {}


def editar(
    id_factura: int,
    *,
    abono=None,
    condic: str | None = None,
    observacion: str | None = None,
    usuario: str = "web",
) -> dict:
    """Edición *blanda* de una factura emitida.

    Regla Ecuador (paridad con MODIFICA.PRG y discusión 2026-04-30):
    una factura emitida NO se edita en sus campos duros (importe, fecha,
    cliente, numf, kg). Para corregir cualquiera de eso → anular y reemitir.
    Esta función sólo permite ajustar:

      - `abono`: corrige el abono manual (e.g., para registrar un pago en
        efectivo no asociado a un cheque). Recompute `saldo = importe - abono`
        atomically. Si nuevo saldo ≈ 0 y stat≠'T', stampa primera vez:
        `stat='T'`, `vencim=CURRENT_DATE`.
      - `condic`: si cambia ' '→'C' aplica 5% pronto pago (importe×=0.95).
        Si cambia 'C'→' ' revierte (importe/=0.95). En ambos casos, el
        SALDO se recomputa con el nuevo importe.
      - `observacion`: append-only, append "[E]" al texto.

    Importe / kg / fecha / cliente / numf NUNCA se editan acá. ValueError
    si el caller los pasa como kwarg.

    Reglas:
      - No se puede editar facturas anuladas (stat ∈ X, Y).
      - `asegurar_fecha_abierta(fact.fecha)` — el período de la factura.
      - Bitácora best-effort vía after_request.

    Devuelve `{id_factura, importe, abono, saldo, stat, condic_previa, condic_nueva}`.
    """
    fact = db.fetch_one(
        "SELECT id_factura, fecha, importe, abono, saldo, stat, condic, vencimiento "
        "FROM scintela.factura WHERE id_factura = %s",
        (id_factura,),
    )
    if not fact:
        raise ValueError("Factura inexistente.")
    stat_actual = (fact.get("stat") or "").upper()
    if stat_actual in STATS_ANULADAS:
        raise ValueError("La factura está anulada/eliminada — no se puede editar.")

    asegurar_fecha_abierta(fact["fecha"])

    importe_actual = float(fact["importe"] or 0)
    abono_actual = float(fact["abono"] or 0)
    condic_actual = (fact.get("condic") or "").strip()
    importe_nuevo = importe_actual
    abono_nuevo = abono_actual if abono is None else float(abono or 0)
    condic_nueva = condic_actual if condic is None else (condic or "").strip()

    # Toggle pronto pago (5%)  — paridad MODIFICA.PRG L435-442.
    # Convención dBase: condic vacío (' ') = no aplicado, 'C' = aplicado.
    if condic is not None:
        if condic_actual in ("", " ") and condic_nueva.upper() == "C":
            importe_nuevo = round(importe_actual * 0.95, 2)
        elif condic_actual.upper() == "C" and condic_nueva in ("", " "):
            importe_nuevo = round(importe_actual / 0.95, 2)

    # Validación: abono no puede exceder importe (con epsilon).
    if abono_nuevo < 0:
        raise ValueError("El abono no puede ser negativo.")
    if abono_nuevo > importe_nuevo + 0.01:
        raise ValueError(
            f"Abono ({abono_nuevo:.2f}) excede el importe ({importe_nuevo:.2f})."
        )

    saldo_nuevo = round(importe_nuevo - abono_nuevo, 2)

    # Stat recompute — paridad MODIFICA.PRG L443.
    if saldo_nuevo <= 0.01:
        stat_nuevo = "T"
    elif abono_nuevo > 0.01:
        stat_nuevo = "A"
    else:
        # importe modificado por condic, pero abono=0 → vuelve a Z (emitida).
        stat_nuevo = "Z"

    # Primera vez stat='T' → stampa vencim=CURRENT_DATE (paridad
    # MODIFICA.PRG L425-426).
    stamp_vencim = stat_actual != "T" and stat_nuevo == "T"

    obs_marca = None
    if observacion:
        obs_marca = f"[E] {observacion[:120]}"

    sql_set = ["importe=%s", "abono=%s", "saldo=%s", "stat=%s",
               "condic=%s", "usuario_modifica=%s"]
    params: list = [importe_nuevo, abono_nuevo, saldo_nuevo, stat_nuevo,
                    condic_nueva or None, usuario]
    if stamp_vencim:
        sql_set.append("vencimiento=CURRENT_DATE")
    if obs_marca:
        sql_set.append("observacion = COALESCE(observacion||' | ','')||%s")
        params.append(obs_marca)
    params.append(id_factura)

    # #32 (TMT 2026-05-14): si cambia el abono manualmente (sin pasar por
    # aplicación de cheque), registrar un mov_doble tipo='factura_abono_manual'
    # con el delta. Esto permite verlo en /historial y, eventualmente,
    # reversarlo. Antes el cambio quedaba sin huella.
    abono_cambio = abs(abono_nuevo - abono_actual) > 0.01

    with db.tx() as conn:
        db.execute(
            f"UPDATE scintela.factura SET {', '.join(sql_set)} WHERE id_factura=%s",
            tuple(params),
            conn=conn,
        )
        if abono_cambio:
            try:
                import mov_doble as _md
                _md.registrar(
                    conn=conn,
                    tipo="factura_abono_manual",
                    origen_table="factura",
                    origen_id=id_factura,
                    destino_table="factura",
                    destino_id=id_factura,
                    importe=round(abono_nuevo - abono_actual, 2),
                    fecha=fact.get("fecha") or date.today(),
                    concepto=(
                        f"Abono manual factura #{fact.get('numf') or id_factura} "
                        f"{abono_actual:.2f} → {abono_nuevo:.2f}"
                    )[:200],
                    usuario=usuario,
                    metadata={"abono_prev": abono_actual,
                              "abono_nuevo": abono_nuevo,
                              "id_factura": id_factura,
                              "stat_previo": stat_actual,
                              "stat_nuevo": stat_nuevo},
                )
            except Exception:
                # mov_doble missing/transient: dejamos burbujar. La edición
                # del abono no debería perder huella.
                raise

    return {
        "id_factura": id_factura,
        "importe": importe_nuevo,
        "abono": abono_nuevo,
        "saldo": saldo_nuevo,
        "stat_previo": stat_actual,
        "stat_nuevo": stat_nuevo,
        "condic_previa": condic_actual,
        "condic_nueva": condic_nueva,
        "vencimiento_stamp": stamp_vencim,
    }


def por_id(id_factura: int) -> dict | None:
    """Cabecera de factura con datos del cliente."""
    return db.fetch_one(
        """
        SELECT f.id_factura, f.numf, f.numf_completo, f.fecha, f.vencimiento,
               f.codigo_cli, f.kg, f.importe, f.abono, f.saldo,
               f.stat, f.condic, f.tipo, f.pase, f.clave,
               COALESCE(c.nombre, '')    AS cliente,
               c.ruc, c.telefono, c.pago
        FROM scintela.factura f
        LEFT JOIN scintela.cliente c ON c.codigo_cli = f.codigo_cli
        WHERE f.id_factura = %s
        """,
        (id_factura,),
    )


def cheques_aplicados(id_factura: int) -> list[dict]:
    """Aplicaciones de cheques a esta factura vía chequesxfact."""
    return db.fetch_all(
        """
        SELECT cxf.id_chequexfact, cxf.fechaing, cxf.tipo, cxf.importe AS aplicado,
               cxf.abono_f, cxf.saldo_f, cxf.stat_f, cxf.no_banco,
               ch.id_cheque, ch.no_cheque, ch.fecha AS cheque_fecha, ch.fechad,
               -- TMT 2026-05-17: fechad_original/fecha_postergacion para
               -- mostrar "original X · postergado Y" si el cheque fue postergado.
               ch.fechad_original, ch.fecha_postergacion,
               ch.importe AS cheque_importe, ch.stat AS cheque_stat,
               ch.banco AS cheque_banco
        FROM scintela.chequesxfact cxf
        LEFT JOIN scintela.cheque ch ON ch.id_cheque = cxf.id_cheque
        WHERE cxf.id_fact = %s
        ORDER BY cxf.fechaing DESC
        """,
        (id_factura,),
    )


def retenciones_aplicadas(codigo_cli: str, numf: int) -> list[dict]:
    """Retenciones emitidas para esta factura (por codigo_cli + numf)."""
    return db.fetch_all(
        """
        SELECT id_retencion, fecha, rete
        FROM scintela.retencion
        WHERE codigo_cli = %s AND numf = %s
        ORDER BY fecha DESC
        """,
        (codigo_cli, numf),
    )


def anular(id_factura: int, *, motivo: str, usuario: str = "web") -> int:
    """Marca una factura como eliminada por error (stat='X').

    Vocabulario canónico (2026-04-29): el botón "anular" del UI es
    realmente "eliminé esto por error" — la factura no debería existir.
    Distinto de la anulación SRI (que sería un trámite tributario aparte).

    Reglas:
        - Debe existir.
        - Stat actual NO puede ser 'X' (ya eliminada).
        - No puede tener aplicaciones de cheques vigentes (chequesxfact).
        - No puede tener retenciones emitidas (retencion).
        - Conserva el histórico — sólo cambia stat a 'X' y deja motivo en
          observacion.
    """
    motivo = (motivo or "").strip()  # opcional. TMT 2026-05-13.

    fact = db.fetch_one(
        "SELECT id_factura, numf, codigo_cli, stat, saldo, importe, fecha "
        "FROM scintela.factura WHERE id_factura = %s",
        (id_factura,),
    )
    if not fact:
        raise ValueError("Factura inexistente.")
    stat_actual = (fact.get("stat") or "").upper()
    if stat_actual in STATS_ANULADAS:
        raise ValueError("La factura ya está anulada/eliminada.")

    # #33 (TMT 2026-05-14): no contar cheques YA reversados/anulados como
    # "aplicaciones vivas". Antes el COUNT(*) incluía aplicaciones de
    # cheques con stat X/3/R (terminales/eliminados) que ya no afectan el
    # saldo — eso bloqueaba la anulación sin sentido.
    aplicadas = db.fetch_one(
        """
        SELECT COUNT(*) AS n
          FROM scintela.chequesxfact cxf
          JOIN scintela.cheque c ON c.id_cheque = cxf.id_cheque
         WHERE cxf.id_fact = %s
           AND COALESCE(c.stat, '') NOT IN ('X', '3', 'R')
        """,
        (id_factura,),
    )
    if aplicadas and int(aplicadas["n"]) > 0:
        raise ValueError(
            "No se puede eliminar: hay cheques aplicados ACTIVOS a esta "
            "factura. Reversar las aplicaciones primero (cheques con "
            "stat X/3/R no cuentan)."
        )

    ret = db.fetch_one(
        "SELECT COUNT(*) AS n FROM scintela.retencion "
        "WHERE codigo_cli = %s AND numf = %s",
        (fact["codigo_cli"], fact["numf"]),
    )
    if ret and int(ret["n"]) > 0:
        raise ValueError(
            "No se puede eliminar: existen retenciones emitidas para esta factura. "
            "Anular las retenciones primero."
        )

    # Bug E fix (TMT 2026-05-16): scintela.factura no tiene columna
    # `observacion` — el motivo queda en bitácora vía registrar_bitacora()
    # que llama el view, y en metadata del mov_doble de reverso (ver abajo).
    with db.tx() as conn:
        rc = db.execute(
            """
            UPDATE scintela.factura
               SET stat = 'X',
                   usuario_modifica = %s,
                   fecha_modifica   = CURRENT_TIMESTAMP
             WHERE id_factura = %s
            """,
            (usuario, id_factura),
            conn=conn,
        )
        # Actualizar mov_doble — marcar el original como 'reversado' y
        # registrar un mov_doble de reverso linkeado. R2 (TMT 2026-05-14):
        # NO suprimir excepciones — antes había try/except: pass silencioso
        # que ocultaba bugs reales. Si falla, abortamos la anulación entera.
        import mov_doble as _md
        md_orig = db.fetch_one(
            """
            SELECT id_mov_doble, importe FROM scintela.mov_doble
             WHERE origen_table = 'factura'
               AND origen_id    = %s
               AND tipo IN ('factura_emitida','factura_devolucion')
               AND estado       = 'activo'
             ORDER BY id_mov_doble DESC LIMIT 1
            """,
            (id_factura,), conn=conn,
        )
        if md_orig:
            _md.registrar(
                conn=conn,
                tipo="reverso_factura_anulada",
                origen_table="factura",
                origen_id=id_factura,
                destino_table="factura",
                destino_id=id_factura,
                importe=float(md_orig.get("importe") or fact.get("importe") or 0),
                fecha=fact.get("fecha"),
                concepto=(f"ANULACION factura #{fact.get('numf') or id_factura}"
                          + (f" — {motivo}" if motivo else ""))[:200],
                usuario=usuario,
                metadata={"motivo": motivo or "",
                          "id_factura": id_factura,
                          "numf": fact.get("numf")},
                id_original=md_orig["id_mov_doble"],
            )
    return rc


def buscar(
    q: str = "",
    desde: str | None = None,
    hasta: str | None = None,
    solo_abiertas: bool = False,
    limite: int = 10000,  # TMT 2026-05-20 v3 — antes 500 truncaba a las
                          # 500 más antiguas y el running ACUM final no
                          # coincidía con el total del header (que cuenta
                          # las 4500+ del bucket entero). Pedido dueña:
                          # 'facturas, acum total, no es igual al total'.
                          # 10k cubre con margen — si las facturas crecen
                          # mucho más, paginamos.
                          # TMT 2026-05-22 — paginación opt-in via `offset`.
                          # `lista()` ahora pagina default 500 con controles
                          # de página; el limite=10000 sigue funcionando
                          # para callers externos sin offset.
    offset: int = 0,
    vista: str = "cartera",
    cliente: str = "",
    monto_min: float | None = None,
    monto_max: float | None = None,
    estado: str = "",
    estados: list[str] | None = None,
) -> list[dict]:
    """Filtros:
        q             — busqueda libre (numero, numf_completo, nombre)
        cliente       — filtro EXPLÍCITO por codigo_cli; si exactamente
                        3 chars alfanuméricos, match exacto; si no, LIKE
        monto_min     — filtra importe >= monto_min
        monto_max     — filtra importe <= monto_max
        desde/hasta   — fecha (YYYY-MM-DD)
        solo_abiertas — saldo > 0 (deprecado a favor de `vista=cartera`)
        vista (TMT 2026-05-19 — pedido dueña):
            'cartera'    → stat IN (Z, A) AND saldo > 0  (cartera viva — DEFAULT)
            'estado'     → todas (antes 'todas'); filtrable con `estado`.
            'canceladas' → stat = T  (cobradas total)
            'eliminadas' → stat = X  (eliminadas — Y removido 2026-05-19, no existe)
        estado (TMT 2026-05-19, sólo aplica con vista='estado'):
            'Z' | 'A' | 'T' | 'X' | 'N' o '' (vacío = todos). 'Y' retirado.
            'N' = anulada en Asinfo (sincronizada por el bridge — 2026-05-22).
        estados (TMT 2026-05-19 v8, sólo aplica con vista='estado'):
            lista de stats — permite filtrar por VARIOS estados a la vez,
            ej. ['Z','A','T']. Lista vacía o None = todos. Si `estados` se
            pasa, tiene precedencia sobre `estado` (scalar legacy).
    """
    q = (q or "").strip()
    like = f"%{q}%" if q else None
    vista = (vista or "cartera").lower().strip()
    # Back-compat — la vista antes se llamaba 'todas'.
    if vista == "todas":
        vista = "estado"
    estado = (estado or "").upper().strip()
    # TMT 2026-05-19 v8 — multi-estado. Filtrar/normalizar.
    # TMT 2026-05-19 v8 — 'Y' retirado del universo de stats.
    estados_validos = ("Z", "A", "T", "X", "N")
    estados_lista = [
        s.upper().strip() for s in (estados or [])
        if s and s.upper().strip() in estados_validos
    ]
    # De-dup conservando orden.
    seen: set[str] = set()
    estados_lista = [s for s in estados_lista if not (s in seen or seen.add(s))]
    # Si vino solo `estado` scalar (legacy), promovemos a lista.
    if not estados_lista and estado in estados_validos:
        estados_lista = [estado]
    # Para el SQL: si está vacía → no filtra; si tiene Z, incluye también
    # los NULL/empty/' ' (legacy = Z implícito).
    estado_incluye_z = "Z" in estados_lista
    # Lista de stats explícitos (sin la Z especial).
    estados_para_in = [s for s in estados_lista if s != "Z"] or [""]
    cliente = (cliente or "").strip().upper()
    # Detector de "código de cliente exacto": 3 caracteres alfanuméricos.
    # Tanto en el campo `q` legacy como en el campo `cliente` nuevo:
    # si tiene 3 chars alfanum, match EXACTO sobre codigo_cli (no fuzzy).
    q_upper = q.upper() if q else ""
    es_q_codigo_exacto = bool(q_upper) and len(q_upper) == 3 and q_upper.replace("_", "").isalnum()
    es_cli_codigo_exacto = bool(cliente) and len(cliente) == 3 and cliente.replace("_", "").isalnum()
    cliente_like = f"%{cliente}%" if cliente else None
    rows = db.fetch_all(
        """
        SELECT f.id_factura, f.numf, f.numf_completo, f.fecha, f.vencimiento,
               f.codigo_cli, COALESCE(c.nombre, '') AS cliente,
               f.kg, f.importe, f.abono, f.saldo, f.stat, f.condic, f.tipo
        FROM scintela.factura f
        LEFT JOIN scintela.cliente c ON c.codigo_cli = f.codigo_cli
        WHERE (
                %(q)s IS NULL
             OR (
                  %(q_codigo_exacto)s
                  AND UPPER(TRIM(COALESCE(f.codigo_cli, ''))) = %(q_upper)s
                )
             OR (
                  NOT %(q_codigo_exacto)s
                  AND (
                       UPPER(f.codigo_cli) LIKE UPPER(%(like)s)
                    OR UPPER(COALESCE(f.numf_completo,'')) LIKE UPPER(%(like)s)
                    OR CAST(f.numf AS TEXT) LIKE %(like)s
                    OR UPPER(c.nombre) LIKE UPPER(%(like)s)
                  )
                )
              )
          -- Filtro explícito por cliente (campo nuevo, separado de `q`).
          AND (
                %(cliente)s IS NULL
             OR (%(cli_codigo_exacto)s
                 AND UPPER(TRIM(COALESCE(f.codigo_cli, ''))) = %(cliente)s)
             OR (NOT %(cli_codigo_exacto)s
                 AND UPPER(COALESCE(f.codigo_cli, '')) LIKE UPPER(%(cliente_like)s))
              )
          -- Filtro por monto USD (importe).
          AND (%(monto_min)s::numeric IS NULL OR COALESCE(f.importe, 0) >= %(monto_min)s::numeric)
          AND (%(monto_max)s::numeric IS NULL OR COALESCE(f.importe, 0) <= %(monto_max)s::numeric)
          AND (%(desde)s::date IS NULL OR f.fecha >= %(desde)s::date)
          AND (%(hasta)s::date IS NULL OR f.fecha <= %(hasta)s::date)
          AND (NOT %(solo_abiertas)s OR COALESCE(f.saldo, 0) > 0)
          AND (
                %(vista)s = 'estado'
             -- TMT 2026-05-19 v7 — dueña: el total en /resultados (b.totf)
             -- no coincidía con el de /facturas vista=cartera. Bug:
             -- informes.totf() NO filtra por signo (incluye sobrepagos
             -- saldo<0 que netean cartera, fórmula dBase legacy). Acá
             -- teníamos saldo > 0 que excluía las 664 facturas negativas
             -- (~$-293k). Cambio: saldo <> 0 para que ambos números cuadren.
             OR (%(vista)s = 'cartera'
                 AND COALESCE(f.saldo, 0) <> 0
                 AND (f.stat IS NULL OR f.stat IN ('Z','A','',' ')))
             OR (%(vista)s = 'canceladas' AND f.stat = 'T')
             OR (%(vista)s = 'eliminadas' AND f.stat = 'X')
          )
          -- TMT 2026-05-19 v8 — filtro multi-estado (lista de stats).
          -- Si la lista está vacía → no filtra (lo marcamos con flag
          -- `estados_vacia`). Si tiene Z, ese matchea NULL/empty/' ' también
          -- (legacy = Z implícito).
          AND (
                %(estados_vacia)s
             OR (%(estado_incluye_z)s
                 AND (f.stat IS NULL OR f.stat IN ('Z','',' ')))
             OR f.stat = ANY(%(estados_para_in)s::text[])
          )
        ORDER BY f.fecha DESC, f.numf DESC
        LIMIT %(limite)s OFFSET %(offset)s
        """,
        {
            "q": q or None, "like": like,
            "q_upper": q_upper, "q_codigo_exacto": es_q_codigo_exacto,
            "cliente": cliente or None, "cliente_like": cliente_like,
            "cli_codigo_exacto": es_cli_codigo_exacto,
            "monto_min": monto_min, "monto_max": monto_max,
            "desde": desde or None, "hasta": hasta or None,
            "solo_abiertas": solo_abiertas,
            "vista": vista,
            "estado": estado,
            "estados_vacia": not estados_lista,
            "estado_incluye_z": estado_incluye_z,
            "estados_para_in": estados_para_in,
            "limite": limite,
            "offset": offset,
        },
    ) or []
    # Running total cronológico (ascendente).
    # TMT 2026-05-20 v2 — el ACUM ahora acumula SALDO (no importe), para
    # que el último valor coincida con el header (que muestra SUM(saldo)
    # del bucket cartera). Pedido dueña: "el total de arriba no coincide
    # con el acumulado. es porque no tenes en cuenta las negativas".
    # Devoluciones y sobrepagos (saldo negativo) restan del corrido, lo
    # mismo que hacen en el header — total visible = último ACUM.
    from datetime import date as _date
    # Calculamos el acum en orden ASC cronológico (running total).
    rows_asc = sorted(rows, key=lambda r: (r.get("fecha") or _date.min,
                                           r.get("numf") or 0))
    acum = 0.0
    for r in rows_asc:
        acum += float(r.get("saldo") or 0)
        r["saldo_acumulado"] = acum
    # Pero la pantalla muestra las facturas en orden DESC (pedido dueña
    # 2026-05-21: las nuevas arriba). El SQL ya devuelve DESC; lo único
    # que necesitamos hacer es invertir el resultado del sort ASC.
    return list(reversed(rows_asc))


def contar_filtrado(
    q: str = "",
    desde: str | None = None,
    hasta: str | None = None,
    solo_abiertas: bool = False,
    vista: str = "cartera",
    cliente: str = "",
    monto_min: float | None = None,
    monto_max: float | None = None,
    estado: str = "",
    estados: list[str] | None = None,
) -> dict:
    """COUNT(*) + SUM(saldo) + SUM(importe) con los MISMOS filtros que `buscar()`.

    TMT 2026-05-22 — usado por la paginación de `/facturas` para mostrar
    "Mostrando X-Y de Z" y el total de importes / saldos del UNIVERSO filtrado
    (no solo la página visible).
    """
    q = (q or "").strip()
    like = f"%{q}%" if q else None
    vista = (vista or "cartera").lower().strip()
    if vista == "todas":
        vista = "estado"
    estado = (estado or "").upper().strip()
    estados_validos = ("Z", "A", "T", "X", "N")
    estados_lista = [
        s.upper().strip() for s in (estados or [])
        if s and s.upper().strip() in estados_validos
    ]
    seen: set[str] = set()
    estados_lista = [s for s in estados_lista if not (s in seen or seen.add(s))]
    if not estados_lista and estado in estados_validos:
        estados_lista = [estado]
    estado_incluye_z = "Z" in estados_lista
    estados_para_in = [s for s in estados_lista if s != "Z"] or [""]
    cliente = (cliente or "").strip().upper()
    q_upper = q.upper() if q else ""
    es_q_codigo_exacto = bool(q_upper) and len(q_upper) == 3 and q_upper.replace("_", "").isalnum()
    es_cli_codigo_exacto = bool(cliente) and len(cliente) == 3 and cliente.replace("_", "").isalnum()
    cliente_like = f"%{cliente}%" if cliente else None
    row = db.fetch_one(
        """
        SELECT COUNT(*) AS n,
               COALESCE(SUM(f.importe), 0) AS total_importe,
               COALESCE(SUM(f.saldo), 0)   AS total_saldo
        FROM scintela.factura f
        LEFT JOIN scintela.cliente c ON c.codigo_cli = f.codigo_cli
        WHERE (
                %(q)s IS NULL
             OR (
                  %(q_codigo_exacto)s
                  AND UPPER(TRIM(COALESCE(f.codigo_cli, ''))) = %(q_upper)s
                )
             OR (
                  NOT %(q_codigo_exacto)s
                  AND (
                       UPPER(f.codigo_cli) LIKE UPPER(%(like)s)
                    OR UPPER(COALESCE(f.numf_completo,'')) LIKE UPPER(%(like)s)
                    OR CAST(f.numf AS TEXT) LIKE %(like)s
                    OR UPPER(c.nombre) LIKE UPPER(%(like)s)
                  )
                )
              )
          AND (
                %(cliente)s IS NULL
             OR (%(cli_codigo_exacto)s
                 AND UPPER(TRIM(COALESCE(f.codigo_cli, ''))) = %(cliente)s)
             OR (NOT %(cli_codigo_exacto)s
                 AND UPPER(COALESCE(f.codigo_cli, '')) LIKE UPPER(%(cliente_like)s))
              )
          AND (%(monto_min)s::numeric IS NULL OR COALESCE(f.importe, 0) >= %(monto_min)s::numeric)
          AND (%(monto_max)s::numeric IS NULL OR COALESCE(f.importe, 0) <= %(monto_max)s::numeric)
          AND (%(desde)s::date IS NULL OR f.fecha >= %(desde)s::date)
          AND (%(hasta)s::date IS NULL OR f.fecha <= %(hasta)s::date)
          AND (NOT %(solo_abiertas)s OR COALESCE(f.saldo, 0) > 0)
          AND (
                %(vista)s = 'estado'
             OR (%(vista)s = 'cartera'
                 AND COALESCE(f.saldo, 0) <> 0
                 AND (f.stat IS NULL OR f.stat IN ('Z','A','',' ')))
             OR (%(vista)s = 'canceladas' AND f.stat = 'T')
             OR (%(vista)s = 'eliminadas' AND f.stat = 'X')
          )
          AND (
                %(estados_vacia)s
             OR (%(estado_incluye_z)s
                 AND (f.stat IS NULL OR f.stat IN ('Z','',' ')))
             OR f.stat = ANY(%(estados_para_in)s::text[])
          )
        """,
        {
            "q": q or None, "like": like,
            "q_upper": q_upper, "q_codigo_exacto": es_q_codigo_exacto,
            "cliente": cliente or None, "cliente_like": cliente_like,
            "cli_codigo_exacto": es_cli_codigo_exacto,
            "monto_min": monto_min, "monto_max": monto_max,
            "desde": desde or None, "hasta": hasta or None,
            "solo_abiertas": solo_abiertas,
            "vista": vista,
            "estado": estado,
            "estados_vacia": not estados_lista,
            "estado_incluye_z": estado_incluye_z,
            "estados_para_in": estados_para_in,
        },
    ) or {}
    return {
        "n": int(row.get("n") or 0),
        "total_importe": float(row.get("total_importe") or 0),
        "total_saldo": float(row.get("total_saldo") or 0),
    }


def conteos_por_vista() -> dict:
    """Conteos rápidos para los tabs: cartera / canceladas / eliminadas / todas."""
    rows = db.fetch_all(
        """
        SELECT
          -- TMT 2026-05-19 v7 — alineado con buscar() y informes.totf():
          -- "cartera" = stat ∈ (Z,A,blank) AND saldo <> 0 (incluye saldos
          -- negativos por sobrepago — netean cartera).
          CASE
            WHEN COALESCE(saldo, 0) <> 0
                 AND (stat IS NULL OR stat IN ('Z','A','',' '))    THEN 'cartera'
            WHEN stat = 'T'                                         THEN 'canceladas'
            WHEN stat = 'X'                                         THEN 'eliminadas'
            ELSE 'otras'
          END                                AS bucket,
          COUNT(*)                           AS n,
          COALESCE(SUM(saldo), 0)            AS total_saldo,
          COALESCE(SUM(importe), 0)          AS total_importe
        FROM scintela.factura
        GROUP BY 1
        """
    ) or []
    out = {r["bucket"]: dict(r) for r in rows}
    # TMT 2026-05-19 — 'estado' es el bucket que abarca todo (= antes 'todas').
    # Mantengo 'todas' como alias por back-compat con cualquier caller externo.
    total_row = {
        "n": sum(r["n"] for r in rows),
        "total_saldo": sum(float(r["total_saldo"] or 0) for r in rows),
        "total_importe": sum(float(r["total_importe"] or 0) for r in rows),
    }
    out["estado"] = total_row
    out["todas"] = total_row
    return out
