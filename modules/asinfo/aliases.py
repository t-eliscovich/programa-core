"""Alias map cliente Asinfo ↔ PC.

Asinfo y PC tienen códigos distintos para el mismo cliente real (ej.
CL2 en Asinfo == CLR en PC, AJ2 == AJO, J3C == VGA). Esta tabla evita
hardcodear el mapping en código — la dueña puede agregar aliases nuevos
sin redeploy.

Uso:
    from modules.asinfo import aliases
    pc_code = aliases.to_pc("CL2")   # → "CLR"
    asinfo_codes = aliases.to_asinfo("CLR")  # → ["CLR", "CL2"]
    same = aliases.misma_entidad("CL2", "CLR")  # → True

Implementación:
    Cache TTL 5 min — cargado del DB la primera vez, refrescado cada 5 min.
    Si el cache no se pudo cargar (DB caída, tabla no existe pre-migración),
    cae fail-soft a un mapping vacío (identidad).
"""
from __future__ import annotations

import logging
import time

import db

_LOG = logging.getLogger("programa_core.asinfo.aliases")

_CACHE_TTL_SECS = 300  # 5 min
_cache_ts: float = 0.0
_cache_asinfo_to_pc: dict[str, str] = {}
_cache_pc_to_asinfo: dict[str, list[str]] = {}


def _norm(s: str | None) -> str:
    return (s or "").strip().upper()


def _refrescar() -> None:
    """Recarga el cache desde DB. Fail-soft si falla (no rompe call sites)."""
    global _cache_ts, _cache_asinfo_to_pc, _cache_pc_to_asinfo
    try:
        rows = db.fetch_all(
            "SELECT codigo_asinfo, codigo_pc FROM scintela.cliente_alias"
        ) or []
    except Exception as e:
        _LOG.warning("alias_cache: fetch falló (%s) — uso vacío", e)
        rows = []
    a2p: dict[str, str] = {}
    p2a: dict[str, list[str]] = {}
    for r in rows:
        a = _norm(r.get("codigo_asinfo"))
        p = _norm(r.get("codigo_pc"))
        if not a or not p:
            continue
        a2p[a] = p
        p2a.setdefault(p, []).append(a)
    _cache_asinfo_to_pc = a2p
    _cache_pc_to_asinfo = p2a
    _cache_ts = time.time()
    _LOG.info("alias_cache: %s aliases cargados", len(a2p))


def _ensure_cache() -> None:
    if time.time() - _cache_ts > _CACHE_TTL_SECS:
        _refrescar()


def to_pc(codigo_asinfo: str | None) -> str:
    """Devuelve el código PC que corresponde a un código Asinfo.

    Si no hay alias, devuelve el código tal cual (identidad).
    """
    _ensure_cache()
    c = _norm(codigo_asinfo)
    if not c:
        return ""
    return _cache_asinfo_to_pc.get(c, c)


def to_asinfo(codigo_pc: str | None) -> list[str]:
    """Devuelve TODOS los códigos Asinfo asociados al código PC.

    Incluye el código PC propio (porque por default Asinfo usa el mismo)
    + los aliases registrados.
    Ej: to_asinfo("CLR") → ["CLR", "CL2"]
    """
    _ensure_cache()
    c = _norm(codigo_pc)
    if not c:
        return []
    out = [c]
    for a in _cache_pc_to_asinfo.get(c, []):
        if a not in out:
            out.append(a)
    return out


def misma_entidad(codigo_a: str | None, codigo_b: str | None) -> bool:
    """True si los dos códigos refieren al mismo cliente (cruzando aliases).

    Considera los dos como "asinfo" candidatos: si normalizando con `to_pc`
    cae al mismo destino, son la misma entidad.
    """
    a = _norm(codigo_a)
    b = _norm(codigo_b)
    if not a or not b:
        return False
    if a == b:
        return True
    return to_pc(a) == to_pc(b)


def todos() -> list[dict]:
    """Lista de aliases. Útil para UI de admin."""
    _ensure_cache()
    return [
        {"codigo_asinfo": a, "codigo_pc": p}
        for a, p in _cache_asinfo_to_pc.items()
    ]


def agregar(codigo_asinfo: str, codigo_pc: str, *, nota: str = "", usuario: str = "web") -> bool:
    """Agrega un alias nuevo. Idempotente — ON CONFLICT DO NOTHING.

    Returns True si insertó, False si ya existía.
    """
    a = _norm(codigo_asinfo)[:10]
    p = _norm(codigo_pc)[:10]
    if not a or not p:
        raise ValueError("codigo_asinfo y codigo_pc requeridos")
    if a == p:
        raise ValueError("alias a sí mismo no aporta")
    res = db.execute(
        """
        INSERT INTO scintela.cliente_alias (codigo_asinfo, codigo_pc, nota, usuario_crea)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (codigo_asinfo, codigo_pc) DO NOTHING
        """,
        (a, p, nota or None, usuario[:50]),
    )
    _refrescar()  # forzar reload — la próxima call ve el nuevo alias
    return bool(res)


def borrar(codigo_asinfo: str, codigo_pc: str) -> int:
    a = _norm(codigo_asinfo)
    p = _norm(codigo_pc)
    n = db.execute(
        "DELETE FROM scintela.cliente_alias WHERE codigo_asinfo=%s AND codigo_pc=%s",
        (a, p),
    )
    _refrescar()
    return int(n or 0)


def reset_cache() -> None:
    """Para tests o tras migración manual."""
    global _cache_ts
    _cache_ts = 0.0
