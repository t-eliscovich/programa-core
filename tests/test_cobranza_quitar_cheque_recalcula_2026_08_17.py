"""Sacar un cheque de la cobranza tiene que rehacer el reparto a facturas.

Dueña 17/08/2026, mirando /cheques/nuevo:

    "cuando elimino un cheque del proceso de carga, se me queda igual el
     monto. Había cargado uno de 200, después borré y en la parte de abajo
     sigue diciendo aplicado 200."

El "× quitar" hacía `b.remove()` y después sólo renumeraba, mostraba/ocultaba
bloques y refrescaba el panel de anticipos. Le faltaban las DOS cosas que sí
hace el listener de `input` sobre `.cheque-imp`: volver a repartir el pool
entre las facturas (`render`) y recalcular el pie Total/Aplicado/Restante
(`updateSummary`, que corre al final de `render`).

Resultado: arriba el total decía $100 (ese sí se actualizaba, vía
`actualizarMultiHint` → `actualizarTotalCheques`) y abajo seguía diciendo
"Total cheques $200 · Aplicado $200 · Restante $0". Dos números de la misma
plata contradiciéndose en la misma pantalla, y el que manda al guardar es el
de abajo.

El fix junta las tres llamadas en `refrescarPorCambioDeCheques()`, que ahora
comparten el listener de `input` y el "× quitar" — para que no se puedan
volver a desincronizar.
"""
from __future__ import annotations

import re
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
NUEVO = (RAIZ / "modules/cheques/templates/cheques/nuevo.html").read_text(encoding="utf-8")


def _cuerpo(nombre: str) -> str:
    """Devuelve el cuerpo de una función JS declarada con `function nombre(`."""
    i = NUEVO.index(f"function {nombre}(")
    j = NUEVO.index("{", i)
    prof = 0
    for k in range(j, len(NUEVO)):
        if NUEVO[k] == "{":
            prof += 1
        elif NUEVO[k] == "}":
            prof -= 1
            if prof == 0:
                return NUEVO[j : k + 1]
    raise AssertionError(f"no encontré el cierre de {nombre}")


def test_existe_la_funcion_compartida():
    assert "function refrescarPorCambioDeCheques()" in NUEVO


def test_refrescar_hace_las_tres_cosas():
    cuerpo = _cuerpo("refrescarPorCambioDeCheques")
    assert "actualizarTotalCheques()" in cuerpo, "falta el total de arriba"
    assert "render(buscar.value)" in cuerpo, "falta rehacer el reparto a facturas"
    assert "updateSummary()" in cuerpo, "sin facturas cargadas el pie igual tiene que refrescar"
    assert "renderAnticipoCheques()" in cuerpo, "falta el panel de cheques en cartera"


def test_quitar_un_bloque_refresca():
    """El handler del '× quitar' llama a la función compartida."""
    i = NUEVO.index("btnRemove.addEventListener('click'")
    j = NUEVO.index("});", i)
    handler = NUEVO[i:j]
    assert "b.remove()" in handler
    assert "refrescarPorCambioDeCheques()" in handler, (
        "quitar un cheque tiene que rehacer el reparto, no sólo renumerar"
    )


def test_el_listener_de_importe_usa_la_misma_funcion():
    """Una sola definición de 'cambió la plata': si se toca, se toca para los dos.

    Si el listener de `input` volviera a tener su propia copia de las tres
    llamadas, el bug puede reaparecer sólo en el '× quitar' — que es
    exactamente lo que pasó.
    """
    i = NUEVO.index("chequesList.addEventListener('input'")
    j = NUEVO.index("});", i)
    listener = NUEVO[i:j]
    assert "refrescarPorCambioDeCheques()" in listener
    assert "actualizarTotalCheques()" not in listener, "no duplicar las llamadas"


def test_importe_input_se_reapunta_si_se_borra_su_bloque():
    """`importeInput` guarda UN nodo; si se borra ese bloque queda colgando.

    Con la referencia muerta, el autocompletado del importe desde las
    facturas escribe en un input que ya no está en la pantalla y no se ve
    nada. Por eso es `let` y se reapunta al bloque que sobrevive.
    """
    assert re.search(r"let importeInput\s*=\s*document\.querySelector\('input\.cheque-imp'\)", NUEVO)
    cuerpo = _cuerpo("refrescarPorCambioDeCheques")
    assert "importeInput.isConnected" in cuerpo
