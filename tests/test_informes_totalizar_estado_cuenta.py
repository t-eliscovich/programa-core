"""Tests del feature "TOTALIZAR estado de cuenta" (re-liquidación FIFO).

TMT 2026-07-06 (dueña): réplica mejorada del dBase CUENTA.PRG (rama oculta
'Y') — junta todos los abonos de las facturas vivas del cliente y los
redistribuye de la más vieja a la más nueva. Decisiones cerradas:
  · las NC / importes negativos ENTRAN a la redistribución (el dBase no);
  · si sobra pool, el excedente queda como saldo NEGATIVO (crédito) en la
    ÚLTIMA factura viva, stat 'A' (saldo<0 nunca totaliza, regla 2026-07-01);
  · las T salen NORMALIZADAS (abono=importe, saldo=0), sin el quirk dBase.

Cubre:
1. totalizar_redistribuir_fifo — caso dBase básico (T…T + A + Z…Z), NC en el
   medio, sobra pool → última A negativa, NC después del corte (invariante).
2. Invariante Σsaldo / Σabono ANTES==DESPUÉS (±0.01) sobre varios casos.
3. totalizar_estado_cuenta_ejecutar (stub db) — updates mínimos, DELETE de
   chequesxfact, mov_doble, guards (sin facturas / pool 0 sin NC /
   invariante roto aborta).

Mismo estilo stub que tests/test_cheques_anticipo_cancela_cartera.py.
"""
from __future__ import annotations

import contextlib
import os
import sys
from datetime import date

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


def _redistribuir(importes, pool, retenciones=None):
    from modules.informes import queries as q
    return q.totalizar_redistribuir_fifo(importes, pool, retenciones)


def _check_invariante(importes, pool, res, retenciones=None):
    """Σabono == pool y Σsaldo == Σimporte − pool − Σretencion (±0.01).

    La retención entra en el invariante desde el 2026-08-20: baja el saldo
    igual que el abono (mig 0179) pero no se reparte.
    """
    rets = list(retenciones or [])
    rets += [0.0] * (len(importes) - len(rets))
    sum_abono = round(sum(r["abono"] for r in res), 2)
    sum_saldo = round(sum(r["saldo"] for r in res), 2)
    assert abs(sum_abono - round(pool, 2)) <= 0.01, (sum_abono, pool)
    esperado = round(sum(round(float(i), 2) for i in importes)
                     - pool - sum(round(float(r), 2) for r in rets), 2)
    assert abs(sum_saldo - esperado) <= 0.01, (sum_saldo, esperado)


# ─────────────────────── totalizar_redistribuir_fifo ──────────────────────

def test_caso_dbase_basico():
    # 5 facturas de 100, abonos que suman 350 → 3 T + 1 A parcial + 1 Z.
    importes = [100.0, 100.0, 100.0, 100.0, 100.0]
    res = _redistribuir(importes, 350.0)
    assert [r["stat"] for r in res] == ["T", "T", "T", "A", "Z"]
    # T normalizadas: abono=importe, saldo=0 (sin el quirk dBase).
    for r in res[:3]:
        assert (r["abono"], r["saldo"]) == (100.0, 0.0)
    assert res[3] == {"stat": "A", "abono": 50.0, "saldo": 50.0}
    assert res[4] == {"stat": "Z", "abono": 0.0, "saldo": 100.0}
    _check_invariante(importes, 350.0, res)


def test_nc_en_el_medio_extiende_cobertura():
    # NC de −30 entre medio: su crédito vuelve al pool y cubre más adelante.
    importes = [100.0, -30.0, 100.0, 100.0]
    res = _redistribuir(importes, 150.0)
    assert res[0] == {"stat": "T", "abono": 100.0, "saldo": 0.0}
    # La NC se totaliza con saldo 0 y abono negativo (devuelve al pool).
    assert res[1] == {"stat": "T", "abono": -30.0, "saldo": 0.0}
    # 150 − 100 + 30 = 80 disponibles para la tercera.
    assert res[2] == {"stat": "A", "abono": 80.0, "saldo": 20.0}
    assert res[3] == {"stat": "Z", "abono": 0.0, "saldo": 100.0}
    _check_invariante(importes, 150.0, res)


def test_sobra_pool_ultima_queda_a_negativa():
    # Σabonos (200) > Σimportes (150): el excedente queda como crédito
    # (saldo negativo) en la ÚLTIMA factura viva, stat 'A' — nunca T.
    importes = [100.0, 50.0]
    res = _redistribuir(importes, 200.0)
    assert res[0] == {"stat": "T", "abono": 100.0, "saldo": 0.0}
    assert res[1] == {"stat": "A", "abono": 100.0, "saldo": -50.0}
    _check_invariante(importes, 200.0, res)


