"""El hecho de capital dice QUÉ CUENTA movió.

🚨 20/08/2026: `aportar()` y `retirar()` guardaban el `mov_doble` con
`metadata={"socio": ...}` y nada más — sin el `no_banco` de la cuenta, aunque
el `destino_id` apuntara a la fila del banco. Todo lo que agrupa `mov_doble`
POR CUENTA mira la metadata: la traza no veía el aporte de 128.625 en
Pichincha y le colgaba el Δ entero del banco al único hecho que sí reconocía,
un cheque de 25.651,68 — un nombre FALSO sobre plata ajena.

La traza lo resuelve por la transacción (eso cubre las filas viejas). Esto fija
que el dato quede en el origen.
"""
from __future__ import annotations

import sys
from datetime import date

import pytest

from tests.test_logicas_contables import _DBStub

_BANCOS = [{"no_banco": 10, "nombre": "BANCO PICHINCHA"},
           {"no_banco": 20, "nombre": "BANCO INTERNACIONAL"}]


@pytest.fixture
def stub(monkeypatch):
    import db
    s = _DBStub()
    for fn in ("fetch_one", "fetch_all", "execute", "execute_returning", "tx"):
        monkeypatch.setattr(db, fn, getattr(s, fn))
    import periodo_guard
    monkeypatch.setattr(periodo_guard, "asegurar_fecha_abierta", lambda *a, **k: None)
    m = sys.modules.get("modules.capital.queries")
    if m and hasattr(m, "asegurar_fecha_abierta"):
        monkeypatch.setattr(m, "asegurar_fecha_abierta", lambda *a, **k: None)
    return s


@pytest.fixture
def md_spy(monkeypatch):
    """Lo que se le pasó a `mov_doble.registrar`."""
    import mov_doble
    calls = []

    def fake(**kw):
        calls.append(kw)
        return len(calls)
    monkeypatch.setattr(mov_doble, "registrar", fake)
    return calls


@pytest.fixture
def banco_spy(monkeypatch):
    import bank_helpers
    calls = []

    def fake(conn, **kw):
        calls.append(kw)
        return {"id_transaccion": 46123}
    monkeypatch.setattr(bank_helpers, "insert_movimiento_bancario", fake)
    return calls


@pytest.fixture
def caja_spy(monkeypatch):
    import caja_helpers
    monkeypatch.setattr(caja_helpers, "insert_movimiento_caja",
                        lambda conn, **kw: {"id_caja": 9})


def test_el_aporte_por_banco_guarda_la_cuenta(stub, md_spy, banco_spy):
    from modules.capital import queries as q
    stub.fetch_all_responses.append(_BANCOS)
    stub.execute_returning_results.append({"id_capital": 77})
    q.aportar(fecha=date(2026, 8, 20), importe=128625.0, cuenta="pichincha",
              socio="RR", doc="AP")
    assert md_spy[0]["metadata"]["no_banco"] == 10


def test_el_retiro_por_banco_guarda_la_cuenta(stub, md_spy, banco_spy):
    """La fila real: el aporte de 128.625 de LAFER, cargado como retiro negativo."""
    from modules.capital import queries as q
    stub.fetch_all_responses.append(_BANCOS)
    stub.execute_returning_results.append({"id_retiro": 1129})
    q.retirar(fecha=date(2026, 8, 20), importe=-128625.0, cuenta="pichincha",
              socio="RR", concepto="LAFER")
    assert md_spy[0]["metadata"]["no_banco"] == 10
    assert md_spy[0]["metadata"]["socio"] == "RR"


def test_en_caja_no_se_inventa_una_cuenta(stub, md_spy, caja_spy):
    """🚨 Un `no_banco: null` se lee como "cuenta desconocida", que NO es lo
    mismo que "no fue por banco". La clave directamente no va."""
    from modules.capital import queries as q
    stub.execute_returning_results.append({"id_retiro": 5})
    q.retirar(fecha=date(2026, 8, 20), importe=-1.0, cuenta="caja", socio="TM")
    assert "no_banco" not in md_spy[0]["metadata"]


def test_el_reverso_del_retiro_tambien_dice_la_cuenta(stub, md_spy, banco_spy):
    from modules.capital import queries as q
    stub.fetch_one_responses.append({
        "id_retiro": 1129, "fecha": date(2026, 8, 20), "ret": -128625.0,
        "de": "RR", "nb": 10, "concepto": "LAFER"})
    stub.fetch_one_responses.append({
        "id_mov_doble": 26707, "tipo": "retiro_socio_de_pichincha",
        "destino_table": "transacciones_bancarias", "destino_id": 46123,
        "importe": -128625.0})
    stub.execute_returning_results.append({"id_retiro": 1130})
    q.reversar_retiro(id_retiro=1129)
    assert md_spy[0]["metadata"]["no_banco"] == 10


def test_el_reverso_del_aporte_tambien_dice_la_cuenta(stub, md_spy, banco_spy):
    from modules.capital import queries as q
    stub.fetch_one_responses.append({
        "id_capital": 77, "fecha": date(2026, 8, 20), "importe": 128625.0,
        "doc": "AP", "concepto": "APORTE RR", "clave": "RR"})
    stub.fetch_one_responses.append({
        "id_mov_doble": 900, "tipo": "aporte_capital_a_pichincha",
        "destino_table": "transacciones_bancarias", "destino_id": 46123,
        "importe": 128625.0})
    stub.fetch_all_responses.append(_BANCOS)
    stub.execute_returning_results.append({"id_capital": 78})
    q.reversar_aporte(id_capital=77)
    assert md_spy[0]["metadata"]["no_banco"] == 10
