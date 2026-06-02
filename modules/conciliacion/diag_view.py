"""Endpoint diagnóstico /admin/diag-pendientes-banco — TMT 2026-06-02.

La dueña preguntó: '548 pendientes para conciliar, no estarán repetidos?'.
El dedupe por número de documento que pusimos en mig 0062 corre al subir
extracto NUEVO — no limpia lo que ya estaba en banco_historicos_pendientes
del backfill viejo (migs 0056-0058).

Este endpoint cuenta cuántas filas pendientes hay duplicadas por
(no_banco, documento) y muestra ejemplos. Si hay muchos duplicados,
podemos limpiarlos.
"""
from __future__ import annotations

import logging

import json

from flask import Blueprint, jsonify, request, redirect, url_for, flash

import db as _db
from auth import requiere_login, requiere_permiso


def _usuario_actual() -> str:
    """Wrapper lazy para evitar circular imports cuando el módulo
    diag_view se carga ANTES de modules.conciliacion.views."""
    try:
        from modules.conciliacion.views import _usuario_actual as _ua
        return _ua()
    except Exception:
        from flask import session
        return (session.get("usuario") or "web")[:50]

_LOG = logging.getLogger("programa_core.conciliacion.diag")
_BANCO_PICHINCHA = 10

bp = Blueprint(
    "conciliacion_diag",
    __name__,
    url_prefix="/admin/diag-pendientes-banco",
)


