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

#: El permiso del TEMA. Se pide ADEMÁS del de la pantalla, no en su lugar.
#:
#: TMT 2026-08-19 (dueña, mirando la campanita de Maribel): *"por ej, esto ve
#: maribel y no debería"* — un aviso de **hilo local** ("se cargó a compras"),
#: visible porque su `url` es `/importaciones`, que pide `stock.ver`. La
#: pantalla a la que lleva no alcanza para decidir: dice a dónde va el link,
#: no de qué es el aviso. Hacen falta las dos llaves.
#: `tests/test_avisos_visibilidad.py` valida que toda fuente conocida esté acá.
FUENTE_PERMISO: dict[str, str] = {
    "ventas": "facturas.ver",
    "tejeduria": "tejeduria.ver",
    "quimicos": "tintura.ver",
    "importaciones": "compras.ver",
    "hilo-local": "compras.ver",
    # OJO: NO es `retenciones.ver` (ese es el módulo de EMITIR retenciones,
    # que INT no tiene). Estos avisos son las retenciones de CLIENTES que
    # llegan de Asinfo y se aplican a las facturas: viven en
    # /facturas/retenciones-asinfo y son parte de la carga de facturas.
    "retenciones": "facturas.crear",
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


def permisos_del_aviso(fuente: str | None, url: str | None):
    """Los permisos que hacen falta para este aviso — hay que tenerlos TODOS.

    `set()` = ninguno (pantalla abierta y tema conocido sin permiso propio).
    `_NO_RESUELVE` = no se pudo saber nada → sólo wildcard.
    """
    de_ruta = _permiso_de_ruta(_path(url))
    de_fuente = FUENTE_PERMISO.get((fuente or "").strip())
    if de_ruta is _NO_RESUELVE and de_fuente is None:
        # Ni la pantalla ni el tema dicen nada: no hay con qué decidir.
        return _NO_RESUELVE
    pedidos = {p for p in (None if de_ruta is _NO_RESUELVE else de_ruta,
                           de_fuente) if p}
    return pedidos


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
    pedidos = permisos_del_aviso(fuente, url)
    if pedidos is _NO_RESUELVE:
        return False
    if not pedidos:
        return True
    try:
        from flask import g

        return pedidos <= (g.get("permisos") or set())
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


def fuentes_visibles(fuentes: dict[str, str], items: list[dict] | None = None,
                    fuente_actual: str | None = None) -> dict[str, str]:
    """El filtro por tema de /novedades, sin los temas que no puede abrir.

    Con `items` se arma de lo que la persona REALMENTE está viendo: un tema
    puede pasar el filtro por su permiso propio y no tener ni un aviso visible
    (químicos pide `tintura.ver`, pero sus avisos llevan a /compras). Un chip
    que no filtra nada es una pestaña que miente.
    """
    try:
        if not hay_que_filtrar():
            return fuentes
        if items is None:
            return {k: v for k, v in fuentes.items() if puede_ver(k, None)}
        vistos = {(a.get("fuente") or "") for a in items}
        if fuente_actual:
            vistos.add(fuente_actual)   # para poder volver a "Todo" desde acá
        return {k: v for k, v in fuentes.items() if k in vistos}
    except Exception:  # noqa: BLE001
        return fuentes
