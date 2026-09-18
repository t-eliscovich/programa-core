"""El neteo del estado de cuenta NO reabre las facturas que pagaba el cheque.
18/09/2026, caso LUT (el segundo en una semana después de CJM, 11/09).

El cheque 3224 de LUT ($21.086,49) pagaba 5 facturas. El cliente lo cubrió con
depósitos que Alex cargó como 3 anticipos, y "Netear cheques con anticipos"
canceló el cheque (Z→X), consumió los 3 anticipos (X) y DESAPLICÓ las 5
facturas: quedaron con saldo pendiente mientras la plata ya estaba en el banco.
El cliente debía lo que ya había pagado.

Ahora el neteo crea un cheque 95 CANCELA ANTICIPO por lo que pagaba el cheque
y lo aplica a las MISMAS facturas con los MISMOS importes (también las
devoluciones negativas que el cheque absorbía). El 95 queda 'X' como todo 95.
Deshacer el neteo saca el 95 y vuelve a poner el cheque.

Corren con `pytest -m db` contra el Postgres embebido de vista_local.py.
"""
from __future__ import annotations

import pytest

CLI = "ZNT"


def _limpiar(conn) -> None:
    cur = conn.cursor()
    for sql in (
        "DELETE FROM scintela.chequesxfact WHERE codigo_cli = %s",
        "DELETE FROM scintela.mov_doble WHERE metadata ->> 'codigo_cli' = %s",
        "DELETE FROM scintela.factura WHERE codigo_cli = %s",
        "DELETE FROM scintela.cheque WHERE codigo_cli = %s",
        "DELETE FROM scintela.cliente WHERE codigo_cli = %s",
    ):
        try:
            cur.execute(sql, (CLI,))
        except Exception:  # noqa: BLE001
            conn.rollback()
    conn.commit()


def _facturas(codigo_cli: str) -> dict[int, dict]:
    import db
    rows = db.fetch_all(
        "SELECT numf, importe, abono, saldo, stat FROM scintela.factura "
        " WHERE codigo_cli=%s ORDER BY numf", (codigo_cli,)) or []
    return {int(r["numf"]): {k: (float(r[k]) if k != "stat" else r[k])
                             for k in ("importe", "abono", "saldo", "stat")}
            for r in rows}


def _cheque(id_cheque: int) -> dict:
    import db
    return db.fetch_one(
        "SELECT stat, no_banco, importe FROM scintela.cheque WHERE id_cheque=%s",
        (id_cheque,))


@pytest.fixture
def cuenta(real_db_conn, migrated_db, monkeypatch):
    """f1 (100) y f2 (60) pagadas por el cheque CH (200), que además absorbe
    la devolución f3 (−40). Tres anticipos (espejos NB=98) por −90, −70 y −40
    = 200 (el caso LUT: 3 depósitos que cubren el cheque)."""
    import db
    from modules.cheques import queries as chq

    monkeypatch.setattr(chq, "asegurar_fecha_abierta", lambda *a, **k: None)
    _limpiar(real_db_conn)
    f, ch = {}, {}
    with db.tx() as c:
        db.execute("INSERT INTO scintela.cliente (codigo_cli, nombre)"
                   " VALUES (%s, 'Prueba neteo')", (CLI,), conn=c)
        for numf, imp, ab, st in [(1, 100, 100, "T"), (2, 60, 60, "T"),
                                  (3, -40, -40, "T")]:
            f[numf] = db.fetch_one(
                "INSERT INTO scintela.factura (numf, fecha, codigo_cli, importe,"
                " abono, saldo, stat, usuario_crea)"
                " VALUES (%s,'2026-08-01',%s,%s,%s,0,%s,'test') RETURNING id_factura",
                (numf, CLI, imp, ab, st), conn=c)["id_factura"]
        ch["CH"] = db.fetch_one(
            "INSERT INTO scintela.cheque (no_cheque, fecha, fechad, codigo_cli,"
            " importe, no_banco, stat)"
            " VALUES ('3224','2026-08-10','2026-08-10',%s,200,10,'Z') RETURNING id_cheque",
            (CLI,), conn=c)["id_cheque"]
        for numf, imp in [(1, 100), (2, 60), (3, -40)]:
            db.execute(
                "INSERT INTO scintela.chequesxfact (id_cheque, id_fact, fechaing,"
                " codigo_cli, importe, no_banco, tipo)"
                " VALUES (%s,%s,CURRENT_DATE,%s,%s,10,'CH')",
                (ch["CH"], f[numf], CLI, imp), conn=c)
        for no, imp in [("A1", -90), ("A2", -70), ("A3", -40)]:
            ch[no] = db.fetch_one(
                "INSERT INTO scintela.cheque (no_cheque, fecha, fechad, codigo_cli,"
                " importe, no_banco, banco, stat)"
                " VALUES (%s,'2026-09-01','2026-09-01',%s,%s,98,'ANTICIPO','Z')"
                " RETURNING id_cheque",
                (no, CLI, imp), conn=c)["id_cheque"]
    yield {"f": f, "ch": ch}
    _limpiar(real_db_conn)


