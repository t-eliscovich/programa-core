"""El foco arranca en el IMPORTE en las dos pantallas de carga de caja.

Pedido de la dueña 2026-08-17, literal: "al cargar salida de caja o entrada,
que el mouse ya este en el importe, asi no tengo que clieckear para poner el
importe".

Las dos pantallas donde se carga plata en caja son `/caja/nuevo` (movimiento
único, con atajos y chips V1..V9) y `/caja/cargar` (multi-línea). En las dos,
la Fecha ya viene puesta en hoy y el Tipo tiene default, así que lo primero
que ella tipea es el importe.

Por qué se prueba sobre el TEXTO del template y no renderizando: el foco lo
pone el navegador (`autofocus` / `.focus()`), y ni el test client de Flask ni
un parseo de HTML lo ejecutan. Lo que sí se puede blindar es que la
instrucción de foco siga estando y siga apuntando al importe — que es
exactamente lo que se rompería si alguien reordena el form.

En `/caja/cargar` el foco NO puede ser un atributo `autofocus`: el botón
"+ línea" clona la última fila con `cloneNode(true)`, y el atributo se
arrastraría a las copias. Por eso ahí va por JS.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

TEMPLATES = Path(__file__).resolve().parent.parent / "modules" / "caja" / "templates" / "caja"


@pytest.fixture(scope="module")
def nuevo() -> str:
    return (TEMPLATES / "nuevo.html").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def cargar() -> str:
    return (TEMPLATES / "cargar.html").read_text(encoding="utf-8")


def _sin_comentarios(html: str) -> str:
    """Saca los comentarios Jinja y los `//` de JS.

    Hace falta porque los dos templates EXPLICAN en un comentario por qué usan
    (o no usan) `autofocus`: contar la palabra a secas daría falsos positivos.
    """
    html = re.sub(r"\{#.*?#\}", "", html, flags=re.S)
    return "\n".join(
        linea for linea in html.splitlines() if not linea.lstrip().startswith("//")
    )


def _bloque_del_input(html: str, name: str) -> str:
    """Devuelve el tag <input> que tiene name="<name>" (el primero)."""
    i = html.index(f'name="{name}"')
    ini = html.rindex("<input", 0, i)
    fin = html.index(">", i)
    return html[ini : fin + 1]


# ---------------------------------------------------------------------------
# /caja/nuevo — movimiento único
# ---------------------------------------------------------------------------


def test_nuevo_el_importe_tiene_autofocus(nuevo: str):
    """El input de importe arranca con el foco puesto."""
    assert "autofocus" in _bloque_del_input(nuevo, "importe")


def test_nuevo_ningun_otro_input_se_roba_el_autofocus(nuevo: str):
    """`autofocus` aparece UNA sola vez: si hay dos, gana el primero del DOM."""
    assert _sin_comentarios(nuevo).count("autofocus") == 1


def test_nuevo_el_js_tambien_enfoca_el_importe(nuevo: str):
    """Refuerzo por JS del autofocus (navegadores que lo ignoran)."""
    assert "getElementById('importe-input')" in nuevo
    assert 'id="importe-input"' in nuevo


# ---------------------------------------------------------------------------
# /caja/cargar — multi-línea
# ---------------------------------------------------------------------------


def test_cargar_enfoca_el_importe_de_la_primera_linea(cargar: str):
    """Al abrir la pantalla el foco va al importe de la primera línea."""
    assert "querySelector('.carga-importe')" in cargar
    assert "primerImporte.focus()" in cargar


def test_cargar_no_usa_autofocus_porque_clona_lineas(cargar: str):
    """`+ línea` hace cloneNode(true): un `autofocus` se copiaría a las filas."""
    assert "cloneNode(true)" in cargar
    assert "autofocus" not in _sin_comentarios(cargar)


def test_cargar_la_linea_nueva_tambien_enfoca_el_importe(cargar: str):
    """Comportamiento que ya existía y no se puede perder: + línea → importe."""
    assert "nueva.querySelector('.carga-importe').focus()" in cargar
