"""Cliente Metabase compartido para todos los bridges externos de Programa Core.

Cómo usarlo:
    from modules._lib import metabase_client
    rows = metabase_client.fetch_card(os.environ["ASINFO_CARD_VENDEDOR_USD"])

Diseño:
- Login lazy + refresh on 401 (token Metabase vence en 14 días por default).
- Siempre fail-soft: cualquier excepción se loguea como WARNING y devuelve [].
- Una sola implementación de auth, reusada por los bridges de Asinfo
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
import threading

_log = logging.getLogger("programa_core.metabase_client")
_session_token: str | None = None

# ── Señal "¿Metabase contestó?" (TMT 2026-07-29) ───────────────────────────
# `fetch_dataset` devuelve [] tanto si la query no tiene filas como si hubo
# timeout/401/500. Quien CACHEA necesita distinguirlos: cachear un fracaso
# por 5 min dejó el 29/07 el stock en cero y la Utilidad Real ~495.000 abajo
# (196.010 en vez de 687.519) sin ningún aviso. `fetch_dataset` marca acá el
# resultado y `fetch_dataset_estado` lo devuelve junto con las filas.
# threading.local porque Waitress atiende con varios hilos.
_estado = threading.local()


# ── BITÁCORA de las consultas (TMT 2026-07-31) ─────────────────────────────
# Cuando la utilidad se movía, nadie podía decir si Metabase había contestado:
# el fallo se loguea por WARNING y ese log, en el servidor, no lo lee nadie.
# Acá quedan las últimas consultas EN MEMORIA (cuánto tardaron, si salieron
# bien, y el error si no) y `/admin/health/metabase` las muestra. Sin tabla y
# sin archivo: se pierde al reiniciar, y está bien — es para mirar AHORA,
# cuando el número se movió hace un minuto.
_BITACORA: list = []
_BITACORA_MAX = 60


def _anotar(db: int, ms: float, ok: bool, error: str = "") -> None:
    try:
        from modules._lib import medidor as _medidor

        _medidor.anotar_puente(ms)  # el termómetro de /admin/pantallas
    except Exception:  # noqa: BLE001 -- medir jamás rompe una consulta
        pass
    try:
        import time as _t
        _BITACORA.append({
            "ts": _t.strftime("%H:%M:%S", _t.gmtime(_t.time() - 5 * 3600)),
            "db": int(db), "ms": round(ms, 1), "ok": bool(ok),
            "error": (error or "")[:200],
        })
        del _BITACORA[:-_BITACORA_MAX]
    except Exception:  # noqa: BLE001 -- una bitácora no puede romper nada
        pass


def bitacora() -> list:
    """Las últimas consultas a Metabase, la más nueva al final. Hora de Ecuador."""
    return list(_BITACORA)


def _timeout_secs() -> int:
    """Cuánto se espera una consulta pesada.

    Eran 20 s contra los 90 de `fetch_card`, sin ninguna razón: una consulta
    que tardara un poco más fallaba salteado y dejaba el inventario a medias
    (31/07). Con la red del "último valor bueno" ya puesta, esperar un rato más
    es mejor que contestar rápido y mal. Ajustable con METABASE_TIMEOUT_SECS.
    """
    try:
        v = int(os.environ.get("METABASE_TIMEOUT_SECS", "45"))
    except (TypeError, ValueError):
        return 45
    return v if 5 <= v <= 180 else 45


def _marcar(ok: bool) -> None:
    _estado.ok = ok


def ultimo_fetch_ok() -> bool:
    """True si el último fetch de ESTE hilo llegó a Metabase. Default True
    (si nadie marcó nada — p.ej. un test que mockea fetch_dataset — se asume
    que el dato es bueno y se cachea normal)."""
    return getattr(_estado, "ok", True)


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

    import time as _t
    _t0 = _t.monotonic()
    try:
        r = requests.post(
            url,
            json=body,
            headers={"X-Metabase-Session": token},
            timeout=90,
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
                timeout=90,
            )
        r.raise_for_status()
        data = r.json()
        _anotar(0, (_t.monotonic() - _t0) * 1000, True)
        return data if isinstance(data, list) else []
    except Exception as e:
        _log.warning("Metabase fetch_card(%s) falló: %s", card_id, e)
        _anotar(0, (_t.monotonic() - _t0) * 1000, False, str(e))
        return []


def fetch_dataset(
    database_id: int,
    sql: str,
    params: list | None = None,
    max_results: int = 10000,
) -> list[dict]:
    """POST /api/dataset — query SQL ad-hoc contra una base configurada en Metabase.

    OJO si vas a CACHEAR el resultado: esta firma NO distingue "Metabase se
    cayó" de "la query no devolvió filas" — las dos dan []. Para eso está
    `fetch_dataset_estado`, que además devuelve si Metabase contestó.

    Útil cuando no hay (o no querés crear) una card guardada para un caso
    puntual. La SQL la escribís VOS — NO interpoles datos del usuario sin
    sanitizar.

    Args:
        database_id: id Metabase de la base (2 = Asinfo, 3 = formulas_app).
        sql: SQL nativa. Puede usar `{{tag}}` template-tags pero la mayoría
            de usos son SQL pura.
        params: opcional, lista de positional/template parameters.
        max_results: límite de filas. Metabase default es 2000 (cliente) /
            2000 (bare-rows). Para datasets que devuelven >2000 productos,
            subirlo evita truncar silenciosamente. Default acá 10.000.

    Returns:
        Lista de dicts (una por fila). [] si falla, si la base no existe
        o si no hay conexión configurada. Fail-soft idéntico a fetch_card().
    """
    if not disponible():
        _marcar(False)
        return []
    try:
        import requests
    except ImportError:
        _log.warning("requests no disponible — Metabase bridge devuelve []")
        _marcar(False)
        return []

    global _session_token
    token = _session_token or _login(requests)
    if not token:
        _marcar(False)
        return []

    body = {
        "database": database_id,
        "type": "native",
        "native": {"query": sql},
        "constraints": {
            "max-results": int(max_results),
            "max-results-bare-rows": int(max_results),
        },
    }
    if params:
        body["parameters"] = params

    def _do():
        return requests.post(
            f"{_url()}/api/dataset",
            json=body,
            headers={"X-Metabase-Session": _session_token, "Content-Type": "application/json"},
            timeout=_timeout_secs(),
        )

    import time as _t
    _t0 = _t.monotonic()
    try:
        r = _do()
        if r.status_code == 401:
            _session_token = None
            token2 = _login(requests)
            if not token2:
                _anotar(database_id, (_t.monotonic() - _t0) * 1000, False,
                        "401 y no pude renovar la sesión")
                _marcar(False)
                return []
            r = _do()
        r.raise_for_status()
        data = r.json() or {}
        cols = [c.get("name") or "" for c in (data.get("data", {}).get("cols") or [])]
        rows = data.get("data", {}).get("rows") or []
        _anotar(database_id, (_t.monotonic() - _t0) * 1000, True)
        _marcar(True)
        return [
            {cols[i]: v for i, v in enumerate(row) if i < len(cols)}
            for row in rows
        ]
    except Exception as e:
        _log.warning("Metabase fetch_dataset(db=%s) falló: %s", database_id, e)
        _anotar(database_id, (_t.monotonic() - _t0) * 1000, False, str(e))
        _marcar(False)
        return []


def fetch_dataset_estado(
    database_id: int,
    sql: str,
    params: list | None = None,
    max_results: int = 10000,
) -> tuple[list[dict], bool]:
    """`fetch_dataset` + si Metabase contestó. Devuelve `(filas, ok)`.

    `ok` es True SOLO si se llegó a Metabase (aunque haya devuelto 0 filas);
    False ante bridge sin configurar, sin token, timeout, 401 o cualquier
    excepción. Usalo en vez de `fetch_dataset` siempre que vayas a CACHEAR:
    cachear un fracaso como si fuera un dato vacío es lo que rompió el
    balance el 29/07 (ver el comentario de `_estado` arriba).

    Delega en `fetch_dataset` a propósito: así los tests que mockean
    `fetch_dataset` siguen funcionando (el mock no marca nada → ok=True).
    """
    _marcar(True)
    # Se llama igual que lo hace todo el resto del código —
    # `fetch_dataset(db, sql, max_results=N)`, sin `params` posicional — para
    # no romper los fakes de los tests, que declaran esa misma firma.
    if params is None:
        filas = fetch_dataset(database_id, sql, max_results=max_results)
    else:
        filas = fetch_dataset(database_id, sql, params, max_results)
    return filas, ultimo_fetch_ok()


def reset_session() -> None:
    """Forzar re-login en la próxima llamada. Útil para tests."""
    global _session_token
    _session_token = None
