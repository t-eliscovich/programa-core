"""El aporte de socio no se llama "retiro", y no le regala el Δ del banco a otro.

🚨 TMT 2026-08-20, mirando la ventana de las 12:24 en /informes/traza:
*"creo que hay 128k de más"*. No había: el aporte de 128.625 de LAFER se cargó,
se reversó y se volvió a cargar como UNITECH. Lo que había eran dos renglones
que mentían.

1. **La palabra y la flecha, las dos al revés.** `capital.retirar()` usa el
   MISMO tipo (`retiro_socio_de_pichincha`) para el retiro y para el aporte —
   un retiro en NEGATIVO es un aporte, paridad dBase—, así que el renglón decía
   "Retiro socio ← Pichincha" para plata que ENTRÓ a Pichincha.

2. **El Δ del banco colgado del hecho equivocado.** En la misma ventana el
   banco 10 tuvo DOS hechos: el depósito del aporte (+128.625) y un cheque de
   25.651,68 contra la OP. `capital.retirar()` guarda `metadata` SIN
   `no_banco`, así que el aporte era invisible para `_cuentas()`, la cuenta
   parecía tener un solo hecho y el renglón decía "Cheque emitido → otro ·
   102.973" — un cheque que fue de 25.651,68.
"""
from __future__ import annotations

import os
import sys
from unittest.mock import patch

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from modules.historial import queries as hq  # noqa: E402
from modules.informes import eventos as ev  # noqa: E402
from modules.informes import traza as t  # noqa: E402

#: Los dos hechos reales de la ventana de las 12:24 del 20/08/2026.
_APORTE = {"id_mov_doble": 26707, "batch_id": None,
           "tipo": "retiro_socio_de_pichincha", "metadata": {"socio": "RR"},
           "origen_table": "retiros", "origen_id": 1129,
           "destino_table": "transacciones_bancarias", "destino_id": 46123,
           "importe": -128625.0, "concepto": "LAFER", "usuario": "andres",
           "estado": "activo", "dia": "2026-08-20"}
_CHEQUE = {"id_mov_doble": 26706, "batch_id": None,
           "tipo": "cheque_emitido_otro", "metadata": {"no_banco": 10},
           "origen_table": "transacciones_bancarias", "origen_id": 46122,
           "destino_table": "transacciones_bancarias", "destino_id": 46122,
           "importe": 25651.68, "concepto": "IN OP AI 2773/2784/2795",
           "usuario": "andres", "estado": "activo", "dia": "2026-08-20"}


#: El `no_banco` que la metadata del aporte no guardó, como lo devuelve la
#: tabla de transacciones.
_TX = [{"id_transaccion": 46123, "no_banco": 10}]


def _base(filas, tx=_TX):
    """La base de mentira: los `mov_doble` de la ventana y el puente al banco."""
    def _fetch(sql, *a, **kw):
        return list(tx) if "transacciones_bancarias" in sql else list(filas)
    return patch.object(ev.db, "fetch_all", side_effect=_fetch)


def _con_label(filas):
    with _base(filas):
        return ev.de_la_ventana("2026-08-20 17:18+00", "2026-08-20 17:24+00")


# ── 1. El nombre lo decide el signo ────────────────────────────────────────

def test_el_retiro_negativo_se_llama_aporte_y_la_flecha_va_al_reves():
    assert hq.label("retiro_socio_de_pichincha", -128625) == "Aporte socio → Pichincha"
    assert hq.corto("retiro_socio_de_pichincha", "RR", -128625) == "Aporte socio → Pichincha"


def test_el_retiro_de_verdad_sigue_diciendo_retiro():
    assert hq.label("retiro_socio_de_pichincha", 30300) == "Retiro socio ← Pichincha"
    assert hq.label("retiro_socio_de_pichincha") == "Retiro socio ← Pichincha"


def test_el_reverso_de_un_aporte_no_dice_que_reversa_un_retiro():
    assert hq.label("reverso_retiro_socio_pichincha", -128625) == "Reverso: aporte → Pichincha"


def test_el_signo_no_toca_los_tipos_que_no_cambian_de_sentido():
    """Una factura en negativo NO es "lo contrario de una factura": la regla es
    para el puñado de tipos donde el signo da vuelta el sentido, no para todos."""
    assert hq.label("factura_emitida", -100) == hq.label("factura_emitida", 100)


def test_el_renglon_de_la_traza_dice_aporte():
    with _base([_APORTE]):
        idx = ev.indice(ev.de_la_ventana("2026-08-20 17:18+00", "2026-08-20 17:24+00"))
    movs = [{"doc_id": "r1129", "componente": "uret", "aporte": -128625.0,
             "regla": "Retiro de dividendos", "etiqueta": "Retiro RR · LAFER",
             "familia": "traspaso", "tipo": "alta"}]
    textos = [g["texto"] for g in t.resumir(movs, -128625.0, idx)]
    assert textos == ["Aporte socio → Pichincha"], textos


