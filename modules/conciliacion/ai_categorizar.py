"""Categorización con Claude Haiku — fallback cuando la heurística regex
no acierta. Diseñado fail-graceful: si no hay ANTHROPIC_API_KEY o falla
la API, devuelve la categoría heurística original sin romper la conciliación.

Caché:
  Por (concepto_norm, tipo) en `scintela.conciliacion_ai_cache`. Una vez
  visto un concepto, no se vuelve a llamar a la API en sesiones futuras.
"""
from __future__ import annotations

import json
import logging
import os
import re

import db
from modules.conciliacion.categorizar import (
    GRUPO_LABEL,
    Categoria,
    categorizar,
    necesita_ai,
)

_LOG = logging.getLogger("programa_core.conciliacion.ai")

_MODELO_DEFAULT = "claude-haiku-4-5"
_API_URL = "https://api.anthropic.com/v1/messages"
_API_VERSION = "2023-06-01"
_TIMEOUT_S = 8.0

# Categorías válidas que aceptamos del LLM
_CATEGORIAS_VALIDAS = set(GRUPO_LABEL.keys())


# ─── Normalización para cache hits ────────────────────────────────────────


_RE_NUM = re.compile(r"\d{2,}")  # 2+ dígitos seguidos
_RE_FECHA = re.compile(r"\b\d{1,2}[\s/.\-]\d{1,2}([\s/.\-]\d{2,4})?\b")
_RE_REF = re.compile(r"\b[A-Z0-9]{8,}\b")  # tokens largos tipo "2605150C3DWJ"
_RE_SPACES = re.compile(r"\s+")


def normalizar_concepto(concepto: str) -> str:
    """Saca números/fechas/refs largas para que cache acierte más.

    >>> normalizar_concepto("2605150C3DWJ-INTELA C-PAG-DHL")
    'INTELA C-PAG-DHL'
    >>> normalizar_concepto("INTELA C-PAG-CASH 05 15")
    'INTELA C-PAG-CASH'
    """
    s = (concepto or "").upper().strip()
    s = _RE_REF.sub("", s)
    s = _RE_FECHA.sub("", s)
    s = _RE_NUM.sub("", s)
    s = s.replace("-", "-").strip(" -")
    s = _RE_SPACES.sub(" ", s).strip()
    return s


# ─── Cache DB ─────────────────────────────────────────────────────────────


def _leer_cache(concepto_norm: str, tipo: str) -> dict | None:
    if not concepto_norm:
        return None
    row = db.fetch_one(
        """
        UPDATE scintela.conciliacion_ai_cache
           SET hits = hits + 1, ultimo_hit = CURRENT_TIMESTAMP
         WHERE concepto_norm = %s AND tipo = %s
         RETURNING categoria, grupo, label, cliente, descripcion, confianza
        """,
        (concepto_norm, tipo or "?"),
    )
    return dict(row) if row else None


def _guardar_cache(concepto_norm: str, tipo: str, parsed: dict) -> None:
    if not concepto_norm or not parsed:
        return
    db.execute(
        """
        INSERT INTO scintela.conciliacion_ai_cache
            (concepto_norm, tipo, categoria, grupo, label, cliente, descripcion, confianza, modelo)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (concepto_norm, tipo) DO UPDATE SET
            categoria   = EXCLUDED.categoria,
            grupo       = EXCLUDED.grupo,
            label       = EXCLUDED.label,
            cliente     = EXCLUDED.cliente,
            descripcion = EXCLUDED.descripcion,
            confianza   = EXCLUDED.confianza,
            modelo      = EXCLUDED.modelo,
            hits        = scintela.conciliacion_ai_cache.hits + 1,
            ultimo_hit  = CURRENT_TIMESTAMP
        """,
        (
            concepto_norm,
            tipo or "?",
            parsed["categoria"],
            parsed["grupo"],
            parsed["label"],
            parsed.get("cliente") or None,
            parsed.get("descripcion") or None,
            float(parsed.get("confianza") or 0.5),
            parsed.get("modelo") or _MODELO_DEFAULT,
        ),
    )


def _tiene_tabla_cache() -> bool:
    """¿Corrió la migration 0048? Cacheado por proceso."""
    if hasattr(_tiene_tabla_cache, "_cache"):
        return _tiene_tabla_cache._cache
    row = db.fetch_one(
        """
        SELECT 1 FROM information_schema.tables
         WHERE table_schema = 'scintela'
           AND table_name = 'conciliacion_ai_cache'
        """
    )
    _tiene_tabla_cache._cache = bool(row)
    return _tiene_tabla_cache._cache


# ─── Llamada a la API ─────────────────────────────────────────────────────


