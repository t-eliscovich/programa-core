"""La pestaña Stock del portal de vendedores (13/10/2026).

Lo que protegen, en orden de gravedad:
  1. Que un timeout de Asinfo NO se vea como "no hay stock" (el error del
     29/07): sólo se cachea el éxito y se devuelve el último bueno.
  2. Que la pantalla no muestre plata — ni precio, ni costo, ni proveedor.
  3. Que el vendedor llegue a /mi-cartera/stock pero NO al stock de la oficina.
  4. Que el armado tela → color sume bien primera, segunda y acabados.
"""
from __future__ import annotations

from datetime import UTC, datetime

import bcrypt
import pytest

from modules.asinfo import stock_terminado as st

FILAS = [
    {"grupo": "Fleece", "tela": "Fleece 102", "cod_color": "NEG", "color": "NEGRO",
     "calidad": "PRI", "acabado": "TUB", "rollos": 90, "kg": 2000.0},
    {"grupo": "Fleece", "tela": "Fleece 102", "cod_color": "NEG", "color": "NEGRO",
     "calidad": "SEG", "acabado": "TUB", "rollos": 5, "kg": 72.5},
    {"grupo": "Fleece", "tela": "Fleece 102", "cod_color": "GOS", "color": "GRIS OSCURO",
     "calidad": "PRI", "acabado": "ABI", "rollos": 11, "kg": 224.1},
    {"grupo": "Jersey", "tela": "Jersey 3", "cod_color": "BLA", "color": "BLANCO",
     "calidad": "PRI", "acabado": "TUB", "rollos": 3, "kg": 60.0},
    {"grupo": "TELA CRUDA", "tela": "Cruda", "cod_color": "CRU", "color": "CRUDO",
     "calidad": "PRI", "acabado": "TUB", "rollos": 2, "kg": 37.0},
]


@pytest.fixture(autouse=True)
def _sin_cache():
    st._ultimo.update(t=0.0, leido=None, telas=None)
    yield
    st._ultimo.update(t=0.0, leido=None, telas=None)


def _fuente(monkeypatch, respuestas):
    """Metabase de mentira: cada llamada devuelve la próxima respuesta."""
    llamadas = []

    def fake(db, sql, max_results=10000):
        llamadas.append(db)
        return respuestas[min(len(llamadas), len(respuestas)) - 1]

    monkeypatch.setattr(st.metabase_client, "fetch_dataset_estado", fake)
    return llamadas


def test_arma_tela_color_con_segunda_y_acabado():
    telas = st._armar(FILAS)
    assert [t["tela"] for t in telas] == ["Fleece 102", "Jersey 3"]
    f = telas[0]
    assert f["kg"] == pytest.approx(2296.6)
    assert f["rollos"] == 106
    assert f["seg_kg"] == pytest.approx(72.5) and f["seg_rollos"] == 5
    neg, gos = f["colores"]
    assert (neg["cod"], neg["kg"], neg["seg_kg"]) == ("NEG", 2072.5, 72.5)
    assert neg["tub_kg"] == 2072.5 and neg["abi_kg"] == 0
    assert gos["abi_kg"] == pytest.approx(224.1)


def test_la_tela_cruda_no_entra():
    assert all(t["grupo"] != "TELA CRUDA" for t in st._armar(FILAS))


def test_un_timeout_no_se_ve_como_stock_cero(monkeypatch):
    """El error del 29/07: un [] de timeout guardado como dato."""
    _fuente(monkeypatch, [(FILAS, True), ([], False)])
    primero = st.por_tela()
    assert primero["telas"] and not primero["viejo"]
    st._ultimo["t"] = 0.0  # vence el caché
    segundo = st.por_tela()
    assert segundo["viejo"] is True
    assert segundo["telas"] == primero["telas"]


def test_un_fracaso_no_se_cachea(monkeypatch):
    llamadas = _fuente(monkeypatch, [([], False), (FILAS, True)])
    assert st.por_tela()["telas"] == []
    assert st.por_tela()["telas"]  # volvió a preguntar, no guardó el fracaso
    assert len(llamadas) == 2


def test_el_exito_se_cachea(monkeypatch):
    llamadas = _fuente(monkeypatch, [(FILAS, True)])
    st.por_tela()
    st.por_tela()
    assert len(llamadas) == 1


# ---------------------------------------------------------------------------
# La pantalla
# ---------------------------------------------------------------------------


@pytest.fixture
def vendedor(app, client, fake_db):
    rid = fake_db.add_role("Vendedor", ["micartera.ver"])
    pw = bcrypt.hashpw(b"Vendedor2026", bcrypt.gensalt(rounds=4))
    fake_db.add_user("pablo", pw, rid, vend="PPR")
    r = client.post("/login", data={"username": "pablo", "password": "Vendedor2026"})
    assert r.status_code in (302, 303)
    return client


def test_el_vendedor_ve_el_stock_sin_plata(vendedor, monkeypatch):
    _fuente(monkeypatch, [(FILAS, True)])
    monkeypatch.setattr(st, "datetime", type("D", (), {
        "now": staticmethod(lambda tz=None: datetime(2026, 10, 13, 19, 48, tzinfo=UTC))}))
    r = vendedor.get("/mi-cartera/stock")
    assert r.status_code == 200
    html = r.data.decode()
    assert "Fleece 102" in html and "GRIS OSCURO" in html
    assert "sin descontar pedidos" in html
    assert "14:48" in html  # hora Ecuador, no la del servidor
    import re
    lista = re.search(r'id="st-lista".*?</div>\s*<div class="vacio"', html, re.S).group(0)
    assert "$" not in lista and "proveedor" not in lista.lower()


