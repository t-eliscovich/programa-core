"""El estado de cuenta como PDF, para mandárselo al cliente por WhatsApp.

TMT 2026-08-04 (dueña): primero lo pidió para el vendedor en la calle y a los
dos minutos lo amplió — *"dejá esto de enviar por WhatsApp para todos los
usuarios, no sólo vendedores; quizás Alex le puede mandar al cliente
también"*.

Lo que protegen estos tests:
  1. Que el PDF sea LA MISMA hoja que se imprime, no una segunda plantilla.
  2. Que el vendedor no pueda pedir el PDF de un cliente ajeno.
  3. Que sin motor de PDF el servidor lo diga en vez de reventar.
  4. Que el teléfono que se le pasa a WhatsApp esté bien formado — un número
     mal armado abre un chat con un desconocido.
"""
from __future__ import annotations

from datetime import date

import bcrypt
import pytest

from filters import wa_tel
from modules._lib import pdf_motor
from modules.informes import estado_cuenta_pdf
from modules.mi_cartera import queries as q
from tests.test_mi_cartera import _totales

# ---------------------------------------------------------------------------
# El teléfono para el link de WhatsApp
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("crudo", "esperado"),
    [
        # Como está cargado en el dBase: prefijo nacional 0.
        ("0989506447", "593989506447"),
        ("099 123 4567", "593991234567"),
        ("032-745-123", "59332745123"),
        # El 0 se REEMPLAZA por 593, no se le pega adelante. Si esto se
        # rompiera, wa.me abriría un chat con un número que no existe.
        ("0989506447", "593989506447"),
        # Ya internacional: se deja.
        ("+593989506447", "593989506447"),
        ("593989506447", "593989506447"),
        ("00593989506447", "593989506447"),
        # Fichas con varios números: gana el primero. Mandarle el estado de
        # cuenta al fax de la empresa no sirve.
        ("0989506447 / 032745123", "593989506447"),
        ("0989506447, 0987654321", "593989506447"),
        # Basura → '' y la pantalla no ofrece el botón.
        ("", ""), (None, ""), ("s/n", ""), ("123", ""), ("-", ""),
        ("0" * 20, ""),
    ],
)
def test_wa_tel(crudo, esperado):
    assert wa_tel(crudo) == esperado


# ---------------------------------------------------------------------------
# El nombre del archivo — es lo primero que ve el cliente en el chat
# ---------------------------------------------------------------------------


def test_el_archivo_se_llama_como_el_cliente():
    assert estado_cuenta_pdf.nombre_archivo("MARIO W INNOVANOVENTA S.A", "MWI") == \
        "Estado de cuenta - MARIO W INNOVANOVENTA S.A.pdf"


def test_el_nombre_del_archivo_aguanta_cualquier_cosa():
    """Pasa por WhatsApp, por mail y por el disco de quien lo reciba."""
    # Tildes y ñ: fuera, no todos los sistemas las bancan en un nombre.
    assert "Nunez" in estado_cuenta_pdf.nombre_archivo("Núñez", "NUN")
    # Barras y dos puntos romperían la ruta al guardarlo.
    assert "/" not in estado_cuenta_pdf.nombre_archivo("A/B: C", "ABC")
    # Sin nombre cae al código: nunca un archivo sin identificar.
    assert estado_cuenta_pdf.nombre_archivo("", "tdv") == "Estado de cuenta - TDV.pdf"
    assert estado_cuenta_pdf.nombre_archivo("!!!", "tdv") == "Estado de cuenta - TDV.pdf"
    # Y siempre termina en .pdf.
    assert estado_cuenta_pdf.nombre_archivo("X" * 200, "TDV").endswith(".pdf")


# ---------------------------------------------------------------------------
# El motor
# ---------------------------------------------------------------------------


def test_sin_navegador_avisa_en_vez_de_reventar(monkeypatch):
    monkeypatch.setattr(pdf_motor, "binario", lambda: None)
    assert pdf_motor.disponible() is False
    with pytest.raises(pdf_motor.SinMotor):
        pdf_motor.desde_html("<html></html>")


