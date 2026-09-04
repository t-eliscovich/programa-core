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
    "retiros",
    "dolares",
    "posdat",
    "xgast",
    # `transacciones_bancarias` va aparte: su URL es /bancos/<no_banco>?id=<id>
    # y el `no_banco` sólo lo sabe el batch de la vista. Sin ese dato,
    # link_origen devuelve None a propósito (ver TABLAS_CON_MAPEO).
]

#: Tablas cuya URL necesita un mapeo que arma la vista. Sin el mapeo, el link
#: es None (correcto: es preferible no linkear a linkear al banco equivocado).
TABLAS_CON_MAPEO = {
    "transacciones_bancarias": {"banco_nos": {473: 9}},
}

# `capital` NO está en la lista: desde el 2026-08-07 no tiene rama en
# link_origen. Ver test_capital_no_linkea_a_una_pantalla_que_no_lo_muestra.


def _paths_de_link_origen():
    from modules.historial import queries as hq

    for tabla in TABLAS:
        url, etiqueta = hq.link_origen({"origen_table": tabla, "origen_id": 473})
        assert etiqueta, f"{tabla}: etiqueta vacía"
        if url is None:
            continue  # sin link — válido
        yield tabla, url
    for tabla, mapeo in TABLAS_CON_MAPEO.items():
        url, etiqueta = hq.link_origen(
            {"origen_table": tabla, "origen_id": 473}, **mapeo)
        assert etiqueta, f"{tabla}: etiqueta vacía"
        assert url is not None, f"{tabla}: con el mapeo tiene que haber URL"
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
    # TMT 2026-09-04: el numf se REPITE (la factura N de BED y una NC de AJ2
    # comparten el 10970), y la ficha, cuando el número da para dos, manda a la
    # lista. El historial SABE el id interno: viaja como `?id=` y desempata.
    ("factura", "/facturas/473?id=473"),
    ("cheque", "/cheques/473"),
    # TMT 2026-08-07 (dueña: "esos links deberían venir filtrados por lo que
    # quiero ver"). Todos caen en LA FILA, no en la pantalla entera.
    ("caja", "/caja?id=473"),
    ("retiros", "/retiros?id=473"),
    ("dolares", "/dolares?id=473"),
    ("xgast", "/gastos?id=473"),
    # TMT 2026-08-07 (dueña: "el link me manda a proveedores y no al posdatado
    # que se menciona"). Decía "/proveedores" desde el primer commit — el test
    # de arriba no lo cazaba porque /proveedores EXISTE: resolvía contra el
    # url_map, sólo que llevaba a la pantalla equivocada. Un link puede estar
    # roto sin dar 404.
    ("posdat", "/posdat?id=473"),
])
def test_forma_de_los_links_principales(tabla, esperado):
    from modules.historial import queries as hq

    url, _ = hq.link_origen({"origen_table": tabla, "origen_id": 473})
    assert url == esperado


def _fila_bap(destino_id=473):
    return {
        "id_mov_doble": 19860, "tipo": "bap_anticipo_a_compra",
        "destino_table": "compra", "destino_id": destino_id,
        "concepto": "BAP AI: 1 anticipo(s) → compra #473 (BAP15)",
        "metadata": {"ids_anticipos": [3209], "codigo_prov": "AI"},
    }


def test_rotulo_de_conversion_usa_el_numero_de_compra_no_el_id(monkeypatch):
    """El rótulo decía "→ compra #473" (id interno, invisible en Compras).

    TMT 2026-08-03: la compra 473 es la N° 10121. La dueña no tenía cómo
    saberlo — ningún otro lado del programa muestra el id.
    """
    import db
    from modules.historial import queries as hq

    def fake_fetch_all(sql, params=None):
        s = " ".join(sql.split()).lower()
        if "from scintela.dolares" in s:
            return [{"id_dolares": 3209, "concepto": "15 SALDO"}]
        if "from scintela.compra" in s:
            return [{"id_compra": 473, "numero": "10121"}]
        raise AssertionError(f"fetch_all inesperado: {s[:80]}")

    monkeypatch.setattr(db, "fetch_all", fake_fetch_all)
    (fila,) = hq._nombrar_conversiones([_fila_bap()])
    assert fila["concepto"] == "AI 15 · 1 anticipo(s) → compra N° 10121"
    assert "#473" not in fila["concepto"]


def test_rotulo_de_conversion_cae_al_id_si_la_compra_no_tiene_numero(monkeypatch):
    import db
    from modules.historial import queries as hq

    def fake_fetch_all(sql, params=None):
        s = " ".join(sql.split()).lower()
        if "from scintela.dolares" in s:
            return [{"id_dolares": 3209, "concepto": "15 SALDO"}]
        return [{"id_compra": 473, "numero": ""}]

    monkeypatch.setattr(db, "fetch_all", fake_fetch_all)
    (fila,) = hq._nombrar_conversiones([_fila_bap()])
    assert fila["concepto"] == "AI 15 · 1 anticipo(s) → compra #473"


def test_link_destino_usa_el_mismo_mapeo():
    from modules.historial import queries as hq

    url, etiqueta = hq.link_destino({"destino_table": "compra", "destino_id": 473})
    assert url == "/compras/473"
    assert etiqueta == "Compra #473"