@pytest.mark.db
def test_neteo_deja_las_facturas_pagadas_por_el_anticipo(cuenta):
    from modules.cheques import queries as chq

    res = chq.netear_cheques_con_anticipos(
        codigo_cli=CLI, ids_cheques=[cuenta["ch"]["CH"]],
        ids_anticipos=[cuenta["ch"][k] for k in ("A1", "A2", "A3")],
        usuario="test")

    # El cheque y los 3 anticipos anulados entre sí, como siempre.
    assert _cheque(cuenta["ch"]["CH"])["stat"] == "X"
    for k in ("A1", "A2", "A3"):
        assert _cheque(cuenta["ch"][k])["stat"] == "X"

    # Las facturas siguen pagadas (antes: saldo 100, 60 y −40 pendientes).
    fs = _facturas(CLI)
    assert fs[1]["saldo"] == 0 and fs[1]["abono"] == 100
    assert fs[2]["saldo"] == 0 and fs[2]["abono"] == 60
    assert fs[3]["saldo"] == 0 and fs[3]["abono"] == -40
    for numf in (1, 2, 3):
        assert fs[numf]["stat"] != "Z", f"f{numf} quedó abierta"

    # Quien las paga es UN 95 CANCELA ANTICIPO por lo aplicado (120 neto),
    # anulado como todo 95, con los mismos tres vínculos.
    assert len(res["ids_95"]) == 1
    c95 = _cheque(res["ids_95"][0])
    assert c95["no_banco"] == 95 and c95["stat"] == "X"
    assert float(c95["importe"]) == 120.0
    import db
    aps = db.fetch_all(
        "SELECT id_fact, importe FROM scintela.chequesxfact WHERE id_cheque=%s",
        (res["ids_95"][0],)) or []
    assert {(int(a["id_fact"]), float(a["importe"])) for a in aps} == {
        (cuenta["f"][1], 100.0), (cuenta["f"][2], 60.0), (cuenta["f"][3], -40.0)}
    # El cheque neteado ya no tiene vínculos (se desaplicó para poder anularlo).
    assert not db.fetch_all(
        "SELECT 1 FROM scintela.chequesxfact WHERE id_cheque=%s",
        (cuenta["ch"]["CH"],))
    assert {r["numf"] for r in res["facturas_pagadas"]} == {"1", "2", "3"}


@pytest.mark.db
def test_deshacer_neteo_saca_el_95_y_vuelve_el_cheque(cuenta):
    import db
    from modules.cheques import queries as chq

    res = chq.netear_cheques_con_anticipos(
        codigo_cli=CLI, ids_cheques=[cuenta["ch"]["CH"]],
        ids_anticipos=[cuenta["ch"][k] for k in ("A1", "A2", "A3")],
        usuario="test")
    ev = chq.neteos_activos_cliente(CLI)
    assert len(ev) == 1
    chq.deshacer_neteo(ev[0]["id_evento"], CLI, usuario="test")

    # Todo como antes: cheque Z pagando las 3, anticipos Z, 95 sin vínculos.
    assert _cheque(cuenta["ch"]["CH"])["stat"] == "Z"
    for k in ("A1", "A2", "A3"):
        assert _cheque(cuenta["ch"][k])["stat"] == "Z"
    fs = _facturas(CLI)
    assert (fs[1]["abono"], fs[2]["abono"], fs[3]["abono"]) == (100, 60, -40)
    assert all(fs[n]["saldo"] == 0 for n in (1, 2, 3))
    id95 = res["ids_95"][0]
    assert not db.fetch_all(
        "SELECT 1 FROM scintela.chequesxfact WHERE id_cheque=%s", (id95,))
    assert _cheque(id95)["stat"] == "X"
    aps_ch = db.fetch_all(
        "SELECT id_fact, importe FROM scintela.chequesxfact WHERE id_cheque=%s",
        (cuenta["ch"]["CH"],)) or []
    assert {(int(a["id_fact"]), float(a["importe"])) for a in aps_ch} == {
        (cuenta["f"][1], 100.0), (cuenta["f"][2], 60.0), (cuenta["f"][3], -40.0)}
    # Sin doble pago: cada factura tiene exactamente un vínculo vivo.
    for numf in (1, 2, 3):
        n = db.fetch_one(
            "SELECT COUNT(*) AS n FROM scintela.chequesxfact WHERE id_fact=%s",
            (cuenta["f"][numf],))["n"]
        assert int(n) == 1


@pytest.mark.db
def test_neteo_de_un_cheque_sin_aplicar_no_crea_95(cuenta):
    import db
    from modules.cheques import queries as chq

    with db.tx() as c:
        db.execute("DELETE FROM scintela.chequesxfact WHERE id_cheque=%s",
                   (cuenta["ch"]["CH"],), conn=c)
    res = chq.netear_cheques_con_anticipos(
        codigo_cli=CLI, ids_cheques=[cuenta["ch"]["CH"]],
        ids_anticipos=[cuenta["ch"][k] for k in ("A1", "A2", "A3")],
        usuario="test")
    assert res["ids_95"] == [] and res["facturas_pagadas"] == []
    assert not db.fetch_all(
        "SELECT 1 FROM scintela.cheque WHERE codigo_cli=%s AND no_banco=95", (CLI,))
