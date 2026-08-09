"""Queries del historial unificado de movimientos dobles.

Lee de scintela.mov_doble + enriquece con info de las tablas origen/destino
para que el timeline muestre nombres legibles (banco "Pichincha" en vez de
"transacciones_bancarias #12345").
"""

import db

# Etiquetas legibles de tipos — uno por línea para que sea fácil agregar.
TIPOS_LABEL = {
    # caja → X (concepto-driven)
    "caja_s_to_transfer_banco":  "Caja → Banco",
    "caja_s_to_retiro_socio":    "Caja → Retiro socio",
    "caja_s_to_dolares":         "Caja → USD",
    "caja_s_to_compra_proveedor": "Caja → Compra proveedor",
    "caja_e_to_transfer_banco":  "Banco → Caja (entrada)",
    "caja_e_to_dolares":         "USD → Caja",
    # caja simple (sin side effect — TMT 2026-05-12 historial completo)
    "caja_e_simple":             "Caja: entrada",
    "caja_s_simple":             "Caja: salida",
    "caja_cb_simple":            "Caja: contra banco",
    # caja directa (UNION con scintela.caja para filas viejas/legacy)
    "caja_e_directo":            "Caja: entrada",
    "caja_s_directo":            "Caja: salida",
    "caja_cb_directo":           "Caja: contra banco",
    # bancos directos (UNION con transacciones_bancarias)
    "banco_ch_directo":          "Banco: cheque emitido",
    "banco_de_directo":          "Banco: depósito",
    "banco_tr_directo":          "Banco: transferencia recibida",
    "banco_nd_directo":          "Banco: nota de débito",
    "banco_nc_directo":          "Banco: nota de crédito",
    "banco_ac_directo":          "Banco: acreditación",
    "banco_mov_directo":         "Banco: movimiento (sin documento)",
    "banco_desde_extracto":      "Banco: creado desde el extracto",
    "reverso_banco_desde_extracto": "Reverso: creado desde el extracto",
    # reversos de caja
    "reverso_caja_s_to_transfer_banco":   "Reverso: Caja → Banco",
    "reverso_caja_s_to_retiro_socio":     "Reverso: Caja → Retiro",
    "reverso_caja_s_to_dolares":          "Reverso: Caja → USD",
    "reverso_caja_s_to_compra_proveedor": "Reverso: Caja → Compra",
    "reverso_caja_e_to_transfer_banco":   "Reverso: Banco → Caja",
    "reverso_caja_simple":                "Reverso de caja (sin side effect)",
    # cheque emitido (chequera bancos)
    "cheque_emitido_proveedor": "Cheque emitido → Proveedor",
    "cheque_emitido_retiro":    "Cheque emitido → Retiro socio",
    "cheque_emitido_caja":      "Cheque emitido → Caja",
    "cheque_emitido_gasto":     "Cheque emitido → Gasto",
    # otros movimientos dobles
    "transfer_banco_banco":     "Transferencia banco ↔ banco",
    "endoso_cheque_a_proveedor": "Endoso cheque → Proveedor",
    "compra_pagada_caja":       "Compra pagada en Caja",
    "compra_pagada_pichincha":  "Compra pagada Pichincha",
    "compra_pagada_internacional": "Compra pagada Internacional",
    "compra_pago_parcial":      "Compra con pago parcial",
    "compra_a_posdat":          "Compra a crédito → Posdat",
    "compra_saldo_a_posdat":    "Saldo compra → Posdat",
    "compra_backfill":          "Compra (backfill)",
    "compra_anticipo_dolares":  "Compra → Anticipo USD",
    "cheque_aplicado_a_factura":"Cheque → Factura aplicada",
    "cheque_reemplazo":         "Cheque reemplazo (XX)",
    # TMT 2026-07-31 (dueña: *"dejá de usar BAP que nadie sabe qué es"*).
    # Y "BAP22" al lado de "AC 22" en la misma línea era una trampa: dos
    # números parecidos que no tienen nada que ver entre sí.
    "bap_anticipo_a_compra":    "Anticipo → Compra",
    "activacion_maquinaria":        "Activación de maquinaria",
    "activacion_maquinaria_reverso": "Reverso: activación de maquinaria",
    "factura_devolucion":       "Factura: devolución",
    "reverso_cheque_rebote":    "Reverso: cheque rebotado",
    "reverso_cheque_administrativo": "Reverso: cheque (admin)",
    "reverso_endoso_cheque":    "Reverso: endoso de cheque",
    "reverso_factura_anulada":  "Reverso: factura anulada",
    "reverso_compra_anulada":   "Reverso: compra anulada",
    "reverso_gasto_anulado":    "Reverso: gasto anulado",
    "reverso_transfer_banco_banco":       "Reverso: transferencia banco↔banco",
    "reverso_aporte_capital_caja":        "Reverso: aporte → Caja",
    "reverso_aporte_capital_pichincha":   "Reverso: aporte → Pichincha",
    "reverso_aporte_capital_internacional": "Reverso: aporte → Internacional",
    "reverso_retiro_socio_caja":          "Reverso: retiro ← Caja",
    "reverso_retiro_socio_pichincha":     "Reverso: retiro ← Pichincha",
    "reverso_retiro_socio_internacional": "Reverso: retiro ← Internacional",
    "reverso_cheque_aplicacion":          "Reverso: aplicación cheque→factura",
    "reverso_caja_s_to_xgast":            "Reverso: desclasificación de caja → gasto",
    "cheque_depositado":                  "Depósito de cheque (Z → B)",
    "cheque_efectivo_to_caja":            "Cheque cobrado en caja (Z → C)",
    "cheque_rebotado":                    "Cheque rebotado por el banco",
    "reverso_cheque_rebotado":            "Reverso: cheque rebotado",
    "cheque_devuelto":                    "Cheque devuelto (vuelve a cartera)",
    "reverso_cheque_devuelto":            "Reverso: cheque devuelto",
    "cheque_stat_cambio":                 "Cheque: cambio de estado",
    "reverso_cheque_stat_cambio":         "Reverso: cambio de estado del cheque",
    "reverso_cheque_depositado":          "Reverso: depósito de cheque",
    "factura_emitida":          "Factura emitida",
    "gasto_simple":             "Gasto pagado",
    "gasto_pagado_caja":        "Gasto pagado en Caja",
    "gasto_pagado_pichincha":   "Gasto pagado Pichincha",
    "gasto_pagado_internacional": "Gasto pagado Internacional",
    "gasto_a_posdat":           "Gasto a crédito → Posdat",
    "aporte_capital_a_caja":    "Aporte capital → Caja",
    "aporte_capital_a_pichincha": "Aporte capital → Pichincha",
    "aporte_capital_a_internacional": "Aporte capital → Internacional",
    "retiro_socio_de_caja":     "Retiro socio ← Caja",
    "retiro_socio_de_pichincha": "Retiro socio ← Pichincha",
    "retiro_socio_de_internacional": "Retiro socio ← Internacional",
    "transfer_usd_cuenta_cuenta": "Transferencia USD ↔ USD",
    # Audit-only (no reversables — son trazas de altas/ediciones).
    # TMT 2026-05-14 #R7 audit: agregados al dispatcher con comentario
    # explícito de "no se reversan" en _REVERSO_DISPATCH.
    "cheque_creado":            "Cheque: alta",
    "cheque_anticipo_espejo":   "Cheque: espejo de anticipo",
    "posdat_anulada":           "Posdat: anulada",
    "posdat_edit_importe":      "Posdat: edit de importe",
    "factura_abono_manual":     "Factura: abono manual",
    "retencion_movida_del_abono": "Retención separada del abono",
    # 🚨 TMT 2026-08-09, viendo "Caja S To Gasto": los tipos que faltaban acá
    # caían al nombre técnico con los guiones bajos en mayúsculas. Medido:
    # 17 tipos, 1.342 filas activas.
    "retencion_asinfo_aplicada": "Retención aplicada",
    "caja_s_to_gasto":          "Caja → Gasto",
    "caja_s_to_xgast":          "Caja → Gasto",
    "caja_e_to_gasto":          "Gasto devuelto → Caja",
    "dolares_anticipo":         "Anticipo en dólares",
    "nota_debito":              "Nota de débito",
    "nota_credito":             "Nota de crédito",
    "deposito":                 "Depósito",
    "cheque_cancelado_por_anticipo": "Anticipo cancela el cheque",
    "anticipo_neteado":         "Anticipo neteado",
    "posdat_yy_cierre_mes":     "Provisión: cierre de mes",
    "factura_stat_cambio":      "Factura: cambio de estado",
    "neteo_estado_cuenta":      "Neteo del estado de cuenta",
    "cheque_emitido_otro":      "Cheque emitido → otro",
    "importacion_anticipo":     "Anticipo de importación",
    "banco_clasificado_gasto":  "Banco → Gasto",
    "factura_cerrada_a_t":      "Factura cerrada (T)",
}


