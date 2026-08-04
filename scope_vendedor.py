"""Scope de datos para usuarios VENDEDOR — allowlist de rutas, fail-closed.

TMT 2026-08-03 (dueña): *"crear varios usuarios nuevos, que serían los
vendedores… tienen que tener acceso solo a sus clientes, sus facturas, sus
cheques"*.

Por qué un allowlist y no `@requiere_permiso` en cada ruta
-----------------------------------------------------------
El modelo de permisos de Programa Core gatea la RUTA, no las FILAS: si un
usuario tiene `facturas.ver`, ve TODAS las facturas de la empresa. Para un
vendedor eso no alcanza — necesita ver un subconjunto.

La solución obvia (poner el filtro por vendedor en cada pantalla) es
**fail-open**: si me olvido de una superficie, el vendedor ve la cartera de
los otros cinco y nada se rompe — sin error, sin aviso, sin log. Una auditoría
del 2026-08-03 encontró **31 rutas alcanzables por cualquier usuario logueado
sin ningún permiso** (`/anticipos/nuevo`, `/cheques/<id>/reversar`,
`/historial/<id>/reverso`, las 4 de estado de cuenta, …), varias de ellas de
ESCRITURA. Enumerar y gatear todas a mano deja el mismo agujero abierto para
la ruta 32 que alguien agregue mañana.

Este módulo invierte la lógica: un usuario CON `vend` asignado (= un
vendedor) sólo puede tocar los paths del allowlist. Cualquier otra cosa —
existente o futura — devuelve 404. Fail-CLOSED por construcción: una pantalla
nueva nace inaccesible para el vendedor hasta que alguien la agregue acá a
propósito.

Para todos los demás usuarios (`vend` vacío o NULL) este hook es un no-op
exacto: no cambia una sola respuesta.
"""

from __future__ import annotations

from flask import g, redirect, render_template, request

# Paths que un vendedor SÍ puede tocar. Se matchea por prefijo exacto de
# segmento (ver `_path_permitido`), así que "/mi-carterax" NO entra por
# "/mi-cartera".
PREFIJOS_PERMITIDOS: tuple[str, ...] = (
    "/mi-cartera",  # el portal del vendedor (todas sus sub-rutas)
)

# Infraestructura: login/logout, estáticos, health. Sin esto el vendedor no
# podría ni desloguearse ni cambiar su contraseña.
PREFIJOS_INFRA: tuple[str, ...] = (
    "/static",
    "/favicon",
    "/healthz",
    "/_healthz",
    "/login",
    "/logout",
    "/password",
)

# Adónde mandamos al vendedor cuando pide "/" (la raíz redirige al tablero
# general, que él no puede ver).
HOME_VENDEDOR = "/mi-cartera"


def vendedor_de(user: dict | None) -> str:
    """Código de vendedor del usuario, normalizado. '' si no es vendedor.

    Un usuario es "vendedor" si tiene `seguridad.usuario.vend` cargado. No
    depende del NOMBRE del rol a propósito: renombrar un rol no puede abrir
    el acceso en silencio (ya pasó con Dueño→Accionista y la IP allowlist).
    """
    if not user:
        return ""
    return (user.get("vend") or "").strip().upper()


def es_vendedor(user: dict | None) -> bool:
    return bool(vendedor_de(user))


def _path_permitido(path: str, prefijos: tuple[str, ...]) -> bool:
    """True si `path` es exactamente un prefijo o cuelga de él como segmento."""
    return any(
        path == p or path.startswith(p + "/") or path.startswith(p + "?")
        for p in prefijos
    )


def enforce_scope_vendedor():
    """before_request: acota a un vendedor a su portal. No-op para el resto.

    DEBE registrarse DESPUÉS de `load_logged_in_user` (necesita `g.user`).
    """
    user = g.get("user")
    if not es_vendedor(user):
        return None

    path = request.path or "/"

    if path == "/":
        return redirect(HOME_VENDEDOR)

    if _path_permitido(path, PREFIJOS_PERMITIDOS):
        return None
    if _path_permitido(path, PREFIJOS_INFRA):
        return None

    # Mismo criterio que `requiere_permiso`: 404, no 403 — que no se entere
    # de que existen secciones a las que no puede entrar.
    return render_template("404.html"), 404
