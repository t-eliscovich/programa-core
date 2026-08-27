"""Envío de MEMOS de pedidos a formulas_app — el único write-back del bridge.

`formulas_db.py` es SELECT-only por contrato y así se queda. Este módulo es
la excepción DELIBERADA y acotada (decisión Tamara 2026-08-27): Programa Core
manda memos de pedidos a la tabla `memos` de formulas_app, y nada más. El
cerco no es una promesa del código sino del ROL de la base:
`programa_core_memos` tiene SELECT e INSERT sobre `public.memos` y ningún
otro privilegio. Aunque alguien escriba acá un UPDATE a `ordenes`, Postgres
lo rechaza.

El memo es una FOTO del pedido al momento de enviar. Si el pedido cambia en
Asinfo después, el memo no se entera — se manda de nuevo (y el UNIQUE de
`pedido_numero` en formulas_app hace que el segundo envío avise "ya estaba"
en vez de duplicar).

Fail-soft como todo el bridge: sin env var o con la base caída, `enviar()`
devuelve (False, "sin_bridge") y `estados()` devuelve {} — la pantalla
muestra el botón igual y el envío avisa que no pudo, nunca rompe.

Env vars:
    FORMULAS_MEMOS_DATABASE_URL  postgresql://programa_core_memos:...@host/postgres?sslmode=require
"""

from __future__ import annotations

import json
import logging
import os
from contextlib import contextmanager

from psycopg2 import errors, pool

_log = logging.getLogger("programa_core.formulas_memos")

_pool: pool.ThreadedConnectionPool | None = None


def init_pool() -> None:
    """Abre el pool si FORMULAS_MEMOS_DATABASE_URL está seteada.

    Llamar una vez desde create_app(), al lado de formulas_db.init_pool().
    Idempotente.
    """
    global _pool
    if _pool is not None:
        return
    url = os.environ.get("FORMULAS_MEMOS_DATABASE_URL", "").strip()
    if not url:
        _log.info("FORMULAS_MEMOS_DATABASE_URL vacío — envío de memos deshabilitado")
        return
    try:
        _pool = pool.ThreadedConnectionPool(minconn=1, maxconn=2, dsn=url)
        _log.info("formulas_memos pool inicializado")
    except Exception as e:  # noqa: BLE001 — fail-soft por contrato del bridge
        _log.warning("formulas_memos init_pool falló: %s — envío deshabilitado", e)
        _pool = None


def disponible() -> bool:
    return _pool is not None


@contextmanager
def _conn():
    c = _pool.getconn()
    try:
        yield c
    finally:
        _pool.putconn(c)


def enviar(numero: str, cliente: str, vendedor: str, enviado_por: str,
           detalle: dict) -> tuple[bool, str]:
    """Inserta el memo. Devuelve `(ok, motivo)`.

    motivos: "enviado" (quedó), "ya_enviado" (otro lo mandó antes — el UNIQUE
    de formulas_app lo frena), "sin_bridge" (env var vacía o base caída).
    """
    if _pool is None:
        return False, "sin_bridge"
    try:
        with _conn() as c:
            with c.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO memos (pedido_numero, cliente, vendedor,
                                       enviado_por, detalle)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (pedido_numero) DO NOTHING
                    RETURNING id
                    """,
                    (numero, cliente or "", vendedor or "", enviado_por or "",
                     json.dumps(detalle, default=str)),
                )
                fila = cur.fetchone()
            c.commit()
        return (True, "enviado") if fila else (False, "ya_enviado")
    except errors.UniqueViolation:
        return False, "ya_enviado"
    except Exception as e:  # noqa: BLE001 — fail-soft por contrato del bridge
        _log.warning("formulas_memos.enviar falló: %s", e)
        return False, "sin_bridge"


def estados(numeros: list[str]) -> dict[str, dict]:
    """`{pedido_numero: {"estado": ..., "en_proceso_por": ...}}` de los memos
    ya enviados entre `numeros`. {} si el bridge no está o falla — la pantalla
    muestra los botones como si nada estuviera enviado, y el envío repetido lo
    frena el UNIQUE del lado de formulas_app.
    """
    if _pool is None or not numeros:
        return {}
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute(
                """
                SELECT pedido_numero, estado, en_proceso_por
                  FROM memos
                 WHERE pedido_numero = ANY(%s)
                """,
                (list(numeros),),
            )
            filas = cur.fetchall()
        return {r[0]: {"estado": r[1], "en_proceso_por": r[2]} for r in filas}
    except Exception as e:  # noqa: BLE001 — fail-soft por contrato del bridge
        _log.warning("formulas_memos.estados falló: %s", e)
        return {}
