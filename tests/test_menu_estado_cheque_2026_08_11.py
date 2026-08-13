"""El menú de "cambiar estado" de un cheque no puede ofrecer dos opciones que
se lean igual.

Reportado por la dueña el 11/08/2026 sobre un cheque DEPOSITADO (B) de CVA:
*"lo estoy intentando cambiar a 1 y no me deja"*. El menú de la lista le
ofrecía DOS "→1" idénticos —el asistente de rebote (9) se mostraba con la
letra de su estado resultante (1) y encima estaba el cambio plano a 1— y el de
arriba ni siquiera cambiaba el estado: era un link a otra pantalla.

Lo que se protege acá NO es "el cheque de CVA" ni "el estado B": es el
invariante, para TODOS los estados y todas las transiciones que existan
mañana. Por eso el texto de la opción lo arma UNA función
(`queries.texto_opcion_estado`) que usan la lista y la ficha, en vez de dos
armados en Jinja que ya se habían separado una vez.
"""
from __future__ import annotations

import re

import pytest

from modules.cheques import queries


def _texto(t: dict) -> str:
    """La opción como la lee un humano: la letra de la ACCIÓN + qué hace."""
    return queries.texto_opcion_estado_completo(t)


def test_ningun_menu_de_estado_ofrece_dos_opciones_que_se_leen_igual():
    """Para CADA estado, no dos opciones con el mismo texto."""
    culpables: list[str] = []
    for stat, trans in queries.transiciones_map().items():
        textos = [_texto(t) for t in trans]
        repetidos = {x for x in textos if textos.count(x) > 1}
        if repetidos:
            culpables.append(f"desde {stat!r}: {sorted(repetidos)} en {textos}")
    assert not culpables, (
        "Hay menús con opciones que se leen igual — el usuario no puede saber "
        "cuál elegir:\n  " + "\n  ".join(culpables)
    )


def test_en_la_ficha_ninguna_opcion_se_lee_solo_como_una_letra():
    """En la FICHA cada opción dice letra + qué hace.

    Es donde se mira UN cheque y hay lugar para las palabras. La LISTA va con
    la letra pelada (dueña, 13/08/2026) y la explicación en el `title` — el
    test de abajo cubre ese menú.
    """
    pelados = [
        (stat, _texto(t))
        for stat, trans in queries.transiciones_map().items()
        for t in trans
        if not re.search(r"[A-Za-zÁÉÍÓÚáéíóúñÑ]{3}", _texto(t))
    ]
    assert not pelados, f"Opciones sin texto que las explique: {pelados}"


def test_en_la_lista_las_letras_solas_siguen_siendo_distinguibles():
    """El menú corto de /cheques: sólo la letra, y ninguna repetida.

    Dueña 13/08/2026 (2ª vez): *"dejá sólo la letra y súper chiquito el
    dropdown"*. Volver a la letra pelada es seguro SÓLO mientras la letra sea
    la de la ACCIÓN (`stat_destino`) y no la del estado en el que el cheque
    QUEDA: dibujar el resultado fue lo que creó los dos "→1" idénticos del
    11/08. Si mañana dos acciones distintas comparten destino desde un mismo
    estado, este test se pone rojo ANTES de que la dueña vea dos opciones
    iguales — y ahí la lista tendrá que volver al texto largo.
    """
    culpables: list[str] = []
    for stat in queries.transiciones_map():
        cortos = [
            queries.texto_opcion_estado_corto(t)
            for t in queries.transiciones_para(stat)
        ]
        repetidos = {x for x in cortos if cortos.count(x) > 1}
        if repetidos:
            culpables.append(f"desde {stat!r}: {sorted(repetidos)} en {cortos}")
    assert not culpables, (
        "Con la letra sola estos menús ofrecen dos opciones idénticas:\n  "
        + "\n  ".join(culpables)
    )


def test_la_letra_sola_siempre_viaja_con_su_explicacion():
    """La opción corta es la letra; el texto largo va en el tooltip.

    Si alguna vez el corto y el largo dejan de hablar de la misma letra, el
    tooltip estaría explicando otra cosa que la que se va a ejecutar.
    """
    for stat in queries.transiciones_map():
        for t in queries.transiciones_para(stat):
            corto = queries.texto_opcion_estado_corto(t)
            largo = queries.texto_opcion_estado_completo(t)
            assert corto == f"→{t.get('stat_destino') or ''}", (stat, corto)
            assert largo.startswith(corto), (stat, corto, largo)
            assert len(largo) > len(corto), (
                f"desde {stat!r} la opción {corto!r} no tiene nada que mostrar "
                "en el tooltip"
            )


def test_la_letra_de_la_opcion_es_la_accion_no_el_resultado():
    """El rebote es la acción '9' aunque el cheque termine en '1'.

    Fue justamente disfrazar la letra lo que creó el duplicado. El estado
    resultante se sigue diciendo, pero con palabras.
    """
    trans_b = queries.transiciones_para("B")
    rebote = [t for t in trans_b if t.get("endpoint") == "cheques.confirmar_reverso"]
    assert rebote, "Desde B tiene que poder marcarse el rebote."
    op = rebote[0]
    assert op["stat_destino"] == "9"
    assert _texto(op).startswith("→9"), _texto(op)
    assert "queda en 1" in _texto(op), (
        "Sacarle la letra al resultado está bien, pero hay que seguir diciendo "
        "en qué estado queda: era la duda original de la dueña (14/07)."
    )


