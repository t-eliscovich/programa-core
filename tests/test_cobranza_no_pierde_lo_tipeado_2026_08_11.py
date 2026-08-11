"""La pantalla de error de /cheques/nuevo devuelve TODO lo que se tipeó.

Reporte de Alex (11/08/2026): *"esta generando poquitin de inconvenientes lo
del duplicado, por que salta la alerta y debo volver a ingresar el banco y el
codigo"*. El aviso de posible duplicado (freno del 05/08, caso BAN 730) no es
un error: pide tildar «Es otro cheque, no es duplicado» y volver a guardar. O
sea que su camino feliz PASA por el re-render — y el re-render le vaciaba
campos.

Por qué pasaba: el primer bloque de cheque se re-poblaba desde `form.importe`
/ `form.no_cheque` / `form.no_banco` sueltos. `form["no_banco"]` sale de
`request.form.get("no_banco")` y ese campo NO EXISTE en el POST (el form manda
`no_banco[]`), así que volvía None; `stat` y `doc_banco` no se guardaban en
ningún lado. Y el JS que re-arma los bloques arranca con
`if (lista.length <= 1) return`, o sea que con UN cheque —el caso normal— no
restauraba nada. El backend siempre tuvo los seis campos crudos en
`form["cheques"]`; el template no los miraba.

Lo que se prueba es el invariante, no los dos campos del reporte: **de todo lo
que el usuario tipeó en el primer cheque, no se puede perder nada.**
"""
from __future__ import annotations

from html.parser import HTMLParser

import pytest

# Valores marcados, distintos entre sí, para que un campo no pueda "acertar"
# copiando el de al lado.
TIPEADO = {
    "importe": "500.00",
    "no_banco": "90",
    "no_cheque": "993906",
    "fechad": "2026-08-20",
    "stat": "B",
    "doc_banco": "8447996",
    "concepto": "Abono a cheques",
}


class _Campos(HTMLParser):
    """Lee el form como lo lee el browser: values e <option selected>."""

    def __init__(self):
        super().__init__()
        self.values: dict[str, str] = {}
        self.seleccionado: dict[str, str] = {}
        self._select: str | None = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "input" and a.get("name"):
            self.values.setdefault(a["name"], a.get("value") or "")
        elif tag == "select":
            self._select = a.get("name")
        elif tag == "option" and self._select and "selected" in a:
            self.seleccionado.setdefault(self._select, a.get("value") or "")

    def handle_endtag(self, tag):
        if tag == "select":
            self._select = None


def _render(app, form: dict) -> _Campos:
    with app.test_request_context("/cheques/nuevo"):
        # Se renderiza el bloque `content` y no la página entera: base.html
        # exige sesión y acá lo que se mira es el form, no el chrome.
        tmpl = app.jinja_env.get_template("cheques/nuevo.html")
        ctx = tmpl.new_context({
            "form": form,
            "errores": ["⚠ Posible duplicado: ya existe un cheque de EPC por $ 500,00."],
            "bancos": [
                {"no_banco": 10, "nombre": "PICHINCHA"},
                {"no_banco": 90, "nombre": "DEP.PICH."},
                {"no_banco": 97, "nombre": "ANTICIPO"},
            ],
            "clientes_datalist": [],
        })
        html = "".join(tmpl.blocks["content"](ctx))
    p = _Campos()
    p.feed(html)
    return p


def _form_como_lo_arma_el_backend() -> dict:
    """Igual que `views.nuevo` cuando vuelve con errores."""
    return {
        "fecha_recibido": "2026-08-11",
        "codigo_cli": "EPC",
        "cheques": [dict(TIPEADO)],
    }


def test_el_aviso_de_duplicado_no_le_borra_nada_de_lo_que_tipeo(app):
    campos = _render(app, _form_como_lo_arma_el_backend())
    perdidos = []
    for campo, esperado in TIPEADO.items():
        if campo in ("no_banco", "stat"):
            actual = campos.seleccionado.get(f"{campo}[]", "")
        else:
            actual = campos.values.get(f"{campo}[]", "")
        if actual != esperado:
            perdidos.append(f"{campo}: esperaba {esperado!r}, volvió {actual!r}")
    assert not perdidos, (
        "El re-render le hace retipear esto:\n  " + "\n  ".join(perdidos)
    )


@pytest.mark.parametrize("campo", sorted(TIPEADO))
def test_cada_campo_del_primer_cheque_vuelve(app, campo):
    """Uno por uno, para que el que falle se lea en el nombre del test."""
    campos = _render(app, _form_como_lo_arma_el_backend())
    actual = (campos.seleccionado if campo in ("no_banco", "stat") else campos.values).get(
        f"{campo}[]", ""
    )
    assert actual == TIPEADO[campo]


def test_el_form_vacio_no_inventa_valores(app):
    """Sin `cheques` (alta nueva) los campos salen limpios, no con basura."""
    campos = _render(app, {})
    assert campos.values.get("importe[]", "") == ""
    assert campos.values.get("no_cheque[]", "") == ""
    assert campos.values.get("doc_banco[]", "") == ""
    assert campos.seleccionado.get("stat[]") == "Z", "el default sigue siendo cartera"


def test_volver_desde_crear_cliente_sigue_restaurando(app):
    """El otro camino: /clientes/nuevo vuelve por querystring, SIN `cheques`.

    Ese form trae los campos sueltos, así que el fallback tiene que seguir
    vivo — si se borra, se rompe el alta de cliente desde la cobranza.
    """
    campos = _render(app, {
        "codigo_cli": "EPC",
        "importe": "500.00",
        "no_cheque": "993906",
        "no_banco": "90",
    })
    assert campos.values.get("importe[]") == "500.00"
    assert campos.values.get("no_cheque[]") == "993906"
    assert campos.seleccionado.get("no_banco[]") == "90"
