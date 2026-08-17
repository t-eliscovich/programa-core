"""De dónde venís: el destino del botón "← Volver". (TMT 2026-08-17)

Pedido de la dueña, mirando el Historial filtrado por "bed":

    *"En todas las pantallas cuando arme un filtro y clickeo en algo, cuando
    regreso debería volver a la pantalla anterior con el filtro incluido.
    Ejemplo si acá clickeo cualquier factura y luego vuelvo, debería
    funcionar."*

El filtro NUNCA se perdió: los formularios de filtro son GET, así que viajan
en la URL (`/historial?q=bed&tipo=…`). Lo que se perdía era el CAMINO: cada
"← Volver" era un `url_for()` fijo a su propia lista pelada, así que el
Historial filtrado por "bed" → una factura → Volver te dejaba en `/facturas`
con las 23.022 filas.

REGLA ÚNICA (decidida con la dueña el 2026-08-17, sobre el inventario de los
44 botones): **Volver = la pantalla anterior, con su filtro**. El destino fijo
que tenía cada botón queda como RESPALDO, para cuando no hay pantalla anterior
(link pegado, pestaña nueva sin referrer, recarga después de un POST).

Gana de dónde venís incluso en los 12 botones que a propósito iban a otra
pantalla ("Volver a Cartera" desde el estado de cuenta, "Volver a Gastos"
desde reclasificar, "Volver a posdat" desde emitir cheque). La alternativa era
dejar dos reglas conviviendo, y dos reglas no se pueden explicar ni testear.

Tres fuentes, en orden de confianza:

  1. ``?volver=`` — explícito, lo pone el listado al armar el link. Sobrevive
     al "abrir en pestaña nueva" y al copiar/pegar la dirección, donde no hay
     historial que desandar. Es el mecanismo que ya usaba /cheques (ver
     modules/cheques/views.py::_volver_a_lista, 2026-08-17) y que esto
     generaliza — con la diferencia de que acá el destino no está limitado a
     la propia lista, así que desde el Historial devuelve al Historial.
  2. ``request.referrer`` — el navegador lo manda solo. Es lo que hace que
     esto funcione en las 43 pantallas sin tocarles el listado de origen.
  3. el respaldo que pasa la pantalla — el `url_for()` fijo de hoy.

⚠ SEGURIDAD: `?volver=` y `Referer` los controla quien manda el link, así que
son entrada NO CONFIABLE. `destino_seguro()` sólo acepta rutas RELATIVAS de
esta misma app y, además, que el path RESUELVA en el url_map por GET. Sin ese
último chequeo el botón sería un open-redirect (`?volver=//evil.com` te saca
de la app con la marca de Intela puesta) o un 405 (mandarte por GET a un
endpoint que sólo acepta POST).
"""

from __future__ import annotations

from urllib.parse import quote, urlparse

from flask import current_app, request
from werkzeug.exceptions import HTTPException
from werkzeug.routing import RequestRedirect

# ── Cómo se llama cada pantalla ───────────────────────────────────────────
#
# Para que el botón no MIENTA. Si Volver te lleva al Historial, tiene que
# decir "← Volver al Historial", no "← Volver a Facturas" — un botón que
# anuncia un destino y va a otro es peor que el bug que vinimos a arreglar.
#
# El valor es el pedazo que va DESPUÉS de "Volver", con su preposición ya
# puesta ("al Historial", "a Facturas"): armarlo con un `f"a {nombre}"` daba
# "Volver a el Historial".
#
# Los nombres son los del sidebar (templates/base.html): la dueña ya llama así
# a las pantallas. Es un mapa CHICO y a propósito incompleto: cubre los
# destinos que aparecen de verdad como "pantalla anterior". Lo que no está cae
# en el texto que traía el botón, que es correcto aunque menos informativo —
# nunca en un nombre inventado.
NOMBRE_DE_PANTALLA = {
    "/historial": "al Historial",
    "/facturas": "a Facturas",
    "/cheques": "a Cheques",
    "/compras": "a Compras",
    "/posdat": "a Posdatados",
    "/bancos": "a Bancos",
    "/caja": "a Caja",
    "/gastos": "a Gastos",
    "/clientes": "a Clientes",
    "/proveedores": "a Proveedores",
    "/proformas": "a Proformas",
    "/activos": "a Activos",
    "/dolares": "a Dólares",
    "/retiros": "a Retiros",
    "/precios": "a Precios",
    "/comisiones": "a Comisiones",
    "/cartera": "a Cartera",
    "/retenciones": "a Retenciones",
    "/provisiones": "a Provisiones",
    "/conciliacion": "a Conciliación",
    "/mi-cartera": "a Mi Cartera",
    "/iniciales": "a Metas",
}


def _es_relativa(url: str) -> bool:
    """Sólo rutas de ESTA app: '/algo'. Nada de host, esquema ni protocol-relative.

    `//evil.com/x` es la trampa clásica: empieza con "/" y el navegador la lee
    como URL absoluta a otro host. Por eso el chequeo de "//" va aparte y ANTES
    que nada.
    """
    if not url or not url.startswith("/") or url.startswith("//"):
        return False
    if "://" in url or "\\" in url:
        return False
    # Un `\n` o un `\r` metidos en el header pueden partir la respuesta.
    return not any(c in url for c in "\r\n\t")


