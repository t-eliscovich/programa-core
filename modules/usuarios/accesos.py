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

import re
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




# ---------------------------------------------------------------------------
# Qué es una PANTALLA y qué es plomería
# ---------------------------------------------------------------------------
#
# TMT 2026-08-09 (dueña, mirando la pantalla): *"¿podemos simplificar esta
# lista?"*. Eran 277 rutas impresas una al lado de la otra. La pregunta que la
# pantalla contesta es "qué VE cada rol", y para contestarla no sirve de nada
# saber que existe `/cheques/_api/anticipos-vivos/<codigo_cli>`: eso es un JSON
# que pide un `<select>`, no algo que alguien abra. Sesenta y siete de las 277
# eran de ese tipo, y estaban mezcladas con las de verdad, así que había que
# leerlas todas para encontrar las que importan.
#
# ⭐ No se BORRAN: se ocultan y se cuentan (`?tecnicas=1` las muestra). Una
# lista que esconde cosas sin decir cuántas miente igual que una que no filtra.
#
# Las tres familias, y por qué cada una no es una pantalla:
_FRAGMENTO = (           # devuelven JSON o un pedacito de HTML para otra vista
    "_api", "_diag", "/fragment", "card-", "preview",
)
_DIAGNOSTICO = (         # existen para mirar el motor, no para operar
    "debug", "diag", "/admin/health/", "/admin/salud/", "auditoria",
    "-audit", "check-",
)
_DESCARGA = (            # el archivo que escupe una pantalla que ya está en la lista
    ".xlsx", "-xlsx", "/excel", "/xml", "/pdf", "/imprimir", "imprimible",
    "/boleta",
)


def clase_tecnica(ruta: str) -> str | None:
    """"api" / "diagnostico" / "descarga", o None si es una pantalla."""
    if any(s.startswith("_") for s in ruta.strip("/").split("/")):
        return "api"
    for grupo, nombre in ((_FRAGMENTO, "api"), (_DIAGNOSTICO, "diagnostico"),
                          (_DESCARGA, "descarga")):
        if any(t in ruta for t in grupo):
            return nombre
    return None


def _es_pantalla(regla) -> bool:
    if any(str(regla.rule).startswith(p) for p in _INFRA_A_PROPOSITO):
        return False
    # Sólo lo que se puede ABRIR con el navegador.
    return "GET" in (regla.methods or set())


# Señales de que la vista se controla SOLA, sin decorador. Una ruta sin
# `requiere_permiso` no es necesariamente una ruta abierta: varias chequean
# adentro (`/impersonate` pide Accionista con un `if`, `/dolares` llama a
# `tiene_permiso`). Meterlas en la misma lista roja que las que no chequean
# NADA es exactamente el error que convierte un aviso en decoración —
# 30 rutas acusadas, la mitad inocentes, y nadie la mira dos veces.
_SEÑALES_DE_CONTROL = (
    "tiene_permiso",     # el helper de siempre, usado a mano
    "g.permisos",        # el set crudo
    "nombre_rol",        # "solo un Accionista puede…"
    "es_vendedor",       # el portal
)

# ⚠ `abort(404)` NO cuenta como control, aunque lo parezca. La mayoría de las
# vistas lo usan para "ese cheque no existe", no para "vos no podés". Contarlo
# marcaba como controladas a `/cheques/<id>/deshacer-deposito` y compañía, que
# son ESCRITURAS y no chequean nada.
#
# ⭐ Ante la duda, la ruta se queda en la lista roja. Una alarma de más se
# revisa y se descarta en un minuto; una alarma de menos esconde el agujero
# para siempre, y encima con la pantalla diciendo que está todo bien.


