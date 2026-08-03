"""El dropdown de estado (Z/A/T/X) tiene que llegar al browser ENTERO.

TMT 2026-08-03 (dueña): "muevo factura de A a T en estado de cuenta y no
funciona". No era el backend: `stat_select` interpolaba el numf con `|tojson`
adentro de un atributo `onchange="…"` delimitado por comillas dobles. `tojson`
devuelve Markup (Jinja no lo escapa) y emite `"175737"` CON comillas dobles, así
que el parser HTML cerraba el atributo ahí: el handler llegaba al browser
truncado en 90 caracteres, no compilaba, y `select.onchange` quedaba en null →
mover el dropdown no hacía nada (ni en el estado de cuenta ni en la Cartera).
Verificado en vivo el 03/08 sobre /informes/estado-cuenta/FET: el `<select>`
tenía 18 atributos en vez de 7 (los pedazos sueltos del JS) y onchange terminaba
en '+'.

Los tests parsean el HTML como lo parsea el browser (html.parser), no con
regex sobre el fuente del template: es la única forma de ver el truncamiento.
"""
from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parent.parent


class _Selects(HTMLParser):
    """Junta los <select> con sus atributos, tal cual los ve el browser."""

    def __init__(self):
        super().__init__()
        self.selects: list[dict[str, str | None]] = []

    def handle_starttag(self, tag, attrs):
        if tag == "select":
            self.selects.append(dict(attrs))


def _render_stat_select(app) -> dict[str, str | None]:
    with app.test_request_context("/informes/estado-cuenta/FET"):
        macros = app.jinja_env.get_template("_ui.html").make_module(vars={})
        html = str(macros.stat_select("A", "FET", 275302, 175737))
    p = _Selects()
    p.feed(html)
    assert len(p.selects) == 1, "se esperaba un solo <select> por fila"
    return p.selects[0]


def test_onchange_llega_completo_y_dispara_el_submit(app):
    """El handler entero — si se trunca, el dropdown es decorativo."""
    sel = _render_stat_select(app)
    onchange = sel.get("onchange") or ""
    assert "requestSubmit" in onchange, (
        "el onchange llegó truncado: sin requestSubmit el dropdown no postea "
        f"nada. Recibido ({len(onchange)} chars): {onchange!r}"
    )
    assert onchange.rstrip().endswith("}"), (
        f"el onchange no cierra — atributo cortado a la mitad: {onchange!r}"
    )
    # El número de factura tiene que estar disponible para el confirm().
    assert sel.get("data-numf") == "175737"


def test_el_select_no_tiene_atributos_basura(app):
    """Un atributo cortado deja los pedazos del JS como atributos sueltos.

    En producción el select tenía 18 atributos; los legítimos son 7.
    """
    sel = _render_stat_select(app)
    esperados = {"name", "data-cur", "data-numf", "title", "class", "style",
                 "onchange"}
    assert set(sel) == esperados, (
        "atributos inesperados en el <select> — casi seguro es un atributo "
        f"que se cerró antes de tiempo y el parser leyó el resto como "
        f"atributos: {sorted(set(sel) - esperados)}"
    )


@pytest.mark.parametrize(
    "tpl",
    sorted(
        str(p.relative_to(RAIZ))
        for p in list((RAIZ / "templates").rglob("*.html"))
        + list((RAIZ / "modules").rglob("templates/**/*.html"))
    ),
)
def test_ningun_atributo_con_comillas_dobles_usa_tojson(tpl):
    """Regla general, no sólo para stat_select.

    `|tojson` adentro de un atributo `algo="…"` rompe el HTML siempre, porque
    tojson escapa `<`, `>`, `&` y `'` pero NO la comilla doble. Si hace falta
    JSON en un atributo: delimitarlo con comillas simples y `|forceescape`
    (como hace conciliacion/banco_resultado.html), o pasarlo por `data-*`.
    """
    import re

    texto = (RAIZ / tpl).read_text(encoding="utf-8")
    # atributo="…|tojson…" en la MISMA línea, con comillas dobles.
    malo = re.compile(r'[\w-]+="[^"\n]*\|\s*tojson(?![^"\n]*forceescape)')
    for n, linea in enumerate(texto.splitlines(), 1):
        assert not malo.search(linea), (
            f"{tpl}:{n} — `|tojson` dentro de un atributo con comillas dobles: "
            "el browser corta el atributo en la primera comilla del JSON. "
            "Usá data-* o comillas simples + |forceescape."
        )
