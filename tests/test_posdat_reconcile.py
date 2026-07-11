"""Tests del plan de reconciliación POSDAT (matcher por clave estable).

TMT 2026-06-08: el matcher viejo pareaba por (prov, importe-ordenado), lo que
generaba falsos UPDATE/DELETE cuando aparecían cheques nuevos (corría el orden).
El nuevo paréa por (prov, concepto) — clave estable del cheque — con fallback de
importe sólo dentro de grupos con concepto vacío. Además RT (IVA) se trata como
provisión display-time igual que YY (re-pin importe=dBase + baseline=hoy).
"""
from modules.admin_dbase.posdat_reconcile_view import reconciliar_posdat_plan


def _dbf(prov, imp, con, fechad=None):
    return {"prov": prov, "importe": imp, "concepto": con, "fechad": fechad}


def _pc(i, prov, imp, con, linked=False, fechad=None, usuario_crea=""):
    return {"id_posdat": i, "prov": prov, "importe": imp, "concepto": con,
            "linked": linked, "fechad": fechad, "usuario_crea": usuario_crea}


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
    # TMT 2026-06-10: "que" ahora muestra la clave canónica (^prefijo PRG)
    assert plan["updates"][0] == {
        "id": 132, "importe": 57000.0, "concepto": "A,E,C AG,EN,CMB",
        "yy": True, "que": "YY ^A,E,C",
    }


def test_yy_concepto_editado_en_pc_sigue_matcheando():
    """TMT 2026-06-10: la identidad YY es el PREFIJO del PRG (clave canónica),
    no el concepto completo. Si la dueña edita el concepto en PC, el reconcile
    NO debe anular + reinsertar (perdía id, baseline y links)."""
    plan = reconciliar_posdat_plan(
        [_dbf("YY", 57000, "A,E,C AG,EN,CMB")],
        [_pc(132, "YY", 49300, "A,E,C aportes y encaje")],  # concepto editado
    )
    assert plan["deletes"] == [] and plan["inserts"] == []
    assert plan["updates"][0]["id"] == 132
    assert plan["updates"][0]["importe"] == 57000.0


def test_mismo_importe_distinto_concepto_no_churn():
    """EL FIX CLAVE: el concepto del DBF lleva un contador volátil ("6023 4" →
    "6023 3"). Match por (prov, importe) → mismo importe matchea aunque el
    concepto difiera → NO genera delete+insert espurio."""
    plan = reconciliar_posdat_plan(
        [_dbf("AQ", 9710.60, "6023  4")], [_pc(1, "AQ", 9710.60, "6023  9")],
    )
    assert plan["updates"] == [] and plan["inserts"] == [] and plan["deletes"] == []


def test_cheque_cambia_importe_es_delete_insert():
    """Cambio de importe = cheque distinto → delete del viejo + insert del nuevo."""
    plan = reconciliar_posdat_plan(
        [_dbf("AQ", 9000, "6023  4")], [_pc(1, "AQ", 8500, "6023  4")],
    )
    assert plan["updates"] == []
    assert len(plan["deletes"]) == 1 and plan["deletes"][0]["id"] == 1
    assert len(plan["inserts"]) == 1 and plan["inserts"][0]["importe"] == 9000.0


def test_cheque_nuevo_solo_inserta_sin_tocar_existentes():
    """Un cheque nuevo (importe nuevo) → INSERT; el existente (mismo importe)
    matchea y NO se toca. El matcher viejo (importe ordenado por posición) los
    habría mis-updateado al correrse el orden."""
    dbf = [_dbf("AQ", 8500, "6023  4"), _dbf("AQ", 9700, "7  2")]
    pc = [_pc(1, "AQ", 8500, "6023  4")]
    plan = reconciliar_posdat_plan(dbf, pc)
    assert plan["updates"] == []
    assert len(plan["inserts"]) == 1 and plan["inserts"][0]["importe"] == 9700.0
    assert plan["deletes"] == []


def test_dos_cheques_mismo_importe_se_parean_por_count():
    """Dos AQ de 5922.50 en ambos lados → matchean por count, sin churn."""
    dbf = [_dbf("AQ", 5922.50, "a"), _dbf("AQ", 5922.50, "b")]
    pc = [_pc(1, "AQ", 5922.50, "x"), _pc(2, "AQ", 5922.50, "y")]
    plan = reconciliar_posdat_plan(dbf, pc)
    assert plan["updates"] == [] and plan["inserts"] == [] and plan["deletes"] == []


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


