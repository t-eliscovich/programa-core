"""Las fichas técnicas adentro de Mi Cartera (13/10/2026).

Lo que protegen:
  1. Que la pestaña F. técnica NO salga de la app (era el problema).
  2. Que el dibujo de lavado salga del texto y nunca uno equivocado.
  3. Que la edición sea sólo para `*` y valide lo que entra.
  4. Que si la base no contesta, la pantalla lo diga en vez de un 500.
"""
from __future__ import annotations

import json

import bcrypt
import pytest
from werkzeug.datastructures import MultiDict

from modules.mi_cartera import fichas as fx

FILA = {
    "codigo": "FT-FL-102", "nombre": "FLEECE 102", "seccion": "tubular", "grupo": "Fleece",
    "revision": "01", "emision": "Julio 2026", "orden": 10, "activa": True,
    "actualizado_en": None, "actualizado_por": None,
    "spec": [["COMPOSICIÓN", "Poliéster", "%", "65 ±3"], ["COMPOSICIÓN", "Algodón", "%", "35 ±3"],
             ["CARACTERÍSTICAS", "Peso", "g/m²", "288 ±5%"], ["CARACTERÍSTICAS", "Ancho", "cm", "102 ±1"],
             ["ENCOGIMIENTO", "Ancho", "%", "5 MAX"]],
    "lavado": json.dumps(["No dejar en remojo.", "No usar secadora.", "Algo raro."]),
    "telas_asinfo": ["Fleece 96 Perchado"],
}
ABI = {**FILA, "codigo": "FT-JE-X", "nombre": "JERSEY X", "seccion": "abierto",
       "grupo": "Jersey", "activa": False}


class _DB:
    def __init__(self, filas=None, existe=False, n=1):
        self.filas = filas if filas is not None else [FILA, ABI]
        self.existe, self.n = existe, n
        self.ejecutado = []

    def fetch_all(self, sql, params=None, conn=None):
        return [f for f in self.filas if "WHERE activa" not in sql or f["activa"]]

    def fetch_one(self, sql, params=None, conn=None):
        if "SELECT 1" in sql:
            return {"x": 1} if self.existe else None
        return next((f for f in self.filas if f["codigo"] == params[0]), None)

    def execute(self, sql, params=None, conn=None):
        self.ejecutado.append((sql, params))
        return self.n


class _Roto:
    def __getattr__(self, _):
        def f(*a, **k):
            raise RuntimeError("sin base")
        return f


def test_simbolo_sale_del_texto():
    assert fx.simbolo("No dejar en remojo.") == "remojo.png"
    assert fx.simbolo("No usar secadora.") == "no_secadora.png"
    assert fx.simbolo("Secar en secadora, temperatura baja") == "secado60.png"
    assert fx.simbolo("No utilizar detergentes clorados") == "cloro.png"
    assert fx.simbolo("Lavado en máquina a 30 °C") == "lavado30.png"
    assert fx.simbolo("Algo que no conozco") is None


def test_armar_agrupa_y_resume(monkeypatch):
    monkeypatch.setattr(fx, "db", _DB())
    (f,) = fx.lista()
    assert [d for d, _ in f["bloques"]] == ["COMPOSICIÓN", "CARACTERÍSTICAS", "ENCOGIMIENTO"]
    assert f["resumen_comp"] == "65% Poliéster · 35% Algodón"
    assert f["resumen_car"] == "288 g/m² · 102 cm"
    assert [x["simbolo"] for x in f["lavado"]] == ["remojo.png", "no_secadora.png", None]
    assert len(fx.lista(todas=True)) == 2
    assert fx.por_tela() == {"fleece102": ["FT-FL-102"], "fleece96perchado": ["FT-FL-102"]}
    assert fx.clave_tela("Fleece 102") == fx.clave_tela("FLEECE  102")