def test_nc_credito_consolida_en_las_mas_viejas():
    # TMT 2026-07-07 (dueña "KAG totalizar no funcionó"): el crédito de la NC
    # entra al pool DESDE EL ARRANQUE, así consolida en la factura MÁS VIEJA.
    # pool 100 + NC 60 = 160 → la 1ra (150) se totaliza entera (T), la 3ra (50)
    # queda con el resto (10 → A saldo 40). Antes quedaba dispersa (1ra A parcial
    # + 3ra con el crédito de la NC).
    importes = [150.0, -60.0, 50.0]
    res = _redistribuir(importes, 100.0)
    assert res[0] == {"stat": "T", "abono": 150.0, "saldo": 0.0}
    assert res[1] == {"stat": "T", "abono": -60.0, "saldo": 0.0}
    assert res[2] == {"stat": "A", "abono": 10.0, "saldo": 40.0}
    _check_invariante(importes, 100.0, res)


def test_solo_nc_pool_cero_queda_credito_en_la_ultima():
    # pool 0 pero hay NC: el crédito termina como saldo negativo A (visible),
    # no desaparece.
    importes = [-50.0]
    res = _redistribuir(importes, 0.0)
    assert res == [{"stat": "A", "abono": 0.0, "saldo": -50.0}]
    _check_invariante(importes, 0.0, res)


def test_tolerancia_medio_centavo():
    # a medio centavo del importe → igual totaliza (redondeo de cobranza).
    res = _redistribuir([100.0], 99.995)
    assert res[0]["stat"] == "T"


@pytest.mark.parametrize("importes,pool", [
    ([100.0, 100.0, 100.0], 250.0),
    ([100.0, -30.0, 100.0, 100.0], 0.0),
    ([10.5, 20.25, -5.75, 33.33], 40.0),
    ([150.0, -60.0, 50.0, -10.0, 200.0], 120.0),
    ([100.0], 0.0),
])
def test_invariante_sigma_saldo_y_abono(importes, pool):
    res = _redistribuir(importes, pool)
    _check_invariante(importes, pool, res)


# ───────────────────────── retenciones (2026-08-20) ───────────────────────
# TMT: "fijate porque estas totalizaciones no se pueden". El invariante
# abortaba en TODO cliente con retenciones porque el reparto ignoraba la
# columna `factura.retencion` (mig 0179) y devolvía Σretencion de saldo de
# más. La retención ya bajó la deuda y NO se reparte.

def test_el_pool_cubre_importe_menos_retencion():
    # Factura de 100 con 15 de retención: al abono le tocan 85, no 100.
    res = _redistribuir([100.0, 100.0], 85.0, [15.0, 0.0])
    assert res[0] == {"stat": "T", "abono": 85.0, "saldo": 0.0}
    assert res[1] == {"stat": "Z", "abono": 0.0, "saldo": 100.0}
    _check_invariante([100.0, 100.0], 85.0, res, [15.0, 0.0])


def test_una_retencion_sola_no_mueve_de_z_a_a():
    # Sin un peso de abono, la factura debe menos pero sigue en 'Z'
    # (regla dueña 2026-08-07: la retención no abona).
    res = _redistribuir([100.0], 0.0, [15.0])
    assert res[0] == {"stat": "Z", "abono": 0.0, "saldo": 85.0}


def test_retencion_que_cubre_toda_la_factura_totaliza_sin_abono():
    res = _redistribuir([100.0], 0.0, [100.0])
    assert res[0] == {"stat": "T", "abono": 0.0, "saldo": 0.0}


def test_abono_parcial_con_retencion_deja_el_resto():
    # 100 − 20 de retención = 80 a cubrir; el pool trae 30.
    res = _redistribuir([100.0], 30.0, [20.0])
    assert res[0] == {"stat": "A", "abono": 30.0, "saldo": 50.0}


def test_sobra_pool_con_retencion_el_credito_va_a_la_ultima():
    res = _redistribuir([100.0, 100.0], 200.0, [0.0, 10.0])
    assert res[1]["stat"] == "A"
    assert res[1]["saldo"] == -10.0
    _check_invariante([100.0, 100.0], 200.0, res, [0.0, 10.0])