def label(tipo: str) -> str:
    """Etiqueta legible para un tipo de mov_doble."""
    return TIPOS_LABEL.get(tipo, tipo.replace("_", " ").title())


def listar(
    *,
    desde: str | None = None,
    hasta: str | None = None,
    tipo: str | None = None,
    estado: str | None = None,
    q: str | None = None,
    usuario: str | None = None,
    origenes_permitidos: list[str] | None = None,
    limite: int = 500,
    offset: int = 0,
) -> list[dict]:
    """Lista unificada de movimientos para el historial.

    UNION de dos fuentes (TMT 2026-05-12 follow-up "todos los movimientos"):
      1. scintela.mov_doble — movimientos dobles + reversos (esquema nuevo).
      2. scintela.caja — filas que NO tienen mov_doble asociado (viejas
         o huérfanas). Aparecen como tipo='caja_<tipo>_directo'.

    `estado` puede ser 'activo', 'reversado', 'reverso', o None (todos).
    `tipo` filtra por prefijo (LIKE 'caja_%') o exacto.
    `q` busca en concepto + tipo.

    Las filas de caja directa se "envuelven" en el mismo shape para que el
    template las renderee uniforme. Origen y destino apuntan a la misma
    fila de caja (sin pareja).
    """
    # TMT 2026-05-15: si la migración 0031 (batch_id) no corrió todavía,
    # detectamos y usamos NULL::uuid en su lugar. Mantiene compat con
    # entornos donde la columna aún no existe.
    try:
        col_batch_id = "m.batch_id"
        _check = db.fetch_one(
            """
            SELECT 1 FROM information_schema.columns
             WHERE table_schema='scintela' AND table_name='mov_doble'
               AND column_name='batch_id'
            """
        )
        if not _check:
            col_batch_id = "NULL::uuid"
    except Exception:
        col_batch_id = "NULL::uuid"

    _filas = db.fetch_all(
        f"""
        WITH unificado AS (
            -- A) mov_doble — todos los registrados explícitamente.
            SELECT m.id_mov_doble                  AS id_mov_doble,
                   m.fecha_operacion               AS fecha_operacion,
                   m.fecha_creacion                AS fecha_creacion,
                   m.tipo                          AS tipo,
                   m.origen_table                  AS origen_table,
                   m.origen_id                     AS origen_id,
                   m.destino_table                 AS destino_table,
                   m.destino_id                    AS destino_id,
                   m.importe                       AS importe,
                   m.concepto                      AS concepto,
                   m.usuario                       AS usuario,
                   m.estado                        AS estado,
                   m.id_reverso                    AS id_reverso,
                   m.id_original                   AS id_original,
                   m.metadata                      AS metadata,
                   {col_batch_id}                  AS batch_id,
                   'mov_doble'::text               AS fuente
              FROM scintela.mov_doble m

            UNION ALL

            -- B) caja directa — filas SIN mov_doble registrado.
            SELECT -c.id_caja                       AS id_mov_doble,
                   c.fecha                          AS fecha_operacion,
                   c.fecha::timestamptz             AS fecha_creacion,
                   ('caja_' || LOWER(c.tipo) || '_directo')::text AS tipo,
                   'caja'::text                     AS origen_table,
                   c.id_caja                        AS origen_id,
                   'caja'::text                     AS destino_table,
                   c.id_caja                        AS destino_id,
                   ABS(c.importe)                   AS importe,
                   c.concepto                      AS concepto,
                   COALESCE(c.usuario_crea, c.clave) AS usuario,
                   'activo'::text                   AS estado,
                   NULL::bigint                     AS id_reverso,
                   NULL::bigint                     AS id_original,
                   NULL::jsonb                      AS metadata,
                   NULL::uuid                       AS batch_id,
                   'caja_directa'::text             AS fuente
              FROM scintela.caja c
             WHERE NOT EXISTS (
                SELECT 1 FROM scintela.mov_doble m
                 WHERE (m.origen_table  = 'caja' AND m.origen_id  = c.id_caja)
                    OR (m.destino_table = 'caja' AND m.destino_id = c.id_caja)
             )

            UNION ALL

            -- C) bancos directos — transacciones_bancarias sin mov_doble.
            SELECT -(t.id_transaccion + 1000000000)       AS id_mov_doble,
                   t.fecha                                AS fecha_operacion,
                   t.fecha::timestamptz                   AS fecha_creacion,
                   ('banco_' || LOWER(COALESCE(t.documento, 'mov')) || '_directo')::text AS tipo,
                   'transacciones_bancarias'::text        AS origen_table,
                   t.id_transaccion                       AS origen_id,
                   'transacciones_bancarias'::text        AS destino_table,
                   t.id_transaccion                       AS destino_id,
                   ABS(COALESCE(t.importe, 0))            AS importe,
                   t.concepto                            AS concepto,
                   t.usuario_crea                         AS usuario,
                   'activo'::text                         AS estado,
                   NULL::bigint                           AS id_reverso,
                   NULL::bigint                           AS id_original,
                   NULL::jsonb                            AS metadata,
                   NULL::uuid                             AS batch_id,
                   'banco_directo'::text                  AS fuente
              FROM scintela.transacciones_bancarias t
             WHERE NOT EXISTS (
                SELECT 1 FROM scintela.mov_doble m
                 WHERE (m.origen_table  = 'transacciones_bancarias' AND m.origen_id  = t.id_transaccion)
                    OR (m.destino_table = 'transacciones_bancarias' AND m.destino_id = t.id_transaccion)
             )
        )
        SELECT *
          FROM unificado u
         WHERE (%(desde)s::date IS NULL OR u.fecha_operacion >= %(desde)s::date)
           AND (%(hasta)s::date IS NULL OR u.fecha_operacion <= %(hasta)s::date)
           AND (%(tipo)s IS NULL OR u.tipo = %(tipo)s OR u.tipo LIKE %(tipo_like)s)
           AND (%(estado)s IS NULL OR u.estado = %(estado)s)
           AND (%(q)s IS NULL
                OR UPPER(COALESCE(u.concepto, '')) LIKE UPPER(%(qlike)s)
                OR UPPER(u.tipo) LIKE UPPER(%(qlike)s)
                OR UPPER(COALESCE(u.usuario, '')) LIKE UPPER(%(qlike)s))
           -- TMT 2026-05-26 dueña: filtro por usuario exacto, para /mi-historial.
           AND (%(usuario)s IS NULL OR UPPER(COALESCE(u.usuario, '')) = UPPER(%(usuario)s))
           -- TMT 2026-05-26 dueña: filtro por origen_tables permitidos.
           -- Alex no debe ver retiros (no tiene retiros.ver). Pasamos la
           -- lista derivada de sus permisos. Si None → sin filtro.
           AND (%(origenes_permitidos)s::text[] IS NULL
                OR u.origen_table = ANY(%(origenes_permitidos)s::text[]))
           -- TMT 2026-05-20 v3 — dedup pedido dueña: cuando una caja S
           -- se clasifica como gasto V1..V9, se generan 2 mov_doble:
           --   (a) caja_s_simple   (caja → caja self-ref)
           --   (b) caja_s_to_xgast (caja → xgast con la categoría)
           -- En el historial queremos UNA sola fila (la de to_xgast,
           -- que es la informativa). Ocultamos las caja_s_simple
           -- cuando existe OTRO mov_doble con el mismo id_caja como
           -- origen y tipo distinto.
           AND NOT (
                u.tipo IN ('caja_s_simple', 'caja_e_simple', 'caja_cb_simple')
                AND u.origen_table = 'caja'
                AND EXISTS (
                    SELECT 1 FROM scintela.mov_doble m2
                     WHERE m2.origen_table = 'caja'
                       AND m2.origen_id    = u.origen_id
                       AND m2.tipo        <> u.tipo
                       AND m2.estado       = u.estado
                )
           )
         ORDER BY u.fecha_operacion DESC, u.id_mov_doble DESC
         LIMIT %(limite)s OFFSET %(offset)s
        """,
        {
            "desde": desde or None, "hasta": hasta or None,
            "tipo": tipo or None, "tipo_like": (tipo or "") + "%" if tipo else None,
            "estado": estado or None,
            "q": q or None, "qlike": f"%{q}%" if q else None,
            "usuario": usuario or None,
            "origenes_permitidos": list(origenes_permitidos) if origenes_permitidos else None,
            "limite": int(limite),
            "offset": max(0, int(offset)),
        },
    ) or []

    return _nombrar_conversiones(_filas)


