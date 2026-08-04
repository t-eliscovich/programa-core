"""`fechad` = el día en que la plata entró al banco, NO el día que se tipeó.

TMT 2026-08-04 (tarde). `modules/comisiones/queries.py` agrupa la cobranza por
`cheque.fechad`: ese campo decide **en qué mes se le paga la comisión al
vendedor**. El alta lo forzaba a `today_ec()` para todo banco de depósito
(90 DEP.PICH · 91 DEP.INTER · 95 CANCELA ANT · 97 ANTICIPO · 99 EFECTIVO),
siguiendo la regla de la dueña *"si es un depósito, obligatoriamente fecha de
hoy"*.

Esa regla describe la cobranza DEL DÍA — el 99 % de las cargas, donde hoy y el
día de cobranza son la misma fecha. **Pisa la fecha en una carga RETROACTIVA**:
medido en vivo cargando el cobro de ECH, cobranza del 21/07 → `fechad` 04/08,
o sea plata de julio contada en agosto. Y no había forma de corregirlo en
pantalla: para esos bancos el campo "A depositar" se oculta.

Cerrar julio costó un día entero de reconciliación contra el dBase. Este
archivo es el candado para que no vuelva a abrirse.
"""
from __future__ import annotations

import os
import re
import sys
from datetime import date

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from modules.cheques.views import fechad_por_defecto  # noqa: E402

COBRANZA = date(2026, 7, 21)   # el día que el cliente pagó
HOY = date(2026, 8, 4)         # el día que se está cargando


# ---------------------------------------------------------------------------
# ⭐ El caso ECH — el que rompía la comisión
# ---------------------------------------------------------------------------

def test_deposito_retroactivo_guarda_el_dia_de_la_cobranza():
    assert fechad_por_defecto(
        es_deposito=True, stat="B", fechad_tipeada=None, fecha_cobranza=COBRANZA,
    ) == COBRANZA


def test_deposito_retroactivo_ignora_el_HOY_que_manda_el_navegador():
    """⭐ El corazón del arreglo.

    El campo "A depositar" está OCULTO para bancos de depósito y el JS de
    `cheques/nuevo.html` le escribe la fecha de hoy antes de mandar el POST.
    O sea: **siempre llega un `fechad` tipeado, y siempre es HOY**. Un
    `fechad_tipeada or fecha_cobranza` compila, parece prudente, y deja el
    bug exactamente igual de vivo — porque el `or` nunca llega a la derecha.
    """
    assert fechad_por_defecto(
        es_deposito=True, stat="B", fechad_tipeada=HOY, fecha_cobranza=COBRANZA,
    ) == COBRANZA


def test_la_cobranza_del_dia_no_cambia_de_comportamiento():
    """El 99 % de las cargas: hoy y el día de cobranza son la misma fecha."""
    assert fechad_por_defecto(
        es_deposito=True, stat="B", fechad_tipeada=HOY, fecha_cobranza=HOY,
    ) == HOY


@pytest.mark.parametrize("stat", ["B", "Z", "P", "", None, "A", "C"])
def test_para_deposito_el_estado_no_cambia_la_fecha(stat):
    """Incluso 'P': un depósito ya ocurrió, no hay nada que postdatar."""
    assert fechad_por_defecto(
        es_deposito=True, stat=stat, fechad_tipeada=HOY, fecha_cobranza=COBRANZA,
    ) == COBRANZA


# ---------------------------------------------------------------------------
# Cheque de verdad (banco emisor real) — el campo SÍ se ve y manda el usuario
# ---------------------------------------------------------------------------

def test_cheque_normal_respeta_la_fecha_tipeada():
    """Acá el campo está visible: lo que puso el usuario es una decisión."""
    a_depositar = date(2026, 8, 15)
    assert fechad_por_defecto(
        es_deposito=False, stat="Z", fechad_tipeada=a_depositar, fecha_cobranza=COBRANZA,
    ) == a_depositar


def test_cheque_normal_sin_fecha_colapsa_al_dia_de_cobranza():
    assert fechad_por_defecto(
        es_deposito=False, stat="Z", fechad_tipeada=None, fecha_cobranza=COBRANZA,
    ) == COBRANZA


