"""Mapa de accesos: qué pantalla abre cada rol.

TMT 2026-08-05 (dueña, después de cerrar Comisiones, Metas y Posdatados en el
mismo día): *"¿tenemos una lista de las páginas y accesos?"*. No la había. La
única forma de saber quién ve qué era leer `config/roles.py` y cruzarlo a mano
con los decoradores de cada vista.

⭐ NO es una lista escrita a mano. Se arma en vivo cruzando dos fuentes que ya
existen y que son las que MANDAN:

  · el `url_map` de Flask + el atributo `_permiso` que deja `requiere_permiso`
    → qué exige cada ruta DE VERDAD;
  · `seguridad.permiso` en la base → qué tiene cada rol AHORA MISMO.

Una lista escrita a mano se desactualiza el día que alguien toca un decorador
y nadie se entera. Ésta no puede: si el decorador cambia, la pantalla cambia.

Y por eso también sirve como control: la sección de rutas SIN permiso es la
auditoría del 2026-08-03 —31 rutas alcanzables por cualquier usuario logueado,
varias de escritura— pero corriendo sola cada vez que se abre la pantalla, en
vez de una vez y a mano.
"""

from __future__ import annotations

from collections import defaultdict

import db
from scope_vendedor import PREFIJOS_INFRA as _INFRA_A_PROPOSITO

# Lo que a propósito NO pide permiso: login, logout, cambio de contraseña,
# estáticos, healthchecks y la salida del "👁 Ver como".
#
# ⭐ Se REUSA `scope_vendedor.PREFIJOS_INFRA` en vez de escribir otra lista.
# Esa constante ya es la definición canónica de "esto lo puede tocar
# cualquiera a propósito" — es la que deja salir al vendedor del portal. Dos
# listas para la misma idea se despegan a la primera que alguien edite, y acá
# despegarse significa que la pantalla acusa a `/logout` de ser un agujero.
#
# Que no aparezcan importa: un aviso rojo con cuatro falsos positivos adentro
# enseña a ignorar el aviso rojo. Mismo criterio que el ⚠ que se sacó del
# panel de coherencia el 2026-07-30.




def _es_pantalla(regla) -> bool:
    if any(str(regla.rule).startswith(p) for p in _INFRA_A_PROPOSITO):
        return False
    # Sólo lo que se puede ABRIR con el navegador.
    return "GET" in (regla.methods or set())


def mapa(app) -> dict:
    """{permisos, sin_permiso, roles} — todo derivado, nada hardcodeado."""
    por_permiso: dict[str, set[str]] = defaultdict(set)
    sin_permiso: set[str] = set()

    for regla in app.url_map.iter_rules():
        if not _es_pantalla(regla):
            continue
        vista = app.view_functions.get(regla.endpoint)
        permiso = getattr(vista, "_permiso", None)
        if permiso:
            por_permiso[permiso].add(str(regla.rule))
        else:
            sin_permiso.add(str(regla.rule))

    return {
        "permisos": sorted(
            ({"permiso": p, "rutas": sorted(r)} for p, r in por_permiso.items()),
            key=lambda x: x["permiso"],
        ),
        "sin_permiso": sorted(sin_permiso),
    }


def roles_con_permisos() -> list[dict]:
    """Cada rol, sus permisos y cuántos usuarios ACTIVOS tiene.

    ⭐ El contador de usuarios es la mitad que hace útil la tabla: de los diez
    roles que existen, hoy sólo cuatro tienen gente. Una columna de un rol
    vacío es una columna que no le importa a nadie, y mezclada con las demás
    hace parecer que una pantalla la ve más gente de la que la ve.
    """
    filas = db.fetch_all(
        """
        SELECT r.id_rol, r.nombre_rol,
               COALESCE(u.n, 0) AS usuarios
          FROM seguridad.rol r
          LEFT JOIN (SELECT id_rol, COUNT(*) AS n
                       FROM seguridad.usuario
                      WHERE COALESCE(activo, TRUE) = TRUE
                      GROUP BY id_rol) u ON u.id_rol = r.id_rol
         ORDER BY COALESCE(u.n, 0) DESC, r.nombre_rol
        """
    ) or []
    permisos = db.fetch_all(
        "SELECT id_rol, nombre_opcion FROM seguridad.permiso"
    ) or []
    por_rol: dict[int, set[str]] = defaultdict(set)
    for p in permisos:
        por_rol[p["id_rol"]].add(p["nombre_opcion"])

    return [
        {
            "nombre": f["nombre_rol"],
            "usuarios": int(f["usuarios"] or 0),
            "permisos": por_rol.get(f["id_rol"], set()),
            "wildcard": "*" in por_rol.get(f["id_rol"], set()),
        }
        for f in filas
    ]


def puede(rol: dict, permiso: str) -> bool:
    return rol["wildcard"] or permiso in rol["permisos"]