def test_orden_desconocido_va_al_final():
    a = {"seccion": "otra", "grupo": "Nuevo", "orden": 1, "nombre": "A"}
    assert fx._orden(a)[0] == 9 and fx._orden(a)[1] == len(fx.ORDEN_GRUPOS)


def test_sin_base_no_rompe(monkeypatch):
    monkeypatch.setattr(fx, "db", _Roto())
    assert fx.lista() is None
    assert fx.una("FT-FL-102") is None
    assert fx.por_tela() == {}


def _form(**k):
    base = {"nombre": " fleece  nuevo ", "seccion": "tubular", "grupo": "", "revision": "",
            "emision": "Octubre 2026", "lavado": "No dejar en remojo.\n\n  No exprimir. ",
            "activa": "1"}
    base.update(k)
    md = MultiDict(base)
    for d, p, u, v in k.pop("_spec", [("COMPOSICIÓN", "Poliéster", "%", "100"),
                                       ("", "Algodón", "%", ""), ("", "", "", ""),
                                       ("caracteristicas", "Peso", "g/m²", "200")]):
        md.add("s_desc", d)
        md.add("s_param", p)
        md.add("s_unidad", u)
        md.add("s_valor", v)
    return md


def test_leer_form_limpia_y_hereda():
    d = fx.leer_form(_form())
    assert d["nombre"] == "FLEECE NUEVO" and d["grupo"] == "Otras" and d["revision"] == "01"
    assert d["spec"] == [["COMPOSICIÓN", "Poliéster", "%", "100"],
                         ["COMPOSICIÓN", "Algodón", "%", ""],
                         ["CARACTERISTICAS", "Peso", "g/m²", "200"]]
    assert d["lavado"] == ["No dejar en remojo.", "No exprimir."] and d["activa"]


@pytest.mark.parametrize("cambio, msg", [
    ({"nombre": " "}, "nombre"),
    ({"seccion": "x"}, "Tubular"),
    ({"emision": ""}, "emisión"),
    ({"_spec": [("", "", "", "")]}, "vacía"),
    ({"_spec": [("", "Peso", "g", "1")]}, "descripción"),
])
def test_leer_form_rechaza(cambio, msg):
    with pytest.raises(ValueError, match=msg):
        fx.leer_form(_form(**cambio))


def test_guardar(monkeypatch):
    fake = _DB()
    monkeypatch.setattr(fx, "db", fake)
    d = fx.leer_form(_form())
    fx.guardar("ft-fl-102", d, por="tamara")
    sql, params = fake.ejecutado[-1]
    assert "UPDATE" in sql and params[-1] == "FT-FL-102" and params[-2] == "tamara"
    fx.guardar("FT-NUEVA", d, por="tamara", nueva=True)
    assert "INSERT" in fake.ejecutado[-1][0]
    with pytest.raises(ValueError, match="FT-"):
        fx.guardar("XX", d, por="t")
    monkeypatch.setattr(fx, "db", _DB(existe=True))
    with pytest.raises(ValueError, match="Ya hay"):
        fx.guardar("FT-FL-102", d, por="t", nueva=True)
    monkeypatch.setattr(fx, "db", _DB(n=0))
    with pytest.raises(LookupError):
        fx.guardar("FT-NOESTA", d, por="t")


# ── Pantallas ────────────────────────────────────────────────────────────────

@pytest.fixture
def vendedor(app, client, fake_db, monkeypatch):
    monkeypatch.setattr(fx, "db", _DB())
    rid = fake_db.add_role("Vendedor", ["micartera.ver"])
    pw = bcrypt.hashpw(b"Vendedor2026", bcrypt.gensalt(rounds=4))
    fake_db.add_user("pablo", pw, rid, vend="PPR")
    client.post("/login", data={"username": "pablo", "password": "Vendedor2026"})
    return client