def test_postdatado_sin_fecha_devuelve_None_para_que_la_validacion_lo_frene():
    """⭐ `P` es el único caso donde `fechad` NO es derivable.

    Un postdatado se deposita el día que dice el cheque, dato que sólo tiene
    quien lo tiene en la mano. Rellenarlo con la fecha de cobranza lo haría
    depositable hoy — y el banco lo rebota. `None` es a propósito: la
    validación de más abajo en `nuevo()` corta el alta.
    """
    assert fechad_por_defecto(
        es_deposito=False, stat="P", fechad_tipeada=None, fecha_cobranza=COBRANZA,
    ) is None


def test_postdatado_con_fecha_la_respeta():
    futura = date(2026, 10, 30)
    assert fechad_por_defecto(
        es_deposito=False, stat="P", fechad_tipeada=futura, fecha_cobranza=COBRANZA,
    ) == futura


@pytest.mark.parametrize("stat", ["p", " P", "p "])
def test_el_postdatado_se_reconoce_aunque_venga_sucio(stat):
    """Viene de un `<select>` pero también de restore-on-error y del CSV."""
    assert fechad_por_defecto(
        es_deposito=False, stat=stat, fechad_tipeada=None, fecha_cobranza=COBRANZA,
    ) is None


@pytest.mark.parametrize("stat", ["", None])
def test_sin_estado_no_explota(stat):
    assert fechad_por_defecto(
        es_deposito=False, stat=stat, fechad_tipeada=None, fecha_cobranza=COBRANZA,
    ) == COBRANZA


# ---------------------------------------------------------------------------
# Que el view use la función y no vuelva a tener su copia
# ---------------------------------------------------------------------------

def _fuente_nuevo() -> str:
    import inspect

    from modules.cheques import views

    return inspect.getsource(views.nuevo)


def test_el_alta_no_vuelve_a_forzar_today_en_el_fechad():
    """⭐ La regresión exacta: `cheque_fechad = today_ec()`."""
    src = _fuente_nuevo()
    assert not re.search(r"cheque_fechad\s*=\s*today_ec\(\)", src), (
        "volvió el `fechad = hoy`: una cobranza retroactiva vuelve a caer en "
        "el mes equivocado y la comisión del vendedor se paga mal"
    )


def test_el_alta_delega_en_la_funcion():
    assert "fechad_por_defecto(" in _fuente_nuevo()


def test_la_regla_es_una_sola_para_las_dos_puertas():
    """`desde_caja` (adoptar una fila de caja) tenía su propia rama.

    Era este mismo bug parchado para UNA de las puertas. Si vuelve a
    bifurcarse, la puerta que quede sin parchar es la que va a mentir.
    """
    src = _fuente_nuevo()
    assert "es_deposito and desde_caja" not in src, (
        "la fecha del depósito volvió a decidirse distinto según la puerta"
    )


def test_la_funcion_es_pura():
    """Sin `today_ec` adentro: si mira el reloj, deja de ser testeable."""
    import inspect

    from modules.cheques import views

    src = inspect.getsource(views.fechad_por_defecto)
    cuerpo = src[src.index('"""', src.index('"""') + 3) + 3 :]  # sin el docstring
    assert "today_ec" not in cuerpo
    assert "date.today" not in cuerpo
    assert "datetime" not in cuerpo


# ---------------------------------------------------------------------------
# El JS que llena el campo oculto
# ---------------------------------------------------------------------------

def _plantilla_nuevo() -> str:
    ruta = os.path.join(
        _REPO_ROOT, "modules", "cheques", "templates", "cheques", "nuevo.html"
    )
    with open(ruta, encoding="utf-8") as fh:
        return fh.read()


def test_el_campo_oculto_se_llena_con_la_fecha_de_cobranza_y_no_con_hoy():
    """Defensa en profundidad: el backend ya lo ignora, pero el campo no
    puede mostrar una fecha falsa — se ve al re-abrir el cheque."""
    html = _plantilla_nuevo()
    assert "fechadInp.value = cobranzaISO()" in html
    assert "fechadInp.value = HOY_ISO" not in html


def test_cobranzaISO_lee_el_campo_de_fecha_de_la_cobranza():
    html = _plantilla_nuevo()
    i = html.index("function cobranzaISO()")
    cuerpo = html[i : i + 500]
    assert 'name="fecha_recibido"' in cuerpo
    # DD/MM/AAAA → AAAA-MM-DD, que es lo que espera <input type="date">
    assert "m[3] + '-' + m[2] + '-' + m[1]" in cuerpo
    assert "HOY_ISO" in cuerpo, "sin fallback, un campo a medio tipear rompe"