@bp.route("/", methods=["GET"])
@requiere_login
@requiere_permiso("admin_dbase.ver")
def diagnose():
    """Cuenta pendientes totales + duplicados por (no_banco, documento)."""
    out: dict = {"ok": True, "no_banco": _BANCO_PICHINCHA}

    # 1. Total pendientes (no conciliados).
    try:
        row = _db.fetch_one(
            """
            SELECT COUNT(*) AS n
              FROM scintela.banco_historicos_pendientes
             WHERE no_banco = %s AND conciliado_en IS NULL
            """,
            (_BANCO_PICHINCHA,),
        )
        out["pendientes_totales"] = int(row["n"]) if row else 0
    except Exception as e:
        out["pendientes_totales"] = None
        out["error_total"] = str(e)

    # 2. Pendientes con documento vacío (no se pueden dedupear).
    try:
        row = _db.fetch_one(
            """
            SELECT COUNT(*) AS n
              FROM scintela.banco_historicos_pendientes
             WHERE no_banco = %s
               AND conciliado_en IS NULL
               AND (documento IS NULL OR documento = '')
            """,
            (_BANCO_PICHINCHA,),
        )
        out["pendientes_sin_documento"] = int(row["n"]) if row else 0
    except Exception as e:
        out["pendientes_sin_documento"] = None
        out["error_sin_doc"] = str(e)

    # 2.5. Cuántas filas marcó la mig 0063 como conciliadas (post-deploy).
    try:
        row = _db.fetch_one(
            """
            SELECT COUNT(*) AS n
              FROM scintela.banco_historicos_pendientes
             WHERE no_banco = %s AND conciliado_por = 'mig-0063-dedupe'
            """,
            (_BANCO_PICHINCHA,),
        )
        out["mig_0063_dedupeadas"] = int(row["n"]) if row else 0
    except Exception as e:
        out["error_mig0063"] = str(e)

    # 2.6. Duplicados REALES por firma estricta (la que usa mig 0063):
    # (no_banco, documento, tipo, monto, fecha). Si esto da 0, no hay nada
    # para dedupear y los 548 son pendientes únicos reales.
    try:
        row = _db.fetch_one(
            """
            SELECT COUNT(*) AS grupos,
                   COALESCE(SUM(extras), 0) AS filas_extra
              FROM (
                SELECT COUNT(*) - 1 AS extras
                  FROM scintela.banco_historicos_pendientes
                 WHERE no_banco = %s
                   AND conciliado_en IS NULL
                   AND documento IS NOT NULL AND documento <> ''
                 GROUP BY no_banco, documento, tipo, monto, fecha
                HAVING COUNT(*) > 1
              ) t
            """,
            (_BANCO_PICHINCHA,),
        )
        out["dup_estrictos_grupos"] = int(row["grupos"]) if row else 0
        out["dup_estrictos_filas_extra"] = int(row["filas_extra"]) if row else 0
    except Exception as e:
        out["error_dup_estrictos"] = str(e)

    # 3. Documentos duplicados — grupos de (no_banco, documento) con >1 fila pendiente.
    # TMT 2026-06-02 dueña: 'puede ser que esos no esten duplicados entonces?'
    # Ahora desglosamos por tipo: si todas las ocurrencias tienen el mismo
    # tipo (todos C o todos D), es duplicado real. Si tienen tipos mezclados
    # (C+D), es un par legítimo (cargo + reverso, neto $0) — NO dedupear.
    try:
        rows = _db.fetch_all(
            """
            SELECT documento,
                   COUNT(*) AS n,
                   SUM(monto) AS suma_monto,
                   MIN(fecha) AS fecha_min, MAX(fecha) AS fecha_max,
                   COUNT(*) FILTER (WHERE tipo = 'C') AS n_creditos,
                   COUNT(*) FILTER (WHERE tipo = 'D') AS n_debitos,
                   ARRAY_AGG(DISTINCT tipo) AS tipos
              FROM scintela.banco_historicos_pendientes
             WHERE no_banco = %s
               AND conciliado_en IS NULL
               AND documento IS NOT NULL AND documento <> ''
             GROUP BY documento
            HAVING COUNT(*) > 1
             ORDER BY n DESC, documento ASC
             LIMIT 30
            """,
            (_BANCO_PICHINCHA,),
        ) or []
        out["docs_duplicados_top30"] = [
            {
                "documento": r["documento"],
                "ocurrencias": int(r["n"]),
                "suma_monto": float(r["suma_monto"] or 0),
                "fecha_min": str(r["fecha_min"]) if r.get("fecha_min") else None,
                "fecha_max": str(r["fecha_max"]) if r.get("fecha_max") else None,
                "n_creditos": int(r["n_creditos"] or 0),
                "n_debitos": int(r["n_debitos"] or 0),
                "tipos": list(r.get("tipos") or []),
                "es_duplicado_real": (
                    int(r["n_creditos"] or 0) == 0 or int(r["n_debitos"] or 0) == 0
                ),
            }
            for r in rows
        ]
        # Conteo global desglosando duplicados reales (mismo tipo) vs pares (C+D).
        row = _db.fetch_one(
            """
            SELECT COUNT(*) AS grupos_total,
                   COUNT(*) FILTER (WHERE n_c = 0 OR n_d = 0) AS grupos_dup_real,
                   COUNT(*) FILTER (WHERE n_c > 0 AND n_d > 0) AS grupos_par_cd,
                   COALESCE(SUM(extras) FILTER (WHERE n_c = 0 OR n_d = 0), 0) AS filas_extra_dup_real,
                   COALESCE(SUM(extras) FILTER (WHERE n_c > 0 AND n_d > 0), 0) AS filas_extra_par_cd
              FROM (
                SELECT COUNT(*) - 1 AS extras,
                       COUNT(*) FILTER (WHERE tipo='C') AS n_c,
                       COUNT(*) FILTER (WHERE tipo='D') AS n_d
                  FROM scintela.banco_historicos_pendientes
                 WHERE no_banco = %s
                   AND conciliado_en IS NULL
                   AND documento IS NOT NULL AND documento <> ''
                 GROUP BY documento
                HAVING COUNT(*) > 1
              ) t
            """,
            (_BANCO_PICHINCHA,),
        )
        out["grupos_duplicados_total"] = int(row["grupos_total"]) if row else 0
        out["grupos_dup_mismo_tipo"] = int(row["grupos_dup_real"]) if row else 0
        out["grupos_par_C_D"] = int(row["grupos_par_cd"]) if row else 0
        out["filas_extra_dup_mismo_tipo"] = int(row["filas_extra_dup_real"]) if row else 0
        out["filas_extra_par_C_D"] = int(row["filas_extra_par_cd"]) if row else 0
        # Backward compat con clave vieja.
        out["filas_extra_duplicadas"] = out["filas_extra_dup_mismo_tipo"]
        out["grupos_duplicados"] = out["grupos_dup_mismo_tipo"]
    except Exception as e:
        out["error_dup"] = str(e)

    # 4. Duplicados cruzados extracto-de-sesión vs histos: docs que están
    #    en el payload de la sesión abierta Y EN banco_historicos_pendientes.
    try:
        from modules.conciliacion import sesion as _ses
        s = _ses.sesion_abierta(_BANCO_PICHINCHA)
        if s:
            movs = _ses.cargar_movs(s)
            docs_sesion = {
                (m.documento or "").strip().upper()
                for m in movs if m.documento
            }
            docs_sesion.discard("")
            if docs_sesion:
                rows = _db.fetch_all(
                    """
                    SELECT documento, COUNT(*) AS n
                      FROM scintela.banco_historicos_pendientes
                     WHERE no_banco = %s
                       AND conciliado_en IS NULL
                       AND UPPER(documento) = ANY(%s::text[])
                     GROUP BY documento
                     LIMIT 30
                    """,
                    (_BANCO_PICHINCHA, list(docs_sesion)),
                ) or []
                out["docs_en_sesion_y_histos"] = [
                    {"documento": r["documento"], "ocurrencias": int(r["n"])}
                    for r in rows
                ]
                out["n_docs_solapados"] = len(rows)
            else:
                out["docs_en_sesion_y_histos"] = []
        else:
            out["sesion_abierta"] = False
    except Exception as e:
        out["error_solapados"] = str(e)

    return jsonify(out)