def test_desde_depositado_se_puede_marcar_devuelto_de_una():
    """B→1 plano existe y es una sola opción, distinta del rebote."""
    textos = [_texto(t) for t in queries.transiciones_para("B")]
    assert "→1 Devuelto" in textos, textos
    assert textos.count("→1 Devuelto") == 1, textos


@pytest.mark.parametrize("stat", ["Z", "P", "D", "B", "A", "V", "1", "2", "3", "X"])
def test_todos_los_estados_arman_su_menu_sin_reventar(stat):
    for t in queries.transiciones_para(stat):
        assert _texto(t).strip()


def test_si_la_opcion_dice_a_donde_va_es_a_donde_va_de_verdad():
    """La segunda cara del mismo bug, encontrada auditando el resto.

    El asistente de reverso ('9') hace DOS cosas según de dónde venga: rebote
    real desde un depositado/devuelto, o reversa administrativa desde cartera
    —que deja el cheque en 'X'—. El menú decía "Sin fondos" en los dos casos y
    en el segundo ni siquiera decía a dónde iba: prometía un estado al que no
    llegaba, igual que los dos "→1".
    """
    for stat in queries.transiciones_map():
        for t in queries.transiciones_para(stat):
            if t.get("endpoint") != "cheques.confirmar_reverso":
                continue
            esperado, es_rebote = queries._stat_destino_reversa(stat)
            assert t.get("destino_real") == esperado, (
                f"Desde {stat!r} el asistente deja el cheque en {esperado!r} "
                f"pero la opción dice {t.get('destino_real')!r}."
            )
            texto = _texto(t)
            assert f"(queda en {esperado})" in texto, (
                f"Desde {stat!r} la opción no dice dónde termina: {texto!r}"
            )
            if es_rebote:
                assert "Sin fondos" in texto, texto
            else:
                assert "Sin fondos" not in texto, (
                    f"Desde {stat!r} no es un rebote —el cheque nunca fue al "
                    f"banco—, así que no puede decir 'Sin fondos': {texto!r}"
                )


def test_pasar_a_devuelto_no_esta_atado_a_cargar_una_fecha():
    """Paridad dBase (MODIFICA.PRG): FECHAD, IMPORTE y ESTADO se editaban
    juntos, con su valor actual. La fecha nunca fue un paso obligatorio para
    cambiar el estado — y hacerla obligatoria fue lo que dejó a la dueña sin
    poder pasar el cheque de CVA a 1.
    """
    op = [t for t in queries.transiciones_para("B") if t["stat_destino"] == "1"]
    assert op, "Desde B tiene que poder marcarse devuelto."
    assert op[0]["kind"] == "POST", "Un click, no un asistente."
    assert op[0]["endpoint"] == "cheques.transicionar"


def test_ningun_cheque_depositado_se_elimina_al_rebotar():
    """Internacional se maneja igual que Pichincha (dueña 11/08/2026).

    `W`, `I`, `J` y `K` —los depositados legacy del DBF, `I` = INTERNACIONAL—
    caían en el default de `_stat_destino_reversa` y volvían `("X", False)`: el
    cheque que el banco devolvía terminaba ELIMINADO en vez de quedar en
    cartera como devuelto. La nota de débito y el gasto salían bien, así que el
    banco quedaba correcto; el que desaparecía era el cheque, y con él la deuda
    del cliente.

    dBase `MODIFICA.PRG` FIL3: `ENBANC='BVWIJK'` — los seis son "en banco" y un
    depositado sólo puede rebotar a 1/2, nunca a X.
    """
    culpables = []
    for stat in queries.STATS_DEPOSITADO:
        destino, es_rebote = queries._stat_destino_reversa(stat)
        if destino == "X" or not es_rebote:
            culpables.append(f"{stat} → {destino}")
    assert not culpables, (
        "Estos cheques DEPOSITADOS se eliminan al rebotar en vez de quedar "
        f"devueltos: {culpables}"
    )


def test_desde_internacional_se_marca_devuelto_igual_que_pichincha():
    textos = [_texto(t) for t in queries.transiciones_para("I")]
    assert "→1 Devuelto" in textos, textos
    assert "→9 Sin fondos (queda en 1)" in textos, textos


def test_postergar_desde_un_devuelto_muestra_la_letra_en_la_que_QUEDA():
    """Postergar un devuelto no lo saca de devuelto — decisión dueña 16/06.

    Si pasara a 'P' se iría de la solapa Devueltos y saldría de CHEQUES
    PROTESTADOS en el estado de cuenta: la hoja del cliente lo mostraría como
    un cheque normal esperando su fecha, no como uno que ya rebotó. Así que la
    opción muestra la letra en la que el cheque QUEDA, no una 'P' a la que no
    va — el menú sigue siendo letras (dueña 11/08: "que sigan siendo letras").
    """
    for origen in ("1", "2", "D"):
        posterg = [t for t in queries.transiciones_para(origen)
                   if t["kind"] == "POSTERGAR"]
        assert posterg, f"desde {origen} tiene que poder cambiarse la fecha"
        op = posterg[0]
        assert op["stat_destino"] == origen, (
            f"desde {origen} la opción promete el estado {op['stat_destino']!r} "
            "y el cheque no se mueve de donde está"
        )
        assert "Nueva fecha" in queries.texto_opcion_estado(op)


def test_postergar_desde_cartera_si_lleva_a_P():
    """Desde Z el cheque SÍ pasa a postergado: ahí la 'P' es verdad."""
    posterg = [t for t in queries.transiciones_para("Z") if t["kind"] == "POSTERGAR"]
    assert posterg and posterg[0]["stat_destino"] == "P"
    assert "Postergar" in queries.texto_opcion_estado(posterg[0])
