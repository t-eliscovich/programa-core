"""Derivar el signo de la cadena reproduce el saldo EXACTO. TMT 2026-08-03.

⭐ EL PROBLEMA DE FONDO. El saldo corrido está GUARDADO y se usa como fuente
de verdad, cuando es un valor DERIVADO. Y no se puede re-derivar porque
`transacciones_bancarias.importe` **no lleva su signo**: hay que adivinarlo
desde el `documento` (`bank_helpers._signed_delta`), con reglas que cambiaron
entre el dBase y la web.

De ahí sale todo: un recompute total es peligroso (el dry-run del 29/06 daba
−472.943), un solo insert backdated corrompe historia, y quedan 7 filas de
junio-julio que no se arreglan sin el extracto.

El arreglo es que el importe lleve signo y el saldo salga de un `SUM()`: sin
cadena no hay cadena que romper, y **una suma no depende del orden**, así que
un insert backdated deja de poder corromper nada.

La premisa que lo hace seguro es puramente algebraica:

    firmado[0] = saldo[0]
    firmado[n] = saldo[n] − saldo[n−1]
    ⇒ SUM(firmado[0..k]) = saldo[k]     (telescópica)

Estos tests clavan esa identidad — incluida la parte incómoda: que también
vale **con la cadena rota**, porque el quiebre queda absorbido en el firmado
de esa fila. Por eso migrar no mueve nada aunque los 7 quiebres sigan ahí.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules.admin_dbase.health_audit_view import _derivar_cadena  # noqa: E402


def _f(id_, imp, saldo, doc="DE"):
    return {"id_transaccion": id_, "fecha": "2026-07-01", "documento": doc,
            "concepto": "x", "importe": imp, "saldo": saldo}


def test_la_suma_reproduce_el_saldo_exacto():
    filas = [_f(1, 1000, 1000), _f(2, 200, 800), _f(3, 500, 1300)]
    d = _derivar_cadena(filas)
    assert d["suma_derivada"] == 1300.0
    assert d["n_derivables"] == 2
    assert d["n_quiebres"] == 0


def test_vale_IGUAL_con_la_cadena_rota():
    """La clave: el quiebre queda absorbido en el firmado de esa fila.

    Por eso migrar no mueve ningún número aunque los 7 quiebres de
    junio-julio sigan ahí — no hace falta arreglarlos primero.
    """
    filas = [_f(1, 1000, 1000), _f(2, 1000, 2963.34), _f(3, 702.27, 3665.61)]
    d = _derivar_cadena(filas)
    assert d["suma_derivada"] == 3665.61, "la suma tiene que dar el saldo final"
    assert d["n_quiebres"] == 1
    q = d["quiebres"][0]
    assert q["id_transaccion"] == 2
    assert q["firmado_derivado"] == 1963.34      # el salto, tal cual está
    assert q["gap"] == 963.34


def test_el_quiebre_sale_gratis_del_mismo_calculo():
    """`|firmado| ≠ |importe|` ES la definición de quiebre. No hay que
    buscarlos aparte: caen del mismo pase que deriva los signos."""
    filas = [_f(1, 100, 100), _f(2, 50, 150), _f(3, 30, 999)]
    d = _derivar_cadena(filas)
    assert [q["id_transaccion"] for q in d["quiebres"]] == [3]
    assert d["n_derivables"] == 1


def test_los_debitos_quedan_con_signo_negativo():
    """El signo se LEE de la cadena, no se adivina del documento."""
    filas = [_f(1, 1000, 1000), _f(2, 300, 700, doc="ND")]
    d = _derivar_cadena(filas)
    assert d["n_quiebres"] == 0
    assert d["suma_derivada"] == 700.0


def test_las_filas_con_saldo_NULL_no_entran():
    """Sin saldo no hay de dónde derivar — no se inventa."""
    filas = [_f(1, 1000, 1000), _f(2, 200, None), _f(3, 500, 1500)]
    d = _derivar_cadena(filas)
    assert d["n_filas"] == 2
    assert d["suma_derivada"] == 1500.0


def test_banco_vacio_no_explota():
    d = _derivar_cadena([])
    assert d["suma_derivada"] == 0.0 and d["n_quiebres"] == 0


def test_el_dry_run_NO_escribe():
    """Es una pantalla de lectura: ni un INSERT/UPDATE/DELETE en el módulo."""
    import inspect

    from modules.admin_dbase.health_audit_view import saldo_derivado
    src = inspect.getsource(saldo_derivado) + inspect.getsource(_derivar_cadena)
    for verbo in ("INSERT", "UPDATE ", "DELETE", "ALTER", "db.execute("):
        assert verbo not in src, f"el dry-run de saldo derivado hace {verbo}"