# ── TMT 2026-07-11: alineación de concepto+fechad de filas dbf-origin ──
# La dueña: "que el sync tome esos datos". El match por (prov,importe) dejaba
# concepto y fechad VIEJOS (SY "58028"→"22132", 15 fechas de junio). Ahora las
# filas que ORIGINAN en el dBase (usuario_crea dbf-import/reconcile-dbf) alinean
# concepto+fechad IN-PLACE (preserva id/link). Las provisiones YY/RT que corren
# por un monto cada día NO se tocan (van por su rama, no por el align).
import datetime as _dt


def test_align_cheque_dbf_origin_pisa_concepto_y_fechad():
    plan = reconciliar_posdat_plan(
        [_dbf("TP", 2792.02, "10616 27", _dt.date(2026, 7, 9))],
        [_pc(9, "TP", 2792.02, "10616 27", fechad=None, usuario_crea="dbf-import")],
    )
    assert plan["updates"] == [] and plan["deletes"] == [] and plan["inserts"] == []
    assert len(plan["aligns"]) == 1
    a = plan["aligns"][0]
    assert a["id"] == 9 and a["concepto"] == "10616 27" and a["fechad"] == _dt.date(2026, 7, 9)


def test_align_no_toca_fila_creada_en_pc():
    """usuario_crea != dbf-import/reconcile-dbf → NO se pisa (cheque de andres/tamara)."""
    plan = reconciliar_posdat_plan(
        [_dbf("XX", 500, "DOC-NEW", _dt.date(2026, 8, 1))],
        [_pc(7, "XX", 500, "DOC-OLD", fechad=_dt.date(2026, 6, 1), usuario_crea="tamara")],
    )
    assert plan["aligns"] == []
    assert plan["updates"] == [] and plan["deletes"] == [] and plan["inserts"] == []


def test_align_no_toca_provisiones_yy_diarias():
    """CLAVE (dueña): las provisiones YY/RT que acumulan por día NO entran al
    align; van por la rama YY (re-pin importe + baseline). El align es SOLO para
    cheques reales."""
    yy = reconciliar_posdat_plan(
        [_dbf("YY", 57000, "A,E,C AG,EN,CMB", _dt.date(2026, 7, 9))],
        [_pc(50, "YY", 49300, "A,E,C AG,EN,CMB", fechad=_dt.date(2026, 6, 1),
             usuario_crea="reconcile-dbf")],
    )
    assert yy["aligns"] == []
    assert yy["updates"][0]["id"] == 50 and yy["updates"][0]["yy"] is True
    rt = reconciliar_posdat_plan(
        [_dbf("RT", 150424, "", _dt.date(2026, 7, 9))],
        [_pc(49, "RT", 140000, "", fechad=_dt.date(2026, 6, 1), usuario_crea="reconcile-dbf")],
    )
    assert rt["aligns"] == [] and rt["updates"][0]["yy"] is True


def test_align_ya_alineada_no_genera_update():
    plan = reconciliar_posdat_plan(
        [_dbf("ZZ", 300, "A 1", _dt.date(2026, 8, 1))],
        [_pc(8, "ZZ", 300, "A 1", fechad=_dt.date(2026, 8, 1), usuario_crea="dbf-import")],
    )
    assert plan["aligns"] == []


def test_align_grupo_mismo_importe_documento_renumerado():
    """SY: 6 cheques de 1450; el dBase renumeró "58028"→"22132" y corrió fechas.
    Tras alinear, el SET (concepto,fechad) de PC == el del dBase (sin borrar/insertar)."""
    D = _dt.date
    dbf = [_dbf("SY", 1450, c, f) for c, f in [
        ("22017 15", D(2026, 7, 14)), ("22043 19", D(2026, 7, 20)),
        ("22065 22", D(2026, 7, 21)), ("22087 26", D(2026, 7, 27)),
        ("22111 28", D(2026, 7, 27)), ("22132 1", D(2026, 7, 31))]]
    pc = [_pc(i, "SY", 1450, c, fechad=f, usuario_crea="dbf-import")
          for i, (c, f) in enumerate([
              ("58028 10", D(2026, 7, 14)), ("22017 15", D(2026, 7, 20)),
              ("22043 19", D(2026, 7, 21)), ("22065 22", D(2026, 7, 27)),
              ("22087 26", D(2026, 7, 27)), ("22111 28", D(2026, 7, 31))], start=1)]
    plan = reconciliar_posdat_plan(dbf, pc)
    assert plan["deletes"] == [] and plan["inserts"] == []
    post = {(p["concepto"], p["fechad"]) for p in pc}
    for a in plan["aligns"]:
        old = next(p for p in pc if p["id_posdat"] == a["id"])
        post.discard((old["concepto"], old["fechad"]))
        post.add((a["concepto"], a["fechad"]))
    assert post == {(d["concepto"], d["fechad"]) for d in dbf}