def _nombrar_conversiones(filas: list[dict]) -> list[dict]:
    """Le pone nombre a las conversiones de anticipo → compra ya guardadas.

    TMT 2026-07-31 (dueña, buscando cuál de las conversiones del día era la de
    AC 22): *"acá nada dice AC 22, ¿cómo sé qué anticipo es?"*.

    Las filas viejas tienen guardado el concepto con el formato anterior
    ("BAP AC: 3 anticipo(s) → compra #10117 (BAP22)"), que no nombra el
    anticipo y encima mete un "BAP22" que se lee igual que "AC 22" sin tener
    nada que ver. Los ids de los anticipos SÍ están en la metadata, así que la
    línea se puede reescribir en la lectura — sin migrar nada.

    Best-effort: si algo falla, se devuelve la lista tal como vino.
    """
    import json as _json
    import re as _re

    try:
        objetivo = [
            f for f in filas
            if (f.get("tipo") or "") == "bap_anticipo_a_compra"
        ]
        if not objetivo:
            return filas

        ids: set[int] = set()
        por_fila: dict[int, list[int]] = {}
        for f in objetivo:
            meta = f.get("metadata")
            if isinstance(meta, str):
                try:
                    meta = _json.loads(meta)
                except Exception:  # noqa: BLE001
                    meta = None
            _ids = [int(i) for i in ((meta or {}).get("ids_anticipos") or [])]
            if _ids:
                por_fila[int(f["id_mov_doble"])] = _ids
                ids.update(_ids)
        if not ids:
            return filas

        placeholder = ",".join(["%s"] * len(ids))
        conceptos = {
            int(r["id_dolares"]): (r.get("concepto") or "")
            for r in (db.fetch_all(
                f"SELECT id_dolares, concepto FROM scintela.dolares "
                f"WHERE id_dolares IN ({placeholder})",
                tuple(sorted(ids)),
            ) or [])
        }

        # TMT 2026-08-03 (dueña): el rótulo decía "→ compra #473", que es el id
        # INTERNO y no coincide con el N° que muestra la pantalla Compras. Se
        # traduce a `numero`, igual que las facturas usan numf.
        _id_compras = {int(f["destino_id"]) for f in objetivo if f.get("destino_id")}
        numeros_compra: dict[int, str] = {}
        if _id_compras:
            _ph = ",".join(["%s"] * len(_id_compras))
            for rc in (db.fetch_all(
                f"SELECT id_compra, COALESCE(numero::text, '') AS numero "
                f"FROM scintela.compra WHERE id_compra IN ({_ph})",
                tuple(sorted(_id_compras)),
            ) or []):
                _n = (rc.get("numero") or "").strip()
                if _n and _n != "0":
                    numeros_compra[int(rc["id_compra"])] = _n

        for f in objetivo:
            _ids = por_fila.get(int(f["id_mov_doble"]))
            if not _ids:
                continue
            refs: list[str] = []
            for i in _ids:
                m = _re.match(r"\s*(\d{1,6})", conceptos.get(i, ""))
                if m and m.group(1) not in refs:
                    refs.append(m.group(1))
            if not refs:
                continue
            refs.sort(key=lambda x: int(x))
            prov = str((f.get("metadata") or {}).get("codigo_prov") or "").strip().upper() \
                if isinstance(f.get("metadata"), dict) else ""
            if not prov:
                # Sale del concepto viejo: "BAP AC: 3 anticipo(s) → …".
                mm = _re.search(r"BAP\s+([A-Z0-9]{1,4})\s*:", f.get("concepto") or "")
                prov = mm.group(1) if mm else ""
            etiqueta = (f"{prov} {'/'.join(refs[:4])}").strip()
            _did = f.get("destino_id")
            _num = numeros_compra.get(int(_did)) if _did else None
            _ref = f"N° {_num}" if _num else f"#{_did or ''}"
            f["concepto"] = f"{etiqueta} · {len(_ids)} anticipo(s) → compra {_ref}"
    except Exception:  # noqa: BLE001 -- un rótulo no puede romper el historial
        pass
    return filas


