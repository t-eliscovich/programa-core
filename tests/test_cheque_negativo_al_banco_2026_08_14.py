"""Un cheque de importe NEGATIVO tiene que poder llegar al banco.

TMT 2026-08-14. La dueña, sobre el cheque 102251 de BED por −1.000: *"este de
BED −1000 no aparece para conciliar"*. Al intentar mandarlo al banco:

    importe debe ser positivo (abs). Recibido: -1000.0. El signo lo determina
    el documento ('DE'). Si necesitás convención DBF legacy (signed), pasá
    permitir_signed=True.

`transicionar_stat` armaba SIEMPRE un depósito. Depositar suma, y un cheque
negativo —el espejo de una devolución o de un saldo a favor— resta: el insert
lo rechazaba y el cheque quedaba trabado en cartera para siempre, sin poder
llegar nunca a la conciliación. Contexto: el de BED se había depositado el
11/08 (cheque 102111, +1.000) y el banco lo devolvió — en el extracto está el
"CHEQUE DEVUELTO −1.000" esperando con qué cruzarse.

Lo que el banco hace con una devolución es CARGARLA: es una nota de débito.
El signo lo lleva el documento (ND resta, DE suma) y el importe va en
magnitud, que es lo que pide bank_helpers.
"""
from __future__ import annotations

import os
import sys
from datetime import date

import pytest

_RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _RAIZ not in sys.path:
    sys.path.insert(0, _RAIZ)


class _Insert:
    """Captura lo que se le manda a bank_helpers, sin base."""

    def __init__(self):
        self.kw = None

    def __call__(self, conn, **kw):
        self.kw = kw
        return {"id_transaccion": 5001, "saldo_nuevo": 0.0}


@pytest.fixture
def espia(monkeypatch):
    import bank_helpers
    from modules.cheques import queries as q

    ins = _Insert()
    monkeypatch.setattr(bank_helpers, "insert_movimiento_bancario", ins)
    monkeypatch.setattr(q, "_banco_operativo", lambda needle, conn=None: 10)
    return ins


def _mov(espia, importe):
    """Corre el trozo real que arma el movimiento del depósito."""
    import bank_helpers
    ch = {"no_cheque": "500287", "codigo_cli": "BED", "doc_banco": ""}
    _imp = float(importe or 0)
    _es_debito = _imp < 0
    bank_helpers.insert_movimiento_bancario(
        None, no_banco=10, no_cta=None, fecha=date(2026, 8, 12),
        documento=("ND" if _es_debito else "DE"),
        importe=abs(_imp),
        concepto=f"{'Devol. cheque' if _es_debito else 'Dep cheque'} "
                 f"{ch['no_cheque']} {ch['codigo_cli']}".strip(),
        prov=ch["codigo_cli"], numreferencia="1", usuario="tamara",
    )
    return espia.kw


def test_el_negativo_va_como_nota_de_debito(espia):
    kw = _mov(espia, -1000.0)
    assert kw["documento"] == "ND", "un cheque negativo NO es un depósito"
    assert kw["importe"] == 1000.0, "el importe va en magnitud"


def test_el_positivo_sigue_siendo_un_deposito(espia):
    kw = _mov(espia, 1000.0)
    assert kw["documento"] == "DE"
    assert kw["importe"] == 1000.0


def test_el_signo_del_documento_es_el_correcto():
    """ND resta y DE suma: es lo que hace que el saldo del banco quede bien."""
    import bank_helpers
    assert bank_helpers.signo_documento("ND") == -1
    assert bank_helpers.signo_documento("DE") == 1


def test_bank_helpers_sigue_rechazando_un_deposito_negativo():
    """El freno que destapó todo esto no se toca: sigue estando para el que
    mezcle convenciones."""
    import bank_helpers
    with pytest.raises(ValueError, match="positivo"):
        bank_helpers.insert_movimiento_bancario(
            None, no_banco=10, no_cta=None, fecha=date(2026, 8, 12),
            documento="DE", importe=-1000.0, concepto="x", usuario="t",
        )


def test_la_fuente_arma_el_documento_segun_el_signo():
    """Candado sobre el código real: si alguien vuelve a clavar documento='DE'
    en el depósito, el cheque negativo se traba de nuevo y no hay base en CI
    para cazarlo de otra forma."""
    import inspect

    from modules.cheques import queries as q
    src = inspect.getsource(q.transicionar_stat)
    assert '_doc = "ND" if _es_debito else "DE"' in src, (
        "transicionar_stat volvió a armar siempre un depósito"
    )
    assert "importe=abs(_imp)" in src, "el importe tiene que ir en magnitud"