@bp.route("/reset-y-cargar", methods=["POST"])
@requiere_login
@requiere_permiso("admin_dbase.ver")
def reset_y_cargar():
    """Reemplaza TODO el historial de pendientes banco con la lista
    provista en el body JSON. Operación atómica:

      1. DELETE banco_historicos_pendientes pendientes (WHERE conciliado_en IS NULL).
      2. Vacía el extracto_payload de la sesión abierta del banco.
      3. INSERTa todas las filas del payload.

    Body esperado:
      {"records": [{fecha, concepto, documento, monto, tipo, detalle}, ...]}

    Devuelve contadores. La dueña pidió esto explícitamente:
    'borres el historial y cargues estos movimientos como historial'.
    """
    body = request.get_json(silent=True) or {}
    records = body.get("records") or []
    if not isinstance(records, list) or not records:
        return jsonify({"ok": False, "error": "body.records vacío o inválido"}), 400

    no_banco = _BANCO_PICHINCHA
    out = {"ok": True, "no_banco": no_banco}

    try:
        with _db.tx() as conn:
            # 1) Borrar pendientes.
            n_del = _db.execute(
                """
                DELETE FROM scintela.banco_historicos_pendientes
                 WHERE no_banco = %s AND conciliado_en IS NULL
                """,
                (no_banco,),
                conn=conn,
            ) or 0
            out["histos_borrados"] = int(n_del)

            # 2) Vaciar payload de la sesión abierta.
            from modules.conciliacion import sesion as _ses
            sesion = _ses.sesion_abierta(no_banco)
            if sesion:
                _db.execute(
                    """
                    UPDATE scintela.banco_conciliacion_sesion
                       SET extracto_payload = '[]'::jsonb
                     WHERE id = %s
                    """,
                    (int(sesion["id"]),),
                    conn=conn,
                )
                out["sesion_payload_vaciada"] = int(sesion["id"])
            else:
                out["sesion_payload_vaciada"] = None

            # 3) Insertar las filas nuevas. Detectar si la columna `codigo`
            # existe — la mig 0064 puede no estar aplicada en prod.
            tiene_codigo = False
            try:
                r_col = _db.fetch_one(
                    """
                    SELECT 1 FROM information_schema.columns
                     WHERE table_schema = 'scintela'
                       AND table_name = 'banco_historicos_pendientes'
                       AND column_name = 'codigo'
                    """,
                    conn=conn,
                )
                tiene_codigo = bool(r_col)
            except Exception:
                tiene_codigo = False
            out["tiene_codigo_col"] = tiene_codigo

            if tiene_codigo:
                sql = """
                    INSERT INTO scintela.banco_historicos_pendientes
                        (no_banco, fecha, concepto, documento, monto, tipo,
                         oficina, detalle, fuente, creado_por, codigo)
                    VALUES (%s, %s, %s, %s, %s::numeric, %s, %s, %s, %s, %s, %s)
                """
            else:
                sql = """
                    INSERT INTO scintela.banco_historicos_pendientes
                        (no_banco, fecha, concepto, documento, monto, tipo,
                         oficina, detalle, fuente, creado_por)
                    VALUES (%s, %s, %s, %s, %s::numeric, %s, %s, %s, %s, %s)
                """

            n_ins = 0
            errores = []
            for i, r in enumerate(records):
                try:
                    params = [
                        no_banco,
                        r.get("fecha"),
                        (r.get("concepto") or "")[:120],
                        (r.get("documento") or "")[:40],
                        str(r.get("monto") or 0),
                        (r.get("tipo") or "C")[:2],
                        "",  # oficina
                        (r.get("detalle") or "")[:30],
                        "feb2023-xlsx-2026-06-02",
                        _usuario_actual()[:50],
                    ]
                    if tiene_codigo:
                        params.append((r.get("codigo") or "")[:20])
                    _db.execute(sql, tuple(params), conn=conn)
                    n_ins += 1
                except Exception as e:
                    errores.append({"i": i, "doc": r.get("documento"), "err": str(e)[:120]})
            out["insertados"] = n_ins
            if errores:
                out["errores"] = errores[:20]
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)[:200]}), 500

    return jsonify(out)


