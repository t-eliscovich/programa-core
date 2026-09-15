"""El listado de /cheques repite el total al PIE de la tabla — TMT 2026-09-15.

Filtrando /cheques por cliente=RUS (2 devueltos +400 c/u, 2 "Saldo a favor"
ANTICIPO -400 c/u, `no_banco=98`) el total de arriba daba $0. Primera vuelta:
*"Falta el valor en el total"* / *"El de abajo no dice nada"*. Investigado
más a fondo, la dueña aclaró que el $0 de arriba (neteo real: los devueltos
se cancelan contra su saldo a favor) NO era el problema — *"estaba bien
igual que el total era 0... eso no era un problema"*. Lo único que faltaba
era que ESE MISMO número se viera también al pie de la lista, sin tener que
scrollear hasta arriba: *"lo unico que queria es que el total de abajo
tenga tambien el numero de total"*.

`total_buscar()` quedó SIN TOCAR — sigue sumando todo lo que no sea
reversado ('X'), saldo a favor incluido, tal como estaba. Lo único nuevo es
el `<tfoot>` que repite `_hero_total`/`_hero_n` al final de la tabla.

Lo que sí queda del primer intento: el checkbox de "Depositar lote" sigue
excluyendo el espejo del saldo a favor (`no_banco=98`) — no es un cheque
depositable, y dejarlo marcable arrastraba su importe negativo a la barra
flotante de abajo cuando se lo tildaba junto con cheques reales. Eso es un
problema distinto (qué se puede depositar) del de la suma que se ve en
pantalla, y la dueña no pidió revertirlo.
"""
from __future__ import annotations

import re
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
QUERIES = (RAIZ / "modules/cheques/queries.py").read_text(encoding="utf-8")
LISTA = (RAIZ / "modules/cheques/templates/cheques/lista.html").read_text(encoding="utf-8")


def _cuerpo_total_buscar() -> str:
    _, _, cola = QUERIES.partition("def total_buscar(")
    return cola.split("\ndef ")[0]


def test_total_buscar_no_excluye_el_saldo_a_favor():
    """La dueña: el $0 (neteo real) no era un bug — no tocar esta suma."""
    cuerpo = _cuerpo_total_buscar()
    assert not re.search(r"no_banco,\s*0\)\s*<>\s*98", cuerpo), (
        "total_buscar() no tiene que excluir no_banco=98: la dueña confirmó "
        "que el total neteado (p.ej. $0 cuando el saldo a favor cancela un "
        "devuelto) es correcto, no un bug"
    )


def test_total_buscar_sigue_excluyendo_los_reversados():
    """El vaivén de hoy no puede pisar el filtro de stat='X' que ya existía."""
    cuerpo = _cuerpo_total_buscar()
    assert "COALESCE(c.stat, '') <> 'X'" in cuerpo


def test_checkbox_de_depositar_lote_excluye_el_espejo_no_banco_98():
    """No se puede depositar un espejo contable — esto es aparte de la suma."""
    assert (
        "{% if stat_u in ('Z', 'P', '1', '2') and (c.no_banco or 0)|int != 98 %}"
        in LISTA
    ), (
        "el checkbox de 'Depositar lote' tiene que seguir excluyendo el "
        "espejo del saldo a favor (no_banco=98): no es un cheque "
        "depositable"
    )


def test_hay_un_total_al_pie_de_la_tabla():
    """El pedido concreto: el mismo número de arriba, repetido abajo."""
    assert "<tfoot>" in LISTA
    _, _, cola = LISTA.partition("<tfoot>")
    pie = cola.split("</tfoot>")[0]
    assert "_hero_total" in pie and "_hero_n" in pie, (
        "el pie tiene que mostrar EL MISMO total que ya se calcula arriba "
        "(_hero_total/_hero_n), no un número aparte"
    )


def test_el_total_del_pie_queda_alineado_bajo_importe():
    """"ponelo debajo de importe, no en cualquier lado" (dueña, 2026-09-15).

    El colspan de ANTES de Importe se arma dinámicamente (checkbox según
    permiso, Cliente+Vend según haya filtro de cliente) para que el número
    caiga siempre bajo la columna Importe, no en una posición fija que se
    desalinea cuando cambia el permiso o el filtro.
    """
    _, _, cola = LISTA.partition("<tfoot>")
    pie = cola.split("</tfoot>")[0]
    assert "_cols_antes_importe" in pie
    assert re.search(r'colspan="\{\{\s*_cols_antes_importe\s*-\s*1\s*\}\}"', pie), (
        "el total tiene que ir en la celda que sigue a un colspan de "
        "`_cols_antes_importe - 1`, o sea justo debajo de Importe"
    )
    # la celda con el monto no puede llevar colspan — si no, se corre de la
    # columna Importe y vuelve a caer "en cualquier lado".
    monto_idx = pie.index("_hero_total")
    celda_monto = pie.rfind("<td", 0, monto_idx)
    assert "colspan" not in pie[celda_monto:monto_idx]


def test_el_pie_no_aparece_con_la_lista_vacia():
    """Sin filas no hay nada que repetir — evita un total de más pegado
    arriba del mensaje 'Sin cheques en el filtro actual.'"""
    assert "{% if filas %}" in LISTA
    i = LISTA.index("{% if filas %}\n      {% set _cols_antes_importe")
    j = LISTA.index("<tfoot>", i)
    assert j - i < 400, "el <tfoot> tiene que estar DENTRO del {% if filas %}"
