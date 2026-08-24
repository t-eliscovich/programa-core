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

TMT 2026-08-24 (dueña: *"cargar la página facturas es un poco lento"*): el
handler salió del atributo y vive UNA vez en base.html; el <form> por fila
también se fue (se arma en el momento). Con 500 filas eran 540 KB de pantalla
repitiendo lo mismo. Lo que estos tests cuidan es lo mismo de antes: que el
dropdown llegue con TODO lo que necesita para postear —la URL, el estado
actual, el número— y que haya alguien del otro lado que lo mande.
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


def test_el_select_llega_con_todo_lo_que_necesita_para_postear(app):
    """Sin la URL, el estado actual o el número, el dropdown es decorativo."""
    sel = _render_stat_select(app)
    assert "stat-select" in (sel.get("class") or ""), (
        "sin la clase `stat-select` el handler de base.html no lo escucha y "
        "mover el dropdown no hace nada"
    )
    assert sel.get("data-action") == "/informes/estado-cuenta/FET/factura/275302/set-stat"
    assert sel.get("data-cur") == "A"
    assert sel.get("data-numf") == "175737"


def test_el_select_no_tiene_atributos_basura(app):
    """Un atributo cortado deja los pedazos del JS como atributos sueltos.

    En producción el select tenía 18 atributos; los legítimos son 6 (el
    `style` con el font-size se fue a la clase `.stat-select`, 24/08).
    """
    sel = _render_stat_select(app)
    esperados = {"name", "data-cur", "data-numf", "data-action", "title",
                 "class"}
    assert set(sel) == esperados, (
        "atributos inesperados en el <select> — casi seguro es un atributo "
        f"que se cerró antes de tiempo y el parser leyó el resto como "
        f"atributos: {sorted(set(sel) - esperados)}"
    )


def test_el_select_ya_no_arrastra_un_form_por_fila(app):
    """El peso que se fue: 500 filas × (form + csrf + next + handler)."""
    with app.test_request_context("/informes/estado-cuenta/FET"):
        macros = app.jinja_env.get_template("_ui.html").make_module(vars={})
        html = str(macros.stat_select("A", "FET", 275302, 175737))
    assert "<form" not in html.lower()
    assert "csrf_token" not in html
    assert "onchange" not in html.lower()
    assert len(html) < 700, f"el select engordó de nuevo: {len(html)} bytes"


def test_hay_alguien_del_otro_lado_que_lo_postea():
    """El handler vive en base.html. Si se borra, ningún select postea."""
    base = (RAIZ / "templates" / "base.html").read_text(encoding="utf-8")
    assert "stat-select" in base, (
        "base.html no escucha los dropdowns de estado: cambiar Z/A/T/X no "
        "haría nada en ninguna pantalla"
    )
    bloque = base[base.index("stat-select") - 800:]
    for pieza in ("csrf_token", "next", "confirm(", "f.submit()",
                  "dataset.action"):
        assert pieza in bloque, f"al handler de base.html le falta {pieza!r}"


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