def test_si_asinfo_nunca_contesto_lo_dice(vendedor, monkeypatch):
    _fuente(monkeypatch, [([], False)])
    r = vendedor.get("/mi-cartera/stock")
    assert r.status_code == 200
    assert "No pude leer el stock de Asinfo" in r.data.decode()


def test_el_stock_de_la_oficina_sigue_cerrado(vendedor):
    for path in ("/stock", "/stock/asinfo-lote", "/stock/asinfo-lote?bodega=53"):
        assert vendedor.get(path).status_code == 404, path


def test_la_pestania_esta_en_la_barra(vendedor, monkeypatch):
    _fuente(monkeypatch, [(FILAS, True)])
    html = vendedor.get("/mi-cartera/stock").data.decode()
    barra = html[html.index('<nav class="tabbar">'):]
    assert ">Stock</a>" in barra and ">Comp</a>" in barra and "F. técnica" in barra


def test_filas_raras_no_rompen_el_armado():
    """Kilos ilegibles o en cero se saltean; un acabado sin cargar no suma a
    TUB ni a ABI pero sí al total."""
    raras = [
        {"grupo": "Rib", "tela": "Rib", "cod_color": "X", "kg": "no", "rollos": 1},
        {"grupo": "Rib", "tela": "Rib", "cod_color": "X", "kg": 0, "rollos": 1},
        {"grupo": "Rib", "tela": "", "cod_color": "ROJ", "color": None,
         "calidad": None, "acabado": None, "rollos": 1, "kg": 10},
    ]
    (t,) = st._armar(raras)
    (c,) = t["colores"]
    assert t["tela"] == "(sin tela)" and c["color"] == "ROJ"
    assert c["kg"] == 10 and c["tub_kg"] == 0 and c["abi_kg"] == 0


def test_el_nombre_del_color_es_el_de_la_mayoria_de_los_kilos():
    """Rollos del mismo código con otro nombre de color en el lote (13/10: un
    rollo de BLANCO con "NEGRO" puesto por error en Asinfo). Manda el código;
    se muestra y se busca el nombre de la mayoría de los kilos."""
    filas = [
        {"grupo": "Rib", "tela": "Rib", "cod_color": "BLA", "color": "NEGRO",
         "calidad": "PRI", "acabado": "TUB", "rollos": 1, "kg": 6},
        {"grupo": "Rib", "tela": "Rib", "cod_color": "BLA", "color": "BLANCO",
         "calidad": "PRI", "acabado": "TUB", "rollos": 20, "kg": 212},
    ]
    (c,) = st._armar(filas)[0]["colores"]
    assert c["color"] == "BLANCO" and c["kg"] == 218 and "_kg_nombre" not in c


def test_el_stock_se_busca_por_codigo_y_por_color():
    from pathlib import Path
    html = Path("modules/mi_cartera/templates/mi_cartera/stock.html").read_text()
    assert "data-buscar=\"{{ (c.color ~ ' ' ~ c.cod)|lower }}\"" in html
    assert '<span class="cc">{{ c.cod }}</span>' in html


def test_lo_de_la_competencia_va_marcado(vendedor, monkeypatch):
    """Tamara 13/10: "chiquito… cuando algo es de la competencia, así tienen
    incentivo a vender ese en particular". Misma llave que Stock (tela +
    código de color). Segunda vuelta (13/10, noche): "mostrá PRI o SEG
    separado de la competencia y aparte si está en la comp" — la categoría y
    el 🏆 van en etiquetas distintas. Tercera vuelta: el 🏆 va PRIMERO y sólo
    dice que está en la competencia ("no competencia y segunda")."""
    _fuente(monkeypatch, [(FILAS, True)])
    from modules.analisis import queries as aq
    monkeypatch.setattr(aq, "items", lambda conn=None: [
        {"subcategoria": "Fleece 102", "color": "NEG", "motivo": "segunda", "stock_kg": 72},
        {"subcategoria": "Fleece 102", "color": "GOS", "motivo": "parado", "stock_kg": 224},
        {"subcategoria": "Jersey 3", "color": "BLA", "motivo": "parado", "stock_kg": 0}])
    monkeypatch.setattr(aq, "con_puntos", lambda fs: [dict(f, puntos=10) for f in fs])
    html = vendedor.get("/mi-cartera/stock").data.decode()
    lista = html[html.index('id="st-lista"'):]
    import re
    neg = re.search(r'<span class="cc">NEG</span>.*?</div>', lista, re.S).group(0)
    assert '<em class="tag pri">PRI</em>' in neg and '<em class="tag dos">SEG</em>' in neg
    assert ">🏆 ×10</em>" in neg           # NEG: entra por la segunda; el 🏆 sólo dice que está
    assert neg.index("🏆") < neg.index(">PRI<")   # la competencia va primero
    gos = re.search(r'<span class="cc">GOS</span>.*?</div>', lista, re.S).group(0)
    assert ">🏆 ×10</em>" in gos and "SEG" not in gos   # GOS: entero, sólo primera
    assert lista.count("🏆") == 2           # BLANCO vendido (0 kg) no se marca
    assert "2da" not in lista[:lista.index('id="st-vacio"')]  # una sola palabra: PRI / SEG


def test_si_la_competencia_falla_el_stock_igual_se_ve(vendedor, monkeypatch):
    _fuente(monkeypatch, [(FILAS, True)])
    from modules.analisis import queries as aq

    def roto(conn=None):
        raise RuntimeError("sin base")
    monkeypatch.setattr(aq, "items", roto)
    r = vendedor.get("/mi-cartera/stock")
    assert r.status_code == 200 and "🏆" not in r.data.decode()[r.data.decode().index('id="st-lista"'):]