def _filtro_fechas_sql():
    """SQL fragment común para el WHERE del UNION (caja, banco, mov_doble)."""
    return (
        " WHERE (%(desde)s::date IS NULL OR fecha_operacion >= %(desde)s::date) "
        "   AND (%(hasta)s::date IS NULL OR fecha_operacion <= %(hasta)s::date) "
    )


def resumen_batches(batch_ids) -> dict:
    """Cuántos movimientos y cuánta plata tiene CADA batch, entero.

    TMT 2026-08-07. La tarjeta del historial armaba su "N movimientos · $X"
    contando las filas de la PÁGINA. Mientras un batch entraba entero en las
    25 filas del default nadie lo notó; con la corrida diaria de retenciones
    (que puede aplicar 30 de una) la tarjeta empezaría a decir "25" cuando
    fueron 30, y el total corto — un número redondo, creíble y falso. El
    conteo sale de la base, sobre el batch completo; la página sigue
    mostrando las filas que entren.

    Devuelve {batch_id: {"n": int, "total": float, "por_tipo": {tipo: (n, total)}}}.
    """
    ids = [str(b) for b in (batch_ids or []) if b]
    if not ids:
        return {}
    filas = db.fetch_all(
        """
        SELECT batch_id::text        AS batch_id,
               tipo                  AS tipo,
               COUNT(*)              AS n,
               COALESCE(SUM(importe), 0) AS total
          FROM scintela.mov_doble
         WHERE batch_id = ANY(%s::uuid[])
         GROUP BY batch_id, tipo
        """,
        (ids,),
    ) or []
    out: dict = {}
    for f in filas:
        b = out.setdefault(f["batch_id"], {"n": 0, "total": 0.0, "por_tipo": {}})
        n, total = int(f["n"] or 0), float(f["total"] or 0)
        b["por_tipo"][f["tipo"]] = (n, total)
        b["n"] += n
        b["total"] = round(b["total"] + total, 2)
    return out


