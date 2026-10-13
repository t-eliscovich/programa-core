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


def test_el_buscador_ve_todos_los_nombres_del_color():
    """Rollos del mismo código con el color escrito distinto: se muestra el
    primero, pero el buscador tiene que encontrar cualquiera."""
    filas = [
        {"grupo": "Rib", "tela": "Rib", "cod_color": "NEG", "color": "NEGRO",
         "calidad": "PRI", "acabado": "TUB", "rollos": 1, "kg": 20},
        {"grupo": "Rib", "tela": "Rib", "cod_color": "NEG", "color": "NEGRO OSC",
         "calidad": "PRI", "acabado": "TUB", "rollos": 1, "kg": 20},
        {"grupo": "Rib", "tela": "Rib", "cod_color": "NEG", "color": "NEGRO",
         "calidad": "SEG", "acabado": "TUB", "rollos": 1, "kg": 20},
    ]
    (c,) = st._armar(filas)[0]["colores"]
    assert c["color"] == "NEGRO" and c["nombres"] == ["NEGRO", "NEGRO OSC"]
