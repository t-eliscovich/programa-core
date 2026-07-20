"""Tests de calcular_propuestas() — backfill de fechas de depósito desde
CHEQUES.DBF (TMT 2026-07-20). Match conservador: (cliente, importe, banco),
desempate por NB; ambiguo → salteado. NUNCA por importe solo."""
from __future__ import annotations

from datetime import date

from modules.admin_dbase.cheques_feching_view import calcular_propuestas


def _pc(**kw):
    base = dict(id_cheque=1, no_cheque="135", codigo_cli="CG3", importe=879.96,
                banco="INTERNACI", no_banco=32, stat="B",
                fecha=date(2026, 7, 13), fechad=date(2026, 7, 13))
    base.update(kw)
    return base


def _dbf(**kw):
    base = dict(CLIENTE="CG3", IMPORTE=879.96, BANCO="INTERNACI", NB=32,
                STAT="V", FECHING=date(2026, 7, 13))
    base.update(kw)
    return base


def test_match_unico_propone():
    r = calcular_propuestas([_pc()], [_dbf()])
    assert len(r["propuestas"]) == 1 and not r["salteados"]
    c, f = r["propuestas"][0]
    assert c["id_cheque"] == 1 and f == date(2026, 7, 13)


def test_dbf_sin_feching_o_no_depositado_no_cuenta():
    r = calcular_propuestas([_pc()], [_dbf(FECHING=None), _dbf(STAT="Z")])
    assert not r["propuestas"]
    assert r["salteados"][0][1].startswith("sin fila")


def test_mismo_importe_otro_banco_no_matchea():
    # NUNCA por importe solo: banco distinto = otra clave.
    r = calcular_propuestas([_pc()], [_dbf(BANCO="GUAYAQUIL")])
    assert not r["propuestas"] and len(r["salteados"]) == 1


def test_ambiguo_en_dbf_desempata_por_nb():
    r = calcular_propuestas(
        [_pc(no_banco=32)],
        [_dbf(NB=32, FECHING=date(2026, 7, 13)), _dbf(NB=17, FECHING=date(2026, 7, 1))],
    )
    assert len(r["propuestas"]) == 1
    assert r["propuestas"][0][1] == date(2026, 7, 13)


def test_ambiguo_sin_desempate_se_saltea():
    r = calcular_propuestas(
        [_pc(no_banco=32)],
        [_dbf(NB=32), _dbf(NB=32, FECHING=date(2026, 7, 1))],
    )
    assert not r["propuestas"]
    assert "ambiguo" in r["salteados"][0][1]


def test_dos_pc_iguales_se_saltean():
    r = calcular_propuestas([_pc(id_cheque=1), _pc(id_cheque=2)], [_dbf()])
    assert not r["propuestas"] and len(r["salteados"]) == 2
