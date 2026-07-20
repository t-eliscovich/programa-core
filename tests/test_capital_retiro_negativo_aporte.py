"""Retiro de socio en NEGATIVO = aporte de capital general (dueña 2026-07-20,
"tenemos que poder cargar aportes que no tengan que ver con OP").

capital.retirar acepta importe negativo: retiros.ret queda negativo (URET
baja) y la plata ENTRA a la cuenta (caja E / banco DE). reversar_retiro es
agnóstico al signo. Stub mock — sin DB real."""
from __future__ import annotations

import sys
from datetime import date

import pytest

from tests.test_logicas_contables import _DBStub


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
    import mov_doble
    monkeypatch.setattr(mov_doble, "registrar", lambda *a, **k: 1)
    return s


@pytest.fixture
def caja_spy(monkeypatch):
    import caja_helpers
    calls = []
    def fake(conn, **kw):
        calls.append(kw)
        return {"id_caja": 9}
    monkeypatch.setattr(caja_helpers, "insert_movimiento_caja", fake)
    return calls


def test_retirar_negativo_en_caja_es_aporte_entra_plata(stub, caja_spy):
    from modules.capital import queries as q
    stub.execute_returning_results.append({"id_retiro": 5})
    r = q.retirar(fecha=date(2026, 7, 20), importe=-1.0, cuenta="caja", socio="TM")
    assert r["importe"] == -1.0
    # retiros.ret negativo (URET baja)
    ins = next(e for e in stub.executes if "insert into scintela.retiros" in e[0].lower())
    assert -1.0 in ins[1]
    # la plata ENTRA a caja: tipo E, magnitud positiva
    assert caja_spy[0]["tipo"] == "E" and caja_spy[0]["importe"] == 1.0


def test_retirar_positivo_sigue_saliendo_de_caja(stub, caja_spy):
    from modules.capital import queries as q
    stub.execute_returning_results.append({"id_retiro": 6})
    q.retirar(fecha=date(2026, 7, 20), importe=50.0, cuenta="caja", socio="TM")
    assert caja_spy[0]["tipo"] == "S" and caja_spy[0]["importe"] == 50.0


def test_retirar_cero_rechaza(stub):
    from modules.capital import queries as q
    with pytest.raises(ValueError, match="cero"):
        q.retirar(fecha=date(2026, 7, 20), importe=0, cuenta="caja", socio="TM")


def test_reversar_aporte_negativo_saca_la_plata(stub, caja_spy):
    from modules.capital import queries as q
    # ret original = -1 (aporte por caja)
    stub.fetch_one_responses.append({
        "id_retiro": 5, "fecha": date(2026, 7, 20), "ret": -1.0,
        "de": "TM", "nb": None, "concepto": "APORTE TM",
    })
    stub.fetch_one_responses.append({
        "id_mov_doble": 3, "tipo": "retiro_socio_de_caja",
        "destino_table": "caja", "destino_id": 9, "importe": -1.0,
    })
    stub.execute_returning_results.append({"id_retiro": 8})
    r = q.reversar_retiro(id_retiro=5)
    # compensación: ret = +1 (el aporte se anula)
    ins = next(e for e in stub.executes if "insert into scintela.retiros" in e[0].lower())
    assert 1.0 in ins[1]
    # la plata SALE de caja (tipo S) por la magnitud
    assert caja_spy[0]["tipo"] == "S" and caja_spy[0]["importe"] == 1.0
    assert r["cuenta"] == "caja"


def test_reversar_retiro_dbase_sin_movdoble_compensa_sin_banco(stub, caja_spy):
    """Retiro que vino del dBase (sin mov_doble): la anulación inserta SOLO la
    compensación (ret=-ret, ANULACION, pc-capital) — sin tocar caja/banco
    (esa pata vive en el dBase). TMT 2026-07-20 (caso RR DEP AMAZONAS)."""
    from modules.capital import queries as q
    stub.fetch_one_responses.append({
        "id_retiro": 900, "fecha": date(2026, 7, 8), "ret": -32104.25,
        "de": "RR", "nb": None, "concepto": "RR DEP AMAZONAS", "clave": "R",
        "usuario_crea": "dbf-import",
    })
    stub.fetch_one_responses.append(None)  # sin mov_doble activo
    stub.fetch_one_responses.append(None)  # sin ANULACION previa
    stub.execute_returning_results.append({"id_retiro": 901})
    r = q.reversar_retiro(id_retiro=900, usuario="t")
    assert r["id_retiro_compensacion"] == 901
    ins = next(e for e in stub.executes if "insert into scintela.retiros" in e[0].lower())
    assert 32104.25 in ins[1]  # compensación = -(-32104.25)
    assert any("pc-capital:t" in str(x) for x in ins[1])
    assert not caja_spy  # NO tocó caja


def test_reversar_compensacion_rechaza(stub):
    """Una fila REV/REVERSO/ANULACION no se re-cancela."""
    from modules.capital import queries as q
    stub.fetch_one_responses.append({
        "id_retiro": 901, "fecha": date(2026, 7, 20), "ret": 32104.25,
        "de": "RR", "nb": None, "concepto": "ANULACION retiro dBase id=900",
        "clave": "REV", "usuario_crea": "pc-capital:t",
    })
    with pytest.raises(ValueError, match="compensaci"):
        q.reversar_retiro(id_retiro=901, usuario="t")



def test_reversar_dbase_dos_veces_rechaza(stub):
    """Segunda anulación del mismo retiro dBase → error (guard ANULACION)."""
    from modules.capital import queries as q
    stub.fetch_one_responses.append({
        "id_retiro": 900, "fecha": date(2026, 7, 8), "ret": -32104.25,
        "de": "RR", "nb": None, "concepto": "RR DEP AMAZONAS", "clave": "R",
        "usuario_crea": "dbf-import",
    })
    stub.fetch_one_responses.append(None)          # sin mov_doble
    stub.fetch_one_responses.append({"ok": 1})     # ya hay ANULACION
    with pytest.raises(ValueError, match="ANULACION"):
        q.reversar_retiro(id_retiro=900, usuario="t")


def test_reversar_pc_sin_movdoble_activo_rechaza(stub):
    """Un retiro PC (no dbf-import) sin mov_doble activo NO cae al camino
    dBase — probablemente ya fue reversado."""
    from modules.capital import queries as q
    stub.fetch_one_responses.append({
        "id_retiro": 700, "fecha": date(2026, 7, 20), "ret": 100.0,
        "de": "TM", "nb": None, "concepto": "RETIRO TM", "clave": "TM",
        "usuario_crea": "pc-capital:t",
    })
    stub.fetch_one_responses.append(None)  # sin mov_doble activo
    with pytest.raises(ValueError, match="rastro"):
        q.reversar_retiro(id_retiro=700, usuario="t")
