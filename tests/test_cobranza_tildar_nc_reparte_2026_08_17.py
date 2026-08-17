"""Tildar una nota de crédito reparte lo que libera, no lo deja como Restante.

Alex, 17/08/2026, sobre un cheque de $10.700 de AJ2:

    "hasta que no aplico y desaplico facturas no se empezaba a contar esa nota
     de crédito por -172"

Reproducido con jsdom (AJ2, cheque $4.000, una NC de 171,03 al fondo):

    reparto automático  -> Aplicado 4.000,00 · Restante 0,00 · 13 filas
    tildo la NC         -> Aplicado 3.828,97 · Restante 171,03 · 14 filas

Los 171,03 que la NC devuelve no llegaban a ninguna factura: quedaban como
Restante. Por eso había que zamarrear la pantalla tildando y destildando hasta
que "se empezara a contar".

## La causa, que era una buena idea con un borde malo

El handler del checkbox congela TODAS las filas en `manualValues` antes de
aplicar el cambio. Se puso el 07/07 por un motivo válido: *"deseleccioné una y
se marcó la siguiente"* — al destildar, la plata liberada volvía al pool y el
re-FIFO la usaba para marcar sola la factura de abajo.

Pero congelaba igual al TILDAR. Y con todas las filas marcadas como "editadas a
mano" no queda ninguna libre para redistribuir, así que el excedente de la NC
moría ahí. El propio código dice lo contrario dos comentarios más abajo
(TMT 2026-07-02, de Alex): *"tildo una NC → la plata liberada cubre más
facturas"*.

Arreglo: congelar sólo cuando la fila que se toca es una **factura**. Una nota
de crédito no le saca plata a nadie — la devuelve.

Este es también el origen del *"Restante $31,42"* que la dueña había reportado
antes en AJ2: la factura parcial quedaba clavada en 1.349,64 en lugar de subir
a sus 1.381,06, y 1.381,06 − 1.349,64 = 31,42.

## Lo que NO cambia

Destildar una factura sigue congelando, así que la regla del 07/07 se mantiene:
verificado con jsdom que no se marca sola ninguna fila nueva, idéntico antes y
después del arreglo.
"""
from __future__ import annotations

from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
NUEVO = (RAIZ / "modules/cheques/templates/cheques/nuevo.html").read_text(encoding="utf-8")


def _handler() -> str:
    """El listener de `change` del checkbox de cada fila."""
    i = NUEVO.index("tbody.querySelectorAll('input.aplic-cancel').forEach(chk => {")
    j = NUEVO.index("const chks = tbody.querySelectorAll('input.aplic-cancel');", i)
    return NUEVO[i:j]


def test_el_congelado_es_condicional():
    h = _handler()
    assert "const _esCreditoLaFila = saldo < -0.005;" in h
    assert "if (!_esCreditoLaFila) {" in h


def test_el_congelado_sigue_existiendo_para_las_facturas():
    """La regla del 07/07 no se toca: destildar una factura no marca a nadie."""
    h = _handler()
    assert "if (manualValues[oid] === undefined) {" in h
    assert "manualValues[oid] = parseMonto(o.value) || 0;" in h
    # y sigue estando ADENTRO del if, no suelto
    i = h.index("if (!_esCreditoLaFila) {")
    j = h.index("manualValues[oid] = parseMonto(o.value) || 0;")
    assert i < j


def test_la_nc_tildada_entra_con_su_saldo_entero():
    """`toApply = saldo` para negativos: una NC nunca se recorta al disponible."""
    h = _handler()
    assert "const toApply = saldo > 0 ? Math.max(0, Math.min(saldo, disponible)) : saldo;" in h


def test_sigue_redistribuyendo_al_final():
    h = _handler()
    assert "render(buscar.value);" in h


def test_el_umbral_es_el_mismo_que_usa_el_resto_de_la_pantalla():
    """0,005 = medio centavo, el corte que usa toda la pantalla para 'es cero'."""
    h = _handler()
    assert "saldo < -0.005" in h
