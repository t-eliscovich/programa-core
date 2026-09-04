"""Envío de MEMOS de pedidos a formulas_app — el único write-back del bridge.

`formulas_db.py` es SELECT-only por contrato y así se queda. Este módulo es
la excepción DELIBERADA y acotada (decisión Tamara 2026-08-27, ampliada
2026-09-01): Programa Core manda memos de pedidos a la tabla `memos` de
formulas_app, y nada más. El cerco no es una promesa del código sino del ROL
de la base: `programa_core_memos` tiene SELECT, INSERT y un UPDATE acotado
por columna sobre `public.memos` (estado, cliente, vendedor, enviado_por,
enviado_en, detalle, cancelado_por, cancelado_en y, desde el 04/09,
modificado_en, modificado_por, cambios) y ningún otro privilegio —
`en_proceso_por/en_proceso_en/terminado_por/terminado_en/id/pedido_numero`
siguen siendo exclusivos de la fábrica. Aunque alguien escriba acá un UPDATE
a `ordenes`, o a una columna fuera de esa lista, Postgres lo rechaza.

El memo es una FOTO del pedido al momento de enviar. Si el pedido cambia en
Asinfo después, el sync de fondo (`modules/pedidos/memos_sync.py`, 04/09)
pisa la foto y deja la alerta en la fábrica; también se puede mandar de nuevo (y el UNIQUE de
`pedido_numero` en formulas_app hace que el segundo envío avise "ya estaba"
en vez de duplicar, salvo que el memo esté 'cancelado': ahí reactiva la
MISMA fila con datos frescos). Un memo 'pendiente' también se puede
`cancelar()` desde acá — sólo mientras la fábrica no lo tomó.

Fail-soft como todo el bridge: sin env var o con la base caída, `enviar()` y
`cancelar()` devuelven (False, "sin_bridge") y `estados()` devuelve {} — la
pantalla muestra el botón igual y el envío avisa que no pudo, nunca rompe.

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


def _url_configurada() -> str:
    """FORMULAS_MEMOS_DATABASE_URL, del entorno o del registro de Windows.

    La variable la escribe el deploy de formulas_app como variable de MÁQUINA
    (scripts/setup_programa_core_memos_role.py de ese repo). El Task Scheduler
    de Windows a veces arranca el proceso con un bloque de entorno cacheado
    que no la trae — mismo motivo por el que el launcher de formulas hidrata
    sus variables a mano. Leer el registro directo saca esa duda del medio.
    En Linux (tests, vista_local) el registro no existe y vale el entorno.
    """
    url = os.environ.get("FORMULAS_MEMOS_DATABASE_URL", "").strip()
    if url or os.name != "nt":
        return url
    try:
        import winreg
        clave = r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, clave) as k:
            return str(winreg.QueryValueEx(k, "FORMULAS_MEMOS_DATABASE_URL")[0]).strip()
    except OSError:
        return ""


def init_pool() -> None:
    """Abre el pool si FORMULAS_MEMOS_DATABASE_URL está seteada.

    Llamar una vez desde create_app(), al lado de formulas_db.init_pool().
    Idempotente.
    """
    global _pool
    if _pool is not None:
        return
    url = _url_configurada()
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
    """Inserta el memo, o reactiva uno CANCELADO con datos frescos. Devuelve
    `(ok, motivo)`.

    Si ya existe una fila con este `pedido_numero` en estado 'cancelado', el
    ON CONFLICT la reactiva (vuelve a 'pendiente', pisa cliente/vendedor/
    detalle/enviado_por/enviado_en, limpia cancelado_por/cancelado_en) en vez
    de insertar una fila nueva — el UNIQUE de formulas_app es sobre
    `pedido_numero`. Si el conflicto es con una fila VIVA (pendiente/
    en_proceso/terminado) el WHERE no matchea, 0 filas, mismo "ya_enviado"
    de siempre.

    motivos: "enviado" (quedó, nueva o reactivada), "ya_enviado" (ya había un
    memo vivo — el UNIQUE de formulas_app lo frena), "sin_bridge" (env var
    vacía o base caída).
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
                    ON CONFLICT (pedido_numero) DO UPDATE
                       SET cliente = EXCLUDED.cliente,
                           vendedor = EXCLUDED.vendedor,
                           enviado_por = EXCLUDED.enviado_por,
                           enviado_en = NOW(),
                           detalle = EXCLUDED.detalle,
                           estado = 'pendiente',
                           cancelado_por = NULL,
                           cancelado_en = NULL
                     WHERE memos.estado = 'cancelado'
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


