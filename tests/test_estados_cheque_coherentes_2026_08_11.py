"""Las definiciones de estado de un cheque no se pueden contradecir entre sí.

La máquina de estados del cheque no vive en un lugar: son ~13 tuplas escritas a
mano (`STATS_DEPOSITADO`, `STATS_EN_CARTERA`, `STATS_NEUTROS`, …), dos
diccionarios (`TRANSICIONES_VALIDAS`, `TRANSICIONES_LEGALES`) y una función
(`_stat_destino_reversa`). Cada vez que alguien agrega un estado o una
transición a UNA de ellas, las otras se quedan atrás — y nada avisa.

Es la causa de los tres bugs del 11/08/2026, que parecían distintos:

· Dos "→1" idénticos en el menú: el destino del asistente y el cambio plano se
  calculaban por caminos separados.
· Un cheque de Internacional que rebotaba se ELIMINABA:
  `_stat_destino_reversa` no listaba I/W/J/K aunque `STATS_DEPOSITADO` sí.
· La pantalla de confirmación del reverso decía "reversión administrativa, no
  afecta al cliente" para esos mismos cheques, porque
  `STATS_REBOTE_REAL` era una tupla paralela que se atrasó.

Este archivo no prueba una pantalla: prueba que las definiciones digan lo mismo.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from modules.cheques import queries

FUENTE = Path(queries.__file__).read_text(encoding="utf8")


def test_quien_es_rebote_real_se_dice_en_un_solo_lugar():
    """`es_rebote_real` deriva de la función que ejecuta, no de una lista.

    Si vuelve a aparecer una tupla con los estados de rebote escrita a mano,
    vuelve el bug: la pantalla dice una cosa y la app hace otra.
    """
    assert not re.search(r"^STATS_REBOTE_REAL\s*=\s*\(", FUENTE, re.M), (
        "Volvió la lista paralela de estados de rebote. Tiene que salir de "
        "_stat_destino_reversa vía es_rebote_real()."
    )
    for stat in queries.STATS_DEPOSITADO:
        assert queries.es_rebote_real(stat), (
            f"Un cheque DEPOSITADO ({stat}) que se reversa es un rebote real: "
            "el banco lo rechazó."
        )
    for stat in ("Z", "P", "D"):
        assert not queries.es_rebote_real(stat), (
            f"Un cheque en cartera ({stat}) nunca fue al banco: no puede rebotar."
        )


def test_ningun_estado_esta_en_dos_familias_a_la_vez():
    dep = set(queries.STATS_DEPOSITADO)
    cart = set(queries.STATS_EN_CARTERA)
    assert not (dep & cart), (
        f"Estos estados dicen estar depositados Y en cartera: {sorted(dep & cart)}"
    )


def test_todo_destino_que_ofrece_el_menu_es_un_estado_conocido():
    conocidos = (
        set(queries.STATS_DEPOSITADO)
        | set(queries.STATS_EN_CARTERA)
        | {"X", "T", "R", "E", "C", "9"}
    )
    ofrecidos = {
        t["stat_destino"]
        for ops in queries.transiciones_map().values()
        for t in ops
    }
    huerfanos = ofrecidos - conocidos
    assert not huerfanos, (
        f"El menú ofrece estados que ninguna familia conoce: {sorted(huerfanos)}"
    )


def test_los_depositados_no_vuelven_a_cartera_por_una_etiqueta_pelada():
    """Salir de un depositado mueve plata: sólo por rebote (9) o anulación (X).

    Un cambio de etiqueta a secas dejaría el depósito colgado en el banco. El
    "1" de B/I es la excepción explícita (casos CJE/NIF, 21/07): NO es pelado,
    emite la nota de débito y el gasto.
    """
    permitido_ademas = {"1"}
    for stat in queries.STATS_DEPOSITADO:
        salidas = set(queries.TRANSICIONES_VALIDAS.get(stat, ()))
        raras = salidas - {"9", "X"} - permitido_ademas
        assert not raras, (
            f"Desde {stat} (depositado) se puede ir a {sorted(raras)} sin tocar "
            "el banco: el depósito quedaría colgado."
        )


@pytest.mark.parametrize("nombre", ["STATS_VIVOS"])
def test_ninguna_constante_de_estados_se_define_dos_veces(nombre):
    """Python se queda con el último binding, sin decir nada.

    `STATS_VIVOS` estuvo definida dos veces con miembros distintos: la de
    arriba mentía y ruff no avisa de una constante redefinida.
    """
    veces = len(re.findall(rf"^{nombre}\s*=\s*\(", FUENTE, re.M))
    assert veces <= 1, (
        f"{nombre} está definida {veces} veces en el mismo módulo; gana la "
        "última y la otra confunde al que lee."
    )


def test_no_hay_constantes_de_estado_muertas_que_mientan():
    """`STATS_TERMINALES = ("B",)` decía que un depositado no tiene salidas.

    No lo usaba nadie, así que no rompía nada — pero el próximo que lo leyera
    de buena fe sí.
    """
    assert not re.search(r"^STATS_TERMINALES\s*=", FUENTE, re.M), (
        "Volvió STATS_TERMINALES. Si hace falta, tiene que derivarse de "
        "TRANSICIONES_VALIDAS (terminal = sin salidas), no escribirse a mano."
    )
