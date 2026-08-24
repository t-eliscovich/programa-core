"""El panel RESULTADOS no puede recortar su última columna (Proyecciones).

Dueña 2026-08-24: *"y proyecciones se corta"*. Dos causas, las dos acá:

1. `.informe-balance .balance-col { overflow-x: auto }` (el fallback de celular)
   estaba DESPUÉS del `@media (min-width:1024px)` que lo pone en `visible`.
   Misma especificidad ⇒ gana el último ⇒ en escritorio el panel recortaba por
   su cuenta en vez de dejar que scrollee el marco entero.
2. El piso de ancho del panel izquierdo (590px) era menor que lo que la tabla
   necesita de verdad (623px medidos en el navegador), así que la quinta
   columna quedaba tapada por el panel ACTIVO.

Federico 2026-08-24 (más tarde, *"los 2 cuadros deberían tener aprox. el mismo
ancho"*): el 623 salía de una tabla con paddings de 12px por lado que NADIE
quiso — los 5/3px declarados en balance.html no llevaban `!important` y
`main table tbody td { padding: .55rem .75rem !important }` de base.html los
pisaba. Con los paddings aplicados de verdad (y el input de Kg en 72px en vez
de 92) el mínimo MEDIDO del panel bajó a 545px, así que el piso pasó a 552.
El test ahora vigila las dos cosas: el piso y el `!important` que lo sostiene —
si alguien se lo saca, la tabla vuelve a pedir 647px y Proyecciones se corta.
"""
from pathlib import Path

import pytest

TPL = (Path(__file__).resolve().parent.parent / "modules" / "informes"
       / "templates" / "informes" / "balance.html")


@pytest.fixture(scope="module")
def css() -> str:
    return TPL.read_text(encoding="utf-8")


def test_el_fallback_de_celular_va_antes_del_media_de_escritorio(css: str) -> None:
    fallback = css.index(".informe-balance .balance-col { min-width: 0; overflow-x: auto; }")
    escritorio = css.index(".informe-balance .balance-col { overflow-x: visible; }")
    assert fallback < escritorio, (
        "el overflow-x:auto del celular quedó después del @media de escritorio: "
        "lo pisa y el panel vuelve a recortar la columna Proyecciones"
    )


def test_el_piso_del_panel_izquierdo_alcanza_para_las_cinco_columnas(css: str) -> None:
    linea = next(linea_css for linea_css in css.splitlines()
                 if ".balance-col:first-child" in linea_css and "min-width" in linea_css)
    ancho = int(linea.split("min-width:")[1].split("px")[0].strip())
    assert ancho >= 545, (
        f"el panel RESULTADOS pide 545px medidos y el piso quedó en {ancho}px: "
        "la columna Proyecciones se corta contra el panel ACTIVO"
    )


def test_los_paddings_de_celda_le_ganan_a_los_globales_de_base(css: str) -> None:
    """Sin `!important` los 5/3px no se aplican y el panel vuelve a pedir 647px.

    base.html pisa TODA celda de `<main>` con `main table tbody td { padding:
    .55rem .75rem !important }` y `main table thead th { padding-left/right:
    .75rem !important }`. Una regla local sin `!important` pierde por más
    específica que sea — es el mismo error que ya se cometió con el font-size
    de `th.col-header`. Si esto se rompe, el piso medido de 545px deja de
    alcanzar y Proyecciones se corta de nuevo.
    """
    for selector in ("td.label-cell", "td.num-cell", "th.col-header"):
        inicio = css.index(f".informe-balance {selector} ")
        # el cierre de la regla es un "}" al principio de línea (los "}" que
        # aparecen dentro de los comentarios están siempre indentados adentro)
        bloque = css[inicio:css.index("\n  }", inicio)]
        assert "padding" in bloque, (
            f".informe-balance {selector} dejó de declarar su padding"
        )
        linea = next(ln for ln in bloque.splitlines() if "padding" in ln)
        assert "!important" in linea, (
            f"el padding de .informe-balance {selector} perdió el !important: "
            "base.html lo pisa con 12px por lado y vuelve el aire entre columnas"
        )
