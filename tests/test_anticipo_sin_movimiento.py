"""Un anticipo puede entrar SIN mover banco ni caja.

TMT 2026-08-06 (dueña), a raíz de BED: *"es porque paga algo que nosotros no
tenemos en el sistema"*. El cliente paga de más — retenciones, redondeos, cosas
que no facturamos — y esa plata hay que dejarla como saldo a favor sin poder
decir por dónde entró.

Hasta hoy el select "Medio del anticipo" era `required` y sus únicas opciones
eran EFECTIVO (99), DEP.PICH (90), DEP.INTER (91) o un banco emisor. O sea:
para cargar el ajuste había que **elegir una caja o un banco**, inventando un
movimiento que no existe. El 90/91 lo levanta después la conciliación como
pendiente sin matchear — el mismo fantasma que costó limpiar la sesión #61 el
06/08. El 99 deja la caja del día descuadrada.

El arreglo NO afloja el `required` (un select en blanco es un dato comido en
silencio): agrega una opción EXPLÍCITA `97 · SIN MOVIMIENTO (ajuste)` que deja
el `no_banco` en 97. Y 97 no genera movimiento de banco — eso lo hacen sólo 90
y 91 (`queries::_genera_mov_banco`).

⚠️ El invariante que este archivo protege es el de abajo, no el de arriba:
**medio "sin movimiento" NO puede terminar en un banco real.** Si alguien
"simplifica" el `if` a `return medio or no_banco`, el 97 sigue funcionando y
nada se rompe — hasta que alguien elija 97 y el cobro caiga en un banco tratado
como real. Por eso se testea la función, no sólo el template.
"""
from __future__ import annotations

import datetime as _dt
import os
import sys

import pytest

# El sandbox puede correr Python 3.10, que no trae `datetime.UTC`.
if not hasattr(_dt, "UTC"):  # pragma: no cover - depende del intérprete
    _dt.UTC = _dt.timezone.utc  # noqa: UP017

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from modules.cheques.views import no_banco_efectivo  # noqa: E402

RUTA_HTML = os.path.join(
    _REPO_ROOT, "modules", "cheques", "templates", "cheques", "nuevo.html"
)

# Los únicos dos que generan movimiento de banco (queries::_genera_mov_banco).
BANCOS_QUE_MUEVEN_PLATA = (90, 91)


def _html() -> str:
    with open(RUTA_HTML, encoding="utf-8") as fh:
        return fh.read()


# ---------------------------------------------------------------------------
# El corazon: el ajuste no puede tocar banco
# ---------------------------------------------------------------------------

def test_medio_sin_movimiento_deja_el_banco_en_97():
    """El caso BED: entro plata y no sabemos por donde."""
    assert no_banco_efectivo(97, 97) == 97


def test_medio_vacio_tambien_deja_el_banco_en_97():
    """Compat: antes del cambio un medio en blanco ya caia aca."""
    assert no_banco_efectivo(97, None) == 97


@pytest.mark.parametrize("medio", [97, None, 0])
def test_el_ajuste_nunca_cae_en_un_banco_que_mueve_plata(medio):
    assert no_banco_efectivo(97, medio) not in BANCOS_QUE_MUEVEN_PLATA


# ---------------------------------------------------------------------------
# Lo que ya andaba tiene que seguir andando
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("medio", [90, 91, 99, 10, 32, 35])
def test_un_medio_real_sigue_mandando(medio):
    assert no_banco_efectivo(97, medio) == medio


@pytest.mark.parametrize("no_banco", [10, 32, 90, 91, 95, 99, 98, None])
def test_si_no_es_anticipo_el_medio_no_toca_nada(no_banco):
    """El medio solo existe para el 97: en cualquier otro cobro se ignora."""
    assert no_banco_efectivo(no_banco, 90) == no_banco
    assert no_banco_efectivo(no_banco, None) == no_banco


# ---------------------------------------------------------------------------
# La pantalla
# ---------------------------------------------------------------------------

def test_la_opcion_sin_movimiento_esta_en_el_select():
    assert '<option value="97">97 · SIN MOVIMIENTO (ajuste)</option>' in _html()


def test_el_medio_sigue_siendo_obligatorio():
    """No se afloja el `required`: se agrega una opcion explicita.

    Dejarlo opcional convierte "no elegi" y "no hubo movimiento" en el mismo
    dato, y el primero es un descuido.
    """
    assert "medioSel.required = esAnticipo97" in _html()


def test_la_pantalla_explica_que_no_toca_banco_ni_caja():
    assert "no toca banco ni caja" in _html()
