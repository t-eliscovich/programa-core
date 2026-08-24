"""El panel RESULTADOS no puede recortar su última columna (Proyecciones).

Dueña 2026-08-24: *"y proyecciones se corta"*. Dos causas, las dos acá:

1. `.informe-balance .balance-col { overflow-x: auto }` (el fallback de celular)
   estaba DESPUÉS del `@media (min-width:1024px)` que lo pone en `visible`.
   Misma especificidad ⇒ gana el último ⇒ en escritorio el panel recortaba por
   su cuenta en vez de dejar que scrollee el marco entero.
2. El piso de ancho del panel izquierdo (590px) era menor que lo que la tabla
   necesita de verdad (623px medidos en el navegador), así que la quinta
   columna quedaba tapada por el panel ACTIVO.
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
    assert ancho >= 623, (
        f"el panel RESULTADOS pide 623px medidos y el piso quedó en {ancho}px: "
        "la columna Proyecciones se corta contra el panel ACTIVO"
    )
