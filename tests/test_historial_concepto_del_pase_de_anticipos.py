"""El pase de anticipos a compra dice de QUÉ importación salió.

🚨 TMT 2026-08-14, mirando la fila del 14/08 que decía "Hilado · fact. 38":
*"acá quiero que diga AC y el número"*. Es el tercer round del mismo pedido —
el 31/07 fue *"acá nada dice AC 22, ¿cómo sé qué anticipo es?"*— y esta vez el
nombre no faltaba: estaba escrito en el `mov_doble` y el historial lo pisaba.

La regla que lo pisaba nació el 09/08 (*"no puede ser ese número lo importante
del movimiento"*, sobre "22758         7"): toda fila que toca una compra pasa
a decir QUÉ se compró. Buena para las 15.000 filas de compras, mala para las
pocas del pase, que son las únicas cuyo concepto propio dice algo mejor.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules.historial.views import concepto_de_la_compra  # noqa: E402

#: Las dos filas reales del 14/08, tal como salen de `queries.listar`.
_PASE = {"tipo": "bap_anticipo_a_compra",
         "origen_table": "dolares", "origen_id": 901,
         "destino_table": "compra", "destino_id": 473,
         "concepto": "AC 38 · 4 anticipo(s) → compra N° 10176"}
_COMPRA = {"tipo": "compra_saldo_a_posdat",
           "origen_table": "compra", "origen_id": 473,
           "destino_table": "posdat", "destino_id": 12,
           "concepto": "22758         7"}
_CONCEPTOS = {473: "Hilado · fact. 38"}


def test_el_pase_se_queda_con_su_concepto():
    assert concepto_de_la_compra(_PASE, _CONCEPTOS) == ""


def test_el_reverso_del_pase_tambien():
    fila = {**_PASE, "tipo": "bap_anticipo_a_compra_reverso"}
    assert concepto_de_la_compra(fila, _CONCEPTOS) == ""


def test_la_compra_comun_sigue_diciendo_que_se_compro():
    """La regla del 09/08 no se toca: es la que sacó el "22758         7"."""
    assert concepto_de_la_compra(_COMPRA, _CONCEPTOS) == "Hilado · fact. 38"


def test_sin_compra_del_lado_no_hay_nada_que_pisar():
    fila = {"tipo": "cheque_depositado", "origen_table": "cheque",
            "origen_id": 5, "destino_table": "banco", "destino_id": 1}
    assert concepto_de_la_compra(fila, _CONCEPTOS) == ""


def test_una_compra_que_no_esta_en_el_mapa_no_borra_el_concepto():
    """Devuelve "" y la fila conserva lo que ya tenía: pisar con vacío dejaría
    la columna muda."""
    fila = {**_COMPRA, "origen_id": 999}
    assert concepto_de_la_compra(fila, _CONCEPTOS) == ""
