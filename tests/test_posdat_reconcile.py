"""Tests del plan de reconciliación POSDAT (matcher por clave estable).

TMT 2026-06-08: el matcher viejo pareaba por (prov, importe-ordenado), lo que
generaba falsos UPDATE/DELETE cuando aparecían cheques nuevos (corría el orden).
El nuevo paréa por (prov, concepto) — clave estable del cheque — con fallback de
importe sólo dentro de grupos con concepto vacío. Además RT (IVA) se trata como
provisión display-time igual que YY (re-pin importe=dBase + baseline=hoy).
"""
from modules.admin_dbase.posdat_reconcile_view import reconciliar_posdat_plan


def _dbf(prov, imp, con):
    return {"prov": prov, "importe": imp, "concepto": con}


def _pc(i, prov, imp, con, linked=False):
    return {"id_posdat": i, "prov": prov, "importe": imp, "concepto": con, "linked": linked}


def test_rt_se_trata_como_yy_repin():
    """RT (IVA) → update con yy=True (fija baseline=hoy), no como cheque plano."""
    plan = reconciliar_posdat_plan([_dbf("RT", 150424, "")], [_pc(49, "RT", 140000, "")])
    assert len(plan["updates"]) == 1
    u = plan["updates"][0]
    assert u["id"] == 49 and u["importe"] == 150424.0 and u["yy"] is True
    assert plan["inserts"] == [] and plan["deletes"] == []


def test_yy_repin_por_concepto():
    plan = reconciliar_posdat_plan(
        [_dbf("YY", 57000, "A,E,C AG,EN,CMB")],
        [_pc(132, "YY", 49300, "A,E,C AG,EN,CMB")],
    )
    assert plan["updates"][0] == {
        "id": 132, "importe": 57000.0, "concepto": "A,E,C AG,EN,CMB",
        "yy": True, "que": "YY A,E,C AG,EN,CMB",
    }


def test_cheque_cambia_importe_mismo_concepto_es_update():
    plan = reconciliar_posdat_plan(
        [_dbf("AQ", 9000, "6023  4")], [_pc(1, "AQ", 8500, "6023  4")],
    )
    assert len(plan["updates"]) == 1
    assert plan["updates"][0]["id"] == 1 and plan["updates"][0]["yy"] is False
    assert plan["inserts"] == [] and plan["deletes"] == []


def test_cheque_nuevo_solo_inserta_sin_tocar_existentes():
    """EL FIX: un cheque nuevo (concepto nuevo) → INSERT, los existentes NO se
    tocan. El matcher viejo (por importe ordenado) los habría mis-updateado."""
    dbf = [_dbf("AQ", 8500, "6023  4"), _dbf("AQ", 9700, "7  2")]
    pc = [_pc(1, "AQ", 8500, "6023  4")]
    plan = reconciliar_posdat_plan(dbf, pc)
    assert plan["updates"] == []
    assert len(plan["inserts"]) == 1 and plan["inserts"][0]["importe"] == 9700.0
    assert plan["deletes"] == []


def test_cheque_removido_se_borra_con_flag_linked():
    plan = reconciliar_posdat_plan(
        [], [_pc(5, "CC", 2053, "ECUAPLAST 13032", linked=True)],
    )
    assert len(plan["deletes"]) == 1 and plan["deletes"][0]["linked"] is True


def test_grupo_concepto_vacio_usa_fallback_importe():
    """prov 'TJ' con concepto vacío: pareo por importe dentro del grupo."""
    dbf = [_dbf("TJ", 1008, ""), _dbf("TJ", 1008, ""), _dbf("TJ", 2000, "")]
    pc = [_pc(1, "TJ", 1008, ""), _pc(2, "TJ", 1008, "")]
    plan = reconciliar_posdat_plan(dbf, pc)
    assert plan["updates"] == []
    assert len(plan["inserts"]) == 1 and plan["inserts"][0]["importe"] == 2000.0
    assert plan["deletes"] == []
