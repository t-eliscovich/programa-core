"""Un cobro en efectivo que YA está en la caja se convierte en cobranza
sin mover el saldo de caja.

TMT 2026-08-03. La dueña cargó 5 cobros en efectivo por la pantalla de CAJA
(DPS 1.500 · STU 1.000 · YHJ 1.000 · CHI 400 · ECH 502,86 = $4.402,86): la
plata entró, pero como no pasó por cobranza nunca se creó el cheque, así que
la deuda del cliente NO bajó — a YHJ le sobraban $1.000 contra el dBase y a
CHI $400, clavados — y no contaba para la comisión del vendedor.

No se podían reversar y recargar: `caja.reversar` no borra, crea la
contrapartida CON FECHA DE HOY. Reversar 5 cobros de julio y volver a
cargarlos dejaba julio +4.402,86 y agosto −4.402,86.

La salida es que el alta ADOPTE la fila de caja existente en vez de insertar
una nueva. Lo que se protege acá es justamente eso: que NO haya un segundo
INSERT, y que la adopción falle ruidosamente si la fila cambió.
"""
from __future__ import annotations

import os
import sys
from datetime import date

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from modules.cheques import queries as q  # noqa: E402


def _fuente_bloque_caja() -> str:
    """El bloque `if no_banco == 99` de `crear`, como texto."""
    import inspect

    src = inspect.getsource(q.crear)
    ini = src.index("if no_banco == 99")
    return src[ini : ini + 6000]


def test_crear_acepta_caja_existente_id():
    import inspect

    assert "caja_existente_id" in inspect.signature(q.crear).parameters


def test_con_caja_existente_no_inserta_una_segunda_fila():
    """El alta de la fila tiene que quedar en el `else` — si no, se duplica la plata.

    TMT 2026-08-14: lo que se busca dejó de ser el texto `INSERT INTO
    scintela.caja` porque el alta pasó a `caja_helpers.insert_movimiento_caja`
    (el INSERT crudo no encadenaba el saldo de las filas de abajo). El
    invariante es el mismo y es el que importa: hay UNA sola escritura que
    crea fila, y vive en la rama en que NO se adoptó nada.
    """
    b = _fuente_bloque_caja()
    i_if = b.index("if caja_existente_id:")
    i_else = b.index("else:", i_if)
    i_insert = b.index("insert_movimiento_caja(")
    assert i_insert > i_else, "el alta quedó fuera del else — duplicaría la caja"
    assert "UPDATE scintela.caja" in b[i_if:i_else]
    assert "insert_movimiento_caja(" not in b[i_if:i_else], (
        "adoptar una fila que ya existe no puede crear otra")


def test_la_adopcion_exige_que_la_fila_siga_libre_y_calce():
    """El UPDATE es la validación: importe, tipo, concepto y sin cheque previo."""
    b = _fuente_bloque_caja()
    upd = b[b.index("UPDATE scintela.caja") : b.index("raise ValueError")]
    assert "id_cheque IS NULL" in upd, "podría robarle la caja a otro cheque"
    assert "tipo      = %s" in upd or "tipo = %s" in upd
    assert "ROUND(importe, 2)" in upd, "sin comparar importe adoptaría una fila por otra"
    assert "UPPER(TRIM(concepto))" in upd, "sin comparar concepto cruzaría clientes"
    assert "fecha     = %s" in upd or "fecha = %s" in upd, (
        "sin comparar la fecha, un cheque cargado HOY adoptaría una fila de "
        "caja de JULIO y el cobro cambiaría de mes"
    )


def test_si_no_adopta_exactamente_una_fila_aborta():
    b = _fuente_bloque_caja()
    assert "!= 1" in b, "tiene que exigir exactamente 1 fila afectada"
    assert "raise ValueError" in b, "no puede seguir dejando el cheque sin caja"


def test_sin_caja_existente_el_comportamiento_es_el_de_siempre():
    b = _fuente_bloque_caja()
    i_else = b.index("else:")
    assert "insert_movimiento_caja(" in b[i_else:]
    # El helper devuelve el dict con `id_caja`, que es de lo que cuelga el
    # `_md.registrar` de más abajo (el link cheque ↔ caja).
    assert "caja_row = caja_helpers.insert_movimiento_caja(" in b[i_else:]