@pytest.mark.parametrize("importes,pool,rets", [
    ([100.0, 100.0, 100.0], 250.0, [5.0, 0.0, 12.34]),
    ([100.0, -30.0, 100.0, 100.0], 40.0, [0.0, 0.0, 7.5, 0.0]),
    ([10.5, 20.25, -5.75, 33.33], 40.0, [1.11, 0.0, 0.0, 2.22]),
    ([467455.79], 75253.73, [54.64]),          # BED, el caso que lo destapó
])
def test_invariante_se_sostiene_con_retenciones(importes, pool, rets):
    res = _redistribuir(importes, pool, rets)
    _check_invariante(importes, pool, res, rets)


def test_ejecutar_no_aborta_con_retenciones(monkeypatch):
    """El bug de BED: el invariante mataba el totalizar y no cambiaba nada."""
    from modules.informes import queries as q
    stub = _DBStub([
        _fact(1, 100.0, 60.0, 25.0, "A", retencion=15.0),
        _fact(2, 100.0, 90.0, 10.0, "A"),
        _fact(3, 100.0, 0.0, 100.0, "Z"),
    ])
    _patch_db(monkeypatch, stub)
    res = q.totalizar_estado_cuenta_ejecutar("bed", usuario="tester")
    assert res["codigo_cli"] == "BED"
    assert stub.updates, "no tocó ninguna factura"
    # 150 de pool sobre 85 + 100 + 100 → T(85) + A(65) + Z(100).
    nuevos = {u[-1]: u[:3] for u in stub.updates}
    assert nuevos[1] == (85.0, 0.0, "T")
    assert nuevos[2] == (65.0, 35.0, "A")
    # la 3 queda igual que como estaba (Z, 0, 100) → no se toca.
    assert 3 not in nuevos


def test_preview_muestra_la_retencion(monkeypatch):
    from modules.informes import queries as q
    stub = _DBStub([_fact(1, 100.0, 60.0, 25.0, "A", retencion=15.0)])
    monkeypatch.setattr(
        q.db, "fetch_one",
        lambda sql, params=None, conn=None: (
            {"codigo_cli": "BED", "nombre": "X"}
            if "from scintela.cliente" in " ".join(sql.split()).lower()
            else {"n": 0}))
    monkeypatch.setattr(
        q.db, "fetch_all",
        lambda sql, params=None, conn=None: [dict(f) for f in stub.facturas])
    data = q.totalizar_estado_cuenta_preview("BED")
    assert data["sum_retencion"] == 15.0
    assert data["filas"][0]["retencion"] == 15.0
    assert data["sum_saldo_antes"] == data["sum_saldo_despues"]


# ─────────────────── totalizar_estado_cuenta_ejecutar ─────────────────────

class _DBStub:
    """Stub mínimo de db para ejecutar() — captura updates/deletes/inserts."""

    def __init__(self, facturas, links=None):
        self.facturas = facturas
        self.links = links or []
        self.updates: list[tuple] = []
        self.deletes: list[tuple] = []
        self.mov_dobles: list[tuple] = []

    def fetch_all(self, sql, params=None, conn=None):
        s = " ".join(sql.split()).lower()
        if "from scintela.chequesxfact" in s:
            # Snapshot de los vínculos ANTES del DELETE (reverso del ↺).
            assert "delete" not in s
            return [dict(x) for x in self.links]
        assert "for update" in s  # lock dentro de la tx
        return [dict(f) for f in self.facturas]

    def fetch_one(self, sql, params=None, conn=None):
        return None

    def execute(self, sql, params=None, conn=None):
        s = " ".join(sql.split()).lower()
        if "update scintela.factura" in s:
            self.updates.append(tuple(params))
            return 1
        if "delete from scintela.chequesxfact" in s:
            self.deletes.append(tuple(params))
            return 4  # simulamos 4 vínculos borrados
        return 0

    def execute_returning(self, sql, params=None, conn=None):
        s = " ".join(sql.split()).lower()
        if "insert into scintela.mov_doble" in s:
            self.mov_dobles.append(tuple(params))
            return {"id_mov_doble": 777}
        return {}

    @contextlib.contextmanager
    def tx(self):
        yield object()


def _patch_db(monkeypatch, stub):
    import db
    monkeypatch.setattr(db, "fetch_all", stub.fetch_all)
    monkeypatch.setattr(db, "fetch_one", stub.fetch_one)
    monkeypatch.setattr(db, "execute", stub.execute)
    monkeypatch.setattr(db, "execute_returning", stub.execute_returning)
    monkeypatch.setattr(db, "tx", stub.tx)