def conteos(
    *,
    desde: str | None = None,
    hasta: str | None = None,
) -> dict:
    """Conteos para las tarjetas KPI del header.

    UNION mov_doble + caja directa + banco directo (filas sin mov_doble).
    Devuelve: total, activos, reversos, reversados, n_por_tipo (top 12).
    """
    params = {"desde": desde or None, "hasta": hasta or None}
    base_subquery = """
        SELECT m.fecha_operacion AS fecha_operacion,
               m.tipo AS tipo, m.importe AS importe, m.estado AS estado
          FROM scintela.mov_doble m
        UNION ALL
        SELECT c.fecha AS fecha_operacion,
               ('caja_' || LOWER(c.tipo) || '_directo')::text AS tipo,
               ABS(c.importe) AS importe,
               'activo'::text AS estado
          FROM scintela.caja c
         WHERE NOT EXISTS (
            SELECT 1 FROM scintela.mov_doble m
             WHERE (m.origen_table  = 'caja' AND m.origen_id  = c.id_caja)
                OR (m.destino_table = 'caja' AND m.destino_id = c.id_caja)
         )
        UNION ALL
        SELECT t.fecha AS fecha_operacion,
               ('banco_' || LOWER(COALESCE(t.documento, 'mov')) || '_directo')::text AS tipo,
               ABS(COALESCE(t.importe, 0)) AS importe,
               'activo'::text AS estado
          FROM scintela.transacciones_bancarias t
         WHERE NOT EXISTS (
            SELECT 1 FROM scintela.mov_doble m
             WHERE (m.origen_table  = 'transacciones_bancarias' AND m.origen_id  = t.id_transaccion)
                OR (m.destino_table = 'transacciones_bancarias' AND m.destino_id = t.id_transaccion)
         )
    """

    base = db.fetch_one(
        f"""
        SELECT COUNT(*) AS n,
               COALESCE(SUM(importe), 0) AS total,
               SUM(CASE WHEN estado='activo'    THEN 1 ELSE 0 END) AS n_activos,
               SUM(CASE WHEN estado='reverso'   THEN 1 ELSE 0 END) AS n_reversos,
               SUM(CASE WHEN estado='reversado' THEN 1 ELSE 0 END) AS n_reversados,
               COALESCE(SUM(CASE WHEN estado='activo' THEN importe ELSE 0 END), 0)
                                                                AS total_activos
          FROM ({base_subquery}) u
         {_filtro_fechas_sql()}
        """,
        params,
    ) or {}

    por_tipo = db.fetch_all(
        f"""
        SELECT tipo, COUNT(*) AS n, COALESCE(SUM(importe), 0) AS total
          FROM ({base_subquery}) u
         {_filtro_fechas_sql()}
         GROUP BY tipo
         ORDER BY n DESC
         LIMIT 12
        """,
        params,
    ) or []

    return {
        "n":              int(base.get("n") or 0),
        "total":          float(base.get("total") or 0),
        "n_activos":      int(base.get("n_activos") or 0),
        "total_activos":  float(base.get("total_activos") or 0),
        "n_reversos":     int(base.get("n_reversos") or 0),
        "n_reversados":   int(base.get("n_reversados") or 0),
        "por_tipo":       por_tipo,
    }


#: Nombres CORTOS para la traza, partidos en (objeto, acción).
#:
#: TMT 2026-08-07: *"barely can read cheque cancelado por anticipo… lo otro se
#: puede acortar mucho más: CH BED → cancela anticipo"*.
#:
#: 🚨 La causa real de que se leyera mal no era sólo el ancho: 25 tipos de
#: `mov_doble` no tenían entrada en TIPOS_LABEL y caían al fallback
#: `tipo.replace("_", " ")`, que produce la cadena MÁS LARGA posible, en
#: minúscula, en la columna más angosta. `cheque_cancelado_por_anticipo` era
#: uno; `retencion_asinfo_aplicada`, que corre todos los días por el cron,
#: otro.
#:
#: El objeto va en dos letras mayúsculas y la acción en minúscula; entre los
#: dos se mete el código de la contraparte: "CH" + "BED" + "→ cancela AN".
#: `/historial` sigue usando el nombre largo — ahí el ancho sobra— y el corto
#: lleva el largo en su `title`, así que no se pierde nada.
TIPOS_CORTO = {
    "cheque_creado": ("CH", "alta"),
    "cheque_aplicado_a_factura": ("CH", "→ FA"),
    "cheque_depositado": ("CH", "→ BC"),
    "cheque_rebotado": ("CH", "rebotó"),
    "cheque_devuelto": ("CH", "devuelto"),
    "cheque_stat_cambio": ("CH", "cambio de estado"),
    "cheque_cancelado_por_anticipo": ("CH", "→ cancela AN"),
    "cheque_anticipo_espejo": ("CH", "espejo AN"),
    "cheque_reemplazo": ("CH", "reemplazo"),
    "cheque_efectivo_to_caja": ("CH", "→ CJ"),
    "cheque_emitido_proveedor": ("CH", "→ proveedor"),
    "cheque_emitido_retiro": ("CH", "→ DV"),
    "cheque_emitido_caja": ("CH", "→ CJ"),
    "cheque_emitido_gasto": ("CH", "→ GS"),
    "endoso_cheque_a_proveedor": ("CH", "endoso"),
    "anticipo_neteado": ("CH", "✗ AN"),
    "factura_emitida": ("FA", "nueva"),
    "factura_devolucion": ("FA", "devolución"),
    "factura_cerrada_a_t": ("FA", "cerrada"),
    "factura_reabierta_de_t": ("FA", "reabierta"),
    "factura_stat_cambio": ("FA", "cambio de estado"),
    "retencion_asinfo_aplicada": ("RT", "→ FA"),
    "retencion_movida_del_abono": ("RT", "sale del abono"),
    "retencion_asinfo_desaplicada": ("RT", "✗ FA"),
    "retencion_doble_corregida": ("RT", "corregida"),
    "caja_e_simple": ("CJ", "entra"),
    "caja_s_simple": ("CJ", "sale"),
    "caja_cb_simple": ("CJ", "contra BC"),
    "caja_e_directo": ("CJ", "entra"),
    "caja_s_directo": ("CJ", "sale"),
    "caja_cb_directo": ("CJ", "contra BC"),
    "caja_s_to_transfer_banco": ("CJ", "→ BC"),
    "caja_e_to_transfer_banco": ("CJ", "← BC"),
    "caja_s_to_retiro_socio": ("CJ", "→ DV"),
    "caja_s_to_dolares": ("CJ", "→ AN"),
    "caja_e_to_dolares": ("CJ", "← AN"),
    "caja_s_to_compra_proveedor": ("CJ", "→ CP"),
    "caja_s_to_xgast": ("CJ", "→ GS"),
    "banco_de_directo": ("BC", "depósito"),
    "banco_ch_directo": ("BC", "→ CH"),
    "banco_tr_directo": ("BC", "transferencia"),
    "banco_nd_directo": ("BC", "nota de débito"),
    "banco_nc_directo": ("BC", "nota de crédito"),
    "banco_ac_directo": ("BC", "acreditación"),
    "banco_mov_directo": ("BC", "movimiento"),
    "banco_desde_extracto": ("BC", "del extracto"),
    "banco_clasificado_gasto": ("BC", "→ GS"),
    "nota_debito": ("BC", "nota de débito"),
    "transfer_banco_banco": ("BC", "→ BC"),
    "compra_a_posdat": ("CP", "→ DE"),
    "compra_saldo_a_posdat": ("CP", "saldo → DE"),
    "compra_pagada_caja": ("CP", "← CJ"),
    "compra_pagada_pichincha": ("CP", "← BC"),
    "compra_pagada_internacional": ("CP", "← BC"),
    "compra_pago_parcial": ("CP", "pago parcial"),
    "compra_anticipo_dolares": ("CP", "→ AN"),
    "compra_backfill": ("CP", "backfill"),
    "bap_anticipo_a_compra": ("AN", "→ CP"),
    # 🚨 TMT 2026-08-07: *"«alta» no dice que salió plata del banco"*. La
    # regla del diff ya lo llama "Anticipo entregado"; el corto decía otra cosa.
    "dolares_anticipo": ("AN", "entregado"),
    "retiro_op": ("DV", "OP"),
    "totalizar_estado_cuenta": ("FA", "totalizar"),
}