def test_el_no_se_reconsulta_pero_el_si_se_cachea(monkeypatch, tmp_path):
    """La misma lección que la columna `vend` (03/08) y que Metabase (29/07):
    un NO no puede vivir tanto como un SÍ. Si mañana instalan Edge en el
    servidor, la app tiene que enterarse sin que nadie la reinicie."""
    pdf_motor._resetear_cache()
    monkeypatch.setattr(pdf_motor.shutil, "which", lambda n: None)
    assert pdf_motor.binario() is None
    # Dentro del TTL no vuelve a mirar el disco en cada request.
    llamadas = []
    monkeypatch.setattr(pdf_motor.shutil, "which",
                        lambda n: llamadas.append(n) or None)
    assert pdf_motor.binario() is None
    assert llamadas == []
    # Pasa el TTL (instalaron el navegador en el medio) → lo encuentra.
    monkeypatch.setattr(pdf_motor, "TTL_NEGATIVO_S", 0.0)
    falso = tmp_path / "chromium"
    falso.write_text("")
    monkeypatch.setattr(pdf_motor.shutil, "which",
                        lambda n: str(falso) if n == "chromium" else None)
    assert pdf_motor.binario() == str(falso)
    # Y una vez que lo tiene, no busca nunca más.
    monkeypatch.setattr(pdf_motor.shutil, "which", lambda n: None)
    assert pdf_motor.binario() == str(falso)
    pdf_motor._resetear_cache()


def test_el_html_queda_listo_para_abrirse_sin_red(tmp_path):
    """Dos cosas que, sin arreglar, dan un PDF en blanco o un cuelgue.

    · `/static/tailwind.css` es una ruta del servidor web y el navegador va a
      abrir un `file://`: sin reescribir, el PDF sale sin una sola regla de
      estilo (texto negro apilado).
    · Los `<script src="https://…">` (htmx viene de unpkg) se sacan: el
      headless corre en el servidor, que puede no tener salida a internet, y
      esperar un script que nunca llega es la forma más común de que el render
      se cuelgue hasta el timeout.
    """
    html = ('<link href="/static/tailwind.css" rel="stylesheet">'
            '<script src="https://unpkg.com/htmx.org@1.9.12"></script>'
            '<script defer src="/static/app.js?v=14"></script>'
            '<p>hola</p>')
    salida = pdf_motor._para_imprimir_offline(html, tmp_path)
    assert "/static/tailwind.css" not in salida
    assert tmp_path.resolve().as_uri() + "/tailwind.css" in salida
    assert "unpkg.com" not in salida
    # El estático local NO se toca: sigue estando, sólo que apuntando al disco.
    assert "app.js" in salida
    assert "<p>hola</p>" in salida


# ---------------------------------------------------------------------------
# Las dos puertas y la misma cocina
# ---------------------------------------------------------------------------


def _datos(cod="MWI"):
    return {
        "cliente": {"codigo_cli": cod, "nombre": "MARIO W INNOVANOVENTA S.A",
                    "provincia": "STO DOMING", "canton": "STO DOMING",
                    "ruc": "2390646219001", "telefono": "0989506447",
                    "vend": "RMY", "pago": "C", "descuento": 14.0,
                    "stop": "", "pase": "", "cupo": 0,
                    "mail": {"mail": "x@y.com", "origen": "", "etiqueta": "",
                             "alternativo": "", "sugerido": "", "origen_alt": "",
                             "incompleto": ""},
                    "direccion1": "MACHALA", "direccion2": ""},
        "facturas": [{"id_factura": 1, "numf": 167080,
                      "numf_completo": "001-099-000167080",
                      "fecha": date(2026, 1, 15), "vencimiento": date(2026, 2, 14),
                      "importe": 7592.26, "abono": 0.0, "saldo": 7592.26,
                      "stat": "A", "tipo": "FA", "kg": 0, "condic": ""}],
        "cheques": [], "anticipos": [],
        "totales": _totales(importe=7592.26, saldo=7592.26, saldo_neto=7592.26,
                            saldo_vivo=7592.26, n_vencidas=1),
    }