@pytest.fixture
def duena(app, client, fake_db, monkeypatch):
    fake = _DB()
    monkeypatch.setattr(fx, "db", fake)
    rid = fake_db.add_role("Accionista", ["*"])
    pw = bcrypt.hashpw(b"Duena2026xx", bcrypt.gensalt(rounds=4))
    fake_db.add_user("tamara", pw, rid)
    client.post("/login", data={"username": "tamara", "password": "Duena2026xx"})
    client.fake = fake
    return client


def test_vendedor_ve_lista_ficha_y_hoja(vendedor):
    html = vendedor.get("/mi-cartera/fichas").data.decode()
    assert "FLEECE 102" in html and "JERSEY X" not in html  # la dada de baja no
    assert "65% Poliéster" in html
    barra = html[html.index('<nav class="tabbar">'):]
    assert 'href="/mi-cartera/fichas"' in barra and "onrender" not in barra
    html = vendedor.get("/mi-cartera/fichas/FT-FL-102").data.decode()
    assert "/mi-cartera/fichas/FT-FL-102/imprimir" in html and "fichas/remojo.png" in html
    r = vendedor.get("/mi-cartera/fichas/FT-FL-102/imprimir?volver=//malo.com")
    assert r.status_code == 200
    h = r.data.decode()
    assert 'href="/mi-cartera/fichas/FT-FL-102"' in h  # el volver de afuera no se usa
    assert "Oscar Canchig" in h and "ESPECIFICACIONES TÉCNICAS" in h
    assert vendedor.get("/mi-cartera/fichas/FT-JE-X").status_code == 404
    assert vendedor.get("/mi-cartera/fichas/FT-JE-X/imprimir").status_code == 404
    assert vendedor.get("/mi-cartera/fichas/NOESTA").status_code == 404


def test_vendedor_no_edita(vendedor):
    assert vendedor.get("/admin/fichas-tecnicas").status_code in (403, 404)
    assert vendedor.get("/admin/fichas-tecnicas/FT-FL-102").status_code in (403, 404)


def test_sin_base_la_lista_lo_dice(vendedor, monkeypatch):
    monkeypatch.setattr(fx, "db", _Roto())
    assert "No pude leer las fichas" in vendedor.get("/mi-cartera/fichas").data.decode()


def test_la_oficina_edita(duena):
    html = duena.get("/admin/fichas-tecnicas").data.decode()
    assert "FLEECE 102" in html and "dada de baja" in html
    assert duena.get("/admin/fichas-tecnicas/FT-FL-102").status_code == 200
    assert duena.get("/admin/fichas-tecnicas/nueva").status_code == 200
    assert duena.get("/admin/fichas-tecnicas/NOESTA").status_code == 404
    # La oficina ve la hoja de una dada de baja, y vuelve a donde vino.
    r = duena.get("/mi-cartera/fichas/FT-JE-X/imprimir?volver=/admin/fichas-tecnicas")
    assert r.status_code == 200 and 'href="/admin/fichas-tecnicas"' in r.data.decode()
    r = duena.post("/admin/fichas-tecnicas/FT-FL-102", data=_form())
    assert r.status_code == 302 and "#f-FT-FL-102" in r.headers["Location"]
    assert "UPDATE" in duena.fake.ejecutado[-1][0]
    r = duena.post("/admin/fichas-tecnicas/nueva", data=_form(codigo="ft-nueva"))
    assert r.status_code == 302 and "#f-FT-NUEVA" in r.headers["Location"]
    r = duena.post("/admin/fichas-tecnicas/nueva", data=_form(codigo="ft-x", nombre=""))
    assert r.status_code == 400 and "Falta el nombre" in r.data.decode()