_PROMPT = """Sos un asistente que clasifica movimientos bancarios de una empresa textil ecuatoriana.

Dado el CONCEPTO del extracto bancario y el TIPO (C=entrada, D=salida),
respondé un único objeto JSON con esta forma exacta:

{{
  "categoria": "<una de: {categorias}>",
  "cliente":   "<nombre del cliente/proveedor extraído, o null>",
  "descripcion": "<frase corta legible que explica qué fue el movimiento, en español>",
  "confianza": <número entre 0 y 1>
}}

REGLAS:
- Usá exactamente uno de los códigos de categoria listados, sin inventar.
- Si el concepto menciona un nombre propio (persona o empresa), ponelo en `cliente`.
- `descripcion` debe ser breve (máx 60 chars), tipo "Pago a DHL por envío" o
  "Cobro transferencia de Marcia Castillo".
- Si no tenés idea, usá categoria="OTRO" con confianza baja.
- Respondé SOLO el JSON, sin texto extra ni markdown.

CONCEPTO: {concepto}
TIPO: {tipo}
""".replace("{categorias}", ", ".join(sorted(_CATEGORIAS_VALIDAS)))


def _llamar_api(concepto: str, tipo: str) -> dict | None:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None
    try:
        import requests
    except ImportError:
        _LOG.warning("requests no disponible; AI desactivada")
        return None

    prompt = _PROMPT.format(concepto=concepto, tipo=tipo or "?")
    try:
        resp = requests.post(
            _API_URL,
            timeout=_TIMEOUT_S,
            headers={
                "x-api-key": api_key,
                "anthropic-version": _API_VERSION,
                "content-type": "application/json",
            },
            json={
                "model": _MODELO_DEFAULT,
                "max_tokens": 200,
                "messages": [{"role": "user", "content": prompt}],
            },
        )
        if resp.status_code != 200:
            _LOG.warning("AI categorizar HTTP %s: %s", resp.status_code, resp.text[:200])
            return None
        body = resp.json()
        texto = (body.get("content") or [{}])[0].get("text", "").strip()
        # Limpiar fences si vinieron
        if texto.startswith("```"):
            texto = texto.strip("`")
            if texto.lower().startswith("json"):
                texto = texto[4:]
        parsed = json.loads(texto.strip())
    except Exception as e:
        _LOG.warning("AI categorizar falló: %s", e)
        return None

    cat = (parsed.get("categoria") or "OTRO").strip().upper()
    if cat not in _CATEGORIAS_VALIDAS:
        cat = "OTRO"
    grupo, label, abrev = GRUPO_LABEL.get(cat, ("OTRO", "Sin categorizar", "?"))
    return {
        "categoria": cat,
        "grupo": grupo,
        "label": label,
        "abrev": abrev,
        "cliente": (parsed.get("cliente") or None),
        "descripcion": (parsed.get("descripcion") or None),
        "confianza": float(parsed.get("confianza") or 0.5),
        "modelo": _MODELO_DEFAULT,
    }


# ─── API pública ──────────────────────────────────────────────────────────


def categorizar_con_ai(concepto: str, tipo: str) -> tuple[Categoria, dict]:
    """Cascada completa: regex heurística → cache DB → API → fallback regex.

    Returns:
        (Categoria, info_extra) donde info_extra es {'cliente': str|None,
        'descripcion': str|None}.
    """
    # 1) Heurística regex
    cat_regex = categorizar(concepto, tipo)
    info_extra = {"cliente": None, "descripcion": None}

    # Si la regex ya dio buena confianza, no llamar a AI.
    if not necesita_ai(cat_regex):
        return cat_regex, info_extra

    # 2) Cache DB (si la migration corrió)
    if not _tiene_tabla_cache():
        return cat_regex, info_extra

    concepto_norm = normalizar_concepto(concepto)
    cached = _leer_cache(concepto_norm, tipo)
    if cached:
        _grp, _lbl, _abr = GRUPO_LABEL.get(cached["categoria"], ("OTRO", "Sin categorizar", "?"))
        cat = Categoria(
            codigo=cached["categoria"],
            grupo=cached["grupo"],
            label=cached["label"],
            abrev=_abr,
            confianza=float(cached["confianza"] or 0.5),
            fuente="ai-cache",
        )
        return cat, {"cliente": cached.get("cliente"), "descripcion": cached.get("descripcion")}

    # 3) API
    parsed = _llamar_api(concepto, tipo)
    if not parsed:
        return cat_regex, info_extra

    _guardar_cache(concepto_norm, tipo, parsed)
    cat = Categoria(
        codigo=parsed["categoria"],
        grupo=parsed["grupo"],
        label=parsed["label"],
        abrev=parsed.get("abrev") or "?",
        confianza=parsed["confianza"],
        fuente="ai",
    )
    return cat, {"cliente": parsed.get("cliente"), "descripcion": parsed.get("descripcion")}


def categorizar_lote(items: list[tuple[str, str]]) -> list[tuple[Categoria, dict]]:
    """Aplica categorizar_con_ai a una lista de (concepto, tipo).

    No paraleliza llamadas a la API (mantiene la implementación simple).
    El cache hace que pasadas sucesivas sean instantáneas.
    """
    return [categorizar_con_ai(c, t) for c, t in items]
