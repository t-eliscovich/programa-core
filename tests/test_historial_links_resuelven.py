"""Los links del Historial tienen que apuntar a rutas que EXISTEN.

TMT 2026-08-03 (dueña: "cuando clické el link de compra 473 me dice 404 no
encontrado"). `historial.queries.link_origen` armaba `/compras/<id>` y esa
ruta nunca existió — compras era el único módulo con links entrantes y sin
ficha. El 404 era invisible desde el código porque el link es un string
hardcodeado, no un `url_for`.

Este test recorre TODAS las tablas que link_origen sabe linkear y verifica
que el path resuelva contra el url_map real de la app. Si mañana alguien
agrega una tabla nueva al mapeo sin crear la pantalla, falla acá.
"""
from __future__ import annotations

import os
import sys
from urllib.parse import urlsplit

import pytest
from werkzeug.exceptions import MethodNotAllowed, NotFound

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# Las tablas que `link_origen` mapea a una URL. El id es irrelevante para
# resolver la ruta (sólo importa la FORMA del path).
TABLAS = [
    "caja",
    "cheque",
    "compra",
    "factura",
    "capital",
    "retiros",
    "dolares",
    "posdat",
    "xgast",
]


def _paths_de_link_origen():
    from modules.historial import queries as hq

    for tabla in TABLAS:
        url, etiqueta = hq.link_origen({"origen_table": tabla, "origen_id": 473})
        assert etiqueta, f"{tabla}: etiqueta vacía"
        if url is None:
            continue  # sin link (ej. transacciones_bancarias) — válido
        yield tabla, url


def test_todas_las_tablas_conocidas_producen_un_link_o_none():
    """Ninguna tabla del mapeo puede caer al `return None, f"{t} #{rid}"`."""
    from modules.historial import queries as hq

    sin_link = [
        t for t in TABLAS
        if hq.link_origen({"origen_table": t, "origen_id": 1})[0] is None
    ]
    assert sin_link == [], f"tablas sin URL: {sin_link}"


def test_links_del_historial_resuelven_contra_el_url_map(app):
    adapter = app.url_map.bind("localhost")
    rotos = []
    for tabla, url in _paths_de_link_origen():
        path = urlsplit(url).path
        try:
            adapter.match(path, method="GET")
        except MethodNotAllowed:
            pass  # la ruta existe, sólo no acepta GET — no es un 404
        except NotFound:
            rotos.append((tabla, url))
    assert rotos == [], (
        "estos links del Historial dan 404 (no hay ruta GET que los reciba): "
        + ", ".join(f"{t} → {u}" for t, u in rotos)
    )


@pytest.mark.parametrize("tabla,esperado", [
    ("compra", "/compras/473"),
    ("factura", "/facturas/473"),
    ("cheque", "/cheques/473"),
])
def test_forma_de_los_links_principales(tabla, esperado):
    from modules.historial import queries as hq

    url, _ = hq.link_origen({"origen_table": tabla, "origen_id": 473})
    assert url == esperado


def test_link_destino_usa_el_mismo_mapeo():
    from modules.historial import queries as hq

    url, etiqueta = hq.link_destino({"destino_table": "compra", "destino_id": 473})
    assert url == "/compras/473"
    assert etiqueta == "Compra #473"
