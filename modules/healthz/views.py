"""Health check endpoints.

Tres niveles:
    GET /healthz                — liveness: el proceso responde. Sin tocar DB. Rápido.
    GET /healthz/ready          — readiness: puede servir tráfico. SELECT 1 a la DB
                                  canónica con timeout corto.
    GET /healthz/integraciones  — estado de los bridges externos (formulas_app y
                                  Metabase). Útil para smoke-test post-deploy.

Separar liveness vs readiness es el pattern estándar:
    - Liveness fallado = restart del container (k8s / ECS / docker).
    - Readiness fallado = quitar del load balancer pero NO restart
      (DB transient, por ejemplo — la app está viva).

El de integraciones es informativo, NO un gate de readiness: si el bridge a
formulas_app está caído la app sigue sirviendo todo lo demás. Por eso devuelve
200 con flags adentro en vez de 503.

Sin auth. No registra en bitácora (son paths de skip). Devuelve JSON.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from flask import Blueprint, jsonify

import db
from modules._lib import formulas_db, metabase_client

_LOG = logging.getLogger("programa_core.healthz")
_UTC = timezone.utc  # noqa: UP017

healthz_bp = Blueprint("healthz", __name__, url_prefix="/healthz")


_BOOT_TS = datetime.now(_UTC).isoformat()


def _secret_fp() -> str:
    """Huella (8 hex) de la SECRET_KEY activa — NO expone la clave.

    TMT 2026-07-06 (dueña: "¿por qué se sale de mi sesión?"): las sesiones
    volvieron a morir coincidiendo con deploys, como el 2026-06-29 (clave
    rotando). Esta huella permite comparar antes/después de un deploy: si
    cambia, la clave rotó y ahí está el deslogueo. Ver app._load_secret_key.
    """
    import hashlib

    from flask import current_app

    try:
        key = current_app.config.get("SECRET_KEY") or ""
        return hashlib.sha256(str(key).encode()).hexdigest()[:8]
    except Exception:  # noqa: BLE001
        return "?"


@healthz_bp.route("", methods=["GET"])
@healthz_bp.route("/", methods=["GET"])
def liveness():
    """Liveness probe — el proceso responde.

    No toca DB, no toca disco. Simplemente confirma que el WSGI worker está
    atendiendo requests. Si esto falla, restartear el container.
    """
    return jsonify(
        {
            "status": "ok",
            "ts": datetime.now(_UTC).isoformat(),
            # huella de la clave de sesión + hora de arranque del proceso —
            # para diagnosticar deslogueos post-deploy (ver _secret_fp).
            "secret_fp": _secret_fp(),
            "boot_ts": _BOOT_TS,
        }
    ), 200


@healthz_bp.route("/ready", methods=["GET"])
def readiness():
    """Readiness probe — puede servir tráfico real.

    Hace un SELECT 1 contra la DB con timeout corto. Si la DB está caída o
    lenta, devolvemos 503 para que el LB nos saque. La app sigue viva
    (liveness OK), sólo degrada la readiness hasta que la DB vuelva.
    """
    t0 = time.perf_counter()
    try:
        # SELECT 1 es lo más barato que podemos pedirle a Postgres. Si tarda
        # más de 2s, la DB está en mal estado o la red saturada.
        row = db.fetch_one("SELECT 1 AS ok")
        db_ok = bool(row and row.get("ok") == 1)
    except Exception as e:
        _LOG.warning("readiness probe falló: %s", e)
        db_ok = False

    ms = (time.perf_counter() - t0) * 1000
    status_code = 200 if db_ok else 503
    return jsonify(
        {
            "status": "ok" if db_ok else "degraded",
            "db": "connected" if db_ok else "unreachable",
            "latency_ms": round(ms, 1),
            "ts": datetime.now(_UTC).isoformat(),
        }
    ), status_code


@healthz_bp.route("/integraciones", methods=["GET"])
def integraciones():
    """Estado de los bridges externos (formulas_app + Metabase).

    Informativo, NO un gate. Siempre devuelve 200; el detalle adentro dice
    qué bridge está conectado y cuál no. Útil para smoke-test post-deploy:

        curl https://<host>/healthz/integraciones

    Cada bloque tiene:
        configured: bool — ¿están las env vars seteadas? (NO toca red)
        reachable:  bool — ¿el bridge responde un SELECT 1 / un login? (toca red)
                          None si no está configured (no se molesta en probar).
        latency_ms: float — sólo si reachable se probó.

    Nada de URLs, usernames o passwords en la respuesta — solo flags.
    """
    out: dict = {
        "ts": datetime.now(_UTC).isoformat(),
        "formulas_app": {"configured": False, "reachable": None, "latency_ms": None},
        "metabase": {"configured": False, "reachable": None, "latency_ms": None},
    }

    # ------ formulas_app (Postgres pool a la otra DB del mismo RDS) ------
    out["formulas_app"]["configured"] = formulas_db.disponible()
    if out["formulas_app"]["configured"]:
        t0 = time.perf_counter()
        try:
            ok = formulas_db.healthcheck()
        except Exception as e:
            _LOG.warning("formulas_db.healthcheck levantó: %s", e)
            ok = False
        out["formulas_app"]["reachable"] = ok
        out["formulas_app"]["latency_ms"] = round((time.perf_counter() - t0) * 1000, 1)

    # ------ Metabase (cliente HTTP) ------
    out["metabase"]["configured"] = metabase_client.disponible()
    if out["metabase"]["configured"]:
        t0 = time.perf_counter()
        try:
            # Forzamos un re-login para validar que credentials siguen vivos.
            # No tocamos cards — solo /api/session. Más barato y diagnóstico.
            metabase_client.reset_session()
            import requests as _requests  # local: ya es dep del cliente

            ok = bool(metabase_client._login(_requests))
        except Exception as e:
            _LOG.warning("metabase login probe falló: %s", e)
            ok = False
        out["metabase"]["reachable"] = ok
        out["metabase"]["latency_ms"] = round((time.perf_counter() - t0) * 1000, 1)

    return jsonify(out), 200
