"""Tests del flujo v2 de conciliación bancaria (Sprint 1 Reforma 2026-05-28).

Cubre:
  - Serialización MovBanco ↔ JSON (sesion._mov_to_dict / _dict_to_mov).
  - CRUD de sesión (mocked DB).
  - bucketizar(ConciliacionBanco) → 3 buckets correctos.
  - PDF: si reportlab no está disponible, devuelve None sin levantar.

No tocan DB real ni filesystem (excepto el PDF que escribe en /tmp).
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from modules.conciliacion import sesion as _sesion
from modules.conciliacion.matcher_banco import (
    Categorizado,
    ConciliacionBanco,
    Match,
    MovBancsis,
)
from modules.conciliacion.parser_banco import MovBanco


# ── Helpers de fixture ────────────────────────────────────────────────


def _mov(monto="500", tipo="C", concepto="Depósito test", documento="123"):
    return MovBanco(
        fecha=date(2026, 5, 28),
        concepto=concepto,
        documento=documento,
        monto=Decimal(monto),
        saldo=Decimal("0"),
        codigo="001045",
        tipo=tipo,
        oficina="AG. NORTE",
    )


def _bancsis(id_tx=99, importe=500.0, documento="DE", numreferencia="123"):
    return MovBancsis(
        id_transaccion=id_tx,
        fecha=date(2026, 5, 28),
        documento=documento,
        concepto="programa",
        importe=importe,
        numreferencia=numreferencia,
        no_banco=10,
        saldo=None,
        prov="TST",
        prov_nombre="TEST CLIENTE",
    )


# ── Serialización ─────────────────────────────────────────────────────


def test_mov_a_dict_round_trip():
    """MovBanco → dict → MovBanco preserva todos los campos."""
    m = _mov(monto="1234.56", tipo="D", concepto="Cheque NN", documento="9999")
    d = _sesion._mov_to_dict(m)
    assert d["fecha"] == "2026-05-28"
    assert d["monto"] == "1234.56"
    assert d["tipo"] == "D"
    assert d["documento"] == "9999"
    m2 = _sesion._dict_to_mov(d)
    assert m2.fecha == m.fecha
    assert m2.monto == m.monto
    assert m2.tipo == m.tipo
    assert m2.documento == m.documento
    assert m2.concepto == m.concepto


def test_dict_a_mov_tolera_campos_vacios():
    """Si fecha o monto faltan, el _dict_to_mov no levanta."""
    m = _sesion._dict_to_mov({"fecha": None, "monto": None})
    assert m.fecha is None
    assert m.monto == Decimal("0")


def test_sha256_bytes_es_determinístico():
    h1 = _sesion.sha256_bytes(b"hello")
    h2 = _sesion.sha256_bytes(b"hello")
    h3 = _sesion.sha256_bytes(b"hellox")
    assert h1 == h2
    assert h1 != h3
    assert len(h1) == 64  # sha256 hex


# ── Bucketizar ────────────────────────────────────────────────────────


def test_bucketizar_separa_comision_de_manual():
    """real_only categorizado como COMISION va al tab Impuestos.
    El resto va al tab Manual.
    """
    res = ConciliacionBanco()
    res.real_only = [
        _mov(monto="500", concepto="Depósito normal"),    # → manual
        _mov(monto="0.05", concepto="IVA COBRADO"),       # → impuestos
        _mov(monto="0.31", concepto="COMISION COBRADO"),  # → impuestos
    ]
    res.real_only_cats = [
        Categorizado(codigo="ENTRADA", grupo="ENTRADA", label="Depósito"),
        Categorizado(codigo="COMISION_IVA", grupo="COMISION", label="IVA"),
        Categorizado(codigo="COMISION_BANCO", grupo="COMISION", label="Comisión"),
    ]
    res.bancsis_only = []
    res.bancsis_only_cats = []
    res.matches = []

    b = _sesion.bucketizar(res)
    assert len(b["manual_banco"]) == 1
    assert len(b["impuestos"]) == 2
    assert b["manual_banco"][0]["mov"].monto == Decimal("500")
    # Impuestos ordenado mayor → menor por monto
    assert b["impuestos"][0]["mov"].monto >= b["impuestos"][1]["mov"].monto


def test_bucketizar_pass0_va_a_transferencias():
    """Match con razon que contiene 'PASS 0' → tab Transferencias."""
    res = ConciliacionBanco()
    res.real_only = []
    res.real_only_cats = []
    res.bancsis_only = []
    res.bancsis_only_cats = []
    res.matches = [
        Match(real=_mov(), bancsis=_bancsis(), score=0.0,
              razon="Doc-ID exacto (PASS 0)"),
        Match(real=_mov(), bancsis=_bancsis(id_tx=100), score=0.5,
              razon="Match por monto+fecha (PASS 1)"),
        Match(real=_mov(), bancsis=_bancsis(id_tx=101), score=1.0,
              razon="Fuzzy P3.5"),
    ]
    b = _sesion.bucketizar(res)
    assert len(b["transferencias"]) == 1
    assert len(b["sugerencias"]) == 2


def test_bucketizar_manual_programa_orden_por_monto_desc():
    res = ConciliacionBanco()
    res.real_only = []
    res.real_only_cats = []
    res.bancsis_only = [
        _bancsis(id_tx=1, importe=100.0),
        _bancsis(id_tx=2, importe=900.0),
        _bancsis(id_tx=3, importe=500.0),
    ]
    res.bancsis_only_cats = [None, None, None]
    res.matches = []

    b = _sesion.bucketizar(res)
    montos = [item["mov"].importe for item in b["manual_programa"]]
    assert montos == [900.0, 500.0, 100.0]


def test_conceptos_iva_comision_van_a_impuestos():
    """TMT 2026-06-29 dueña: estos conceptos del extracto deben caer SIEMPRE
    en el bucket Impuestos/comisiones (grupo COMISION), no en Manual.
    Robusto: case-insensitive y con números/fecha pegados.
    """
    from modules.conciliacion.categorizar import categorizar

    casos = [
        "COST CHEQUE DEVUELTO",
        "REV IVA+COMIS B10 # 2 16062026",
        "IVA CAUSADO SERVICIO",
        "cost cheque devuelto",          # minúsculas
        "rev iva + comis algo 010203",   # con espacios alrededor del +
    ]
    for concepto in casos:
        cat = categorizar(concepto, "D")
        assert (cat.grupo or "").upper() == "COMISION", (
            f"{concepto!r} debería ser COMISION, fue {cat.grupo!r}"
        )

    # Y de punta a punta: vía bucketizar caen en 'impuestos'.
    res = ConciliacionBanco()
    res.real_only = [_mov(monto="1", concepto=c, tipo="D") for c in casos]
    res.real_only_cats = [categorizar(c, "D") for c in casos]
    res.bancsis_only = []
    res.bancsis_only_cats = []
    res.matches = []
    b = _sesion.bucketizar(res)
    assert len(b["impuestos"]) == len(casos)
    assert b["manual_banco"] == []


def test_bucketizar_resultado_vacio_devuelve_buckets_vacios():
    res = ConciliacionBanco()
    b = _sesion.bucketizar(res)
    assert b["manual_banco"] == []
    assert b["manual_programa"] == []
    assert b["impuestos"] == []
    assert b["transferencias"] == []
    assert b["sugerencias"] == []


# ── CRUD sesión (mocked) ─────────────────────────────────────────────


def test_sesion_abierta_lee_solo_por_banco(monkeypatch):
    """TMT 2026-06-02: una sesión por banco — no filtra por usuario."""
    captured = {}
    def fake_fetch_one(sql, params=None):
        captured["sql"] = sql
        captured["params"] = params
        return {"id": 42, "matches_hechos": 0, "cerrada_en": None}
    monkeypatch.setattr(_sesion.db, "fetch_one", fake_fetch_one)
    row = _sesion.sesion_abierta(10)
    assert row["id"] == 42
    assert "cerrada_en IS NULL" in captured["sql"]
    assert captured["params"] == (10,)


def test_crear_sesion_crea_nueva_si_no_hay_abierta(monkeypatch):
    """Sin sesión abierta → INSERT directo y devuelve (sid, n_added, 0)."""
    # No hay sesión abierta.
    monkeypatch.setattr(_sesion, "sesion_abierta", lambda no_banco: None)
    monkeypatch.setattr(_sesion, "_firmas_ya_conocidas",
                        lambda no_banco: set())

    returnings = [{"id": 999}]
    def fake_execute_returning(sql, params=None, conn=None):
        return returnings.pop(0) if returnings else None
    monkeypatch.setattr(_sesion.db, "execute_returning", fake_execute_returning)

    sid, n_added, n_skipped = _sesion.crear_sesion(
        no_banco=10, usuario="tamara",
        movs=[_mov(documento="A1"), _mov(documento="A2", monto="42")],
        extracto_nombre="pichincha.xlsx",
    )
    assert sid == 999
    assert n_added == 2
    assert n_skipped == 0


def test_crear_sesion_dedupe_por_firma_completa(monkeypatch):
    """TMT 2026-06-02: filas con firma (doc+codigo+tipo+monto+fecha) ya
    conocida se omiten. El `documento` solo NO es único."""
    monkeypatch.setattr(_sesion, "sesion_abierta", lambda no_banco: None)
    # Firma de A1 con el monto default ($500) ya existe.
    sig_a1 = _sesion._firma_mov("A1", "", "C", "500", date(2026, 5, 28))
    monkeypatch.setattr(_sesion, "_firmas_ya_conocidas",
                        lambda no_banco: {sig_a1})
    returnings = [{"id": 100}]
    def fake_execute_returning(sql, params=None, conn=None):
        return returnings.pop(0) if returnings else None
    monkeypatch.setattr(_sesion.db, "execute_returning", fake_execute_returning)

    sid, n_added, n_skipped = _sesion.crear_sesion(
        no_banco=10, usuario="tamara",
        movs=[_mov(documento="A1"), _mov(documento="A2", monto="42")],
        extracto_nombre="x.xlsx",
    )
    assert sid == 100
    assert n_added == 1   # solo A2 entra
    assert n_skipped == 1  # A1 se omitió por firma


def test_crear_sesion_mergea_si_hay_abierta(monkeypatch):
    """Si ya hay sesión abierta, mergea en su payload (UPDATE, no INSERT)."""
    sesion_existente = {
        "id": 555, "no_banco": 10, "extracto_payload": [],
        "extracto_nombre": "viejo.xlsx",
    }
    monkeypatch.setattr(_sesion, "sesion_abierta",
                        lambda no_banco: sesion_existente)
    monkeypatch.setattr(_sesion, "_firmas_ya_conocidas",
                        lambda no_banco: set())
    monkeypatch.setattr(_sesion, "cargar_movs", lambda s: [])

    executed = {}
    def fake_execute(sql, params=None, conn=None):
        executed["sql"] = sql
        executed["params"] = params
        return 1
    monkeypatch.setattr(_sesion.db, "execute", fake_execute)

    sid, n_added, n_skipped = _sesion.crear_sesion(
        no_banco=10, usuario="tamara",
        movs=[_mov(documento="NEW1")],
        extracto_nombre="nuevo.xlsx",
    )
    assert sid == 555  # misma sesión
    assert n_added == 1
    assert "UPDATE scintela.banco_conciliacion_sesion" in executed["sql"]
    assert "extracto_payload = %s::jsonb" in executed["sql"]


# ── Edge cases del dedupe por documento (TMT 2026-06-02) ─────────────


def test_dedupe_es_case_insensitive(monkeypatch):
    """'abc123' en histos debe dedupear 'ABC123' del extracto nuevo
    (firma se normaliza a uppercase)."""
    monkeypatch.setattr(_sesion, "sesion_abierta", lambda no_banco: None)
    # Firma de ABC123 con los defaults del _mov helper.
    sig = _sesion._firma_mov("ABC123", "001045", "C", "500", date(2026, 5, 28))
    monkeypatch.setattr(_sesion, "_firmas_ya_conocidas",
                        lambda no_banco: {sig})
    returnings = [{"id": 1}]
    monkeypatch.setattr(_sesion.db, "execute_returning",
                        lambda sql, params=None, conn=None:
                            returnings.pop(0) if returnings else None)
    sid, n_added, n_skipped = _sesion.crear_sesion(
        no_banco=10, usuario="t",
        movs=[_mov(documento="abc123"), _mov(documento="ABC123"), _mov(documento="xYz999")],
    )
    # abc123 y ABC123 ambos chocan con la firma → omitidos. xYz999 nuevo → entra.
    assert n_added == 1
    assert n_skipped == 2


def test_dedupe_documento_vacio_no_se_dedupe(monkeypatch):
    """Filas sin documento (None o '') no se pueden comparar — todas entran.

    Razón: el banco a veces devuelve documento vacío en transferencias
    raras. Si dedupeáramos por 'vacío' descartaríamos filas legítimas.
    """
    monkeypatch.setattr(_sesion, "sesion_abierta", lambda no_banco: None)
    monkeypatch.setattr(_sesion, "_firmas_ya_conocidas",
                        lambda no_banco: set())
    returnings = [{"id": 1}]
    monkeypatch.setattr(_sesion.db, "execute_returning",
                        lambda sql, params=None, conn=None:
                            returnings.pop(0) if returnings else None)
    sid, n_added, n_skipped = _sesion.crear_sesion(
        no_banco=10, usuario="t",
        movs=[_mov(documento=""), _mov(documento=""), _mov(documento="X1")],
    )
    assert n_added == 3
    assert n_skipped == 0


def test_dedupe_interno_dentro_del_mismo_upload(monkeypatch):
    """Si el extracto trae el mismo documento dos veces, dedupea contra sí mismo."""
    monkeypatch.setattr(_sesion, "sesion_abierta", lambda no_banco: None)
    monkeypatch.setattr(_sesion, "_firmas_ya_conocidas",
                        lambda no_banco: set())
    returnings = [{"id": 1}]
    monkeypatch.setattr(_sesion.db, "execute_returning",
                        lambda sql, params=None, conn=None:
                            returnings.pop(0) if returnings else None)
    sid, n_added, n_skipped = _sesion.crear_sesion(
        no_banco=10, usuario="t",
        movs=[_mov(documento="D1"), _mov(documento="D1"), _mov(documento="D2")],
    )
    assert n_added == 2
    assert n_skipped == 1


def test_dedupe_contra_payload_existente_en_sesion_abierta(monkeypatch):
    """Re-uploadear el mismo archivo a la sesión abierta no agrega nada."""
    sesion_existente = {"id": 1, "no_banco": 10, "extracto_payload": []}
    monkeypatch.setattr(_sesion, "sesion_abierta",
                        lambda no_banco: sesion_existente)
    sig_d1 = _sesion._firma_mov("D1", "001045", "C", "500", date(2026, 5, 28))
    monkeypatch.setattr(_sesion, "_firmas_ya_conocidas",
                        lambda no_banco: {sig_d1})
    monkeypatch.setattr(_sesion, "cargar_movs", lambda s: [])
    monkeypatch.setattr(_sesion.db, "execute",
                        lambda sql, params=None, conn=None: 1)

    sid, n_added, n_skipped = _sesion.crear_sesion(
        no_banco=10, usuario="t",
        movs=[_mov(documento="D1"), _mov(documento="D2")],
    )
    assert n_added == 1   # D2
    assert n_skipped == 1  # D1 (ya estaba)


def test_dedupe_preserva_iva_cost_misma_documento(monkeypatch):
    """TMT 2026-06-02: el extracto Pichincha emite varias filas con MISMO
    documento cuando hay cargos relacionados (CHEQUE DEVUELTO + IVA + COST).
    El dedupe debe preservar las 3 filas — comparten doc pero difieren en
    codigo/monto."""
    monkeypatch.setattr(_sesion, "sesion_abierta", lambda no_banco: None)
    monkeypatch.setattr(_sesion, "_firmas_ya_conocidas",
                        lambda no_banco: set())
    monkeypatch.setattr(_sesion.db, "execute_returning",
                        lambda sql, params=None, conn=None: {"id": 1})

    # 3 filas con MISMO documento, distintos codigo + monto (escenario real
    # mostrado en el extracto de la dueña: cheque devuelto + IVA + costo).
    from modules.conciliacion.parser_banco import MovBanco as _MB
    from decimal import Decimal
    movs = [
        _MB(fecha=date(2026, 5, 27), concepto="CHEQUE DEVUELTO", documento="106",
            monto=Decimal("5000.00"), saldo=Decimal("0"), codigo="001314",
            tipo="D", oficina="LA SCALA"),
        _MB(fecha=date(2026, 5, 27), concepto="IVA COBRADO", documento="62207167",
            monto=Decimal("0.37"), saldo=Decimal("0"), codigo="098450",
            tipo="D", oficina="LA SCALA"),
        _MB(fecha=date(2026, 5, 27), concepto="COST CHEQUE DEVUELTO", documento="62207167",
            monto=Decimal("2.49"), saldo=Decimal("0"), codigo="098426",
            tipo="D", oficina="LA SCALA"),
    ]
    sid, n_added, n_skipped = _sesion.crear_sesion(
        no_banco=10, usuario="t", movs=movs,
    )
    # Las 3 filas entran — comparten documento pero las firmas difieren.
    assert n_added == 3
    assert n_skipped == 0


def test_firmas_ya_conocidas_junta_histos_matches_y_payload(monkeypatch):
    """_firmas_ya_conocidas lee de las 3 fuentes y devuelve set de tuplas."""
    fetched = {"histos": False, "matches": False}
    def fake_fetch_all(sql, params=None):
        if "banco_historicos_pendientes" in sql:
            fetched["histos"] = True
            return [
                {"documento": "h1", "codigo": "098", "fecha": date(2026, 5, 1),
                 "tipo": "C", "monto": 100},
                {"documento": "H2", "codigo": "098", "fecha": date(2026, 5, 2),
                 "tipo": "D", "monto": 50},
            ]
        if "banco_conciliacion_match" in sql:
            fetched["matches"] = True
            return [
                {"real_documento": "m1", "real_fecha": date(2026, 5, 3),
                 "real_tipo": "C", "real_monto": 200},
            ]
        return []
    monkeypatch.setattr(_sesion.db, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(_sesion, "sesion_abierta", lambda no_banco: None)

    sigs = _sesion._firmas_ya_conocidas(10)
    assert fetched["histos"] is True
    assert fetched["matches"] is True
    # Verificar que las 3 firmas están (documento normalizado uppercase).
    docs_solo = {s[0] for s in sigs}
    assert "H1" in docs_solo and "H2" in docs_solo and "M1" in docs_solo


def test_incrementar_matches_suma_a_la_columna(monkeypatch):
    captured = {}
    monkeypatch.setattr(_sesion.db, "execute",
                        lambda sql, params=None: captured.setdefault("p", params) or 1)
    _sesion.incrementar_matches(42, 3)
    assert captured["p"] == (3, 42)


# ── Cargar movs desde payload ─────────────────────────────────────────


def test_cargar_movs_acepta_lista_o_string_json():
    """jsonb puede venir como str (driver viejo) o como list (psycopg2 + jsonb)."""
    payload_list = [_sesion._mov_to_dict(_mov(monto="100"))]
    payload_str = '[{"fecha":"2026-05-28","concepto":"x","documento":"1","monto":"50","saldo":"0","codigo":"","tipo":"C","oficina":""}]'
    assert len(_sesion.cargar_movs({"extracto_payload": payload_list})) == 1
    assert len(_sesion.cargar_movs({"extracto_payload": payload_str})) == 1
    assert _sesion.cargar_movs({"extracto_payload": None}) == []


def test_cargar_movs_json_invalido_devuelve_vacio():
    assert _sesion.cargar_movs({"extracto_payload": "not valid json {"}) == []
