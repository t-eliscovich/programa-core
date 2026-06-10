"""Tests del plan de reconciliación FACTURAS (dry-run, estudio 2026-06-10).

El plan bucketé: [A] solo-dBase (pendiente de sync), [B] solo-PC backfill
Asinfo, [C] solo-PC creadas directo (el sync las borraría), [D] solo-PC origen
dbf-import huérfanas, [E] diffs (misma clave, distinta firma económica).
Clave (codigo_cli, numf) multiset; numf=0 cae a (codigo_cli, importe).
"""
from modules.admin_dbase.facturas_reconcile_view import (
    _saldo_za,
    reconciliar_facturas_plan,
)


def _row(numf, cli, saldo, stat="Z", importe=None, abono=0.0, uc=None, id_=None):
    r = {"numf": numf, "codigo_cli": cli, "fecha": "2026-06-01",
         "importe": importe if importe is not None else saldo,
         "abono": abono, "saldo": saldo, "stat": stat, "tipo": "F",
         "usuario_crea": uc}
    if id_ is not None:
        r["id_factura"] = id_
    return r


def test_match_exacto_no_genera_nada():
    plan = reconciliar_facturas_plan(
        [_row(100, "AB", 500.0)], [_row(100, "AB", 500.0, uc="dbf-import", id_=1)],
    )
    assert plan["match"] == 1
    assert plan["solo_dbase"] == [] and plan["diffs"] == []
    assert (plan["solo_pc_backfill"] == plan["solo_pc_carga"]
            == plan["solo_pc_directa"] == plan["solo_pc_dbf_huerfana"] == [])


def test_solo_dbase_pendiente_de_sync():
    plan = reconciliar_facturas_plan([_row(200, "CD", 1000.0)], [])
    assert len(plan["solo_dbase"]) == 1
    assert plan["solo_dbase"][0]["numf"] == 200


def test_solo_pc_clasifica_por_usuario_crea():
    pc = [
        _row(300, "EF", 100.0, uc="asinfo-backfill", id_=1),
        _row(301, "EF", 200.0, uc="tamara", id_=2),         # creada directo en PC
        _row(302, "EF", 300.0, uc="dbf-import", id_=3),      # huérfana (dBase la borró)
        _row(303, "EF", 400.0, uc=None, id_=4),              # legacy NULL → huérfana
        _row(304, "EF", 500.0, uc="asinfo-carga", id_=5),    # botón Cargar
    ]
    plan = reconciliar_facturas_plan([], pc)
    assert [r["numf"] for r in plan["solo_pc_backfill"]] == [300]
    assert [r["numf"] for r in plan["solo_pc_carga"]] == [304]
    assert [r["numf"] for r in plan["solo_pc_directa"]] == [301]
    assert sorted(r["numf"] for r in plan["solo_pc_dbf_huerfana"]) == [302, 303]


def test_diff_cobranza_post_sync():
    """dBase cobró después del sync: abono sube, saldo baja → DIFF, no churn."""
    plan = reconciliar_facturas_plan(
        [_row(400, "GH", 0.0, stat="T", importe=800.0, abono=800.0)],
        [_row(400, "GH", 800.0, stat="Z", importe=800.0, uc="dbf-import", id_=9)],
    )
    assert plan["solo_dbase"] == [] and plan["solo_pc_dbf_huerfana"] == []
    assert len(plan["diffs"]) == 1
    d = plan["diffs"][0]
    # PC todavía la tiene viva con 800; en dBase quedó cancelada (T, fuera de ZA)
    assert d["delta_za"] == 800.0


def test_numf_cero_multiset_por_importe():
    """8 facturas numf=0 del estudio: sin numerar → clave (cli, importe)."""
    dbf = [_row(0, "IJ", 50.0), _row(0, "IJ", 50.0), _row(0, "IJ", 70.0)]
    pc = [_row(0, "IJ", 50.0, uc="dbf-import", id_=1),
          _row(0, "IJ", 50.0, uc="dbf-import", id_=2)]
    plan = reconciliar_facturas_plan(dbf, pc)
    assert plan["match"] == 2
    assert len(plan["solo_dbase"]) == 1 and plan["solo_dbase"][0]["saldo"] == 70.0


def test_duplicados_misma_clave_multiset():
    """2 facturas mismo (cli, numf) en ambos lados → matchean por multiset."""
    dbf = [_row(500, "KL", 10.0), _row(500, "KL", 20.0)]
    pc = [_row(500, "KL", 20.0, uc="dbf-import", id_=1),
          _row(500, "KL", 10.0, uc="dbf-import", id_=2)]
    plan = reconciliar_facturas_plan(dbf, pc)
    assert plan["match"] == 2 and plan["diffs"] == []


def test_saldo_za_solo_cuenta_vivas():
    assert _saldo_za({"saldo": 100, "stat": "Z"}) == 100.0
    assert _saldo_za({"saldo": 100, "stat": "A"}) == 100.0
    assert _saldo_za({"saldo": 100, "stat": None}) == 100.0
    assert _saldo_za({"saldo": 100, "stat": " "}) == 100.0
    assert _saldo_za({"saldo": 100, "stat": "T"}) == 0.0
    assert _saldo_za({"saldo": -50, "stat": "Z"}) == -50.0  # sobrepagos netean


def test_identidad_totf_cierra():
    """Self-check: PC − dBase = −A + B + C + D + ΔE, residuo 0."""
    dbf = [_row(1, "AA", 100.0), _row(2, "AA", 200.0), _row(3, "BB", 50.0)]
    pc = [
        _row(1, "AA", 100.0, uc="dbf-import", id_=1),         # match
        _row(2, "AA", 150.0, abono=50.0, uc="dbf-import", id_=2),  # diff (cobró PC)
        _row(9, "CC", 75.0, uc="asinfo-backfill", id_=3),     # B
        _row(10, "CC", 25.0, uc="vendedor", id_=4),           # C
    ]
    plan = reconciliar_facturas_plan(dbf, pc)
    totf_dbf = sum(_saldo_za(r) for r in dbf)
    totf_pc = sum(_saldo_za(r) for r in pc)
    a = sum(_saldo_za(r) for r in plan["solo_dbase"])
    b = sum(_saldo_za(r) for r in plan["solo_pc_backfill"])
    c = sum(_saldo_za(r) for r in plan["solo_pc_directa"])
    c2 = sum(_saldo_za(r) for r in plan["solo_pc_carga"])
    h = sum(_saldo_za(r) for r in plan["solo_pc_dbf_huerfana"])
    e = sum(x["delta_za"] for x in plan["diffs"])
    assert abs((totf_pc - totf_dbf) - (-a + b + c + c2 + h + e)) < 0.01