@bp.route("/borrar-conciliados-sesion", methods=["POST"])
@requiere_login
@requiere_permiso("admin_dbase.ver")
def borrar_conciliados_sesion():
    """Borra todos los matches creados en la ventana de la sesión abierta
    actual. Resetea stat='*' en las txs PC asociadas (excepto las que
    vinieron del DBF original) y conciliado_en en los histos linkeados.

    TMT 2026-06-02 dueña: 'borremos estos 36, que son de otra sesion y
    no sirven'.
    """
    no_banco = _BANCO_PICHINCHA
    from modules.conciliacion import sesion as _ses
    sesion = _ses.sesion_abierta(no_banco)
    if not sesion:
        return jsonify({"ok": False, "error": "no hay sesión abierta"}), 400

    abierta_en = sesion.get("abierta_en")
    out = {"ok": True, "sesion_id": sesion["id"]}

    try:
        with _db.tx() as conn:
            # 1) Capturar IDs de matches afectados (para reset histos / stat).
            rows = _db.fetch_all(
                """
                SELECT id, id_transaccion
                  FROM scintela.banco_conciliacion_match
                 WHERE no_banco = %s
                   AND creado_en >= %s
                """,
                (no_banco, abierta_en),
                conn=conn,
            ) or []
            match_ids = [r["id"] for r in rows]
            tx_ids = [r["id_transaccion"] for r in rows if r.get("id_transaccion")]
            out["matches_encontrados"] = len(match_ids)
            out["txs_afectadas"] = len(set(tx_ids))

            # 2) Reset conciliado_en en histos linkeados (vuelven a pendientes).
            if match_ids:
                n_hist = _db.execute(
                    """
                    UPDATE scintela.banco_historicos_pendientes
                       SET conciliado_en = NULL,
                           conciliado_match_id = NULL,
                           conciliado_por = NULL
                     WHERE conciliado_match_id = ANY(%s)
                    """,
                    (match_ids,),
                    conn=conn,
                ) or 0
                out["histos_revertidos"] = int(n_hist)

            # 3) Reset stat='*' en txs PC (excepto las que vinieron del DBF
            # original — esas las mantiene el sync con dBase).
            if tx_ids:
                n_stat = _db.execute(
                    """
                    UPDATE scintela.transacciones_bancarias
                       SET stat = NULL
                     WHERE id_transaccion = ANY(%s)
                       AND no_banco = %s
                       AND usuario_crea NOT IN ('dbf-import', 'asinfo-backfill')
                    """,
                    (list(set(tx_ids)), no_banco),
                    conn=conn,
                ) or 0
                out["stat_reset"] = int(n_stat)

            # 4) Hard-delete de los matches.
            if match_ids:
                n_del = _db.execute(
                    """
                    DELETE FROM scintela.banco_conciliacion_match
                     WHERE id = ANY(%s)
                    """,
                    (match_ids,),
                    conn=conn,
                ) or 0
                out["matches_borrados"] = int(n_del)

            # 5) Reset stat='*' orfanos: PCs marcados conciliados pero sin
            # match activo, que NO sean del dbf-import original. Esos son
            # residuos de conciliaciones N:M con código viejo.
            try:
                n_orphan = _db.execute(
                    """
                    UPDATE scintela.transacciones_bancarias tb
                       SET stat = NULL
                     WHERE tb.no_banco = %s
                       AND TRIM(COALESCE(tb.stat, '')) = '*'
                       AND tb.usuario_crea NOT IN ('dbf-import', 'asinfo-backfill')
                       AND NOT EXISTS (
                           SELECT 1 FROM scintela.banco_conciliacion_match m
                            WHERE m.id_transaccion = tb.id_transaccion
                              AND m.deshecho_en IS NULL
                       )
                    """,
                    (no_banco,),
                    conn=conn,
                ) or 0
                out["stat_orphans_limpiados"] = int(n_orphan)
            except Exception as e:
                _LOG.warning("limpiar stat orfans falló: %s", e)

            # 6) Reset contador de la sesión.
            _db.execute(
                """
                UPDATE scintela.banco_conciliacion_sesion
                   SET matches_hechos = 0
                 WHERE id = %s
                """,
                (int(sesion["id"]),),
                conn=conn,
            )
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)[:200]}), 500

    return jsonify(out)