def test_la_fila_de_caja_no_se_escribe_a_mano():
    """⭐ TMT 2026-08-14 — el saldo de la caja se calcula en UN solo lugar.

    Hasta hoy este bloque insertaba con `saldo = NULL` y dejaba que el trigger
    `trg_caja_set_saldo` (mig 0022) lo estampara. El trigger encadena la fila
    nueva pero NO re-encadena las que quedan debajo: un cobro en efectivo con
    fecha atrasada partía la cadena, el mismo bug que en bancos costó los
    155.187,31 del 03/08 y que se acaba de cerrar en `/caja/nuevo`.

    Que no vuelva un INSERT a mano acá: la caja ya tuvo cuatro definiciones
    del signo conviviendo y una estaba mal; el saldo tiene una sola.
    """
    b = _fuente_bloque_caja()
    assert "INSERT INTO scintela.caja" not in b, (
        "volvió el INSERT crudo — el saldo se calcula en caja_helpers")
    assert "saldo" not in b.split("insert_movimiento_caja(")[1][:400], (
        "el saldo no se pasa desde acá: lo calcula el helper")


def test_la_caja_cae_en_la_misma_transaccion_que_el_cheque():
    """El primer argumento del helper es el `conn` del `with _tx as conn`.

    Si la fila de caja se escribiera en una transacción aparte, un cobro que
    revienta después (el candado de la cadena, una aplicación a factura que
    falla) dejaría la plata en la caja sin cheque que la explique.
    TMT 2026-08-14.
    """
    b = _fuente_bloque_caja()
    llamada = b[b.index("insert_movimiento_caja("):][:200]
    assert llamada.split("(", 1)[1].lstrip().startswith("conn,")


@pytest.mark.parametrize("importe,tipo", [(400.0, "E"), (-400.0, "S")])
def test_el_signo_sigue_yendo_en_el_tipo(importe, tipo):
    """Regresión TMT 2026-07-30: el trigger usa ABS(importe) y el signo del tipo."""
    b = _fuente_bloque_caja()
    assert '_tipo_caja = "S" if _es_salida_caja else "E"' in b
    assert "_importe_caja = abs(importe_principal)" in b
    # y la adopción compara contra el mismo par (tipo, importe absoluto)
    upd = b[b.index("UPDATE scintela.caja") : b.index("raise ValueError")]
    assert "_tipo_caja" in b[b.index("UPDATE scintela.caja") : b.index("caja_row = {")]
    assert "importe" in upd


def test_el_mov_doble_se_registra_igual_adoptando_que_insertando():
    """Sin el link, «anular por error de carga» no sabe compensar la caja."""
    b = _fuente_bloque_caja()
    i_registrar = b.index("_md.registrar")
    i_if = b.index("if caja_existente_id:")
    assert i_registrar > i_if
    # el registrar cuelga de `if caja_row.get("id_caja")`, común a las dos ramas
    assert 'if caja_row.get("id_caja")' in b[:i_registrar]


def test_crear_sigue_exigiendo_fecha_abierta():
    """Adoptar una caja de julio no puede saltear el guard de período."""
    import inspect

    src = inspect.getsource(q.crear)
    assert "asegurar_fecha_abierta(fecha)" in src


def test_fecha_del_cheque_no_se_fuerza_a_hoy():
    """El cobro adoptado conserva SU fecha — si no, cambia de mes."""
    import inspect

    src = inspect.getsource(q.crear)
    i = src.index("def crear") if "def crear" in src else 0
    # `fecha` es parámetro obligatorio y no se pisa con today_ec en el cuerpo
    assert "fecha: date," in src[i : i + 400]
    assert "fecha = today_ec()" not in src


def test_caja_existente_id_se_castea_a_int():
    """Viene de un form: si entra como string, el UPDATE no matchea."""
    b = _fuente_bloque_caja()
    assert "int(caja_existente_id)" in b


def test_el_error_dice_que_hacer():
    b = _fuente_bloque_caja()
    msg = b[b.index("raise ValueError") : b.index("raise ValueError") + 500]
    assert "/caja" in msg, "el mensaje tiene que decir de dónde reintentar"


def test_fecha_hoy_no_aparece_en_la_rama_de_adopcion():
    """Regresión del motivo por el que no servía reversar: nada de fecha de hoy."""
    b = _fuente_bloque_caja()
    i_if = b.index("if caja_existente_id:")
    i_else = b.index("else:", i_if)
    rama = b[i_if:i_else]
    assert "today_ec" not in rama
    assert "utcnow" not in rama