def corto(tipo: str, quien: str = "") -> str:
    """El nombre corto de un tipo, con el código de la contraparte en el medio.

    "cheque_cancelado_por_anticipo" + "BED"  →  "CH BED → cancela AN"

    Un reverso lleva ↩ adelante y hereda el corto del tipo original. Si el tipo
    no está mapeado cae al nombre largo, que es feo pero nunca miente.
    """
    tipo = (tipo or "").strip()
    vuelta = ""
    if tipo.startswith("reverso_"):
        vuelta, tipo = "↩ ", tipo[len("reverso_"):]
    par = TIPOS_CORTO.get(tipo)
    if not par:
        return vuelta + TIPOS_LABEL.get(tipo, tipo.replace("_", " "))
    objeto, accion = par
    return vuelta + " ".join(x for x in (objeto, quien, accion) if x)


def link_origen(row: dict, factura_numfs: dict | None = None, cheque_nos: dict | None = None,
                posdat_etiquetas: dict | None = None,
                banco_nos: dict | None = None) -> tuple[str | None, str]:
    """Devuelve (url, etiqueta) para el lado origen del mov.

    Tamara 2026-05-23: los links de factura/cheque deben usar el numero
    REAL (numf/no_cheque) en la URL, no el id interno. Cuando el caller
    pasa los mappings, los usamos. Sino caemos al id interno (legacy).
    """
    t = row.get("origen_table")
    rid = row.get("origen_id")
    if not rid:
        return None, ""
    # TMT 2026-08-07 (dueña): *"esos links deberían venir filtrados por lo que
    # quiero ver"*. Todos los destinos de acá abajo caen en LA FILA, no en la
    # pantalla entera. Criterio: si al clickear hay que buscar la fila a ojo, el
    # link no está terminado.
    if t == "caja":
        # Antes era "/caja#id-{rid}" — un ancla que NO EXISTE en ningún template
        # de la app (la única aparición de la cadena era esta línea, la que la
        # generaba). O sea que el link era, literalmente, "/caja". Y /caja pagina
        # de a 500: 52 de las 466 filas linkeadas ni siquiera estaban en la
        # primera página.
        # 🚨 TMT 2026-08-09: *"Caja tampoco sé el concepto exacto"*. Una fila
        # de caja no tiene número: su identidad es fecha + concepto + importe,
        # y las tres ya están en la fila del historial. El id interno no
        # agregaba nada, así que la etiqueta es "Caja" a secas (el link sigue
        # llevando a LA fila).
        return f"/caja?id={rid}", "Caja"
    if t == "transacciones_bancarias":
        # 1.862 movimientos apuntan acá — el destino de MÁS volumen del
        # historial — y hasta hoy no tenía link: sólo el nombre del banco como
        # texto muerto. La URL necesita el `no_banco` porque la pantalla es
        # /bancos/<no_banco>; el batch de la vista lo trae. Sin él no se puede
        # armar una URL válida, así que se queda sin link (como antes) en vez de
        # mandar a un banco equivocado.
        nb = (banco_nos or {}).get(int(rid)) if rid else None
        if nb:
            return f"/bancos/{nb}?id={rid}", f"Banco mov #{rid}"
        return None, f"Banco mov #{rid}"
    if t == "cheque":
        # Si conocemos el no_cheque, lo usamos como path (más human-readable).
        nch = (cheque_nos or {}).get(int(rid)) if rid else None
        if nch and str(nch).strip():
            return f"/cheques/{nch}", f"Cheque {nch}"
        return f"/cheques/{rid}", f"Cheque #{rid}"
    if t == "compra":
        return f"/compras/{rid}", f"Compra #{rid}"
    if t == "factura":
        # Si conocemos el numf, lo usamos en la URL.
        nfact = (factura_numfs or {}).get(int(rid)) if rid else None
        if nfact and str(nfact).strip() and str(nfact).strip() != "0":
            return f"/facturas/{nfact}", f"Factura {nfact}"
        return f"/facturas/{rid}", f"Factura #{rid}"
    # `capital` NO tiene rama a propósito (TMT 2026-08-07). Medido contra
    # producción: **0 movimientos** de mov_doble apuntan a esa tabla. Y mandarla
    # a /retiros era peor que inútil — /retiros lee SÓLO `scintela.retiros`,
    # mientras que los aportes viven en `scintela.capital` (ver
    # modules/capital/queries.py::aportar), así que con el ?id= nuevo habría
    # mostrado OTRA fila cualquiera, silenciosamente y sin 404. Cae al
    # `return None` del final.
    if t == "retiros":
        return f"/retiros?id={rid}", f"Retiro #{rid}"
    if t == "dolares":
        # Antes "/dolares?cta=" — con el `cta` VACÍO, o sea un no-op. Y peor: la
        # pantalla trae solo_vivos=1 por default, que esconde todo anticipo ya
        # convertido o aplicado… que es justamente el que genera el movimiento.
        # Medido: 132 de 149 filas linkeadas (89%) eran invisibles.
        return f"/dolares?id={rid}", f"USD #{rid}"
    if t == "posdat":
        # TMT 2026-08-07 (dueña: "el link me manda a proveedores y no al
        # posdatado que se menciona"). Esto decía "/proveedores" desde el
        # primer commit: nunca llevó al posdatado, y como los links son
        # strings a mano no se veía desde el código. Va a la lista pedida por
        # id — que apaga los filtros de deuda viva / tab / anulada, así el
        # posdatado del movimiento aparece siempre.
        #
        # La URL va por id INTERNO y la etiqueta por el `num` visible (mismo
        # criterio que factura/compra/cheque): un `num` puede coincidir con el
        # id de OTRO posdatado. Ojo que el CONCEPTO del movimiento ya venía
        # escrito con el `num` (posdat/queries.py "Edit importe posdat #…"),
        # así que sin este mapeo la fila mostraba DOS números distintos con la
        # misma pinta.
        # 🚨 TMT 2026-08-09: *"quiero que me diga es sueldos, si no cómo sé? a
        # mí posdat 133 no me dice nada"*. La etiqueta ya no es sólo el `num`
        # (las provisiones YY/RT no tienen): es el número Y el nombre —
        # "10096 · Hiltexpoy S.A.", "SUELDOS"—. El "#id" queda como último
        # recurso, para un posdatado sin número, sin proveedor y sin concepto.
        npd = (posdat_etiquetas or {}).get(int(rid)) if rid else None
        if npd and str(npd).strip():
            return f"/posdat?id={rid}", f"Posdat {npd}"
        return f"/posdat?id={rid}", f"Posdat #{rid}"
    if t == "xgast":
        return f"/gastos?id={rid}", f"Gasto #{rid}"
    if t == "importacion_pago_mov":
        # 2 movimientos. No tenía rama: salía "Importacion Pago Mov #id" por el
        # fallback genérico. Sin link porque no hay pantalla que muestre ESA
        # fila (mandar a /importaciones sería el bug del 07/08 otra vez).
        return None, "Pago de importación"
    return None, f"{t} #{rid}"