@bp.route("/match-potencial", methods=["GET"])
@requiere_login
@requiere_permiso("admin_dbase.ver")
def match_potencial():
    """Para cada pendiente banco (no conciliado), buscar si existe una
    transacción PC con numreferencia o no_cheque que matchee el documento.

    TMT 2026-06-02 dueña: 'transferencias por numero de doc no encuentra
    ninguna, podes aunque sea buscar si para el pasado hubiera matcheado?'.

    Considera TODO el universo de transacciones_bancarias (incluyendo ya
    conciliadas, para saber si la estrategia PASS 0 tiene fundamento).
    """
    out = {"ok": True, "no_banco": _BANCO_PICHINCHA}

    # 1) Match contra numreferencia y no_cheque (vía chequextransaccion).
    # no_cheque NO está en transacciones_bancarias — vive en scintela.cheque
    # ligado vía chequextransaccion. numreferencia es INTEGER en algunos
    # casos → casteamos a text para comparar con documento.
    try:
        rows = _db.fetch_all(
            """
            WITH pend AS (
                SELECT documento, monto, tipo, fecha
                  FROM scintela.banco_historicos_pendientes
                 WHERE no_banco = %s
                   AND conciliado_en IS NULL
                   AND documento IS NOT NULL AND documento <> ''
            ),
            txs AS (
                SELECT id_transaccion,
                       CAST(numreferencia AS TEXT) AS numref,
                       documento AS doc_pc, importe, fecha, stat
                  FROM scintela.transacciones_bancarias
                 WHERE no_banco = %s
            ),
            cheques AS (
                SELECT DISTINCT CAST(ch.no_cheque AS TEXT) AS no_cheque,
                                cxt.id_transaccion
                  FROM scintela.cheque ch
                  JOIN scintela.chequextransaccion cxt
                    ON cxt.id_cheque = ch.id_cheque
                 WHERE ch.no_cheque IS NOT NULL
            )
            SELECT
                COUNT(*) FILTER (WHERE EXISTS (
                    SELECT 1 FROM txs t WHERE t.numref = pend.documento
                )) AS match_por_numref,
                COUNT(*) FILTER (WHERE EXISTS (
                    SELECT 1 FROM cheques c WHERE c.no_cheque = pend.documento
                )) AS match_por_no_cheque,
                COUNT(*) FILTER (WHERE EXISTS (
                    SELECT 1 FROM txs t WHERE t.doc_pc = pend.documento
                )) AS match_por_doc_pc,
                COUNT(*) FILTER (WHERE EXISTS (
                    SELECT 1 FROM txs t WHERE t.numref = pend.documento
                    UNION
                    SELECT 1 FROM cheques c WHERE c.no_cheque = pend.documento
                )) AS match_cualquiera,
                COUNT(*) AS pendientes_totales
              FROM pend
            """,
            (_BANCO_PICHINCHA, _BANCO_PICHINCHA),
        ) or []
        if rows:
            r = rows[0]
            out["pendientes_totales"] = int(r.get("pendientes_totales") or 0)
            out["match_por_numreferencia"] = int(r.get("match_por_numref") or 0)
            out["match_por_no_cheque"] = int(r.get("match_por_no_cheque") or 0)
            out["match_por_documento_pc"] = int(r.get("match_por_doc_pc") or 0)
            out["match_cualquiera"] = int(r.get("match_cualquiera") or 0)
    except Exception as e:
        out["error_agregado"] = str(e)

    # 2) Top 15 ejemplos concretos de matches potenciales.
    try:
        ejemplos = _db.fetch_all(
            """
            SELECT h.documento, h.monto, h.fecha AS fecha_banco, h.tipo,
                   t.id_transaccion,
                   CAST(t.numreferencia AS TEXT) AS numref_pc,
                   t.fecha AS fecha_pc, t.importe, t.documento AS doc_pc,
                   t.stat,
                   (CASE WHEN TRIM(COALESCE(t.stat,'')) = '*' THEN 'conciliado_dbase'
                         ELSE 'pendiente_pc' END) AS estado_pc
              FROM scintela.banco_historicos_pendientes h
              JOIN scintela.transacciones_bancarias t
                ON t.no_banco = h.no_banco
               AND CAST(t.numreferencia AS TEXT) = h.documento
             WHERE h.no_banco = %s
               AND h.conciliado_en IS NULL
               AND h.documento IS NOT NULL AND h.documento <> ''
             ORDER BY h.fecha DESC, h.documento
             LIMIT 15
            """,
            (_BANCO_PICHINCHA,),
        ) or []
        out["ejemplos_match_potencial"] = [
            {
                "documento": e.get("documento"),
                "monto_banco": float(e.get("monto") or 0),
                "fecha_banco": str(e.get("fecha_banco")) if e.get("fecha_banco") else None,
                "tipo": e.get("tipo"),
                "id_transaccion": e.get("id_transaccion"),
                "numref_pc": e.get("numref_pc"),
                "doc_pc": e.get("doc_pc"),
                "fecha_pc": str(e.get("fecha_pc")) if e.get("fecha_pc") else None,
                "importe_pc": float(e.get("importe") or 0),
                "estado_pc": e.get("estado_pc"),
            }
            for e in ejemplos
        ]
    except Exception as e:
        out["error_ejemplos"] = str(e)

    # 3) Match por (monto, fecha) — fallback más relajado.
    try:
        row = _db.fetch_one(
            """
            SELECT COUNT(*) AS n
              FROM scintela.banco_historicos_pendientes h
             WHERE h.no_banco = %s
               AND h.conciliado_en IS NULL
               AND EXISTS (
                   SELECT 1 FROM scintela.transacciones_bancarias t
                    WHERE t.no_banco = h.no_banco
                      AND ABS(t.importe) = h.monto
                      AND t.fecha BETWEEN h.fecha - INTERVAL '3 days'
                                       AND h.fecha + INTERVAL '3 days'
               )
            """,
            (_BANCO_PICHINCHA,),
        )
        out["match_por_monto_fecha_3d"] = int(row["n"]) if row else 0
    except Exception as e:
        out["error_monto_fecha"] = str(e)

    return jsonify(out)