def _fact(id_, importe, abono, saldo, stat, fecha=None, retencion=0.0):
    return {
        "id_factura": id_, "numf": id_, "numf_completo": f"001-001-{id_:09d}",
        "fecha": fecha or date(2026, 6, id_), "importe": importe,
        "abono": abono, "retencion": retencion, "saldo": saldo, "stat": stat,
    }


def test_ejecutar_actualiza_borra_links_y_registra(monkeypatch):
    from modules.informes import queries as q
    stub = _DBStub([
        _fact(1, 100.0, 60.0, 40.0, "A"),
        _fact(2, 100.0, 90.0, 10.0, "A"),
        _fact(3, 100.0, 0.0, 100.0, "Z"),
    ])
    _patch_db(monkeypatch, stub)
    res = q.totalizar_estado_cuenta_ejecutar("aaa", usuario="tester")
    assert res["codigo_cli"] == "AAA"
    assert res["n_facturas"] == 3 and res["pool"] == 150.0
    assert (res["n_T"], res["n_A"], res["n_Z"]) == (1, 1, 1)
    # La 3ra (Z 0/100) no cambia → solo 2 UPDATEs.
    assert res["n_actualizadas"] == 2 and len(stub.updates) == 2
    # f1 → T normalizada, f2 → A 50/50.
    assert stub.updates[0] == (100.0, 0.0, "T", "tester", 1)
    assert stub.updates[1] == (50.0, 50.0, "A", "tester", 2)
    # DELETE de los vínculos de TODAS las facturas del universo.
    assert stub.deletes == [([1, 2, 3],)]
    assert res["n_links_borrados"] == 4
    # UN mov_doble con la huella.
    assert len(stub.mov_dobles) == 1
    md = stub.mov_dobles[0]
    assert md[1] == "totalizar_estado_cuenta"
    assert md[2] == "factura" and md[3] == 1      # origen = 1ra factura
    assert md[4] == "factura" and md[5] == 3      # destino = última
    # Invariante: el saldo total del cliente no cambió.
    assert res["saldo"] == 150.0


def test_ejecutar_sin_facturas_avisa(monkeypatch):
    from modules.informes import queries as q
    stub = _DBStub([])
    _patch_db(monkeypatch, stub)
    with pytest.raises(ValueError, match="sin facturas vivas"):
        q.totalizar_estado_cuenta_ejecutar("AAA")
    assert not stub.updates and not stub.deletes and not stub.mov_dobles


def test_ejecutar_pool_cero_sin_nc_avisa(monkeypatch):
    from modules.informes import queries as q
    stub = _DBStub([_fact(1, 100.0, 0.0, 100.0, "Z")])
    _patch_db(monkeypatch, stub)
    with pytest.raises(ValueError, match="nada que redistribuir"):
        q.totalizar_estado_cuenta_ejecutar("AAA")
    assert not stub.updates and not stub.deletes


def test_ejecutar_pool_cero_con_nc_si_corre(monkeypatch):
    # pool 0 pero hay NC → SÍ redistribuye (decisión dueña #2).
    from modules.informes import queries as q
    stub = _DBStub([
        _fact(1, -30.0, 0.0, -30.0, "A"),
        _fact(2, 100.0, 0.0, 100.0, "Z"),
    ])
    _patch_db(monkeypatch, stub)
    res = q.totalizar_estado_cuenta_ejecutar("AAA")
    # NC totalizada (crédito viaja) + la de 100 queda A con 30 abonados.
    assert stub.updates[0] == (-30.0, 0.0, "T", "web", 1)
    assert stub.updates[1] == (30.0, 70.0, "A", "web", 2)
    assert res["saldo"] == 70.0  # == −30 + 100 (saldo total intacto)
    # mov_doble con importe = crédito NC (pool era 0).
    assert len(stub.mov_dobles) == 1


def test_ejecutar_invariante_roto_aborta_todo(monkeypatch):
    # saldo guardado inconsistente (saldo ≠ importe − abono) → aborta ANTES
    # de tocar nada (rollback total).
    from modules.informes import queries as q
    stub = _DBStub([_fact(1, 100.0, 60.0, 999.0, "A")])
    _patch_db(monkeypatch, stub)
    with pytest.raises(ValueError, match="[Ii]nvariante"):
        q.totalizar_estado_cuenta_ejecutar("AAA")
    assert not stub.updates and not stub.deletes and not stub.mov_dobles