def link_destino(row: dict, factura_numfs: dict | None = None, cheque_nos: dict | None = None,
                 posdat_etiquetas: dict | None = None,
                 banco_nos: dict | None = None) -> tuple[str | None, str]:
    """Mismo concepto para el lado destino."""
    return link_origen(
        {"origen_table": row.get("destino_table"), "origen_id": row.get("destino_id")},
        factura_numfs=factura_numfs, cheque_nos=cheque_nos,
        posdat_etiquetas=posdat_etiquetas, banco_nos=banco_nos,
    )


def op_cuenta_por_retiro(ids) -> dict:
    """id_retiro → concepto de la LÍNEA OP de la que salió el retiro, para que el
    origen del historial diga "OP · <cuenta>" (dueña 2026-07-14). Batch (1 query).
    """
    _ids = list({int(i) for i in (ids or []) if i})
    if not _ids:
        return {}
    try:
        rows = db.fetch_all(
            "SELECT id_retiro, line_key, concepto "
            "  FROM scintela.op_retiro_linea WHERE id_retiro = ANY(%s)",
            (_ids,),
        ) or []
    except Exception:  # noqa: BLE001
        return {}
    out: dict = {}
    for x in rows:
        c = (x.get("concepto") or "").strip()
        lk = x.get("line_key") or ""
        if not c and lk.startswith("P|"):
            p = lk.split("|", 2)
            c = (p[2] if len(p) == 3 else "").strip()
        if x.get("id_retiro") is not None:
            out[int(x["id_retiro"])] = c
    return out


# =====================================================================
# Detalle "uno por uno" de los movimientos consolidados.
# TMT 2026-07-09 (pedido dueña): un mov_doble puede consolidar VARIOS
# items — p.ej. "BAP CT: 6 anticipo(s) → compra #10001". La dueña quiere
# ver cada anticipo por separado, y lo mismo para cualquier movimiento
# cuya metadata liste más de un id (cheques de un depósito en lote,
# cuotas/posdatados de una activación, etc.). Resolvemos los ids que ya
# guarda la metadata contra su tabla y devolvemos líneas legibles.
# =====================================================================

# key en metadata → cómo resolver cada id a una línea. `{IN}` se
# reemplaza por los placeholders %s (uno por id).
_DETALLE_FUENTES = {
    "ids_anticipos": {
        "etiqueta": "Anticipo",
        "sql": (
            "SELECT id_dolares AS id, COALESCE(cta::text,'') AS ref, "
            "COALESCE(concepto,'') AS concepto, importe, COALESCE(st,'') AS extra "
            "FROM scintela.dolares WHERE id_dolares IN ({IN}) ORDER BY id_dolares"
        ),
    },
    "ids_cheques": {
        "etiqueta": "Cheque",
        "sql": (
            "SELECT id_cheque AS id, COALESCE(no_cheque::text,'') AS ref, "
            "COALESCE(concepto,'') AS concepto, importe, '' AS extra "
            "FROM scintela.cheque WHERE id_cheque IN ({IN}) ORDER BY id_cheque"
        ),
    },
    "ids_posdat": {
        "etiqueta": "Cuota",
        "sql": (
            "SELECT id_posdat AS id, COALESCE(num::text,'') AS ref, "
            "COALESCE(concepto,'') AS concepto, importe, "
            "COALESCE(to_char(fechad,'DD/MM/YYYY'),'') AS extra "
            "FROM scintela.posdat WHERE id_posdat IN ({IN}) ORDER BY fechad, id_posdat"
        ),
    },
}


def detalle_consolidado(metadata) -> list[dict]:
    """Devuelve las líneas individuales de un mov_doble que consolidó
    VARIOS items (>=2). [] si no aplica (lo normal).

    Cada línea: {etiqueta, ref, concepto, importe, extra}. Best-effort:
    si una tabla no resuelve un id, igual lo listamos como "#id".
    """
    import json as _json

    meta = metadata
    if isinstance(meta, str):
        try:
            meta = _json.loads(meta)
        except Exception:  # noqa: BLE001
            return []
    if not isinstance(meta, dict):
        return []

    items: list[dict] = []
    for key, cfg in _DETALLE_FUENTES.items():
        crudos = meta.get(key) or []
        ids: list[int] = []
        for x in crudos:
            try:
                ids.append(int(x))
            except (TypeError, ValueError):
                continue
        ids = sorted(set(ids))
        if len(ids) < 2:  # "más de uno"
            continue
        ph = ", ".join(["%s"] * len(ids))
        try:
            rows = db.fetch_all(cfg["sql"].replace("{IN}", ph), tuple(ids)) or []
        except Exception:  # noqa: BLE001
            rows = []
        encontrados = set()
        for r in rows:
            encontrados.add(int(r["id"]))
            items.append(
                {
                    "etiqueta": cfg["etiqueta"],
                    "ref": (str(r.get("ref") or "").strip() or f"#{r['id']}"),
                    "concepto": (r.get("concepto") or "").strip(),
                    "importe": float(r["importe"]) if r.get("importe") is not None else None,
                    "extra": (str(r.get("extra") or "").strip()),
                }
            )
        for _id in ids:
            if _id not in encontrados:
                items.append(
                    {
                        "etiqueta": cfg["etiqueta"],
                        "ref": f"#{_id}",
                        "concepto": "(no encontrado)",
                        "importe": None,
                        "extra": "",
                    }
                )
    return items


