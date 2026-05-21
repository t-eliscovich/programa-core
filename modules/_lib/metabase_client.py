"""Cliente Metabase compartido para todos los bridges externos de Programa Core.

Cómo usarlo:
    from modules._lib import metabase_client
    rows = metabase_client.fetch_card(os.environ["ASINFO_CARD_VENDEDOR_USD"])

Diseño:
- Login lazy + refresh on 401 (token Metabase vence en 14 días por default).
- Siempre fail-soft: cualquier excepción se loguea como WARNING y devuelve [].
- Una sola implementación de auth, reusada por MetabaseAdapter (costos_ot)
  y por cualquier módulo nuevo que lea de Asinfo (DB 2) o formulas_app
  (DB 3) vía card guardada.

Env vars que lee:
    METABASE_URL          base URL (http://localhost:3000 en EC2)
    METABASE_USERNAME     usuario con view-data en la DB / collection target
    METABASE_PASSWORD     password (rotable)
"""

from __future__ import annotations

import logging
import os

_log = logging.getLogger("programa_core.metabase_client")
_session_token: str | None = None


def _url() -> str | None:
    u = os.environ.get("METABASE_URL", "").strip()
    return u.rstrip("/") if u else None


def _creds() -> tuple[str | None, str | None]:
    return (
        os.environ.get("METABASE_USERNAME") or None,
        os.environ.get("METABASE_PASSWORD") or None,
    )


def disponible() -> bool:
    """True si las env vars necesarias están seteadas. NO hace I/O."""
    return bool(_url() and all(_creds()))


def _login(requests_mod) -> str | None:
    """Login. Setea _session_token. Devuelve el token o None si falla."""
    global _session_token
    user, pwd = _creds()
    if not (_url() and user and pwd):
        return None
    try:
        r = requests_mod.post(
            f"{_url()}/api/session",
            json={"username": user, "password": pwd},
            timeout=5,
        )
        r.raise_for_status()
        _session_token = r.json().get("id")
        return _session_token
    except Exception as e:
        _log.warning("Metabase login falló: %s", e)
        return None


def fetch_card(card_id: int | str | None, params: list[dict] | None = None) -> list[dict]:
    """POST /api/card/<id>/query/json.

    `params` opcional: lista de dicts con el formato Metabase de parameters,
    p.ej. [{"type": "category", "target": ["variable", ["template-tag", "vendedor"]], "value": "JTX"}].
    """
    if not card_id or not disponible():
        return []
    try:
        import requests  # local import — requests es dep transitiva ya
    except ImportError:
        _log.warning("requests no disponible — Metabase bridge devuelve []")
        return []

    global _session_token
    token = _session_token or _login(requests)
    if not token:
        return []

    url = f"{_url()}/api/card/{card_id}/query/json"
    body: dict = {"parameters": params} if params else {}

    try:
        r = requests.post(
            url,
            json=body,
            headers={"X-Metabase-Session": token},
            timeout=15,
        )
        if r.status_code == 401:
            # Token vencido: re-login una vez y reintento.
            _session_token = None
            token = _login(requests)
            if not token:
                return []
            r = requests.post(
                url,
                json=body,
                headers={"X-Metabase-Session": token},
                timeout=15,
            )
        r.raise_for_status()
        data = r.json()
        return data if isinstance(data, list) else []
    except Exception as e:
        _log.warning("Metabase fetch_card(%s) falló: %s", card_id, e)
        return []


def reset_session() -> None:
    """Forzar re-login en la próxima llamada. Útil para tests."""
    global _session_token
    _session_token = None
