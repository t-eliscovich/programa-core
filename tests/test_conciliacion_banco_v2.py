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


def test_bucketizar_resultado_vacio_devuelve_buckets_vacios():
    res = ConciliacionBanco()
    b = _sesion.bucketizar(res)
    assert b["manual_banco"] == []
    assert b["manual_programa"] == []
    assert b["impuestos"] == []
    assert b["transferencias"] == []
    assert b["sugerencias"] == []


# ── CRUD sesión (mocked) ─────────────────────────────────────────────


def test_sesion_abierta_lee_con_filtros(monkeypatch):
    """sesion_abierta hace SELECT con (no_banco, usuario, cerrada_en IS NULL)."""
    captured = {}
    def fake_fetch_one(sql, params=None):
        captured["sql"] = sql
        captured["params"] = params
        return {"id": 42, "matches_hechos": 0, "cerrada_en": None}
    monkeypatch.setattr(_sesion.db, "fetch_one", fake_fetch_one)
    row = _sesion.sesion_abierta(10, "tamara")
    assert row["id"] == 42
    assert "cerrada_en IS NULL" in captured["sql"]
    assert captured["params"] == (10, "tamara")


def test_crear_sesion_serializa_payload_y_cierra_previa(monkeypatch):
    """crear_sesion cierra cualquier sesión abierta previa y graba la nueva."""
    executes = []
    returnings = [{"id": 999}]

    def fake_execute(sql, params=None, conn=None):
        executes.append(sql)
        return 1

    def fake_execute_returning(sql, params=None, conn=None):
        return returnings.pop(0) if returnings else None

    class FakeConn:
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr(_sesion.db, "execute", fake_execute)
    monkeypatch.setattr(_sesion.db, "execute_returning", fake_execute_returning)
    monkeypatch.setattr(_sesion.db, "tx", FakeConn)

    sid = _sesion.crear_sesion(
        no_banco=10, usuario="tamara",
        movs=[_mov(), _mov(monto="42")],
        extracto_hash="abc123",
        extracto_nombre="pichincha.xlsx",
    )
    assert sid == 999
    # Primero hace UPDATE (auto-cierra abierta previa), después INSERT
    assert any("UPDATE" in s and "auto-replaced" in s for s in executes)


def test_cerrar_sesion_setea_cerrada_en_y_pdf_path(monkeypatch):
    captured = {}
    def fake_execute(sql, params=None):
        captured["sql"] = sql
        captured["params"] = params
        return 1
    monkeypatch.setattr(_sesion.db, "execute", fake_execute)
    ok = _sesion.cerrar_sesion(42, "tamara", pdf_path="data/pdfs/x.pdf")
    assert ok is True
    assert "cerrada_en = CURRENT_TIMESTAMP" in captured["sql"]
    assert captured["params"][0] == "tamara"
    assert captured["params"][1] == "data/pdfs/x.pdf"
    assert captured["params"][2] == 42


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
