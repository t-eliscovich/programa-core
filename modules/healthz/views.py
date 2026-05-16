"""Health check endpoints.

Dos niveles:
    GET /healthz       — liveness: el proceso responde. Sin toca DB. Rápido.
    GET /healthz/ready — readiness: puede servir tráfico. Toca DB con un
                         SELECT 1 y un timeout corto.

Separar liveness vs readiness es el pattern estándar:
    - Liveness fallado = restart del container (k8s / ECS / docker).
    - Readiness fallado = quitar del load balancer pero NO restart
      (DB transient, por ejemplo — la app está viva).

Sin auth. No registra en bitácora (son paths de skip). Devuelve JSON.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from flask import Blueprint, jsonify

import db

_LOG = logging.getLogger("programa_core.healthz")
_UTC = timezone.utc  # noqa: UP017

healthz_bp = Blueprint("healthz", __name__, url_prefix="/healthz")


@healthz_bp.route("", methods=["GET"])
@healthz_bp.route("/", methods=["GET"])
def liveness():
    """Liveness probe — el proceso responde.

    No toca DB, no toca disco. Simplemente confirma que el WSGI worker está
    atendiendo requests. Si esto falla, restartear el container.
    """
    return jsonify({
        "status": "ok",
        "ts": datetime.now(_UTC).isoformat(),
    }), 200


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
    return jsonify({
        "status": "ok" if db_ok else "degraded",
        "db": "connected" if db_ok else "unreachable",
        "latency_ms": round(ms, 1),
        "ts": datetime.now(_UTC).isoformat(),
    }), status_code
