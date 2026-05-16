"""Módulo 'recientes' — últimos N registros tocados por usuario.

Helpers diseñados para ser llamados desde cualquier view de detalle sin
añadir coste real al render (UPSERT + DELETE opcional de poda).

Todo es *best-effort*: si la tabla no existe todavía (migración 0010 aún
no corrida) o hay cualquier error de DB, los helpers tragan la excepción
y siguen. NUNCA romper un detail view por esto.
"""
from __future__ import annotations

import logging
from typing import Literal

from flask import g

import db

_log = logging.getLogger("programa_core.recientes")

TipoReciente = Literal["cliente", "factura", "cheque", "proveedor", "posdat", "retencion"]

_TIPOS_VALIDOS = {"cliente", "factura", "cheque", "proveedor", "posdat", "retencion"}

# Máximo histórico por (usuario, tipo). Mayor a este, se recorta el más
# viejo. Chosen para que el btree siempre quede corto.
_MAX_POR_TIPO = 50

# Default de cuántos mostrar en la UI.
_LIMITE_DEFAULT = 5


def _user_id() -> int | None:
    """Resuelve el id_usuario del request actual, o None si no hay sesión."""
    u = g.get("user") if g else None
    if not u:
        return None
    return u.get("id_usuario")


def registrar(tipo: str, id_ref: str | int, etiqueta: str | None = None) -> None:
    """UPSERT idempotente: bump `tocado_en` al ahora.

    Si ya hay una fila para (usuario, tipo, id_ref) se actualiza `tocado_en`
    y `etiqueta` (la etiqueta puede cambiar si el cliente cambió de nombre).
    Si no, se inserta. Después se poda por si superamos `_MAX_POR_TIPO`.
    """
    if tipo not in _TIPOS_VALIDOS:
        return  # best-effort: tipo inválido, no logueamos nada
    uid = _user_id()
    if not uid:
        return
    id_ref_s = str(id_ref)[:40] if id_ref is not None else ""
    if not id_ref_s:
        return
    etiqueta_s = (etiqueta or "")[:200] or None
    try:
        db.execute(
            """
            INSERT INTO seguridad.usuario_recientes
                (id_usuario, tipo, id_ref, etiqueta, tocado_en)
            VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP)
            ON CONFLICT (id_usuario, tipo, id_ref) DO UPDATE
               SET tocado_en = CURRENT_TIMESTAMP,
                   etiqueta  = COALESCE(EXCLUDED.etiqueta, seguridad.usuario_recientes.etiqueta)
            """,
            (uid, tipo, id_ref_s, etiqueta_s),
        )
        # Trim — best effort, si falla igual seguimos.
        _trim(uid, tipo)
    except Exception:
        _log.debug("registrar recientes falló", exc_info=True)


def _trim(id_usuario: int, tipo: str) -> None:
    """Si hay >MAX_POR_TIPO filas para (usuario, tipo), borrar las más viejas."""
    try:
        db.execute(
            """
            DELETE FROM seguridad.usuario_recientes r
            WHERE r.id_usuario = %s
              AND r.tipo = %s
              AND r.tocado_en < (
                  SELECT MIN(tocado_en) FROM (
                      SELECT tocado_en
                      FROM seguridad.usuario_recientes
                      WHERE id_usuario = %s AND tipo = %s
                      ORDER BY tocado_en DESC
                      LIMIT %s
                  ) AS keep
              )
            """,
            (id_usuario, tipo, id_usuario, tipo, _MAX_POR_TIPO),
        )
    except Exception:
        _log.debug("trim recientes falló", exc_info=True)


def listar_recientes(tipo: str | None = None, limite: int = _LIMITE_DEFAULT) -> list[dict]:
    """Lee los últimos N del usuario actual.

    Si `tipo` es None → across all tipos, interleaved by tocado_en DESC.
    """
    uid = _user_id()
    if not uid:
        return []
    limite = max(1, min(int(limite or _LIMITE_DEFAULT), 50))
    try:
        if tipo and tipo in _TIPOS_VALIDOS:
            return db.fetch_all(
                """
                SELECT tipo, id_ref, etiqueta, tocado_en
                FROM seguridad.usuario_recientes
                WHERE id_usuario = %s AND tipo = %s
                ORDER BY tocado_en DESC
                LIMIT %s
                """,
                (uid, tipo, limite),
            ) or []
        return db.fetch_all(
            """
            SELECT tipo, id_ref, etiqueta, tocado_en
            FROM seguridad.usuario_recientes
            WHERE id_usuario = %s
            ORDER BY tocado_en DESC
            LIMIT %s
            """,
            (uid, limite),
        ) or []
    except Exception:
        _log.debug("listar_recientes falló", exc_info=True)
        return []