@bp.route("/cleanup-sesion", methods=["GET", "POST"])
@requiere_login
@requiere_permiso("admin_dbase.ver")
def cleanup_sesion_payload():
    """Quita del payload de la sesión abierta los documentos que YA están
    en banco_historicos_pendientes (no conciliados).

    TMT 2026-06-02: el dedupe de mig 0062 protege uploads FUTUROS. Esta
    acción es una limpieza one-shot del payload ya cargado, para corregir
    el solapamiento histórico señalado por `docs_en_sesion_y_histos`.

    GET = dry-run (devuelve cuántos se sacarían).
    POST = ejecuta el cleanup (UPDATE del payload jsonb).
    """
    from modules.conciliacion import sesion as _ses

    s = _ses.sesion_abierta(_BANCO_PICHINCHA)
    if not s:
        return jsonify({"ok": False, "error": "no hay sesión abierta"}), 400

    # 1) Firmas EN HISTOS (no usar _firmas_ya_conocidas porque incluye
    # el propio payload de la sesión → cada fila se encontraría como
    # duplicada de sí misma y removeríamos TODO el payload).
    # TMT 2026-06-02 fix crítico: aquí solo queremos histos + matches
    # activos, NO el payload actual.
    sigs_histos: set[tuple] = set()
    try:
        rows = _db.fetch_all(
            """
            SELECT documento, fecha, tipo, monto
              FROM scintela.banco_historicos_pendientes
             WHERE no_banco = %s
               AND documento IS NOT NULL AND documento <> ''
               AND conciliado_en IS NULL
            """,
            (_BANCO_PICHINCHA,),
        ) or []
        for r in rows:
            sigs_histos.add(_ses._firma_mov(
                r.get("documento"), "",
                r.get("tipo"), r.get("monto"), r.get("fecha"),
            ))
    except Exception as e:
        return jsonify({"ok": False, "error": f"query histos falló: {e}"}), 500

    # 2) Recorrer payload de la sesión y filtrar por firma.
    movs = _ses.cargar_movs(s)
    keep: list = []
    removed: list[str] = []
    for m in movs:
        if not m.documento:
            keep.append(m)
            continue
        sig = _ses._firma_mov(m.documento, getattr(m, "codigo", ""),
                              m.tipo, m.monto, m.fecha)
        if sig in sigs_histos:
            removed.append(f"{m.documento}/{m.codigo}/{m.tipo}/{m.monto}")
            continue
        keep.append(m)

    result = {
        "ok": True,
        "sesion_id": s["id"],
        "payload_antes": len(movs),
        "payload_despues": len(keep),
        "removidos": len(removed),
        "ejemplos_removidos": removed[:20],
    }

    if request.method == "GET":
        result["modo"] = "dry-run"
        result["nota"] = "POST a este mismo endpoint para ejecutar"
        return jsonify(result)

    # Ejecutar el cleanup — UPDATE del payload jsonb.
    new_payload = json.dumps([_ses._mov_to_dict(m) for m in keep])
    try:
        _db.execute(
            """
            UPDATE scintela.banco_conciliacion_sesion
               SET extracto_payload = %s::jsonb
             WHERE id = %s
            """,
            (new_payload, int(s["id"])),
        )
        result["modo"] = "ejecutado"
    except Exception as e:
        return jsonify({"ok": False, "error": f"update falló: {e}"}), 500

    return jsonify(result)
