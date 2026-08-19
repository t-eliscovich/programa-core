"""Quién ve cada aviso: el permiso de la PANTALLA a la que lleva.

TMT 2026-08-19 (dueña): *"¿podés habilitar la campanita para INT? que vean
despachos y facturas. y que vean de cualquier notificación que sea páginas que
ellos están habilitados"*.

Hasta hoy la campanita entera colgaba de `compras.ver` — un permiso que INT
perdió el 05/08, cuando se le sacaron compras, activos, gastos, iniciales,
provisiones y retenciones. Resultado: el rol que MÁS opera (Maribel, Alex) era
justo el que no veía ninguna novedad, ni siquiera las de ventas y facturas, que
son suyas.

⭐ La regla NO es una lista de fuentes por rol escrita a mano. Cada aviso ya
trae el `url` de la pantalla que lo resuelve; ese path se resuelve contra el
`url_map` real de Flask y se lee el atributo `_permiso` que deja
`requiere_permiso` — el mismo mecanismo que usa `/usuarios/accesos`, y por la
misma razón: una lista a mano se desactualiza el día que alguien toca un
decorador y nadie se entera. Si podés ABRIR la pantalla, ves el aviso.

Fail-closed a propósito: un aviso cuyo destino no resuelve y cuya fuente no
está en `FUENTE_PERMISO` sólo lo ven los roles wildcard (Accionista y
Administrador). Un aviso de más manda a alguien a un 404 y le enseña a ignorar
la campanita; uno de menos lo ve igual la dueña, que ve todo.
"""
from __future__ import annotations

#: Red de contención para los avisos SIN url (o con un url que no resuelve).
#: El url MANDA: esto es sólo el fallback, y existe para que una fuente nueva
#: no desaparezca en silencio. `tests/test_avisos_visibilidad.py` valida que
#: toda fuente conocida esté acá.
FUENTE_PERMISO: dict[str, str] = {
    "ventas": "facturas.ver",
    "tejeduria": "tejeduria.ver",
    "quimicos": "tintura.ver",
    "importaciones": "compras.ver",
    "hilo-local": "compras.ver",
    "retenciones": "retenciones.ver",
    "clientes": "clientes.ver",
    "stock": "stock.ver",
    "traza": "informes.ver",
}

#: "La ruta no resolvió" — distinto de "resolvió y no pide permiso" (None).
_NO_RESUELVE = object()


def _path(url: str | None) -> str:
    """El path pelado: sin query string ni ancla, y sólo si es interno."""
    u = (url or "").strip()
    if not u or not u.startswith("/") or u.startswith("//"):
        return ""
    return u.split("?", 1)[0].split("#", 1)[0]


def _permiso_de_ruta(path: str):
    """El `_permiso` que exige esa pantalla, None si no exige, o _NO_RESUELVE."""
    if not path:
        return _NO_RESUELVE
    try:
        from flask import current_app

        adapter = current_app.url_map.bind("localhost")
        endpoint, _args = adapter.match(path, method="GET")
        vista = current_app.view_functions.get(endpoint)
    except Exception:  # noqa: BLE001 -- 404, redirect por la barra, sin app…
        return _NO_RESUELVE
    if vista is None:
        return _NO_RESUELVE
    return getattr(vista, "_permiso", None)


def permiso_del_aviso(fuente: str | None, url: str | None):
    """Qué permiso hace falta para este aviso.

    None = ninguno (la pantalla está abierta a cualquier logueado).
    `_NO_RESUELVE` = no se pudo saber → sólo wildcard.
    """
    p = _permiso_de_ruta(_path(url))
    if p is not _NO_RESUELVE and p is not None:
        return p
    # La pantalla no resolvió, o resolvió y no pide permiso (se controla sola
    # adentro, como /dolares). En los dos casos la ruta no da señal y manda la
    # fuente. Si tampoco está mapeada, `_NO_RESUELVE` → sólo wildcard.
    por_fuente = FUENTE_PERMISO.get((fuente or "").strip(), _NO_RESUELVE)
    if por_fuente is _NO_RESUELVE and p is None:
        return None          # ruta abierta a propósito y fuente desconocida
    return por_fuente


def hay_que_filtrar() -> bool:
    """False fuera de un request, sin usuario, o si la persona tiene `*`."""
    try:
        from flask import g, has_request_context

        if not has_request_context() or not g.get("user"):
            return False
        return "*" not in (g.get("permisos") or set())
    except Exception:  # noqa: BLE001
        return False


def puede_ver(fuente: str | None, url: str | None) -> bool:
    if not hay_que_filtrar():
        return True
    permiso = permiso_del_aviso(fuente, url)
    if permiso is None:
        return True
    if permiso is _NO_RESUELVE:
        return False
    try:
        from flask import g

        return permiso in (g.get("permisos") or set())
    except Exception:  # noqa: BLE001
        return False


def filtrar(items: list[dict]) -> list[dict]:
    """Los avisos que ESTA persona puede abrir. Nunca rompe la campanita."""
    try:
        if not hay_que_filtrar():
            return items
        return [a for a in items if puede_ver(a.get("fuente"), a.get("url"))]
    except Exception:  # noqa: BLE001
        return items


def fuentes_visibles(fuentes: dict[str, str]) -> dict[str, str]:
    """El filtro por tema de /novedades, sin los temas que no puede abrir."""
    try:
        if not hay_que_filtrar():
            return fuentes
        return {k: v for k, v in fuentes.items() if puede_ver(k, None)}
    except Exception:  # noqa: BLE001
        return fuentes