@pytest.fixture()
def vendedor(app, client, fake_db):
    rid = fake_db.add_role("Vendedor", ["micartera.ver"])
    fake_db.add_user("pablo", bcrypt.hashpw(b"V2026", bcrypt.gensalt(rounds=4)),
                     rid, vend="PPR")
    client.post("/login", data={"username": "pablo", "password": "V2026"})
    return client


@pytest.fixture()
def oficina(app, fake_db):
    rid = fake_db.add_role("Oficina", ["informes.ver"])
    uid = fake_db.add_user("alex", b"$2b$12$fake", rid)
    c = app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = uid
    return c


def test_el_pdf_sale_del_template_que_ya_se_imprime(app, monkeypatch):
    """⭐ La invariante de todo esto.

    El primer intento fue armar el PDF en el celular con html2canvas. Andaba,
    y estaba mal: la hoja linda vive en `@media print`, así que para que el
    PDF saliera igual había que copiar esas reglas a otro lado — dos versiones
    de la misma hoja, y esta es la que se le manda al cliente. Ahora el PDF se
    genera imprimiendo el MISMO template. Si alguien le hace uno propio, acá
    se cae.
    """
    visto = {}

    def _render(tpl, **ctx):
        visto["tpl"] = tpl
        visto["n"] = len(ctx.get("clientes") or [])
        return "<html>hoja</html>"

    monkeypatch.setattr(estado_cuenta_pdf, "render_template", _render)
    monkeypatch.setattr(estado_cuenta_pdf.pdf_motor, "desde_html",
                        lambda h: b"%PDF-1.4 " + h.encode())

    with app.test_request_context("/"):
        out = estado_cuenta_pdf.generar(_datos())

    assert visto["tpl"] == "informes/estado_cuenta_lote_print.html"
    assert visto["n"] == 1
    assert out.startswith(b"%PDF")


def test_el_vendedor_no_saca_el_pdf_de_un_cliente_ajeno(vendedor, monkeypatch):
    """La misma fuga que la ficha: alcanzaría con tipear el código en la URL."""
    monkeypatch.setattr(q, "cliente_es_mio", lambda vend, cod: False)
    assert vendedor.get("/mi-cartera/cliente/AJENO/pdf").status_code == 404


def test_las_dos_puertas_devuelven_EL_MISMO_archivo(vendedor, oficina, monkeypatch):
    """Dueña 2026-08-04: el vendedor y Alex mandan lo mismo.

    Si divergieran, el cliente podría recibir dos estados de cuenta distintos
    del mismo día según quién se lo mandó.
    """
    from modules.informes import queries as iq
    from modules.mi_cartera import views as mv

    monkeypatch.setattr(q, "cliente_es_mio", lambda vend, cod: True)
    monkeypatch.setattr(mv.informes_queries, "estado_cuenta_cliente",
                        lambda cod: _datos(cod))
    monkeypatch.setattr(iq, "estado_cuenta_cliente", lambda cod: _datos(cod))
    monkeypatch.setattr(pdf_motor, "desde_html", lambda h: b"%PDF-1.4 igual")

    a = vendedor.get("/mi-cartera/cliente/MWI/pdf")
    b = oficina.get("/informes/estado-cuenta/MWI/pdf")
    assert a.status_code == b.status_code == 200
    assert a.data == b.data
    assert a.mimetype == b.mimetype == "application/pdf"
    # Y el archivo llega con el nombre del cliente, que es lo que el cliente
    # ve en el chat antes de abrirlo.
    for r in (a, b):
        assert "MARIO W INNOVANOVENTA S.A.pdf" in r.headers["Content-Disposition"]


def test_sin_motor_el_servidor_explica_en_vez_de_dar_500(oficina, monkeypatch):
    from modules.informes import queries as iq

    monkeypatch.setattr(iq, "estado_cuenta_cliente", lambda cod: _datos(cod))

    def _explota(_html):
        raise pdf_motor.SinMotor("no hay navegador")

    monkeypatch.setattr(pdf_motor, "desde_html", _explota)
    r = oficina.get("/informes/estado-cuenta/MWI/pdf")
    assert r.status_code == 503
    assert "navegador" in r.data.decode()
    # Y aclara que lo otro sigue andando: el usuario no tiene que adivinar si
    # se rompió todo o sólo esto.
    assert "impresi" in r.data.decode().lower()
