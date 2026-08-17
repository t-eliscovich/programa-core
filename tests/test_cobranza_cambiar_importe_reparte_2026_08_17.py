"""Cambiar el importe del cheque reparte la plata nueva, y avisa qué agregó.

Salió de una auditoría con 20 casos borde sobre la pantalla real (17/08/2026).
Es el mismo mecanismo que los dos bugs anteriores del día, con otro disparador:

    tildo una factura a mano   -> Aplicado   976,20 · cheque 1.000
    subo el cheque a 3.000     -> Aplicado   976,20 · Restante 2.023,80

`manualValues` mezclaba dos cosas que parecen iguales:

* lo que la dueña **decidió** — tildó, destildó, tipeó, Max, TODAS/NINGUNA;
* lo que se fijó **de rebote** al tocar otra fila (el congelado del 07/07).

Con todo marcado como decidido, al entrar plata nueva no queda ninguna fila
libre y la diferencia muere como "Restante". Ahora las de rebote se anotan en
`congeladas` y se sueltan cuando cambia la plata; las decididas no se tocan.

Decisión de la dueña: repartir **avisando** — se pinta un cartel amarillo
diciendo cuántas filas se agregaron y por cuánto, "para que no aparezcan por
sorpresa".

Medido después del arreglo, sobre AJ2:

* subir 1.000 → 3.000 con una fila tildada: Aplicado 3.000, Restante 0, y el
  aviso *"Se repartió la diferencia: 5 filas nuevas por $ 2.023,80 (10674,
  175020, 10689, 10697, 175136)"*;
* un monto tipeado a mano (123,45) sigue valiendo 123,45 después de subir;
* una fila destildada a mano NO vuelve a marcarse;
* sin ningún toque manual el aviso no aparece.
"""
from __future__ import annotations

from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
NUEVO = (RAIZ / "modules/cheques/templates/cheques/nuevo.html").read_text(encoding="utf-8")


def test_existe_el_registro_de_congeladas():
    assert "const congeladas = new Set();" in NUEVO


def test_solo_el_congelado_de_rebote_marca():
    """La marca se pone en el congelado masivo, que es el único de rebote."""
    i = NUEVO.index("if (manualValues[oid] === undefined) {")
    j = NUEVO.index("}", NUEVO.index("congeladas.add", i))
    assert "congeladas.add(String(oid));" in NUEVO[i:j + 1]


def test_cada_decision_del_usuario_desmarca():
    """Tildar, destildar, tipear y Max sacan la fila de `congeladas`.

    Si alguna se olvidara, esa fila se soltaría al cambiar el importe y la
    decisión de la dueña se perdería sin que nada lo avise.
    """
    assert NUEVO.count("congeladas.delete(String(id));") == 5


def test_los_masivos_limpian_todo():
    """TODAS, NINGUNA, el tick del header, el rango y la H deciden sobre todo."""
    assert NUEVO.count("congeladas.clear();") >= 7  # 6 masivos + el suelto al refrescar


def test_cambiar_la_plata_suelta_las_de_rebote():
    i = NUEVO.index("function refrescarPorCambioDeCheques()")
    j = NUEVO.index("renderAnticipoCheques();", i)
    cuerpo = NUEVO[i:j]
    assert "congeladas.forEach(id => { delete manualValues[id]; });" in cuerpo
    assert "congeladas.clear();" in cuerpo
    # y se suelta ANTES de re-renderizar, o el render no vería las filas libres
    assert cuerpo.index("congeladas.clear();") < cuerpo.index("render(buscar.value)")


def test_avisa_lo_que_agrego():
    assert 'id="aviso-reparto"' in NUEVO
    assert "function _avisarReparto(antes, despues)" in NUEVO
    i = NUEVO.index("function _avisarReparto")
    j = NUEVO.index("function refrescarPorCambioDeCheques", i)
    cuerpo = NUEVO[i:j]
    assert "Se repartió la diferencia" in cuerpo
    assert "Lo que habías marcado a mano quedó como estaba." in cuerpo
    # sin filas nuevas, el cartel se esconde (no queda un aviso viejo colgado)
    assert "box.style.display = 'none'; box.textContent = ''; return;" in cuerpo


def test_el_aviso_usa_el_numero_visible_de_la_factura():
    """La dueña nombra las facturas por su número, no por el id interno."""
    i = NUEVO.index("function _avisarReparto")
    j = NUEVO.index("function refrescarPorCambioDeCheques", i)
    assert "shortNum(f)" in NUEVO[i:j]