# =====================================================================
# Segunda pata (doble asiento) — TMT 2026-07-14 (dueña).
# Algunos movimientos tienen DOS efectos reales pero su mov_doble es una
# AUTO-REFERENCIA (origen == destino), así que el historial mostraba una
# sola pata. Ej. arquetípico: un RETIRO OP (1) crea el retiro a accionistas
# (banco USA) Y (2) imputa a la línea OP bajando su restante (posdat OP). El
# ↺ ya revierte AMBAS — sólo faltaba VERLAS.
#
# Este resolver hace lookup por origen_id contra la tabla del efecto
# secundario (así funciona también con las filas HISTÓRICAS, sin depender de
# metadata nueva) y devuelve la 2ª pata para mostrarla en el historial y en
# el cartel de reverso. Best-effort: nunca levanta (defensivo a migraciones
# sin correr / tablas PC-only ausentes).
# =====================================================================


def _segunda_pata_retiro_op(row) -> dict | None:
    """2ª pata de un retiro OP: la imputación a la línea OP (op_retiro_linea)
    que baja el restante — y, si es del esquema nuevo, sube el posdat OP."""
    id_retiro = row.get("origen_id")
    if not id_retiro:
        return None
    imp = None
    try:
        imp = db.fetch_one(
            "SELECT id_op_retiro_linea, line_key, monto, "
            "       COALESCE(bajo_posdat, FALSE) AS bajo_posdat, fecha "
            "  FROM scintela.op_retiro_linea "
            " WHERE id_retiro = %s "
            " ORDER BY id_op_retiro_linea DESC LIMIT 1",
            (int(id_retiro),),
        )
    except Exception:  # noqa: BLE001
        # columna bajo_posdat puede no existir (mig 0111 sin correr).
        try:
            imp = db.fetch_one(
                "SELECT id_op_retiro_linea, line_key, monto, fecha "
                "  FROM scintela.op_retiro_linea "
                " WHERE id_retiro = %s "
                " ORDER BY id_op_retiro_linea DESC LIMIT 1",
                (int(id_retiro),),
            )
        except Exception:  # noqa: BLE001
            return None
    if not imp:
        return None
    monto = round(float(imp.get("monto") or 0), 2)
    line_key = imp.get("line_key") or ""
    ref, concepto_op = "", ""
    # line_key = 'P|num|concepto' → mostrar algo legible.
    if line_key.startswith("P|"):
        parts = line_key.split("|", 2)
        if len(parts) == 3:
            ref = f"OP #{parts[1]}"
            concepto_op = parts[2]
    concepto = "baja el restante de la línea OP"
    if imp.get("bajo_posdat"):
        concepto += " (sube el posdat OP → baja el crédito)"
    if concepto_op:
        concepto += f" — {concepto_op}"
    return {
        "nota": ("Este movimiento tiene 2 patas — el ↺ revierte AMBAS: borra "
                 "el retiro y la imputación (la línea OP vuelve a subir su "
                 "restante)."),
        "lineas": [
            {
                "etiqueta": "Imputado a línea",
                "ref": ref or (line_key[:24] if line_key else "OP"),
                "concepto": concepto,
                "importe": monto,
                "extra": "",
            }
        ],
    }


def _segunda_pata_gasto_a_posdat(row) -> dict | None:
    """2ª pata de un gasto a crédito: la línea de crédito posdat (el pasivo).
    En el legacy el xgast suele ser auto-contenido (la posdat la crea el
    reconcile después); igual explicamos la deuda para que se vean las dos."""
    id_xgast = row.get("origen_id")
    if not id_xgast:
        return None
    try:
        g = db.fetch_one(
            "SELECT prov, num, importe, "
            "       COALESCE(to_char(fechad,'DD/MM/YYYY'), '') AS fechad "
            "  FROM scintela.xgast WHERE id_xgast = %s",
            (int(id_xgast),),
        )
    except Exception:  # noqa: BLE001
        return None
    if not g:
        return None
    importe = round(float(g.get("importe") or 0), 2)
    fechad = g.get("fechad") or ""
    ref = ""
    if (g.get("prov") or "").strip() and g.get("num") is not None:
        try:
            posd = db.fetch_one(
                "SELECT num FROM scintela.posdat "
                " WHERE prov = %s AND num = %s "
                "   AND (anulada IS NOT TRUE OR anulada IS NULL) "
                " ORDER BY id_posdat LIMIT 1",
                (g["prov"], g["num"]),
            )
        except Exception:  # noqa: BLE001
            posd = None
        if posd and posd.get("num") is not None:
            ref = f"#{posd['num']}"
    return {
        "nota": ("Este movimiento tiene 2 patas — el ↺ revierte AMBAS: anula "
                 "el gasto y su deuda posdat."),
        "lineas": [
            {
                "etiqueta": "Línea de crédito posdat",
                "ref": ref or "(pendiente)",
                "concepto": "deuda posdat (pasivo) — el gasto queda pendiente de pago",
                "importe": importe,
                "extra": (f"vence {fechad}" if fechad else ""),
            }
        ],
    }


# tipo → resolver del efecto secundario (self-ref con 2ª pata invisible).
_SEGUNDA_PATA = {
    "retiro_op": _segunda_pata_retiro_op,
    "gasto_a_posdat": _segunda_pata_gasto_a_posdat,
}


def segunda_pata(row) -> dict | None:
    """Devuelve la 2ª pata (efecto secundario) de un movimiento cuyo
    mov_doble es auto-referencia, o None si no aplica.

    Estructura: {"nota": str, "lineas": [{etiqueta, ref, concepto, importe,
    extra}, ...]}. Best-effort: nunca levanta."""
    fn = _SEGUNDA_PATA.get((row or {}).get("tipo") or "")
    if not fn:
        return None
    try:
        return fn(row)
    except Exception:  # noqa: BLE001
        return None