# ── Los links que cambiaron el 2026-08-07 ────────────────────────────────────

def test_el_movimiento_bancario_linkea_a_su_banco_y_a_su_fila():
    """1.862 movimientos apuntaban acá y no tenían link ninguno: era el destino
    de MÁS volumen del historial. La URL es POR BANCO, así que necesita el
    `no_banco` del batch de la vista."""
    from modules.historial import queries as hq

    url, _ = hq.link_origen({"origen_table": "transacciones_bancarias",
                             "origen_id": 8123}, banco_nos={8123: 9})
    assert url == "/bancos/9?id=8123"


def test_sin_saber_el_banco_prefiere_no_linkear_a_linkear_mal():
    """Mandar a un banco equivocado es peor que no linkear: la pantalla filtra
    por no_banco Y por id, así que saldría vacía sin decir por qué."""
    from modules.historial import queries as hq

    url, etiqueta = hq.link_origen({"origen_table": "transacciones_bancarias",
                                    "origen_id": 8123})
    assert url is None
    assert etiqueta == "Banco mov #8123"


def test_capital_no_linkea_a_una_pantalla_que_no_lo_muestra():
    """`capital` mandaba a /retiros, que lee SÓLO `scintela.retiros` — los
    aportes viven en `scintela.capital` (modules/capital/queries.py::aportar).

    Con el `?id=` nuevo eso pasaba de inútil a peligroso: `/retiros?id=<id_capital>`
    filtraría por `id_retiro` y mostraría OTRA fila cualquiera, en silencio y sin
    404. Medido contra producción el 07/08: **0 movimientos** apuntan a `capital`,
    así que la rama se borró en vez de arreglarse."""
    from modules.historial import queries as hq

    url, etiqueta = hq.link_origen({"origen_table": "capital", "origen_id": 5})
    assert url is None
    assert "capital" in etiqueta.lower()


def test_ningun_link_manda_a_la_pantalla_entera():
    """El criterio de la dueña: "si al clickear hay que buscar la fila a ojo, el
    link no está terminado". Ningún destino puede ser una ruta pelada sin el id
    de la fila adentro."""
    from modules.historial import queries as hq

    pelados = []
    for tabla in TABLAS:
        url, _ = hq.link_origen({"origen_table": tabla, "origen_id": 473})
        if url and "473" not in url:
            pelados.append((tabla, url))
    assert pelados == [], f"links que caen en la pantalla entera: {pelados}"


# ---------------------------------------------------------------------------
# El NÚMERO visible no siempre sirve como pedazo de URL.
# TMT 2026-08-24 (dueña: *"el link acá no anda"*, sobre "Factura s/n"). Desde el
# 09/08 las 538 facturas con `numf = 0` se rotulan "s/n", y esa misma etiqueta
# se estaba usando para armar el path: `/facturas/s/n`. La barra parte el path y
# ninguna ruta lo recibe. Mismo bug que "Dep. Pich." en cheques.
# ---------------------------------------------------------------------------

#: (mapa que arma la vista, url esperada). Lo que no sirve para el path tiene
#: que caer al id interno, que SÍ resuelve.
NUMEROS_QUE_NO_VAN_EN_LA_URL = [
    "s/n",          # factura sin número (numf = 0)
    "Dep. Pich.",   # etiqueta con espacios y puntos
    "12/3",         # cualquier cosa con barra
    "#473",         # el fallback del id
    "0",            # el numf del legacy dBase
    "",
    None,
]


@pytest.mark.parametrize("numf", NUMEROS_QUE_NO_VAN_EN_LA_URL)
def test_factura_sin_numero_usable_cae_al_id(app, numf):
    from modules.historial import queries as hq

    url, _ = hq.link_origen(
        {"origen_table": "factura", "origen_id": 473}, factura_numfs={473: numf})
    assert url == "/facturas/473?id=473", f"numf {numf!r} armó {url!r}"
    app.url_map.bind("localhost").match(url.split("?")[0], method="GET")  # no levanta NotFound


@pytest.mark.parametrize("no_cheque", NUMEROS_QUE_NO_VAN_EN_LA_URL)
def test_cheque_sin_numero_usable_cae_al_id(app, no_cheque):
    from modules.historial import queries as hq

    url, _ = hq.link_origen(
        {"origen_table": "cheque", "origen_id": 473}, cheque_nos={473: no_cheque})
    assert url == "/cheques/473", f"no_cheque {no_cheque!r} armó {url!r}"
    app.url_map.bind("localhost").match(url, method="GET")


@pytest.mark.parametrize("numero,esperado", [
    ("10894", "/facturas/10894?id=473"),
    (10894, "/facturas/10894?id=473"),
    ("  10894  ", "/facturas/10894?id=473"),
    ("A-12", "/facturas/A-12?id=473"),
])
def test_el_numero_bueno_sigue_yendo_en_la_url(numero, esperado):
    from modules.historial import queries as hq

    url, etiqueta = hq.link_origen(
        {"origen_table": "factura", "origen_id": 473}, factura_numfs={473: numero})
    assert url == esperado
    assert etiqueta == "Factura " + esperado.rsplit("/", 1)[1].split("?")[0]