# ── 2. El Δ del banco no se le cuelga a un hecho solo ──────────────────────

def test_el_aporte_cuenta_como_hecho_de_la_cuenta_aunque_no_traiga_no_banco():
    """El `no_banco` que la metadata no guardó lo sabe la transacción."""
    evs = _con_label([_CHEQUE, _APORTE])
    with _base([]):
        cuentas = ev._cuentas(evs)
    assert cuentas["b10"]["tipo"] == "banco_varios"
    assert "2 movimientos" in cuentas["b10"]["label"]


def test_el_cheque_no_se_queda_con_los_128k_del_aporte():
    """🚨 El renglón decía "Cheque emitido → otro · 102.973" y el cheque fue de
    25.651,68: los otros 128.625 eran el depósito del aporte."""
    evs = _con_label([_CHEQUE, _APORTE])
    movs = [{"doc_id": "b10", "componente": "bancos", "aporte": 102973.32,
             "regla": "Movimiento bancario", "etiqueta": "Banco PICHINCHA",
             "familia": "traspaso"}]
    with _base([]):
        idx = ev.indice(evs)
    texto = t.resumir(movs, 102973.32, idx)[0]["texto"]
    assert "Cheque emitido" not in texto, texto
    assert "LAFER" in texto and "IN OP" in texto, texto


def test_si_la_base_no_contesta_la_pantalla_sigue_andando():
    """Fail-soft: sin el `no_banco` los nombres son más pobres, nunca un 500."""
    evs = _con_label([_CHEQUE, _APORTE])
    with patch.object(ev.db, "fetch_all", side_effect=RuntimeError("sin base")):
        cuentas = ev._cuentas(evs)
    assert cuentas["b10"]["tipo"] == "cheque_emitido_otro"



# ── 3. Dos aportes juntados siguen siendo aportes ──────────────────────────

def _aporte(id_md, id_ret, id_tx, importe, quien):
    return dict(_APORTE, id_mov_doble=id_md, origen_id=id_ret,
                destino_id=id_tx, importe=importe, concepto=quien)


def test_dos_aportes_en_un_renglon_no_vuelven_a_llamarse_retiro():
    """El renglón juntado decía "2 Retiro socio ← Pichincha · RR" para los dos
    aportes de las 12:33 (UNITECH 128.625 + LAFER 34.860)."""
    evs = _con_label([_aporte(26709, 1131, 46125, -128625.0, "UNITECH"),
                      _aporte(26710, 1132, 46126, -34860.0, "LAFER")])
    movs = [{"doc_id": "r1131", "componente": "uret", "aporte": -128625.0,
             "regla": "Retiro de dividendos", "etiqueta": "Retiro RR · UNITECH",
             "familia": "traspaso", "tipo": "alta"},
            {"doc_id": "r1132", "componente": "uret", "aporte": -34860.0,
             "regla": "Retiro de dividendos", "etiqueta": "Retiro RR · LAFER",
             "familia": "traspaso", "tipo": "alta"}]
    with _base([]):
        idx = ev.indice(evs)
    g = t.resumir(movs, -163485.0, idx)[0]
    assert g["texto"] == "2 Aporte socio → Pichincha · RR", g["texto"]
    assert g["regla"] == "Aporte socio → Pichincha", g["regla"]


def test_un_retiro_y_un_aporte_juntos_no_se_llaman_ni_una_cosa_ni_la_otra():
    """🚨 El tipo es el mismo para los dos: si el grupo mezcla signos, ningún
    nombre es cierto para todos. Un nombre FALSO es peor que uno pobre."""
    evs = _con_label([_aporte(26709, 1131, 46125, -128625.0, "UNITECH"),
                      _aporte(26711, 1133, 46127, 30300.0, "RICKY KON")])
    movs = [{"doc_id": "r1131", "componente": "uret", "aporte": -128625.0,
             "regla": "Retiro de dividendos", "etiqueta": "Retiro RR · UNITECH",
             "familia": "traspaso", "tipo": "alta"},
            {"doc_id": "r1133", "componente": "uret", "aporte": 30300.0,
             "regla": "Retiro de dividendos", "etiqueta": "Retiro RR · RICKY KON",
             "familia": "traspaso", "tipo": "alta"}]
    with _base([]):
        idx = ev.indice(evs)
    g = t.resumir(movs, -98325.0, idx)[0]
    assert "Aporte" not in g["texto"], g["texto"]
    assert g["texto"] == "2 Retiro socio ← Pichincha · RR", g["texto"]