def test_whatsapp_manda_la_hoja_como_foto_o_pdf(vendedor, monkeypatch):
    """Tamara 13/10: "me gustaría también enviar por WhatsApp". La MISMA hoja
    que la de imprimir, sin la barrita de arriba, por los mismos motores que
    el estado de cuenta."""
    from modules._lib import imagen_motor, pdf_motor
    hojas = []
    monkeypatch.setattr(pdf_motor, "desde_html", lambda html, **k: hojas.append(html) or b"%PDF")
    monkeypatch.setattr(imagen_motor, "desde_html", lambda html, **k: hojas.append(html) or b"PNG")
    r = vendedor.get("/mi-cartera/fichas/FT-FL-102/pdf")
    assert r.status_code == 200 and r.mimetype == "application/pdf"
    assert "Ficha técnica Fleece 102.pdf" in r.headers["Content-Disposition"]
    r = vendedor.get("/mi-cartera/fichas/FT-FL-102/imagen")
    assert r.status_code == 200 and r.mimetype == "image/png"
    assert all('class="barra"' not in h and "FLEECE 102" in h for h in hojas)
    assert vendedor.get("/mi-cartera/fichas/FT-JE-X/pdf").status_code == 404

    def sin_motor(html, **k):
        raise pdf_motor.SinMotor("no hay navegador")
    monkeypatch.setattr(pdf_motor, "desde_html", sin_motor)
    r = vendedor.get("/mi-cartera/fichas/FT-FL-102/pdf")
    assert r.status_code == 503 and "no hay navegador" in r.data.decode()


def test_la_ficha_tiene_el_boton_de_whatsapp(vendedor, monkeypatch):
    from modules._lib import pdf_motor
    monkeypatch.setattr(pdf_motor, "disponible", lambda: True)
    html = vendedor.get("/mi-cartera/fichas/FT-FL-102").data.decode()
    assert "data-wa-pdf" in html and 'data-url="/mi-cartera/fichas/FT-FL-102/pdf"' in html
    assert 'data-img="/mi-cartera/fichas/FT-FL-102/imagen"' in html


def test_la_oficina_baja_la_hoja_de_una_dada_de_baja(duena, monkeypatch):
    from modules._lib import pdf_motor
    monkeypatch.setattr(pdf_motor, "desde_html", lambda html, **k: b"%PDF")
    assert duena.get("/mi-cartera/fichas/FT-JE-X/pdf").status_code == 200


def test_stock_linkea_por_telas_de_asinfo_y_elige_si_hay_dos(vendedor, monkeypatch):
    """Tamara 13/10: "fijate si podés linkear todas". La tela de Asinfo cruza
    con la ficha también por `telas_asinfo`; con dos fichas (tubular y
    abierta) el 📄 abre la lista de esas dos."""
    abierta = {**FILA, "codigo": "FT-FL-102ABI", "nombre": "FLEECE 102 ABIERTO",
               "seccion": "abierto", "telas_asinfo": ["Fleece 96 Perchado"]}
    monkeypatch.setattr(fx, "db", _DB(filas=[FILA, abierta, ABI]))
    assert fx.por_tela()["fleece96perchado"] == ["FT-FL-102", "FT-FL-102ABI"]
    html = vendedor.get("/mi-cartera/fichas?tela=Fleece%2096%20Perchado").data.decode()
    assert "Fichas de <b>Fleece 96 Perchado</b>" in html
    assert "FLEECE 102 ABIERTO" in html and "JERSEY X" not in html
    html = vendedor.get("/mi-cartera/fichas?tela=Nada").data.decode()
    assert "Fichas de" not in html and "FLEECE 102" in html


def test_editar_guarda_las_telas_de_asinfo(duena):
    r = duena.post("/admin/fichas-tecnicas/FT-FL-102",
                   data=_form(telas_asinfo="Fleece 96 Perchado\n\n Fleece 96 Perchado \nOtra"))
    assert r.status_code == 302
    sql, params = duena.fake.ejecutado[-1]
    assert "telas_asinfo = %s::text[]" in sql
    assert ["Fleece 96 Perchado", "Otra"] in params
    r = duena.post("/admin/fichas-tecnicas/nueva",
                   data=_form(codigo="ft-x", nombre="", telas_asinfo="Una\nDos"))
    assert r.status_code == 400 and "Una\nDos" in r.data.decode()
