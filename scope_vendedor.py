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
    # ⭐ TMT 2026-08-25 — se les abre la Competencia, el día de la largada.
    # Estuvo frenada desde el 17/08 por pedido de la dueña ("todavia igual no
    # se las habilites") hasta que dijo "abrilo para vendedores". Les quedan
    # las tres pantallas que cuelgan de este prefijo: el tablero, /telas (los
    # saldos CON SUS clientes) y /mi-hoja (la hoja para imprimir). Las tres ya
    # recortan por el vendedor del USUARIO logueado, nunca por la URL.
    #
    # ⚠ El matcheo es por segmento, así que TODO lo que cuelgue de este prefijo
    # les queda abierto — por eso la pantalla de metas vive en /analisis/metas y
    # la lista con los clientes de TODOS en /analisis/parado/clientes, las dos
    # afuera. Antes de colgar una pantalla nueva de /analisis/competencia,
    # preguntarse si un vendedor la puede ver.
    "/analisis/competencia",
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
    # El flow OAuth de Google: /auth/google/login redirige al consent screen y
    # /auth/google/callback vuelve con el code. Los dos corren ANTES de que
    # exista una sesión, así que pedirles permiso es imposible por
    # construcción: sería preguntarle los permisos a un usuario que todavía no
    # se sabe quién es. Estaban acusados de "ruta sin permiso". TMT 2026-08-09.
    "/auth/google",
    # ⭐ Sin esto, la dueña que usa "👁 Ver como" sobre un vendedor queda
    # ENCERRADA: el botón "Volver a mi cuenta" postea acá y el allowlist se
    # lo comía con un 404, sin más salida que borrar la cookie de sesión.
    # Un candado no puede cerrar la puerta por la que se entró.
    "/stop-impersonate",
)

# Adónde mandamos al vendedor cuando pide una pantalla de ENTRADA.
HOME_VENDEDOR = "/mi-cartera"

# ⭐ Las pantallas de ENTRADA se REDIRIGEN, no se 404ean.
#
# El login manda a `dashboard.index` (`/tablero/`), que a su vez rebota a
# `historial.operaciones` (`/operaciones`). Ninguna de las dos está en el
# allowlist, así que un vendedor que ponía su usuario y contraseña aterrizaba
# en un **404** — lo primero que veía del sistema. Lo mismo al usar "Ver como"
# y al clickear "Volver al inicio" desde cualquier página de error.
#
# Es el mismo error que tenía `/stop-impersonate`, y por eso está acá arriba
# escrito: **un candado no puede cerrar la puerta por la que se entra ni la
# puerta por la que se sale.** Cada vez que alguien hardcodea "la home" en un
# redirect, esta lista es la que evita que el vendedor termine en un 404.
PREFIJOS_HOME: tuple[str, ...] = (
    "/",
    "/tablero",
    "/operaciones",
)

# ⭐ Las pantallas de la OFICINA que el vendedor tiene con otra ruta se
# redirigen a la suya, tampoco se 404ean.
#
# TMT 2026-08-26 (dueña, mirando la sección como Patricio): *"¿por qué no puedo
# ver saldos como patricio, o a quién ofrecerle qué?"*. Sí puede: son las
# MISMAS pantallas, con sus clientes adentro, colgadas de
# /analisis/competencia. Lo que no funciona es llegar por la URL de la oficina
# —un bookmark, un link copiado, o la dueña previsualizando con "Ver como"— y
# ahí el allowlist contestaba un 404 seco.
#
# ⚠ Redirigir sólo donde la pantalla EXISTE del otro lado. Para todo lo demás
# el 404 se queda: el vendedor no tiene por qué enterarse de qué hay.
EQUIVALENTE_VENDEDOR: dict[str, str] = {
    "/analisis": "/analisis/competencia",
    "/analisis/parado": "/analisis/competencia/telas",
    "/analisis/parado/clientes": "/analisis/competencia/mi-hoja",
}


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

    if path == "/" or _path_permitido(path, PREFIJOS_HOME):
        return redirect(HOME_VENDEDOR)

    destino = EQUIVALENTE_VENDEDOR.get(path.rstrip("/") or "/")
    if destino:
        return redirect(destino)

    if _path_permitido(path, PREFIJOS_PERMITIDOS):
        return None
    if _path_permitido(path, PREFIJOS_INFRA):
        return None

    # Mismo criterio que `requiere_permiso`: 404, no 403 — que no se entere
    # de que existen secciones a las que no puede entrar.
    return render_template("404.html"), 404