# ---------------------------------------------------------------------------
# Las que YA se miraron una por una
# ---------------------------------------------------------------------------
#
# TMT 2026-08-09: la lista roja tenía 26 rutas y **12 no eran un agujero** —
# redirects, el flow de Google que por definición corre antes del login, y
# `/iniciales`, que sí chequea. Una alarma con la mitad de mentiras adentro es
# una alarma que se ignora; el detector arregla esas doce, y las que quedan
# son de verdad, así que se revisaron a mano y la dueña decidió qué hacer.
#
# ⭐ Esto NO es "silenciar el aviso": es separar *lo que ya se miró* de *lo que
# nadie miró todavía*. El bloque rojo queda para lo segundo, que es lo único
# que sirve mirar. Cada línea lleva el POR QUÉ, así el que la lea dentro de un
# año puede discutirla en vez de tenerle miedo.
#
# ⚠ Y no puede podrirse: `tests/test_accesos_aceptadas.py` falla si una de
# estas rutas deja de existir. Una excepción a una ruta que ya no existe es
# una excepción que mañana tapa otra cosa con el mismo nombre.
ACEPTADAS: dict[str, str] = {
    # Abiertas a propósito y ya escrito en el código.
    "/precios": "la lista de precios la ven todos los roles (sólo pide login)",
    "/precios/imprimir": "la hoja de la lista de precios — mismo criterio",
    "/mi-historial": "es el historial DEL usuario logueado: ya está acotado a quien la abre",

    # Lectura del estado de cuenta. Los vendedores quedan afuera por
    # scope_vendedor.py, que sólo les deja /mi-cartera.
    "/informes/estado-cuenta": "estado de cuenta: lectura, aceptada 2026-08-09",
    "/informes/estado-cuenta/<codigo_cli>": "estado de cuenta: lectura, aceptada 2026-08-09",
    "/informes/estado-cuenta/<codigo_cli>/pdf": "estado de cuenta: lectura, aceptada 2026-08-09",
    "/informes/estado-cuenta/grupos": "estado de cuenta: lectura, aceptada 2026-08-09",
    "/informes/estado-cuenta/imprimir": "estado de cuenta: lectura, aceptada 2026-08-09",
    "/cheques/<int:id_cheque>/reverso-preview": "JSON del modal de reverso: no escribe",
    "/historial/<int:id_mov_doble>/reverso-preview": "JSON del modal de reverso: no escribe",

    # ESCRITURAS sin candado. Tamara, 2026-08-09: "dejarlas así" — todas las
    # cuentas de hoy son de confianza. Quedan acá y no en rojo para que el
    # rojo signifique "esto es nuevo y nadie lo miró".
    "/cheques/<int:id_cheque>/anular-error-carga": "escritura sin candado — aceptada por Tamara 2026-08-09",
    "/cheques/<int:id_cheque>/confirmar-reverso": "escritura sin candado — aceptada por Tamara 2026-08-09",
    "/cheques/<int:id_cheque>/desaplicar/<int:id_factura>": "escritura sin candado — aceptada por Tamara 2026-08-09",
    "/cheques/<int:id_cheque>/deshacer-deposito": "escritura sin candado — aceptada por Tamara 2026-08-09",
    "/cheques/corregir-devoluciones-sin-nd": "escritura sin candado — aceptada por Tamara 2026-08-09",
    "/historial/<int:id_mov_doble>/reverso": "escritura sin candado — aceptada por Tamara 2026-08-09",
    # `/informes/factura/cambio-stat/.../deshacer` NO está acá: parecía un
    # agujero y resultó que chequea con `_puede_tocar_stat_factura()`. Lo
    # descubrió el detector nuevo, no una lectura a mano. TMT 2026-08-09.

    # Muertas: comparaban Programa Core contra el dBase, que se retiró el
    # 2026-08-05. Se van en la limpieza de septiembre (BACKLOG.md).
    "/admin/dbase-compare/": "muerta: el dBase se retiró el 2026-08-05 — se borra en septiembre",
    "/admin/dbase-compare/vivo": "muerta: el dBase se retiró el 2026-08-05 — se borra en septiembre",
}


def _fuente(fn) -> str:
    import inspect

    try:
        return inspect.getsource(inspect.unwrap(fn))
    except (OSError, TypeError):  # pragma: no cover — builtins, parciales
        return ""


# Una vista que sólo REDIRIGE no muestra nada, así que no hay nada que
# proteger. TMT 2026-08-09: la lista roja acusaba a `/`, a `/tablero/` y a
# `/anticipos/` —las tres rebotan a otra pantalla, y la de destino sí tiene su
# candado—. Acusar a un redirect es acusar a la puerta en vez de al cuarto.
_QUE_MUESTRA_ALGO = ("render_template", "jsonify", "send_file", "Response(")


def _es_redirect_puro(src: str) -> bool:
    return "redirect(" in src and not any(t in src for t in _QUE_MUESTRA_ALGO)


def _control_interno(vista) -> str | None:
    """Qué chequeo hace la vista por su cuenta, si hace alguno.

    Se lee el CÓDIGO de la función (no un listado aparte) para que esto no se
    despegue: si alguien le saca el `if` a una vista, acá se nota solo.

    ⭐ Y también el código de los HELPERS del mismo módulo que la vista llama.
    TMT 2026-08-09: `/iniciales` aparecía en la lista roja como si fuera un
    agujero, y hace exactamente lo que hay que hacer —`if not
    _puede_ver_iniciales(): abort(404)`—; lo único que pasaba es que el
    `tiene_permiso` está una función más adentro. Un detector que sólo mira el
    cuerpo de la vista premia al que copia y pega el chequeo y castiga al que
    lo factoriza, que es al revés de lo que uno quiere.
    """
    src = _fuente(vista)
    if not src:
        return None
    for señal in _SEÑALES_DE_CONTROL:
        if señal in src:
            return señal
    if _es_redirect_puro(src):
        return "redirect"
    # Un nivel de indirección: los helpers `_algo()` del módulo de la vista.
    # Uno solo — con dos ya no se entiende quién controla a quién, y un
    # detector que nadie entiende se desactiva al primer falso positivo.
    # ⚠ Los globals hay que sacarlos de la función DESENVUELTA: `vista` es el
    # wrapper que dejó `@requiere_login`, y sus globals son los de `auth.py`,
    # donde `_puede_ver_iniciales` no existe. El helper se buscaba en el
    # módulo equivocado y no aparecía nunca — sin error, sólo un falso
    # positivo más en la lista roja.
    import inspect

    modulo = getattr(inspect.unwrap(vista), "__globals__", {})
    for nombre in re.findall(r"\b(_[a-z_]\w*)\s*\(", src):
        helper = modulo.get(nombre)
        if not callable(helper):
            continue
        interno = _fuente(helper)
        for señal in _SEÑALES_DE_CONTROL:
            if señal in interno:
                return f"{nombre}() → {señal}"
    return None