def cancelar(numero: str, usuario: str) -> tuple[bool, str]:
    """Cancela un memo PENDIENTE — el pedido cambió después de enviarlo y
    hay que mandar una foto nueva. No toca memos que la fábrica ya tomó
    ('en_proceso'/'terminado'): ahí cancelar podría dejar huérfana una
    orden de tintura ya armada (decisión Tamara 2026-09-01).

    Devuelve (ok, motivo): "cancelado", "no_pendiente" (ya lo tomó la
    fábrica, ya estaba cancelado, o no existe), "sin_bridge".
    """
    if _pool is None:
        return False, "sin_bridge"
    try:
        with _conn() as c:
            with c.cursor() as cur:
                cur.execute(
                    """
                    UPDATE memos
                       SET estado = 'cancelado',
                           cancelado_por = %s,
                           cancelado_en = NOW()
                     WHERE pedido_numero = %s AND estado = 'pendiente'
                 RETURNING id
                    """,
                    (usuario or "", numero),
                )
                fila = cur.fetchone()
            c.commit()
        return (True, "cancelado") if fila else (False, "no_pendiente")
    except Exception as e:  # noqa: BLE001 — fail-soft por contrato del bridge
        _log.warning("formulas_memos.cancelar falló: %s", e)
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


# ── Memos que cambiaron en Asinfo después de mandarlos (2026-09-04) ─────────
# David (audio 04/09): "Irene le modifica el pedido y a David no le refleja".
# Programa Core vigila `pedido_cliente.fecha_modificacion` y, si el pedido
# cambió después del envío, pisa `detalle` con la foto nueva y deja en
# `cambios` qué cambió, con `modificado_en/modificado_por` para la alerta de
# la fábrica. Son las tres columnas nuevas del grant (formulas_app 04/09).

def vivos() -> list[dict]:
    """Los memos que la fábrica todavía tiene entre manos ('pendiente' o
    'en_proceso'), con su foto — lo que hay que vigilar contra Asinfo.
    [] sin bridge o con la base caída."""
    if _pool is None:
        return []
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute(
                """
                SELECT pedido_numero, estado, enviado_en, detalle
                  FROM memos
                 WHERE estado IN ('pendiente', 'en_proceso')
                """
            )
            filas = cur.fetchall()
        return [{"numero": r[0], "estado": r[1], "enviado_en": r[2],
                 "detalle": r[3] if isinstance(r[3], dict) else (r[3] or {})}
                for r in filas]
    except Exception as e:  # noqa: BLE001 — fail-soft por contrato del bridge
        _log.warning("formulas_memos.vivos falló: %s", e)
        return []


def actualizar(numero: str, detalle: dict, cambio: dict | None,
               modificado_por: str) -> tuple[bool, str]:
    """Pisa la foto del memo con `detalle`. Si `cambio` viene (hubo
    diferencias visibles), además sella `modificado_en/modificado_por` y
    agrega `cambio` al final de `cambios` — eso es lo que prende la alerta
    en la fábrica. Sin `cambio`, sólo se refresca la foto en silencio (el
    pedido se tocó en Asinfo pero lo que ve la fábrica quedó igual).

    Sólo toca memos vivos: si mientras tanto la fábrica lo terminó o
    Programa Core lo canceló, no se pisa nada ("no_vivo").
    """
    if _pool is None:
        return False, "sin_bridge"
    try:
        with _conn() as c:
            with c.cursor() as cur:
                if cambio:
                    cur.execute(
                        """
                        UPDATE memos
                           SET detalle = %s,
                               modificado_en = NOW(),
                               modificado_por = %s,
                               cambios = COALESCE(cambios, '[]'::jsonb) || %s::jsonb
                         WHERE pedido_numero = %s
                           AND estado IN ('pendiente', 'en_proceso')
                     RETURNING id
                        """,
                        (json.dumps(detalle, default=str), modificado_por or "",
                         json.dumps([cambio], default=str), numero),
                    )
                else:
                    cur.execute(
                        """
                        UPDATE memos
                           SET detalle = %s
                         WHERE pedido_numero = %s
                           AND estado IN ('pendiente', 'en_proceso')
                     RETURNING id
                        """,
                        (json.dumps(detalle, default=str), numero),
                    )
                fila = cur.fetchone()
            c.commit()
        return (True, "actualizado") if fila else (False, "no_vivo")
    except Exception as e:  # noqa: BLE001 — fail-soft por contrato del bridge
        _log.warning("formulas_memos.actualizar falló: %s", e)
        return False, "sin_bridge"