def _resuelve_por_get(path: str) -> bool:
    """¿Existe esa pantalla y se abre por GET?

    Se le pregunta al `url_map` real, no a una lista escrita a mano — mismo
    criterio que `tests/test_historial_links_resuelven.py` y que
    `/usuarios/accesos`: una lista a mano se despega el día que alguien
    renombra una ruta, y acá despegarse significa mandar a la dueña a un 404.
    """
    try:
        adapter = current_app.url_map.bind("localhost")
        adapter.match(path, method="GET")
        return True
    except RequestRedirect:
        # `/caja` → `/caja/`: existe, sólo que con barra al final. Vale.
        return True
    except (HTTPException, ValueError):
        return False


def destino_seguro(crudo: str | None, actual: str | None = None) -> str | None:
    """Valida un candidato a "pantalla anterior". None si no sirve.

    `actual` es el path de la pantalla que se está dibujando: volver a UNO
    MISMO no es volver. Pasa de verdad — después de un POST que redirige a la
    propia ficha, el referrer es la ficha.
    """
    if not crudo:
        return None
    crudo = crudo.strip()
    # El referrer llega absoluto ("https://programa.intela.com.ec/historial?q=bed").
    # Lo pasamos a relativo ANTES de validar, y sólo si el host es el nuestro.
    if "://" in crudo:
        partes = urlparse(crudo)
        if partes.netloc != request.host:
            return None
        crudo = partes.path + (f"?{partes.query}" if partes.query else "")
    if not _es_relativa(crudo):
        return None
    path = urlparse(crudo).path
    if actual and path == actual:
        return None
    if not _resuelve_por_get(path):
        return None
    return crudo


def volver_a(respaldo: str) -> str:
    """URL del botón "← Volver". `respaldo` = el destino fijo de la pantalla.

    Uso en templates:  {{ volver_a(url_for('facturas.lista')) }}
    """
    actual = request.path
    for candidato in (request.args.get("volver"), request.referrer):
        destino = destino_seguro(candidato, actual=actual)
        if destino:
            return destino
    return respaldo


def nombre_de(url: str) -> str:
    """'/historial?q=bed' → 'al Historial'. '' si no la conocemos."""
    if not url:
        return ""
    path = urlparse(url).path.rstrip("/") or "/"
    # Se busca por el PRIMER tramo: /bancos/47 y /bancos son "Bancos". El
    # diccionario tiene claves de un tramo, así que alcanza con cortar ahí.
    raiz = "/" + path.lstrip("/").split("/")[0]
    return NOMBRE_DE_PANTALLA.get(raiz, "")


def texto_volver(url: str, respaldo_texto: str = "← Volver") -> str:
    """Etiqueta del botón, acorde al destino REAL.

    Si no sabemos cómo se llama la pantalla de destino, se usa el texto que
    traía la pantalla. Nunca se inventa un nombre.
    """
    nombre = nombre_de(url)
    return f"← Volver {nombre}" if nombre else respaldo_texto


def volver_texto(respaldo: str, respaldo_texto: str = "← Volver") -> str:
    """Atajo para templates: la etiqueta que le corresponde a `volver_a(respaldo)`.

    Existe para que el template no tenga que escribir `volver_a(...)` dos veces:
        <a href="{{ volver_a(X) }}">{{ volver_texto(X, '← Volver a Facturas') }}</a>

    ⭐ Si el botón termina yendo A DONDE IBA SIEMPRE, se queda con el texto que
    ya tenía. Sólo se renombra cuando el destino CAMBIÓ. Dos motivos: (a) el
    texto de la pantalla suele ser más específico que el genérico del mapa
    ("← Volver a aging por cliente" > "← Volver a Cartera", "← Resultados" >
    "← Volver a Informes"), y (b) así este cambio no le mueve el cartel a 25
    pantallas de golpe — la etiqueta se mueve exactamente cuando el destino se
    movió, que es lo único que hay que avisarle a quien mira.
    """
    destino = volver_a(respaldo)
    if urlparse(destino).path.rstrip("/") == urlparse(respaldo).path.rstrip("/"):
        return respaldo_texto
    return texto_volver(destino, respaldo_texto)


def con_volver(url: str | None) -> str | None:
    """Le cuelga a un link el `?volver=` con la URL FILTRADA de esta pantalla.

    El referrer alcanza para el click normal, pero NO para "abrir en pestaña
    nueva" ni para copiar/pegar la dirección: ahí no hay historial que
    desandar. Es el mismo truco que ya usaba el listado de /cheques.

    Uso:  <a href="{{ r.origen_url | con_volver }}">
    """
    if not url or "volver=" in url:
        return url
    aqui = request.full_path.rstrip("?")
    return f"{url}{'&' if '?' in url else '?'}volver={quote(aqui, safe='')}"


def register(app) -> None:
    """Deja `volver_a` y `texto_volver` disponibles en todos los templates."""
    app.jinja_env.globals["volver_a"] = volver_a
    app.jinja_env.globals["texto_volver"] = texto_volver
    app.jinja_env.globals["volver_texto"] = volver_texto
    app.jinja_env.filters["con_volver"] = con_volver
