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


# ── Segunda pasada (24/08, tarde): las clases repetidas fila por fila ───────
# Quedaba en 2.568 KB, 5 KB por fila, y la mitad eran las MISMAS listas de
# clases copiadas 500 veces. Ahora van una vez, en el <style> de la pantalla.

def test_los_lapicitos_no_repiten_su_lista_de_clases():
    """130 caracteres × 6 lapicitos × 500 filas = 400 KB de lo mismo."""
    texto = _fuente()
    filas = texto[texto.index("{% for f in filas %}"):texto.index("</table>")]
    assert 'class="ec-edit-btn lapiz"' in filas
    assert 'class="numf-edit-btn lapiz lapiz-oscuro"' in filas
    assert "hover:bg-sky-50" not in filas, (
        "volvieron las clases de Tailwind al lapicito: van en `.lapiz`"
    )


def test_las_vistas_y_la_fila_van_por_clase_propia():
    texto = _fuente()
    filas = texto[texto.index("{% for f in filas %}"):texto.index("</table>")]
    assert 'class="ec-view"' in filas and 'class="numf-view"' in filas
    assert "ec-view inline-flex" not in filas
    assert '<tr class="fila">' in filas


def test_el_estilo_de_la_pantalla_define_lo_que_saco_del_markup():
    """Si el bloque se borra, la Cartera se ve rota."""
    texto = _fuente()
    estilo = texto[texto.index("<style>"):texto.index("</style>")]
    for regla in (".lapiz ", ".lapiz-oscuro", ".lapiz:hover",
                  ".ec-view:not(.hidden)", ".fila:hover"):
        assert regla in estilo, f"falta la regla {regla}"


def test_la_fila_conserva_el_separador_NEGRO():
    """Pedido de la dueña (19/05): *"las líneas en negro, mi papá no las ve"*.

    El negro venía de rebote: la fila traía `border-slate-100` y eso
    enganchaba el override `!important` de base.html. Al sacar la clase hay
    que ponerlo a mano o el separador queda gris clarito.
    """
    estilo = _fuente()
    assert ".fila { border-bottom-color: rgb(15 23 42); }" in estilo


def test_el_dropdown_de_estado_no_repite_sus_clases():
    ui = (RAIZ / "templates" / "_ui.html").read_text(encoding="utf-8")
    macro = ui[ui.index("{% macro stat_select"):ui.index("{% endmacro %}",
                                                        ui.index("{% macro stat_select"))]
    assert 'class="stat-select"' in macro
    assert "border-slate-300" not in macro
    assert "font-size:11px" not in macro
    base = (RAIZ / "templates" / "base.html").read_text(encoding="utf-8")
    assert ".stat-select {" in base, "la clase tiene que existir en base.html"
