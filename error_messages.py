"""Traducir excepciones técnicas a mensajes en español contable.

Objetivo: que la contadora nunca vea un traceback Python crudo. Todo lo que
pueda fallar se mapea a una frase que le da a un operador humano una idea
razonable de qué hacer (qué corregir, a quién avisar).

Uso:
    from error_messages import humanize
    mensaje_amigable = humanize(exc)

Jinja filter:
    {{ mi_exc | humanizar }}
"""
from __future__ import annotations

import logging
import re

_log = logging.getLogger("programa_core.errors")

# --- Mapeos ---------------------------------------------------------------
# Orden: primero los más específicos (ValueError con texto puntual), después
# los genéricos (UniqueViolation, ForeignKeyViolation), por último Exception.

_MONTO_RE = re.compile(r"monto inv[aá]lido:?\s*'?([^'\"]+)'?", re.IGNORECASE)
_FECHA_RE = re.compile(r"fecha inv[aá]lida", re.IGNORECASE)


def _msg_valueerror(exc: ValueError) -> str:
    """ValueError — cubrimos los dos casos más comunes del sistema."""
    text = str(exc)
    m = _MONTO_RE.search(text)
    if m:
        valor = m.group(1).strip()
        return (
            f"El importe '{valor}' no es un número válido. "
            "Usá formato 1234.56 o 1.234,56."
        )
    if _FECHA_RE.search(text):
        return "La fecha no es válida. Usá formato DD/MM/AAAA (ej: 17/04/2026)."
    # ValueError genérico — el mensaje ya es más o menos legible si viene de
    # validaciones nuestras (ej. "Motivo requerido"). Lo devolvemos tal cual.
    return text or "Dato inválido."


def _msg_unique_violation() -> str:
    return "Ya existe un registro con ese identificador."


def _msg_fk_violation() -> str:
    return "Referencia inválida: el registro al que apuntás no existe."


def _msg_check_violation() -> str:
    return "Valor fuera de rango o no permitido por las reglas del sistema."


def _msg_not_null() -> str:
    return "Falta completar un dato obligatorio."


def _msg_generico() -> str:
    return "Hubo un problema. Avisá a soporte si persiste."


def flash_exc(prefix: str, exc: Exception, category: str = "error") -> None:
    """Flashea un mensaje amigable en español a partir de una excepción.

    Reemplaza el idiom `flash(f"No pude X: {e}", "error")` que expone el
    detalle crudo al usuario (tipo `psycopg2.errors.UniqueViolation:
    duplicate key value violates unique constraint "..."`). Todo pasa por
    `humanize(exc)` primero.

    Import perezoso de flask para evitar el costo de importarlo cuando
    `error_messages` se usa desde código sin contexto Flask (scripts,
    tests de queries, etc.).
    """
    from flask import flash as _flash
    _flash(f"{prefix.rstrip(':').rstrip()}: {humanize(exc)}", category)


def humanize(exc: Exception) -> str:
    """Map exception → Spanish contable-friendly message.

    Never raises. Logs the full stack at WARNING for the generic case so
    the operator can correlate with the audit trail (request_id).
    """
    if exc is None:
        return ""

    # --- psycopg2 specific errors (check pgcode if available) -------------
    pgcode = getattr(exc, "pgcode", None)
    if pgcode:
        if pgcode == "23505":
            return _msg_unique_violation()
        if pgcode == "23503":
            return _msg_fk_violation()
        if pgcode == "23514":
            return _msg_check_violation()
        if pgcode == "23502":
            return _msg_not_null()

    # Some wrappers expose the class name but not pgcode — check the name.
    clsname = type(exc).__name__
    if clsname == "UniqueViolation":
        return _msg_unique_violation()
    if clsname == "ForeignKeyViolation":
        return _msg_fk_violation()
    if clsname == "CheckViolation":
        return _msg_check_violation()
    if clsname == "NotNullViolation":
        return _msg_not_null()

    # --- ValueError (nuestras propias validaciones) -----------------------
    if isinstance(exc, ValueError):
        return _msg_valueerror(exc)

    # --- PermissionError — acceso denegado lógico -------------------------
    if isinstance(exc, PermissionError):
        return "No tenés permiso para realizar esta acción."

    # --- Default: loggeamos con stack, devolvemos mensaje neutro ----------
    _log.warning("humanize fallback for %s: %s", clsname, exc, exc_info=True)
    return _msg_generico()
