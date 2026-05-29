"""Timeline persistente de cambios en la conciliación bancaria.

Muestra en orden cronológico inverso:
  ✓ Match creado          (banco_conciliacion_match.creado_en)
  ↶ Match deshecho        (banco_conciliacion_match.deshecho_en)
  ✓ Histórico conciliado  (banco_historicos_pendientes.conciliado_en)

Default: últimos 14 días, todos los bancos.
Filtros: ?desde / ?hasta / ?usuario / ?no_banco.

TMT 2026-05-28 dueña: "que se quede, si yo vuelvo a conciliar manana
que me vaya diciendo cuales son los cambios. esta muy dificil seguir
lo que hacemos".
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

from flask import render_template, request

import db as _db
from auth import requiere_login, requiere_permiso
from modules.conciliacion.views import conciliacion_bp

_LOG = logging.getLogger("programa_core.conciliacion.cambios")


def _parse_fecha(s, default):
    if not s:
        return default
    try:
        return date.fromisoformat(s[:10])
    except (ValueError, AttributeError, TypeError):
        return default


@conciliacion_bp.route("/cambios", methods=["GET"])
@requiere_login
@requiere_permiso("bancos.conciliar")
def cambios_timeline():
    hoy = date.today()
    desde = _parse_fecha(request.args.get("desde"), hoy - timedelta(days=14))
    hasta = _parse_fecha(request.args.get("hasta"), hoy)
    usuario_filter = (request.args.get("usuario") or "").strip()
    no_banco_raw = (request.args.get("no_banco") or "").strip()
    try:
        no_banco = int(no_banco_raw) if no_banco_raw else None
    except (TypeError, ValueError):
        no_banco = None

    # ─── 1) Matches creados ───
    # Incluimos deshecho_en para que el template sepa si todavía está activo
    # (botón "↶ desmatch" solo si está vivo).
    # saldo_despues = snapshot capturado en el momento del evento.
    sql_creados = """
        SELECT
          m.creado_en       AS ts,
          'match_creado'    AS accion,
          m.usuario         AS usuario,
          m.no_banco        AS no_banco,
          m.id              AS match_id,
          m.id_transaccion  AS id_tx,
          m.real_fecha      AS fecha,
          m.real_documento  AS doc,
          m.real_concepto   AS concepto,
          m.real_monto      AS monto,
          m.real_tipo       AS tipo,
          m.estado          AS estado,
          m.deshecho_en     AS deshecho_en,
          (SELECT s.saldo_conc
             FROM scintela.banco_saldo_conc_snapshot s
            WHERE s.no_banco = m.no_banco
              AND s.evento_tipo = 'match_creado'
              AND s.evento_ref = m.id::text
            ORDER BY s.creado_en DESC LIMIT 1) AS saldo_despues
        FROM scintela.banco_conciliacion_match m
        WHERE m.creado_en::date BETWEEN %(desde)s AND %(hasta)s
    """
    params_creados: dict = {"desde": desde, "hasta": hasta}
    if no_banco is not None:
        sql_creados += " AND m.no_banco = %(no_banco)s"
        params_creados["no_banco"] = no_banco
    if usuario_filter:
        sql_creados += " AND UPPER(TRIM(m.usuario)) LIKE UPPER(%(usu)s)"
        params_creados["usu"] = f"%{usuario_filter}%"

    # ─── 2) Matches deshechos ───
    sql_deshechos = """
        SELECT
          m.deshecho_en     AS ts,
          'match_deshecho'  AS accion,
          m.deshecho_por    AS usuario,
          m.no_banco        AS no_banco,
          m.id              AS match_id,
          m.id_transaccion  AS id_tx,
          m.real_fecha      AS fecha,
          m.real_documento  AS doc,
          m.real_concepto   AS concepto,
          m.real_monto      AS monto,
          m.real_tipo       AS tipo,
          m.estado          AS estado,
          (SELECT s.saldo_conc
             FROM scintela.banco_saldo_conc_snapshot s
            WHERE s.no_banco = m.no_banco
              AND s.evento_tipo = 'match_deshecho'
              AND s.evento_ref = m.id::text
            ORDER BY s.creado_en DESC LIMIT 1) AS saldo_despues
        FROM scintela.banco_conciliacion_match m
        WHERE m.deshecho_en IS NOT NULL
          AND m.deshecho_en::date BETWEEN %(desde)s AND %(hasta)s
    """
    params_deshechos: dict = {"desde": desde, "hasta": hasta}
    if no_banco is not None:
        sql_deshechos += " AND m.no_banco = %(no_banco)s"
        params_deshechos["no_banco"] = no_banco
    if usuario_filter:
        sql_deshechos += " AND UPPER(TRIM(m.deshecho_por)) LIKE UPPER(%(usu)s)"
        params_deshechos["usu"] = f"%{usuario_filter}%"

    # ─── 3) Históricos marcados conciliados ───
    sql_hist = """
        SELECT
          h.conciliado_en   AS ts,
          'hist_conciliado' AS accion,
          h.conciliado_por  AS usuario,
          h.no_banco        AS no_banco,
          h.conciliado_match_id AS match_id,
          NULL::int         AS id_tx,
          h.fecha           AS fecha,
          h.documento       AS doc,
          h.concepto        AS concepto,
          h.monto           AS monto,
          h.tipo            AS tipo,
          NULL              AS estado
        FROM scintela.banco_historicos_pendientes h
        WHERE h.conciliado_en IS NOT NULL
          AND h.conciliado_en::date BETWEEN %(desde)s AND %(hasta)s
    """
    params_hist: dict = {"desde": desde, "hasta": hasta}
    if no_banco is not None:
        sql_hist += " AND h.no_banco = %(no_banco)s"
        params_hist["no_banco"] = no_banco
    if usuario_filter:
        sql_hist += " AND UPPER(TRIM(h.conciliado_por)) LIKE UPPER(%(usu)s)"
        params_hist["usu"] = f"%{usuario_filter}%"

    eventos: list[dict] = []
    try:
        for sql, params in [
            (sql_creados, params_creados),
            (sql_deshechos, params_deshechos),
            (sql_hist, params_hist),
        ]:
            rows = _db.fetch_all(sql, params) or []
            for r in rows:
                eventos.append(dict(r))
    except Exception as exc:
        _LOG.exception("cambios_timeline query failed: %s", exc)
        eventos = []

    # Ordenar por timestamp desc, agrupar por día
    eventos.sort(key=lambda e: e.get("ts") or 0, reverse=True)

    # Calcular saldo_antes y delta para cada evento: el "antes" es el saldo
    # del evento INMEDIATAMENTE ANTERIOR cronológico (que en orden desc es
    # el siguiente índice). Si no hay snapshot, queda None.
    # Iteramos de atrás (más viejo) hacia adelante (más nuevo) llevando el
    # último saldo conocido como running.
    _running = None
    for e in reversed(eventos):
        s_after = e.get("saldo_despues")
        if s_after is not None:
            try:
                s_after_f = float(s_after)
                e["saldo_antes"] = _running
                e["delta"] = round(s_after_f - _running, 2) if _running is not None else None
                _running = s_after_f
            except (TypeError, ValueError):
                e["saldo_antes"] = None
                e["delta"] = None
        else:
            e["saldo_antes"] = None
            e["delta"] = None
    por_dia: dict = {}
    for e in eventos:
        ts = e.get("ts")
        if not ts:
            continue
        dia = ts.date() if hasattr(ts, "date") else ts
        por_dia.setdefault(dia, []).append(e)

    # Stats globales del rango
    stats = {
        "n_total": len(eventos),
        "n_match_creado": sum(1 for e in eventos if e["accion"] == "match_creado"),
        "n_match_deshecho": sum(1 for e in eventos if e["accion"] == "match_deshecho"),
        "n_hist_conciliado": sum(1 for e in eventos if e["accion"] == "hist_conciliado"),
    }

    # Usuarios para el dropdown del filtro
    try:
        users_rows = _db.fetch_all(
            """
            SELECT DISTINCT usuario AS u FROM scintela.banco_conciliacion_match
              WHERE usuario IS NOT NULL AND TRIM(usuario) <> ''
            UNION
            SELECT DISTINCT deshecho_por AS u FROM scintela.banco_conciliacion_match
              WHERE deshecho_por IS NOT NULL AND TRIM(deshecho_por) <> ''
            UNION
            SELECT DISTINCT conciliado_por AS u FROM scintela.banco_historicos_pendientes
              WHERE conciliado_por IS NOT NULL AND TRIM(conciliado_por) <> ''
            ORDER BY u
            """
        ) or []
        usuarios = sorted({r["u"].strip() for r in users_rows if r.get("u")})
    except Exception:
        usuarios = []

    return render_template(
        "conciliacion/cambios.html",
        eventos=eventos,
        por_dia=sorted(por_dia.items(), key=lambda kv: kv[0], reverse=True),
        stats=stats,
        desde=desde,
        hasta=hasta,
        usuario_filter=usuario_filter,
        no_banco=no_banco,
        usuarios=usuarios,
    )
