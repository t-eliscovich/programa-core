"""La Cartera pesaba 4,5 MB de HTML. La mitad no se usa nunca.

TMT 2026-08-24 (dueña): *"también cargar la página facturas es un poco
lento"*. Medido sobre producción: /facturas devolvía **4.528 KB** con 500
filas — **9 KB por fila**. De esos 9 KB:

  · **1,9 MB en total (43 %)** eran los SEIS editores escondidos de cada fila
    (N°, fecha, cliente, kg, importe, retención): input + ✓ + ✗ por celda,
    dibujados por si alguien toca el lapicito. Casi nunca se tocan.
  · **540 KB (12 %)** eran el `<form>` del dropdown de estado repetido 500
    veces, cada uno con su csrf, su `next` y el handler entero adentro del
    atributo `onchange`.

Ahora el editor se arma cuando se toca el lapicito y el formulario del estado
se arma al cambiarlo. Lo que se ve en pantalla es idéntico.

Estos tests miran el FUENTE del template a propósito: el peso se recupera
escribiendo HTML de más en el `{% for %}`, y eso se ve leyendo el template.
"""
from __future__ import annotations

import re
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
LISTA = RAIZ / "modules" / "facturas" / "templates" / "facturas" / "lista.html"


def _fuente() -> str:
    return LISTA.read_text(encoding="utf-8")


def test_la_fila_no_trae_editores_dibujados():
    """Seis por fila × 500 filas = 1,9 MB que el browser parsea al pedo."""
    texto = _fuente()
    filas = texto[texto.index("{% for f in filas %}"):texto.index("</table>")]
    assert 'class="ec-edit hidden' not in filas
    assert 'class="numf-edit hidden' not in filas
    assert "ec-input" not in filas, (
        "el input de edición volvió al HTML de la fila: se arma en JS"
    )


def test_el_editor_se_arma_al_tocar_el_lapicito():
    """Si no lo arma nadie, el lapicito deja de funcionar."""
    texto = _fuente()
    assert "function armarEditor(cell)" in texto
    assert "function armarEditorNumf(cell)" in texto
    assert "armarEditor(cell).classList.remove('hidden')" in texto
    assert "const box = armarEditorNumf(cell);" in texto


def test_el_editor_armado_trae_las_tres_piezas():
    """input + guardar + cancelar, con las clases que el handler busca."""
    texto = _fuente()
    for pieza in ("'ec-input", "'ec-save", "'ec-cancel",
                  "'numf-input", "'numf-save", "'numf-cancel"):
        assert pieza in texto, f"al editor le falta {pieza}"


def test_las_clases_van_escritas_enteras():
    """Tailwind lee este archivo como TEXTO para saber qué clases existen.

    Una clase armada con `+` no la ve, y después de un rebuild el input sale
    sin ancho ni borde. Ver la memoria «tailwind congelado, clases fantasma».
    """
    texto = _fuente()
    js = texto[texto.index("function armarEditor"):]
    assert not re.search(r"className\s*=\s*'[^']*'\s*\+", js), (
        "una clase armada con `+`: Tailwind no la ve y el rebuild la borra"
    )
    for clase in ("w-32", "w-24", "w-20"):
        assert f"ec-input {clase} " in texto, (
            f"{clase} tiene que aparecer literal para que Tailwind la conserve"
        )


def test_el_editor_de_la_retencion_arranca_vacio():
    """La retención se CARGA (no se edita): el input no propone el 0,00."""
    texto = _fuente()
    assert "if (campo === 'retencion') inp.placeholder = '0,00';" in texto


def test_las_celdas_siguen_diciendo_que_campo_son():
    """El editor se arma leyendo `data-campo`: sin eso no sabe qué dibujar."""
    filas = _fuente()
    for campo in ("fecha", "codigo_cli", "kg", "importe", "retencion"):
        assert f'data-campo="{campo}"' in filas
