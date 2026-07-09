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
    "bap_anticipo_a_compra":    "BAP: anticipo USD → Compra",
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

    return db.fetch_all(
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
         LIMIT %(limite)s
        """,
        {
            "desde": desde or None, "hasta": hasta or None,
            "tipo": tipo or None, "tipo_like": (tipo or "") + "%" if tipo else None,
            "estado": estado or None,
            "q": q or None, "qlike": f"%{q}%" if q else None,
            "usuario": usuario or None,
            "origenes_permitidos": list(origenes_permitidos) if origenes_permitidos else None,
            "limite": int(limite),
        },
    ) or []


def _filtro_fechas_sql():
    """SQL fragment común para el WHERE del UNION (caja, banco, mov_doble)."""
    return (
        " WHERE (%(desde)s::date IS NULL OR fecha_operacion >= %(desde)s::date) "
        "   AND (%(hasta)s::date IS NULL OR fecha_operacion <= %(hasta)s::date) "
    )


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


def link_origen(row: dict, factura_numfs: dict | None = None, cheque_nos: dict | None = None) -> tuple[str | None, str]:
    """Devuelve (url, etiqueta) para el lado origen del mov.

    Tamara 2026-05-23: los links de factura/cheque deben usar el numero
    REAL (numf/no_cheque) en la URL, no el id interno. Cuando el caller
    pasa los mappings, los usamos. Sino caemos al id interno (legacy).
    """
    t = row.get("origen_table")
    rid = row.get("origen_id")
    if not rid:
        return None, ""
    if t == "caja":
        return f"/caja#id-{rid}", f"Caja #{rid}"
    if t == "transacciones_bancarias":
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
    if t == "capital":
        return "/capital", f"Capital #{rid}"
    if t == "retiros":
        return "/capital?filtro=retiros", f"Retiro #{rid}"
    if t == "dolares":
        return "/dolares?cta=", f"USD #{rid}"
    if t == "posdat":
        return "/proveedores", f"Posdat #{rid}"
    if t == "xgast":
        return "/gastos", f"Gasto #{rid}"
    return None, f"{t} #{rid}"


def link_destino(row: dict, factura_numfs: dict | None = None, cheque_nos: dict | None = None) -> tuple[str | None, str]:
    """Mismo concepto para el lado destino."""
    return link_origen(
        {"origen_table": row.get("destino_table"), "origen_id": row.get("destino_id")},
        factura_numfs=factura_numfs, cheque_nos=cheque_nos,
    )


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