def mapa(app) -> dict:
    """{permisos, grupos, sin_permiso, control_propio} — todo derivado."""
    por_permiso: dict[str, set[str]] = defaultdict(set)
    sin_permiso: set[str] = set()
    control_propio: dict[str, str] = {}
    redirects: set[str] = set()

    for regla in app.url_map.iter_rules():
        if not _es_pantalla(regla):
            continue
        vista = app.view_functions.get(regla.endpoint)
        permiso = getattr(vista, "_permiso", None)
        if permiso:
            por_permiso[permiso].add(str(regla.rule))
            continue
        señal = _control_interno(vista)
        if señal == "redirect":
            # No es "controlada": es que no muestra nada. Va con las revisadas,
            # no con las que chequean a mano ni con las que no chequean nada.
            redirects.add(str(regla.rule))
        elif señal:
            control_propio[str(regla.rule)] = señal
        else:
            sin_permiso.add(str(regla.rule))

    permisos = sorted(
        (
            {
                "permiso": p,
                # Las pantallas primero y solas: son la respuesta a "qué ve
                # este rol". Lo técnico se cuenta aparte y se muestra con
                # ?tecnicas=1.
                "rutas": sorted(r for r in rr if not clase_tecnica(r)),
                "tecnicas": sorted(r for r in rr if clase_tecnica(r)),
            }
            for p, rr in por_permiso.items()
        ),
        key=lambda x: x["permiso"],
    )
    return {
        "permisos": permisos,
        "grupos": agrupar(permisos),
        "sin_permiso": sorted(r for r in sin_permiso if r not in ACEPTADAS),
        # El motivo escrito a mano gana sobre el genérico: "la hoja vive al pie
        # de /precios" explica más que "sólo redirige". El genérico es el
        # piso, no el techo.
        "aceptadas": [
            {
                "ruta": r,
                "motivo": ACEPTADAS.get(
                    r, "sólo redirige: la pantalla de destino tiene su candado"
                ),
            }
            for r in sorted((sin_permiso & set(ACEPTADAS)) | redirects)
        ],
        "control_propio": [
            {"ruta": r, "señal": control_propio[r]} for r in sorted(control_propio)
        ],
    }


def agrupar(permisos: list[dict]) -> list[dict]:
    """Los 58 permisos, juntados por el sustantivo que tienen adelante.

    TMT 2026-08-05 (dueña, viendo la primera versión): *"me podés agrupar"*.
    Cincuenta y ocho filas alfabéticas se leen como un log, no como una tabla:
    `posdat.anular` y `posdat.ver` quedaban a ocho renglones de distancia y
    había que ir y volver para contestar "¿quién toca posdatados?".

    ⭐ El grupo sale del NOMBRE del permiso (`posdat.ver` → `posdat`), no de un
    mapa escrito a mano de secciones. Un mapa habría quedado más lindo —
    "Cobranza", "Producción"— y se habría desactualizado con el primer permiso
    nuevo, que es justo lo que esta pantalla existe para evitar. La convención
    `sustantivo.accion` ya está en los 58, así que el agrupamiento es gratis y
    no puede mentir.

    Dentro de cada grupo las acciones van en orden de PODER (ver, crear,
    editar, anular, admin) y no alfabético: leer no es lo mismo que borrar, y
    la fila que importa mirar es la última.
    """
    ORDEN = {"ver": 0, "crear": 1, "editar": 2, "anular": 3, "admin": 4}
    grupos: dict[str, list[dict]] = defaultdict(list)
    for p in permisos:
        grupos[p["permiso"].split(".")[0]].append(p)
    salida = []
    for nombre in sorted(grupos):
        filas = sorted(
            grupos[nombre],
            key=lambda x: (ORDEN.get(x["permiso"].split(".", 1)[-1], 9),
                           x["permiso"]),
        )
        salida.append({"nombre": nombre, "permisos": filas})
    return salida


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