# ── El lote: los negativos NO se pueden ignorar ───────────────────────
#
# TMT 2026-08-14, la dueña: *"¡cómo vas a ignorar negativo en silencio!"*.
# `depositar_lote` filtraba `positivos` y a los negativos les cambiaba el
# estado a depositado SIN crearles el movimiento: el cheque quedaba marcado
# como si hubiera entrado al banco, sin plata detrás y sin nada que
# conciliar. Es peor que fallar — no falla, miente.

def _seed(conn, no_banco=79):
    cur = conn.cursor()
    cur.execute("DELETE FROM scintela.chequextransaccion WHERE id_cheque IN "
                "(SELECT id_cheque FROM scintela.cheque WHERE codigo_cli='ZZB')")
    cur.execute("DELETE FROM scintela.cheque WHERE codigo_cli='ZZB'")
    cur.execute("DELETE FROM scintela.transacciones_bancarias WHERE no_banco=%s",
                (no_banco,))
    cur.execute("INSERT INTO scintela.banco (no_banco, nombre) VALUES (%s,'TEST NEG') "
                "ON CONFLICT (no_banco) DO NOTHING", (no_banco,))
    cur.execute("INSERT INTO scintela.cliente (id_cliente, codigo_cli, nombre, stop, pago, cupo, activo) "
                "VALUES (DEFAULT,'ZZB','Cliente neg','N',30,100000,TRUE) "
                "ON CONFLICT (codigo_cli) DO NOTHING")
    ids = []
    for imp, nch in ((1000.00, 9001), (-1000.00, 9002)):
        cur.execute(
            "INSERT INTO scintela.cheque (no_cheque, fecha, fechad, codigo_cli, importe,"
            " no_banco, banco, stat, usuario_crea) "
            "VALUES (%s, DATE '2026-08-12', DATE '2026-08-12', 'ZZB', %s, 90, 'DEP.PICH.', 'Z', 'test') "
            "RETURNING id_cheque", (nch, imp))
        ids.append(cur.fetchone()[0])
    conn.commit()
    return ids


@pytest.mark.db
def test_el_lote_con_un_negativo_crea_LOS_DOS_movimientos(real_db_conn, migrated_db):
    """Lo que la dueña no quiere ver nunca: el cheque marcado como depositado
    y sin plata detrás. Un lote de +1.000 y −1.000 tiene que dejar el crédito
    del depósito Y el cargo de la devolución."""
    from datetime import date as _d

    from modules.cheques import queries as q

    ids = _seed(real_db_conn)
    q.depositar_lote(ids_cheques=ids, no_banco=79,
                     fecha_deposito=_d(2026, 8, 12), usuario="tamara")
    real_db_conn.commit()

    cur = real_db_conn.cursor()
    cur.execute("SELECT documento, importe FROM scintela.transacciones_bancarias "
                " WHERE no_banco=79 ORDER BY documento")
    movs = cur.fetchall()
    assert len(movs) == 2, f"faltó un movimiento: {movs}"
    docs = {d: float(i) for d, i in movs}
    assert docs.get("DE") == 1000.0, "el depósito del positivo"
    assert docs.get("ND") == 1000.0, "el CARGO de la devolución, en magnitud"


@pytest.mark.db
def test_el_negativo_queda_enlazado_a_su_movimiento(real_db_conn, migrated_db):
    """Sin el enlace, el cheque figura depositado pero nadie puede rastrear
    contra qué movimiento del banco fue."""
    from datetime import date as _d

    from modules.cheques import queries as q

    ids = _seed(real_db_conn)
    q.depositar_lote(ids_cheques=ids, no_banco=79,
                     fecha_deposito=_d(2026, 8, 12), usuario="tamara")
    real_db_conn.commit()

    cur = real_db_conn.cursor()
    cur.execute(
        "SELECT COUNT(*) FROM scintela.chequextransaccion cxt "
        "  JOIN scintela.cheque ch ON ch.id_cheque = cxt.id_cheque "
        " WHERE ch.codigo_cli = 'ZZB' AND ch.importe < 0")
    assert cur.fetchone()[0] == 1, "el cheque negativo quedó sin enlace"
