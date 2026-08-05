"""Las dos fechas del cheque no se pisan entre sí (TMT 2026-08-05).

`scintela.cheque` tiene `fechaing` (día que el cheque ENTRÓ a cartera —
FECHING del dBase, `ALTAS.PRG` L30) y `fechaout` (día que SALIÓ — FECHOUT,
`BANCOS.PRG` L1234). Durante meses el depósito de PC escribió la fecha de
SALIDA dentro de `fechaing`, borrando el día de ingreso de las filas del
dBase. El resumen de cobranza agrupa por día de ingreso → esos cheques
aparecían como cobranza del día del depósito: la hoja del 04/08 que va a
contabilidad salió con 46 fantasmas por $74.165,81.

Estos tests leen el FUENTE de las rutas (no hay Postgres en CI) y fijan las
dos reglas:

  · toda ruta que SACA un cheque de cartera escribe `fechaout`;
  · toda ruta que lo DEVUELVE a cartera limpia `fechaout` y NO toca `fechaing`
    (borrarlo dejaría a la fila del dBase sin día de ingreso para siempre).
"""
from __future__ import annotations

import inspect
import re


def _sql_normalizado(fn) -> str:
    return " ".join(inspect.getsource(fn).split()).lower()


def test_transicion_a_depositado_escribe_fechaout():
    from modules.cheques import queries as q
    src = _sql_normalizado(q.transicionar_stat)
    assert "set stat=%s, fechaout=%s, no_banco=%s" in src, (
        "la transición Z→B tiene que escribir la fecha en fechaout"
    )


def test_ninguna_ruta_de_deposito_escribe_fechaing():
    """Ni depositar_lote ni la transición individual pueden tocar fechaing."""
    from modules.cheques import queries as q
    for fn in (q.depositar_lote, q.transicionar_stat):
        src = _sql_normalizado(fn)
        # `fechaing` puede aparecer en un comentario; lo que no puede aparecer
        # es una asignación.
        assert not re.search(r"fechaing\s*=\s*%s", src), (
            f"{fn.__name__} está escribiendo en fechaing"
        )


def test_volver_a_cartera_limpia_fechaout_y_preserva_fechaing():
    """El deshacer no puede borrar el día de ingreso del dBase."""
    from modules.cheques import queries as q
    src = _sql_normalizado(q.deshacer_deposito_cheque)
    assert "fechaout = null" in src
    assert "fechaing = null" not in src, (
        "borrar fechaing deja a la fila del dBase sin día de ingreso"
    )


def test_el_dia_de_salida_es_una_sola_definicion_compartida():
    """informes IMPORTA la expresión, no la copia (regla de la casa)."""
    from modules.cheques.queries import SQL_DIA_SALIDA
    from modules.informes.queries import _SQL_DIA_SALIDA_CHEQUE
    assert _SQL_DIA_SALIDA_CHEQUE is SQL_DIA_SALIDA
    # fechaout PRIMERO: es la columna correcta en los dos orígenes.
    assert SQL_DIA_SALIDA.replace(" ", "") == "COALESCE(c.fechaout,c.fechaing)"


def test_balance_as_of_es_aditivo_para_no_mover_historico():
    """/balance?as_of suma la condición nueva, no reemplaza la vieja.

    Con COALESCE(fechaout, fechaing) las filas que tienen las DOS cargadas
    cambiarían de lado y se moverían números históricos (medido: −4.468,63 al
    31/07). Con el OR, lo ya impreso da idéntico y los depósitos nuevos —que
    sólo tendrán fechaout— también cuentan.
    """
    from modules.informes import views
    src = " ".join(inspect.getsource(views._asof_dia_overrides).split()).lower()
    assert "(fechaing is not null and fechaing > %s) or (fechaout is not null and fechaout > %s)" in src
    assert "coalesce(fechaout, fechaing) >" not in src
